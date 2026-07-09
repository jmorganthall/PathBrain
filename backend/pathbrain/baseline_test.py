"""Baseline test: measure the link with SQM turned **off**.

Occasionally we want to know what the shaper is actually buying — so this session
disables FQ-CoDel on every pipe, benchmarks the unshaped link, and restores the prior
state. The flow (the write-then-restore sibling of ``profile_test``, chunked like
``current_test``):

1. Snapshot each shaper pipe's on/off state (the baseline to restore).
2. **Disable SQM on every currently-enabled pipe** (``provider.set_pipe_enabled``).
3. Wait a configurable "settle down" time for the link to stabilize without shaping.
4. Benchmark for a configurable number of iterations, in ``runner.CHUNK_ITERATIONS``
   chunks so a stop/crash keeps every completed chunk.
5. **Always** restore each pipe's prior state at the end (and on crash-restart, via
   ``reconcile_interrupted_baseline_tests``).

Because a disabled pipe fingerprints as its own profile (see
``settings_profile.fingerprint``), the collected runs form a distinct "SQM off" profile
rather than merging into the matching shaped one.

It runs in its own thread and holds the coordination lock for the whole session, so it
never overlaps a sweep, an experiment, a profile test, or a monitoring/manual run. Each
chunk carries the runner's read-before/read-after integrity guarantee — SQM stays off for
the whole run, so before/after match and no spurious drift is flagged.

Kicked either on demand (``start``) or by the scheduler on a nightly schedule
(``config.baseline_test``).
"""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from . import coordinator
from .database import session_scope
from .logging_config import get_logger
from .models import BaselineTest, BaselineTestStatus
from .providers import get_provider
from .runner import CHUNK_ITERATIONS, run_chunk

log = get_logger("baseline_test")

# One baseline test at a time. Module state coordinates with the driver thread and carries
# the cooperative cancel flag (the row only records durable state).
_state: dict = {"active": False, "id": None, "cancel": False, "thread": None}


def active() -> bool:
    return bool(_state.get("active"))


def _set_stage(bt_id: int, stage: str) -> None:
    """Record the current step on the row (for the live UI readout) and log it."""
    log.info("Baseline test %s: %s", bt_id, stage)
    try:
        with session_scope() as session:
            bt = session.get(BaselineTest, bt_id)
            if bt is not None:
                bt.stage = stage[:255]
    except Exception:  # noqa: BLE001 — a status write must never break the test
        log.debug("Baseline test %s: could not persist stage %r", bt_id, stage, exc_info=True)


def start(iterations: int, settle_seconds: int, *, trigger: str = "manual") -> int:
    """Launch a baseline (SQM off) test. Returns the ``BaselineTest`` id.

    Raises ``ValueError`` for non-positive iterations / negative settle time and
    ``RuntimeError`` if a baseline test is already running. The pipe-state baseline is
    snapshotted inside the driver (under the lock) so it reflects the true pre-disable state.
    """
    iterations = int(iterations)
    settle_seconds = int(settle_seconds)
    if iterations <= 0:
        raise ValueError("Iterations must be a positive whole number.")
    if settle_seconds < 0:
        raise ValueError("Settle time cannot be negative.")
    if active():
        raise RuntimeError("A baseline test is already running.")
    with session_scope() as session:
        bt = BaselineTest(
            status=BaselineTestStatus.PENDING,
            trigger=trigger if trigger in ("manual", "scheduled") else "manual",
            iterations=iterations,
            settle_s=settle_seconds,
            iterations_run=0,
            runs_created=0,
            run_ids=[],
            stage="Queued — waiting for any running benchmark to finish",
        )
        session.add(bt)
        session.flush()
        bt_id = bt.id

    _state.update({"active": True, "id": bt_id, "cancel": False})
    thread = threading.Thread(target=_drive, args=(bt_id,), name="pathbrain-baseline-test", daemon=True)
    _state["thread"] = thread
    thread.start()
    log.info(
        "Baseline test %s started (%s): %s iteration(s), %ss settle",
        bt_id, trigger, iterations, settle_seconds,
    )
    return bt_id


def cancel() -> bool:
    """Ask the running baseline test to stop after its current chunk (SQM is still restored).
    Returns True if one was active."""
    if not active():
        return False
    _state["cancel"] = True
    log.info("Baseline test %s: cancel requested", _state.get("id"))
    return True


def _disabled_pipes(baseline: list[dict]) -> list[dict]:
    """The pipes that were on before the test (the ones we turn off and must turn back on)."""
    return [s for s in (baseline or []) if s.get("enabled")]


def _restore(provider, baseline: list[dict]) -> None:
    """Re-enable every pipe we disabled, back to its snapshotted state. Never raises."""
    for state in _disabled_pipes(baseline):
        try:
            provider.set_pipe_enabled(state.get("uuid"), True)
        except Exception:  # noqa: BLE001 — best-effort per pipe; keep restoring the rest
            log.exception("Baseline test: could not re-enable pipe %s", state.get("uuid"))


def _wait_settle(seconds: int) -> None:
    """Sleep in short slices so a cancel during the settle window is honored promptly."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not _state.get("cancel"):
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def _drive(bt_id: int) -> None:
    provider = get_provider()
    final_status = BaselineTestStatus.COMPLETE
    err: str | None = None
    baseline: list[dict] = []
    run_ids: list[int] = []
    try:
        # Hold the coordination lock for the whole session (disable → settle → benchmark →
        # restore). Queues behind any in-progress firewall/benchmark session.
        with coordinator.hold(f"baseline-test#{bt_id}"):
            _set_stage(bt_id, "Reading current SQM state")
            baseline = provider.pipe_states()
            with session_scope() as session:
                bt = session.get(BaselineTest, bt_id)
                bt.status = BaselineTestStatus.RUNNING
                bt.started_at = datetime.now(timezone.utc)
                bt.baseline = baseline
                iterations = bt.iterations
                settle_s = bt.settle_s
            try:
                to_disable = _disabled_pipes(baseline)
                if to_disable:
                    _set_stage(bt_id, f"Disabling SQM on {len(to_disable)} pipe(s)")
                    for state in to_disable:
                        provider.set_pipe_enabled(state.get("uuid"), False)
                else:
                    _set_stage(bt_id, "SQM already off on all pipes")

                if settle_s and not _state.get("cancel"):
                    _set_stage(bt_id, f"Settling for {settle_s}s before benchmarking")
                    _wait_settle(settle_s)

                if _state.get("cancel"):
                    final_status = BaselineTestStatus.CANCELLED
                else:
                    n_chunks = math.ceil(iterations / CHUNK_ITERATIONS)
                    scheduled = 0
                    iterations_run = 0
                    for chunk in range(n_chunks):
                        if _state.get("cancel"):
                            final_status = BaselineTestStatus.CANCELLED
                            break
                        n = min(CHUNK_ITERATIONS, iterations - scheduled)
                        _set_stage(
                            bt_id,
                            f"Benchmarking SQM off — {iterations_run}/{iterations} iteration(s)",
                        )
                        run_id, ok, completed = run_chunk(
                            label="Baseline · SQM off",
                            notes=f"Baseline test #{bt_id}: unshaped link ({iterations} iterations)",
                            iterations=n,
                        )
                        scheduled += n
                        iterations_run += completed
                        run_ids.append(run_id)
                        with session_scope() as session:
                            bt = session.get(BaselineTest, bt_id)
                            if bt is not None:
                                bt.iterations_run = iterations_run
                                bt.runs_created = len(run_ids)
                                bt.run_ids = list(run_ids)
                        if not ok:
                            log.warning(
                                "Baseline test %s: chunk run %s did not complete; stopping",
                                bt_id, run_id,
                            )
                            final_status = BaselineTestStatus.FAILED
                            err = "A benchmark chunk failed (see its run); stopped with data kept."
                            break
                    else:
                        _set_stage(bt_id, "Benchmark complete")
            except Exception as exc:  # noqa: BLE001 — record + restore, never crash the thread
                log.exception("Baseline test %s failed", bt_id)
                final_status = BaselineTestStatus.FAILED
                err = f"{type(exc).__name__}: {exc}"
            finally:
                # Always turn SQM back on, whatever happened above.
                _set_stage(bt_id, "Restoring SQM (re-enabling shaping)")
                _restore(provider, baseline)
                log.info("Baseline test %s: SQM restored", bt_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("Baseline test %s: unexpected failure", bt_id)
        final_status = BaselineTestStatus.FAILED
        err = f"{type(exc).__name__}: {exc}"
    finally:
        with session_scope() as session:
            bt = session.get(BaselineTest, bt_id)
            if bt is not None:
                bt.status = final_status
                bt.error = err
                bt.stage = {
                    BaselineTestStatus.COMPLETE: "Done — SQM restored",
                    BaselineTestStatus.CANCELLED: "Cancelled — SQM restored",
                }.get(final_status, err or "Failed — SQM restored")
                bt.finished_at = datetime.now(timezone.utc)
        _state.update({"active": False, "id": None, "cancel": False})
        log.info(
            "Baseline test %s finished: %s (%s run(s))", bt_id, final_status.value, len(run_ids)
        )


def _serialize(bt: BaselineTest) -> dict:
    return {
        "id": bt.id,
        "status": bt.status.value if hasattr(bt.status, "value") else str(bt.status),
        "trigger": bt.trigger,
        "iterations": bt.iterations,
        "settle_s": bt.settle_s,
        "iterations_run": bt.iterations_run,
        "runs_created": bt.runs_created,
        "run_ids": bt.run_ids or [],
        "baseline": bt.baseline or [],
        "error": bt.error,
        "stage": bt.stage,
        "created_at": bt.created_at.isoformat() if bt.created_at else None,
        "started_at": bt.started_at.isoformat() if bt.started_at else None,
        "finished_at": bt.finished_at.isoformat() if bt.finished_at else None,
        # Best-effort label of whatever holds the coordination lock, so the UI can
        # explain a queued/waiting test.
        "lock_owner": coordinator.owner(),
    }


def current() -> dict | None:
    """The most recent baseline test (for status polling), or None."""
    with session_scope() as session:
        bt = session.scalars(select(BaselineTest).order_by(BaselineTest.id.desc())).first()
        return _serialize(bt) if bt else None


def reconcile_interrupted_baseline_tests() -> int:
    """Re-enable SQM for any baseline test left RUNNING/PENDING by a previous process.

    Called once at startup, like ``profile_test.reconcile_interrupted_profile_tests``. The
    driving thread is gone, so the firewall may be stranded with SQM off — turn it back on
    from the snapshotted pipe states.
    """
    provider = None
    restored = 0
    with session_scope() as session:
        tests = session.scalars(
            select(BaselineTest).where(
                BaselineTest.status.in_(
                    [BaselineTestStatus.RUNNING, BaselineTestStatus.PENDING]
                )
            )
        ).all()
        for bt in tests:
            if bt.baseline:
                try:
                    provider = provider or get_provider()
                    _restore(provider, bt.baseline)
                except Exception:  # noqa: BLE001
                    log.exception("Baseline test %s: SQM restore on reconcile failed", bt.id)
            bt.status = BaselineTestStatus.FAILED
            bt.error = "Interrupted — service restarted mid-test; SQM re-enabled (best-effort)."
            bt.finished_at = datetime.now(timezone.utc)
            restored += 1
    if restored:
        log.warning("Reconciled %s interrupted baseline test(s); SQM re-enabled", restored)
    return restored


__all__ = ["start", "cancel", "active", "current", "reconcile_interrupted_baseline_tests"]
