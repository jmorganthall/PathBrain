"""Profile test: top a "limited data" settings profile up to the confidence bar.

A profile is "confident" once its runs total at least ``correlation.min_iterations``
iterations. For a profile that's short of that, this runs a single, supervised
session that:

1. Snapshots the live firewall settings (the baseline to restore).
2. Applies the target profile for real (via ``provider.apply()``).
3. **Reads the firewall back** and verifies it actually reached the target profile.
4. Runs one benchmark with exactly the iterations still needed to hit the minimum.
5. **Always** restores the pre-test baseline at the end (and on crash-restart, via
   ``reconcile_interrupted_profile_tests``).

It runs in its own thread and holds the coordination lock for the whole session, so
it never overlaps a sweep, an experiment, or a monitoring/manual run. The benchmark
itself adds the read-before/read-after integrity guarantee (see ``runner``).
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from sqlalchemy import select

from . import coordinator
from .database import session_scope
from .logging_config import get_logger
from .models import ProfileTest, ProfileTestStatus
from .providers import get_provider
from .runner import create_run, execute_run
from .settings_profile import fingerprint, normalize, plan_apply

log = get_logger("profile_test")

# Single profile test at a time. Module state coordinates with the driver thread
# and holds the target settings (which aren't stored on the row).
_state: dict = {"active": False, "id": None, "target": None, "thread": None}


def active() -> bool:
    return bool(_state.get("active"))


def _apply_all(provider, changes: list[dict]) -> None:
    for ch in changes:
        provider.apply({"pipe_uuid": ch["pipe_uuid"], "param": ch["param"], "value": ch["value"]})


def start(fingerprint_: str, target_settings: list[dict], label: str, iterations: int) -> int:
    """Launch a profile test. Returns the ``ProfileTest`` id.

    Raises ``RuntimeError`` if a test is already running. The baseline is snapshotted
    inside the driver (under the lock) so it reflects the true pre-apply state.
    """
    if active():
        raise RuntimeError("A profile test is already running.")
    with session_scope() as session:
        pt = ProfileTest(
            status=ProfileTestStatus.PENDING,
            fingerprint=fingerprint_,
            target_label=label,
            iterations=iterations,
            baseline=None,
        )
        session.add(pt)
        session.flush()
        pt_id = pt.id

    _state.update({"active": True, "id": pt_id, "target": target_settings})
    thread = threading.Thread(target=_drive, args=(pt_id,), name="pathbrain-profile-test", daemon=True)
    _state["thread"] = thread
    thread.start()
    log.info("Profile test %s started: %s (%s iteration(s))", pt_id, fingerprint_, iterations)
    return pt_id


def _drive(pt_id: int) -> None:
    provider = get_provider()
    target = _state.get("target")
    final_status = ProfileTestStatus.COMPLETE
    err: str | None = None
    try:
        # Hold the coordination lock for the whole session (apply → benchmark →
        # restore). Queues behind any in-progress firewall/benchmark session.
        with coordinator.hold(f"profile-test#{pt_id}"):
            live = provider.discover()
            baseline = normalize(live)
            with session_scope() as session:
                pt = session.get(ProfileTest, pt_id)
                pt.status = ProfileTestStatus.RUNNING
                pt.started_at = datetime.now(timezone.utc)
                pt.baseline = baseline
                iterations = pt.iterations
                target_fp = pt.fingerprint
                label = pt.target_label or target_fp
            try:
                # Apply the target profile, then read it back to confirm we reached it.
                changes, _warnings = plan_apply(target, live)
                _apply_all(provider, changes)
                reached_fp = fingerprint(normalize(provider.discover()))
                if reached_fp != target_fp:
                    raise RuntimeError(
                        f"Could not reach the target profile (got {reached_fp}, wanted {target_fp})."
                    )

                run_id = create_run(
                    label=f"test · {label}",
                    notes=f"Profile test #{pt_id}: top up {target_fp} to the confidence minimum",
                    iterations=iterations,
                )
                execute_run(run_id)  # blocking; its own read-before/after integrity applies
                with session_scope() as session:
                    pt = session.get(ProfileTest, pt_id)
                    pt.run_id = run_id
            except Exception as exc:  # noqa: BLE001 — record + restore, never crash the thread
                log.exception("Profile test %s failed", pt_id)
                final_status = ProfileTestStatus.FAILED
                err = f"{type(exc).__name__}: {exc}"
            finally:
                # Always restore the pre-test baseline.
                try:
                    restore_changes, _ = plan_apply(baseline, provider.discover())
                    _apply_all(provider, restore_changes)
                    log.info("Profile test %s: restored baseline", pt_id)
                except Exception:  # noqa: BLE001 — never raise out of cleanup
                    log.exception("Profile test %s: baseline restore failed", pt_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("Profile test %s: unexpected failure", pt_id)
        final_status = ProfileTestStatus.FAILED
        err = f"{type(exc).__name__}: {exc}"
    finally:
        with session_scope() as session:
            pt = session.get(ProfileTest, pt_id)
            if pt is not None:
                pt.status = final_status
                pt.error = err
                pt.finished_at = datetime.now(timezone.utc)
        _state.update({"active": False, "id": None, "target": None})
        log.info("Profile test %s finished: %s", pt_id, final_status.value)


def _serialize(pt: ProfileTest) -> dict:
    return {
        "id": pt.id,
        "status": pt.status.value if hasattr(pt.status, "value") else str(pt.status),
        "fingerprint": pt.fingerprint,
        "label": pt.target_label,
        "iterations": pt.iterations,
        "run_id": pt.run_id,
        "error": pt.error,
        "created_at": pt.created_at.isoformat() if pt.created_at else None,
        "started_at": pt.started_at.isoformat() if pt.started_at else None,
        "finished_at": pt.finished_at.isoformat() if pt.finished_at else None,
        # Best-effort label of whatever currently holds the coordination lock, so
        # the UI can explain a queued/waiting test.
        "lock_owner": coordinator.owner(),
    }


def current() -> dict | None:
    """The most recent profile test (for status polling), or None."""
    with session_scope() as session:
        pt = session.scalars(select(ProfileTest).order_by(ProfileTest.id.desc())).first()
        return _serialize(pt) if pt else None


def reconcile_interrupted_profile_tests() -> int:
    """Restore the baseline for any profile test left RUNNING by a previous process.

    Called once at startup, like ``sweep.reconcile_interrupted_sweeps``. The driving
    thread is gone, so the firewall may be stranded on the tested profile — set it
    back to the snapshotted baseline.
    """
    provider = None
    restored = 0
    with session_scope() as session:
        tests = session.scalars(
            select(ProfileTest).where(
                ProfileTest.status.in_([ProfileTestStatus.RUNNING, ProfileTestStatus.PENDING])
            )
        ).all()
        for pt in tests:
            baseline = pt.baseline or []
            if baseline:
                try:
                    provider = provider or get_provider()
                    changes, _ = plan_apply(baseline, provider.discover())
                    _apply_all(provider, changes)
                except Exception:  # noqa: BLE001
                    log.exception("Profile test %s: restore on reconcile failed", pt.id)
            pt.status = ProfileTestStatus.FAILED
            pt.error = "Interrupted — service restarted mid-test; baseline restored (best-effort)."
            pt.finished_at = datetime.now(timezone.utc)
            restored += 1
    if restored:
        log.warning("Reconciled %s interrupted profile test(s); baseline restored", restored)
    return restored


__all__ = ["start", "active", "current", "reconcile_interrupted_profile_tests"]
