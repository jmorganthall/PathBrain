"""Profile test: top a "limited data" settings profile up to the confidence bar.

A profile is "confident" once its runs total at least ``correlation.min_iterations``
iterations. For a profile that's short of that, this runs a single, supervised
session that:

1. Snapshots the live firewall settings (the baseline to restore).
2. Applies the target profile for real (via ``provider.apply()``).
3. **Reads the firewall back** and verifies it actually reached the target profile.
4. Benchmarks exactly the iterations still needed to hit the minimum, **chunked** into
   blocks of ``runner.CHUNK_ITERATIONS`` iterations each (the same pattern as the timed
   "test current" engine and large manual runs) so each block is persisted the moment it
   finishes — an interruption keeps every completed chunk instead of losing the whole run.
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
from .runner import CHUNK_ITERATIONS, run_chunk, teardown_plugins
from .settings_profile import fingerprint, normalize, plan_apply
from .shaper_fields import WRITABLE_FIELDS

log = get_logger("profile_test")

# Single profile test at a time. Module state coordinates with the driver thread
# and holds the target settings (which aren't stored on the row) + a cooperative cancel flag.
_state: dict = {"active": False, "id": None, "target": None, "thread": None, "cancel": False}


def active() -> bool:
    return bool(_state.get("active"))


def cancel() -> bool:
    """Ask the running profile test to stop after its current chunk. Returns True if one was
    active. The baseline is still restored (the driver's ``finally``)."""
    if not active():
        return False
    _state["cancel"] = True
    log.info("Profile test %s: cancel requested", _state.get("id"))
    return True


def _apply_all(provider, changes: list[dict]) -> None:
    for ch in changes:
        provider.apply({"pipe_uuid": ch["pipe_uuid"], "param": ch["param"], "value": ch["value"]})


def _set_stage(pt_id: int, stage: str) -> None:
    """Record the current step on the row (for the live UI readout) and log it."""
    log.info("Profile test %s: %s", pt_id, stage)
    try:
        with session_scope() as session:
            pt = session.get(ProfileTest, pt_id)
            if pt is not None:
                pt.stage = stage
    except Exception:  # noqa: BLE001 — a status write must never break the test
        log.debug("Profile test %s: could not persist stage %r", pt_id, stage, exc_info=True)


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
            stage="Queued — waiting for any running benchmark to finish",
        )
        session.add(pt)
        session.flush()
        pt_id = pt.id

    _state.update({"active": True, "id": pt_id, "target": target_settings, "cancel": False})
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
            _set_stage(pt_id, "Reading current firewall settings")
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
                # Apply the target profile, then read it back to confirm every writable field
                # actually took — semantically (via plan_apply), not by exact fingerprint hash,
                # which is format-sensitive ("5ms" vs 5) and would false-negative on values the
                # firewall stores in its own representation.
                changes, warnings = plan_apply(target, live)
                if changes:
                    detail = ", ".join(
                        f"{c['label']}·{c['field']} {c.get('from')}→{c.get('to')}" for c in changes
                    )
                    _set_stage(pt_id, f"Applying {len(changes)} change(s): {detail}"[:255])
                    _apply_all(provider, changes)
                else:
                    _set_stage(pt_id, "Firewall already on the target profile — no changes to apply")

                _set_stage(pt_id, "Verifying the firewall reached the target")
                live_after = provider.discover()
                after = normalize(live_after)
                remaining, _ = plan_apply(target, live_after)
                if remaining:
                    missed = ", ".join(
                        f"{c['label']}·{c['field']} (wanted {c.get('to')}, is {c.get('from')})"
                        for c in remaining
                    )
                    raise RuntimeError(
                        f"Firewall did not accept {len(remaining)} field(s): {missed}. "
                        "The apply did not take — check provider write permissions / field support."
                    )
                reached_fp = fingerprint(after)
                # The verify above raises unless every writable field matches, so reaching this
                # line means the firewall IS on the requested profile. If the fingerprints still
                # differ it is a spelling difference — we write ``55``, the firewall reports
                # ``"55"`` — and the runs will be filed under the firewall's version. Log the
                # actual field values behind it, because "settled on a different fingerprint" is
                # alarming and unfalsifiable without them.
                if reached_fp != target_fp:
                    spelled = "; ".join(
                        f"{t.get('label')}·{f}: asked {t.get(f)!r} ({type(t.get(f)).__name__}), "
                        f"reports {a.get(f)!r} ({type(a.get(f)).__name__})"
                        for t, a in zip(target or [], after)
                        for f in WRITABLE_FIELDS
                        if t.get(f) is not None and t.get(f) != a.get(f)
                    )
                    log.warning(
                        "Profile test %s: firewall reports %s, we asked for %s — same profile, "
                        "different spelling (every field verified equal). %s",
                        pt_id, reached_fp, target_fp, spelled or "(no field-level difference found)",
                    )
                else:
                    log.info("Profile test %s: firewall reached %s (target %s)", pt_id, reached_fp, target_fp)
                with session_scope() as session:
                    row = session.get(ProfileTest, pt_id)
                    if row is not None:
                        row.reached_fingerprint = reached_fp

                # Benchmark the target profile in blocks of CHUNK_ITERATIONS, not one long
                # run, so each block persists as it finishes (an interruption keeps the data
                # collected so far). The target stays applied for the whole session — every
                # chunk benchmarks the same firewall state — and the coordinator lock is held
                # across all chunks. ``run_chunk`` reports completion so a failed chunk (e.g.
                # mid-run settings drift) stops the series with the environment flagged unstable.
                n_chunks = (iterations + CHUNK_ITERATIONS - 1) // CHUNK_ITERATIONS
                run_ids: list[int] = []
                done = 0
                idx = 0
                while done < iterations:
                    if _state.get("cancel"):
                        final_status = ProfileTestStatus.CANCELLED
                        _set_stage(pt_id, f"Cancelled after {done} iteration(s) — restoring baseline")
                        break
                    idx += 1
                    iters = min(CHUNK_ITERATIONS, iterations - done)
                    _set_stage(
                        pt_id,
                        f"Benchmarking on the target profile — part {idx}/{n_chunks} "
                        f"({done}/{iterations} iteration(s) done)",
                    )
                    run_id, ok, completed = run_chunk(
                        label=f"test · {label}",
                        notes=(
                            f"Profile test #{pt_id}: top up {target_fp} to the confidence "
                            f"minimum · part {idx}/{n_chunks}"
                        ),
                        iterations=iters,
                        teardown=False,  # keep Chromium warm across chunks; closed after the loop
                        job_group=f"profile_test-{pt_id}",  # group chunks under the parent job
                        job_group_total=iterations,
                    )
                    run_ids.append(run_id)
                    # Record the first chunk's run as the test's representative run_id (the UI
                    # links to it), and track progress for the live readout.
                    if idx == 1:
                        with session_scope() as session:
                            pt = session.get(ProfileTest, pt_id)
                            if pt is not None:
                                pt.run_id = run_id
                    done += completed
                    if not ok:
                        raise RuntimeError(
                            f"A benchmark chunk failed (run #{run_id}); stopped after "
                            f"{done} iteration(s) with collected data kept."
                        )
                _set_stage(pt_id, f"Benchmark complete ({done} iteration(s) across {len(run_ids)} chunk(s))")
            except Exception as exc:  # noqa: BLE001 — record + restore, never crash the thread
                log.exception("Profile test %s failed", pt_id)
                final_status = ProfileTestStatus.FAILED
                err = f"{type(exc).__name__}: {exc}"
            finally:
                # Chromium was kept warm across the benchmark chunks; close it once now.
                teardown_plugins()
                # Always restore the pre-test baseline.
                try:
                    _set_stage(pt_id, "Restoring your original settings")
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
                pt.stage = {
                    ProfileTestStatus.COMPLETE: "Done — baseline restored",
                    ProfileTestStatus.CANCELLED: "Cancelled — baseline restored",
                }.get(final_status, err or "Failed")
                pt.finished_at = datetime.now(timezone.utc)
        _state.update({"active": False, "id": None, "target": None, "cancel": False})
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
        "stage": pt.stage,
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
