"""Shotgun Sweep: a fast, supervised, foreground sweep of shaper parameters.

Kicked off on demand (not window-gated like ``experiment.py``): for each variant in
a quantum × target grid it applies the value for real, lets it settle, runs the
normal benchmark suite, records the score, and — always, in a ``finally`` — restores
the pre-sweep baseline. A background thread drives it; the scheduler yields while a
sweep is ``active()`` so benchmark runs never overlap. The ``Sweep`` DB row persists
the baseline so a crash mid-sweep can still restore the firewall on startup
(``reconcile_interrupted_sweeps``).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from .database import session_scope
from .logging_config import get_logger
from .models import Run, RunStatus, Sweep, SweepStatus
from .providers import get_provider
from .runner import create_run, execute_run
from .settings_profile import normalize

log = get_logger("sweep")

# A broad sweep is fine; a runaway grid is not. Cap the variant count.
MAX_VARIANTS = 64

# Single sweep at a time. Module state coordinates with the scheduler thread.
_state: dict = {"active": False, "sweep_id": None, "cancel": None, "thread": None}


def active() -> bool:
    return bool(_state.get("active"))


def active_sweep_id() -> int | None:
    return _state.get("sweep_id") if active() else None


# ── pure helpers (unit-tested) ───────────────────────────────────────────────


def _value_range(spec: dict) -> list[float]:
    """Inclusive numeric range [min, max] by step, robust to float drift."""
    lo, hi, step = float(spec["min"]), float(spec["max"]), float(spec["step"])
    if step <= 0 or hi < lo:
        return [lo]
    out: list[float] = []
    i = 0
    while True:
        v = lo + i * step
        if v > hi + 1e-9 or len(out) >= MAX_VARIANTS + 1:
            break
        out.append(round(v, 6))
        i += 1
    return out


def generate_variants(spec: dict) -> list[dict]:
    """Cartesian product of the enabled parameters' value ranges.

    ``spec`` = ``{"quantum": {"enabled", "min", "max", "step"}, "target": {...}}``.
    Quantum values are ints; target values are ``"<n>ms"`` strings (the form the
    provider expects). Returns ``[]`` when no parameter is enabled.
    """
    dims: list[tuple[str, list]] = []
    q = spec.get("quantum") or {}
    if q.get("enabled"):
        dims.append(("quantum", [int(round(v)) for v in _value_range(q)]))
    t = spec.get("target") or {}
    if t.get("enabled"):
        dims.append(("target", [f"{int(round(v))}ms" for v in _value_range(t)]))
    if not dims:
        return []
    variants: list[dict] = [{}]
    for name, vals in dims:
        variants = [{**v, name: val} for v in variants for val in vals]
    return variants


def estimate(variants: list[dict], iterations: int, dwell_s: float, per_iteration_ms: float | None) -> dict:
    """Total variants + wall-clock ETA (None until we have a per-iteration timing)."""
    per_variant_ms = (dwell_s * 1000.0) + iterations * (per_iteration_ms or 0.0)
    eta = round(len(variants) * per_variant_ms, 1) if per_iteration_ms else None
    return {"total_variants": len(variants), "eta_ms": eta}


# ── baseline / apply ─────────────────────────────────────────────────────────


def _baseline(provider, pipe_uuid: str | None) -> dict:
    """Read the target pipe's current quantum + target (and snapshot all settings)."""
    configs = provider.discover()
    target = None
    if pipe_uuid:
        target = next((c for c in configs if (c.extra or {}).get("uuid") == pipe_uuid), None)
    target = target or (configs[0] if configs else None)
    return {
        "quantum": target.quantum if target else None,
        "target": target.target if target else None,
        "settings": normalize(configs),
    }


def _apply(provider, pipe_uuid: str | None, param: str, value, dry_run: bool) -> None:
    if value is None:
        return
    if dry_run:
        log.info("[sweep dry-run] would apply %s=%s", param, value)
        return
    provider.apply({"pipe_uuid": pipe_uuid or None, "param": param, "value": value})


def _label(quantum, target) -> str:
    parts = []
    if quantum is not None:
        parts.append(f"q{quantum}")
    if target is not None:
        parts.append(f"t{target}")
    return "sweep · " + " ".join(parts)


def _run_sops(run_id: int) -> float | None:
    with session_scope() as session:
        run = session.get(Run, run_id)
        return run.score.sops if run and run.score else None


def _restore(provider, sweep_id: int) -> None:
    """Apply the stored baseline quantum + target back (best-effort, logs on fail)."""
    try:
        with session_scope() as session:
            sw = session.get(Sweep, sweep_id)
            baseline = (sw.baseline or {}) if sw else {}
            dry_run = sw.dry_run if sw else False
            pipe_uuid = sw.pipe_uuid if sw else None
        _apply(provider, pipe_uuid, "quantum", baseline.get("quantum"), dry_run)
        _apply(provider, pipe_uuid, "target", baseline.get("target"), dry_run)
        log.info("Sweep %s: restored baseline %s", sweep_id, baseline)
    except Exception:  # noqa: BLE001 — never raise out of cleanup
        log.exception("Sweep %s: baseline restore failed", sweep_id)


def _wait_for_idle(cancel: threading.Event, timeout_s: float = 120.0) -> None:
    """Wait until no run is RUNNING/PENDING (e.g. a scheduler run finishing)."""
    start = time.time()
    while time.time() - start < timeout_s:
        if cancel.is_set():
            return
        with session_scope() as session:
            busy = session.scalar(
                select(Run.id).where(Run.status.in_([RunStatus.RUNNING, RunStatus.PENDING])).limit(1)
            )
        if busy is None:
            return
        time.sleep(2.0)


# ── lifecycle ────────────────────────────────────────────────────────────────


def start(spec: dict, iterations: int, dwell_s: float, dry_run: bool, pipe_uuid: str | None) -> int:
    """Validate, snapshot the baseline, create the Sweep row, and launch the driver.

    Raises ``RuntimeError`` if a sweep is already running, ``ValueError`` for an
    invalid spec, or propagates provider discovery errors.
    """
    if active():
        raise RuntimeError("A sweep is already running.")
    variants = generate_variants(spec)
    if not variants:
        raise ValueError("Enable at least one parameter (quantum or target) with a valid range.")
    if len(variants) > MAX_VARIANTS:
        raise ValueError(f"{len(variants)} variants exceeds the cap of {MAX_VARIANTS}.")

    provider = get_provider()
    baseline = _baseline(provider, pipe_uuid)  # may raise (discovery) — surfaced to caller

    with session_scope() as session:
        sweep = Sweep(
            status=SweepStatus.PENDING,
            dry_run=dry_run,
            iterations=iterations,
            dwell_s=dwell_s,
            pipe_uuid=pipe_uuid,
            spec={**spec, "variants": variants},
            baseline=baseline,
            total_variants=len(variants),
            completed_variants=0,
            results=[],
        )
        session.add(sweep)
        session.flush()
        sweep_id = sweep.id

    cancel = threading.Event()
    _state.update({"active": True, "sweep_id": sweep_id, "cancel": cancel})
    thread = threading.Thread(target=_drive, args=(sweep_id,), name="pathbrain-sweep", daemon=True)
    _state["thread"] = thread
    thread.start()
    log.info("Sweep %s started: %s variant(s), dry_run=%s", sweep_id, len(variants), dry_run)
    return sweep_id


def cancel(sweep_id: int) -> bool:
    """Signal the active sweep to stop after the current variant (it then restores)."""
    if _state.get("sweep_id") == sweep_id and _state.get("cancel") is not None:
        _state["cancel"].set()
        return True
    return False


def _drive(sweep_id: int) -> None:
    provider = get_provider()
    cancel_evt: threading.Event = _state["cancel"]
    final_status = SweepStatus.COMPLETE
    err: str | None = None
    try:
        _wait_for_idle(cancel_evt)
        with session_scope() as session:
            sw = session.get(Sweep, sweep_id)
            sw.status = SweepStatus.RUNNING
            sw.started_at = datetime.now(timezone.utc)
            variants = list((sw.spec or {}).get("variants") or [])
            iterations, dwell_s, dry_run, pipe_uuid = sw.iterations, sw.dwell_s, sw.dry_run, sw.pipe_uuid
            results = list(sw.results or [])

        for idx, variant in enumerate(variants):
            if cancel_evt.is_set():
                final_status = SweepStatus.CANCELLED
                break
            quantum, target = variant.get("quantum"), variant.get("target")
            _apply(provider, pipe_uuid, "quantum", quantum, dry_run)
            _apply(provider, pipe_uuid, "target", target, dry_run)
            # Let the change settle (and serve as a cancel checkpoint).
            if dwell_s > 0 and cancel_evt.wait(dwell_s):
                final_status = SweepStatus.CANCELLED
                break

            run_id = create_run(
                label=_label(quantum, target),
                notes=f"Shotgun sweep #{sweep_id} variant {idx + 1}/{len(variants)}",
                iterations=iterations,
            )
            execute_run(run_id)  # blocking; captures the just-applied settings onto the run
            results.append(
                {"index": idx, "quantum": quantum, "target": target, "run_id": run_id, "sops": _run_sops(run_id)}
            )
            with session_scope() as session:
                sw = session.get(Sweep, sweep_id)
                sw.completed_variants = idx + 1
                sw.results = results
    except Exception as exc:  # noqa: BLE001 — record + restore, never crash the thread
        log.exception("Sweep %s failed", sweep_id)
        final_status = SweepStatus.FAILED
        err = f"{type(exc).__name__}: {exc}"
    finally:
        _restore(provider, sweep_id)
        with session_scope() as session:
            sw = session.get(Sweep, sweep_id)
            if sw is not None:
                sw.status = final_status
                sw.error = err
                sw.finished_at = datetime.now(timezone.utc)
        _state.update({"active": False, "sweep_id": None, "cancel": None})
        log.info("Sweep %s finished: %s", sweep_id, final_status.value)


def reconcile_interrupted_sweeps() -> int:
    """Restore the baseline for any sweep left RUNNING by a previous process.

    Called once at startup, like ``runner.reconcile_interrupted_runs``. The driving
    thread is gone, so the firewall may be stranded on a test value — set it back.
    """
    provider = None
    restored = 0
    with session_scope() as session:
        sweeps = session.scalars(
            select(Sweep).where(Sweep.status.in_([SweepStatus.RUNNING, SweepStatus.PENDING]))
        ).all()
        for sw in sweeps:
            baseline = sw.baseline or {}
            if not sw.dry_run:
                try:
                    provider = provider or get_provider()
                    for param in ("quantum", "target"):
                        if baseline.get(param) is not None:
                            provider.apply(
                                {"pipe_uuid": sw.pipe_uuid or None, "param": param, "value": baseline[param]}
                            )
                except Exception:  # noqa: BLE001
                    log.exception("Sweep %s: restore on reconcile failed", sw.id)
            sw.status = SweepStatus.FAILED
            sw.error = "Interrupted — service restarted mid-sweep; baseline restored (best-effort)."
            sw.finished_at = datetime.now(timezone.utc)
            restored += 1
    if restored:
        log.warning("Reconciled %s interrupted sweep(s); baseline restored", restored)
    return restored
