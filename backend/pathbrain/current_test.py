"""Test current: a time-boxed data-collection loop on the *current* firewall profile.

Given "test the current settings for X minutes", this benchmarks whatever the firewall
is already on, in short chunks (``runner.CHUNK_ITERATIONS`` iterations each), until the
deadline passes (or the user cancels). Because it measures the live profile as-is it
**never writes the firewall** — there is no baseline to snapshot or restore (the key
difference from ``profile_test``/``challenger``/``refresh``). Chunking means each block of
data is persisted the moment it finishes, so an interrupted session keeps everything
collected so far rather than losing the whole series.

It runs in its own thread and holds the coordination lock for the whole session, so it
never overlaps a sweep, an experiment, a profile test, or a monitoring/manual run. Each
chunk benchmark carries the runner's own read-before/after integrity guarantee.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from . import coordinator
from .database import session_scope
from .logging_config import get_logger
from .models import CurrentTest, CurrentTestStatus
from .providers import get_provider
from .runner import CHUNK_ITERATIONS, run_chunk
from .settings_profile import normalize, summarize

log = get_logger("current_test")

# One test at a time. Module state coordinates with the driver thread and carries the
# cooperative cancel flag (the row only records durable state).
_state: dict = {"active": False, "id": None, "cancel": False, "thread": None}


def active() -> bool:
    return bool(_state.get("active"))


def _current_label() -> str | None:
    """Short human summary of the live profile (best-effort), for the status display."""
    try:
        return summarize(normalize(get_provider().discover()))
    except Exception:  # noqa: BLE001 — label is cosmetic; the test still runs
        log.debug("Could not summarize current settings for test-current label", exc_info=True)
        return None


def start(minutes: float) -> int:
    """Launch a "test current for X minutes" session. Returns the ``CurrentTest`` id.

    Raises ``ValueError`` for a non-positive duration and ``RuntimeError`` if a test is
    already running.
    """
    if not minutes or minutes <= 0:
        raise ValueError("Duration must be a positive number of minutes.")
    if active():
        raise RuntimeError("A test-current session is already running.")
    duration_s = int(round(minutes * 60))
    with session_scope() as session:
        ct = CurrentTest(
            status=CurrentTestStatus.PENDING,
            duration_s=duration_s,
            target_label=_current_label(),
            iterations_run=0,
            runs_created=0,
            run_ids=[],
        )
        session.add(ct)
        session.flush()
        ct_id = ct.id

    _state.update({"active": True, "id": ct_id, "cancel": False})
    thread = threading.Thread(target=_drive, args=(ct_id,), name="pathbrain-current-test", daemon=True)
    _state["thread"] = thread
    thread.start()
    log.info("Test-current %s started: %s minute(s)", ct_id, minutes)
    return ct_id


def cancel() -> bool:
    """Ask the running test to stop after its current chunk. Returns True if one was active."""
    if not active():
        return False
    _state["cancel"] = True
    log.info("Test-current %s: cancel requested", _state.get("id"))
    return True


def _drive(ct_id: int) -> None:
    final_status = CurrentTestStatus.COMPLETE
    err: str | None = None
    run_ids: list[int] = []
    try:
        # Hold the lock for the whole session, queuing behind any in-progress
        # firewall/benchmark session (and deferring the periodic scheduler).
        with coordinator.hold(f"test-current#{ct_id}"):
            with session_scope() as session:
                ct = session.get(CurrentTest, ct_id)
                ct.status = CurrentTestStatus.RUNNING
                ct.started_at = datetime.now(timezone.utc)
                duration_s = ct.duration_s
                label = ct.target_label or "current settings"
            deadline = time.monotonic() + duration_s
            iterations_run = 0
            # Keep collecting until the clock runs out or the user cancels. Each chunk is
            # persisted as its own run, so a stop at any point keeps prior chunks' data.
            while time.monotonic() < deadline and not _state.get("cancel"):
                run_id, ok, completed = run_chunk(
                    label=f"test-current · {label}",
                    notes=f"Test-current #{ct_id}: {duration_s // 60} min on the live profile",
                    iterations=CHUNK_ITERATIONS,
                )
                run_ids.append(run_id)
                iterations_run += completed
                with session_scope() as session:
                    ct = session.get(CurrentTest, ct_id)
                    if ct is not None:
                        ct.iterations_run = iterations_run
                        ct.runs_created = len(run_ids)
                        ct.run_ids = list(run_ids)
                if not ok:
                    # A failed chunk (e.g. mid-run settings drift) means the environment
                    # isn't stable — stop rather than hammer it, keeping what we collected.
                    log.warning("Test-current %s: chunk run %s did not complete; stopping", ct_id, run_id)
                    final_status = CurrentTestStatus.FAILED
                    err = "A benchmark chunk failed (see its run); stopped early with data kept."
                    break
            if _state.get("cancel") and final_status == CurrentTestStatus.COMPLETE:
                final_status = CurrentTestStatus.CANCELLED
    except Exception as exc:  # noqa: BLE001 — record, never crash the thread
        log.exception("Test-current %s: unexpected failure", ct_id)
        final_status = CurrentTestStatus.FAILED
        err = f"{type(exc).__name__}: {exc}"
    finally:
        with session_scope() as session:
            ct = session.get(CurrentTest, ct_id)
            if ct is not None:
                ct.status = final_status
                ct.error = err
                ct.finished_at = datetime.now(timezone.utc)
        _state.update({"active": False, "id": None, "cancel": False})
        log.info(
            "Test-current %s finished: %s (%s run(s))", ct_id, final_status.value, len(run_ids)
        )


def _serialize(ct: CurrentTest) -> dict:
    return {
        "id": ct.id,
        "status": ct.status.value if hasattr(ct.status, "value") else str(ct.status),
        "label": ct.target_label,
        "duration_s": ct.duration_s,
        "iterations_run": ct.iterations_run,
        "runs_created": ct.runs_created,
        "run_ids": ct.run_ids or [],
        "error": ct.error,
        "created_at": ct.created_at.isoformat() if ct.created_at else None,
        "started_at": ct.started_at.isoformat() if ct.started_at else None,
        "finished_at": ct.finished_at.isoformat() if ct.finished_at else None,
        # Best-effort label of whatever holds the coordination lock, so the UI can
        # explain a queued/waiting test.
        "lock_owner": coordinator.owner(),
    }


def current() -> dict | None:
    """The most recent test-current session (for status polling), or None."""
    with session_scope() as session:
        ct = session.scalars(select(CurrentTest).order_by(CurrentTest.id.desc())).first()
        return _serialize(ct) if ct else None


def reconcile_interrupted_current_tests() -> int:
    """Mark any test-current session left RUNNING/PENDING by a previous process FAILED.

    Called once at startup. Nothing to restore — the test never writes the firewall — so
    this only closes out the orphaned row (its completed chunk runs are already persisted).
    """
    closed = 0
    with session_scope() as session:
        tests = session.scalars(
            select(CurrentTest).where(
                CurrentTest.status.in_([CurrentTestStatus.RUNNING, CurrentTestStatus.PENDING])
            )
        ).all()
        for ct in tests:
            ct.status = CurrentTestStatus.FAILED
            ct.error = "Interrupted — service restarted mid-test; collected data kept."
            ct.finished_at = datetime.now(timezone.utc)
            closed += 1
    if closed:
        log.warning("Reconciled %s interrupted test-current session(s)", closed)
    return closed


__all__ = ["start", "cancel", "active", "current", "reconcile_interrupted_current_tests"]
