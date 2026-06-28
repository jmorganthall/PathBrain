"""Autonomous experiment engine: sweep one shaper parameter, safely.

Within an **experimentation window** (configured days/hours, local time), the
engine interleaves a set of candidate values for a single FQ-CoDel parameter
(e.g. quantum), benchmarking each so time-of-day noise is shared across values.

Safety model (the part that matters):
* **Disarmed by default** — does nothing unless ``experiment.enabled``.
* **Dry-run by default** — logs intended changes without touching the firewall.
* **Snapshot the baseline** (the param's value + full settings) when an
  experiment starts.
* **Restore the pre-experiment baseline when the window closes** — always, unless
  ``auto_promote`` is on AND a candidate beat the baseline by ``improve_pct`` with
  enough trials. Disarming mid-run also restores and aborts.

The engine runs inside the scheduler thread, so it never overlaps benchmark runs.
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timezone
from statistics import median

from sqlalchemy import select

from .config_store import get_config
from .database import session_scope
from .logging_config import get_logger
from .models import Experiment, ExperimentStatus, ExperimentTrial, Run
from .providers import get_provider
from .runner import create_run, execute_run
from .settings_profile import fingerprint, normalize

log = get_logger("experiment")

# In-memory dwell tracking (which value is applied and when).
_state: dict = {"current_value": None, "applied_at": 0.0}


def in_window(window: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    days = window.get("days") or []
    if days and now.weekday() not in days:
        return False
    sh, eh, h = int(window.get("start_hour", 0)), int(window.get("end_hour", 0)), now.hour
    if sh == eh:
        return False
    return sh <= h < eh if sh < eh else (h >= sh or h < eh)  # overnight if sh>eh


def _ordered_values(candidates: list, baseline_value: str) -> list[str]:
    """Baseline first, then unique candidates."""
    out = [str(baseline_value)]
    for c in candidates:
        if str(c) not in out:
            out.append(str(c))
    return out


def _apply(provider, cfg: dict, value: str, dry_run: bool) -> bool:
    changes = {"pipe_uuid": cfg.get("pipe_uuid") or None, "param": cfg["param"], "value": value}
    if dry_run:
        log.info("[dry-run] would apply %s=%s", cfg["param"], value)
        return True
    try:
        provider.apply(changes)
        log.info("Applied %s=%s", cfg["param"], value)
        return True
    except Exception:  # noqa: BLE001
        log.exception("Failed to apply %s=%s", cfg["param"], value)
        return False


def _baseline_value(cfg: dict, normalized: list[dict]) -> str | None:
    uuid = cfg.get("pipe_uuid")
    pipe = None
    if uuid:
        pipe = next((p for p in normalized if (p.get("label") or "") and uuid in str(p)), None)
    pipe = pipe or (normalized[0] if normalized else None)
    if not pipe:
        return None
    val = pipe.get(cfg["param"])
    return str(val) if val is not None else None


def _start(session, cfg: dict) -> Experiment | None:
    from .shaper_fields import WRITABLE_FIELDS

    # You can only experiment on a field apply() can actually write — otherwise every trial
    # silently no-ops. Validate against the one capability source instead of trusting config.
    param = cfg.get("param")
    if param not in WRITABLE_FIELDS:
        log.warning(
            "Experiment start: param '%s' isn't writable (writable: %s) — not starting",
            param, WRITABLE_FIELDS,
        )
        return None
    provider = get_provider()
    try:
        normalized = normalize(provider.discover())
    except Exception:  # noqa: BLE001
        log.exception("Experiment start: discovery failed")
        return None
    baseline_value = _baseline_value(cfg, normalized)
    if baseline_value is None:
        log.warning("Experiment start: could not read baseline for param '%s'", cfg.get("param"))
        return None
    exp = Experiment(
        status=ExperimentStatus.RUNNING,
        param=cfg["param"],
        pipe_uuid=cfg.get("pipe_uuid") or None,
        candidates=[str(c) for c in cfg.get("candidates", [])],
        dry_run=bool(cfg.get("dry_run", True)),
        baseline_value=baseline_value,
        baseline_settings=normalized,
    )
    session.add(exp)
    session.flush()
    _state["current_value"] = None
    _state["applied_at"] = 0.0
    log.info("Experiment %s started: sweep %s over %s (baseline=%s, dry_run=%s)",
             exp.id, exp.param, exp.candidates, baseline_value, exp.dry_run)
    return exp


def _trial_counts(session, exp: Experiment) -> Counter:
    rows = session.scalars(
        select(ExperimentTrial.value).where(ExperimentTrial.experiment_id == exp.id)
    ).all()
    return Counter(rows)


def _run_trial_step(session, exp: Experiment, cfg: dict) -> None:
    provider = get_provider()
    values = _ordered_values(exp.candidates, exp.baseline_value)
    dwell_s = max(float(cfg.get("dwell_minutes", 10)) * 60.0, 0.0)
    now = time.time()

    if _state["current_value"] is None:
        _apply(provider, cfg, values[0], exp.dry_run)
        _state["current_value"] = values[0]
        _state["applied_at"] = now
        return

    if now - _state["applied_at"] < dwell_s:
        return  # let the setting settle / sample this time slice

    # Benchmark the currently-applied value.
    current = _state["current_value"]
    run_id = create_run(label=f"exp{exp.id} {exp.param}={current}")
    execute_run(run_id)
    run = session.get(Run, run_id)
    sops = run.score.sops if run and run.score else None
    session.add(
        ExperimentTrial(
            experiment_id=exp.id, value=current, run_id=run_id, sops=sops, applied=not exp.dry_run
        )
    )
    session.flush()

    # Move to the least-sampled value (round-robin interleave).
    counts = _trial_counts(session, exp)
    next_value = min(values, key=lambda v: (counts.get(v, 0), values.index(v)))
    _apply(provider, cfg, next_value, exp.dry_run)
    _state["current_value"] = next_value
    _state["applied_at"] = time.time()


def _finalize(session, exp: Experiment, cfg: dict) -> None:
    """Window closed (or disabled): decide promote vs restore, end the experiment."""
    provider = get_provider()
    trials = session.scalars(
        select(ExperimentTrial).where(ExperimentTrial.experiment_id == exp.id)
    ).all()
    by_value: dict[str, list[float]] = {}
    for t in trials:
        if t.sops is not None:
            by_value.setdefault(t.value, []).append(t.sops)

    min_trials = int(cfg.get("min_trials_per_value", 3))
    improve = float(cfg.get("improve_pct", 5))
    medians = {v: round(median(s), 2) for v, s in by_value.items()}
    baseline = str(exp.baseline_value)
    base_med = medians.get(baseline)

    # Eligible winners: not baseline, enough trials, beat baseline by improve_pct.
    winner, winner_med = None, None
    for v, med in sorted(medians.items(), key=lambda kv: kv[1], reverse=True):
        if v == baseline or len(by_value.get(v, [])) < min_trials:
            continue
        if base_med is None or med >= base_med * (1 + improve / 100.0):
            winner, winner_med = v, med
            break

    promote = bool(cfg.get("auto_promote")) and winner is not None
    final_value = winner if promote else baseline
    applied_ok = _apply(provider, cfg, final_value, exp.dry_run)

    exp.status = ExperimentStatus.COMPLETED
    exp.finished_at = datetime.now(timezone.utc)
    exp.result = {
        "medians": medians,
        "baseline_value": baseline,
        "baseline_median": base_med,
        "winner": winner,
        "winner_median": winner_med,
        "action": "promoted" if promote else "restored_baseline",
        "final_value": final_value,
        "final_applied_ok": applied_ok,
        "min_trials_per_value": min_trials,
        "improve_pct": improve,
    }
    _state["current_value"] = None
    log.info("Experiment %s finalized: %s -> %s", exp.id, exp.result["action"], final_value)


def _abort(session, exp: Experiment, cfg: dict) -> None:
    provider = get_provider()
    _apply(provider, cfg, str(exp.baseline_value), exp.dry_run)
    exp.status = ExperimentStatus.ABORTED
    exp.finished_at = datetime.now(timezone.utc)
    exp.notes = "Disarmed mid-run; restored baseline."
    _state["current_value"] = None
    log.info("Experiment %s aborted; baseline restored", exp.id)


def abort_active() -> bool:
    """Manually abort the running experiment and restore baseline. Returns True
    if one was aborted."""
    with session_scope() as session:
        cfg = get_config(session).get("experiment", {}) or {}
        active = session.scalars(
            select(Experiment)
            .where(Experiment.status == ExperimentStatus.RUNNING)
            .order_by(Experiment.id.desc())
        ).first()
        if not active:
            return False
        _abort(session, active, cfg)
        return True


def step() -> bool:
    """Advance the engine one tick. Returns True if it did experiment work
    (so the scheduler skips a monitoring run this tick)."""
    try:
        with session_scope() as session:
            cfg = get_config(session).get("experiment", {}) or {}
            active = session.scalars(
                select(Experiment)
                .where(Experiment.status == ExperimentStatus.RUNNING)
                .order_by(Experiment.id.desc())
            ).first()

            if not cfg.get("enabled"):
                if active:
                    _abort(session, active, cfg)
                    return True
                return False

            within = in_window(cfg.get("window", {}) or {})
            if not within:
                if active:
                    _finalize(session, active, cfg)
                    return True
                return False

            if not cfg.get("candidates"):
                return False
            if active is None:
                active = _start(session, cfg)
                if active is None:
                    return False
            # Hold the coordination lock for the apply+benchmark sub-step. If another
            # session (sweep, profile test, manual run) holds it, defer to the next
            # tick rather than overlap — but still return True so monitoring stays
            # suppressed throughout the experiment window.
            from . import coordinator

            try:
                with coordinator.try_hold(f"experiment#{active.id}"):
                    _run_trial_step(session, active, cfg)
            except coordinator.CoordinatorBusy:
                log.info("Experiment %s: deferring trial step; another session holds the lock", active.id)
            return True
    except Exception:  # noqa: BLE001 — never kill the scheduler
        log.exception("Experiment step failed")
        return False
