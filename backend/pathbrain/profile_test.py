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

**Tests queue; they are never refused.** This module used to be a strict singleton — a
second ``start`` raised, the API turned that into a 409, and the button dead-ended. The
coordinator underneath has always queued (``hold`` blocks), so the refusal was not
protecting anything: it fired *before* the queueing machinery was reached, and it fired
hardest exactly when queueing is what a person wants. Worse, the flag was set the moment
a test was created rather than when it started running, so a test sitting behind a duel
window — hours — refused every other test for the whole night.

So the pending rows **are** the queue. Each carries its own ``target`` (persisted, because
a queue cannot keep several targets in one in-memory slot), one worker thread drains them
oldest-first, and a queued test is a row anyone can list, count or cancel before it ever
touches the firewall. A test only becomes RUNNING once it holds the coordination lock, so
"pending" honestly means *not yet applied to anything*.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from sqlalchemy import func, select

from . import coordinator
from .database import session_scope
from .logging_config import get_logger
from .session_runtime import describe_failure
from .models import ProfileTest, ProfileTestStatus
from .providers import get_provider
from .runner import CHUNK_ITERATIONS, run_chunk, teardown_plugins
from .settings_profile import fingerprint, normalize, plan_apply
from .shaper_fields import WRITABLE_FIELDS

log = get_logger("profile_test")

# The queue lives in the database (PENDING rows, ordered by id); this is only the drainer.
# ``thread`` is the single worker, ``driving`` the test it has claimed, and ``cancel`` the
# ids asked to stop — a set, because the test being cancelled may not be the running one.
_state: dict = {"thread": None, "driving": None, "cancel": set()}
# Guards _state *and* the worker's decision to exit. Without it there is a window where a
# worker finds the queue empty while a request is committing a row: the worker exits, the
# request sees a live thread and spawns nothing, and the queue strands with no drainer.
_gate = threading.RLock()


def active() -> bool:
    """True while a test is claimed by the worker (queued for the lock, or running)."""
    with _gate:
        return _state.get("driving") is not None


def queue_depth() -> int:
    """How many tests are waiting to start."""
    with session_scope() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(ProfileTest)
                .where(ProfileTest.status == ProfileTestStatus.PENDING)
            )
            or 0
        )


def cancel(test_id: int | None = None) -> bool:
    """Cancel a test. Returns True if one was cancelled.

    ``test_id`` omitted cancels whichever test is currently claimed (the historical
    behaviour, and what the toolbar button means). A *pending* test is dropped from the
    queue outright — it has applied nothing, so there is nothing to restore and no reason
    to make the user wait for it to start just so it can stop. A *running* test is asked
    to stop after its current chunk; its baseline is still restored by the driver's
    ``finally``.
    """
    if test_id is None:
        with _gate:
            test_id = _state.get("driving")
        if test_id is None:
            return False
        _state["cancel"].add(test_id)
        log.info("Profile test %s: cancel requested", test_id)
        return True

    with _gate:
        if _state.get("driving") == test_id:
            _state["cancel"].add(test_id)
            log.info("Profile test %s: cancel requested", test_id)
            return True
    # Not the claimed one: drop it from the queue before it ever runs.
    with session_scope() as session:
        pt = session.get(ProfileTest, test_id)
        if pt is None or pt.status != ProfileTestStatus.PENDING:
            return False
        pt.status = ProfileTestStatus.CANCELLED
        pt.stage = "Cancelled while queued — nothing was applied"
        pt.finished_at = datetime.now(timezone.utc)
    log.info("Profile test %s: removed from the queue before it started", test_id)
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
    """Enqueue a profile test. Returns the ``ProfileTest`` id.

    Never refuses: the row is written PENDING and a single worker drains the queue
    oldest-first. The baseline is snapshotted inside the driver (under the coordination
    lock) so it reflects the true pre-apply state — which is also why a queued test is
    harmless: it has read nothing and applied nothing until its turn comes.
    """
    with session_scope() as session:
        ahead = int(
            session.scalar(
                select(func.count())
                .select_from(ProfileTest)
                .where(ProfileTest.status == ProfileTestStatus.PENDING)
            )
            or 0
        )
        pt = ProfileTest(
            status=ProfileTestStatus.PENDING,
            fingerprint=fingerprint_,
            target_label=label,
            iterations=iterations,
            baseline=None,
            target=target_settings,
            stage=(
                "Queued — waiting for any running benchmark to finish"
                if ahead or coordinator.busy()
                else "Queued — starting"
            ),
        )
        session.add(pt)
        session.flush()
        pt_id = pt.id

    _ensure_worker()
    log.info(
        "Profile test %s queued: %s (%s iteration(s), %s ahead of it)",
        pt_id, fingerprint_, iterations, ahead,
    )
    return pt_id


def _ensure_worker() -> None:
    """Make sure a drainer is running. Idempotent and safe to call from any thread."""
    with _gate:
        thread = _state.get("thread")
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(target=_worker, name="pathbrain-profile-test", daemon=True)
        _state["thread"] = thread
        thread.start()


def _next_pending_id() -> int | None:
    """The oldest queued test, or None. FIFO: tests run in the order they were asked for."""
    with session_scope() as session:
        return session.scalar(
            select(ProfileTest.id)
            .where(ProfileTest.status == ProfileTestStatus.PENDING)
            .order_by(ProfileTest.id)
            .limit(1)
        )


def _worker() -> None:
    """Drain the queue until it is empty, then retire.

    Claiming the next test and deciding to exit both happen under ``_gate``, so a row
    committed by a request either is seen by this worker or arrives after the worker has
    cleared itself — in which case that request starts a new one. There is no third case,
    which is what stops the queue stranding with nobody draining it.
    """
    while True:
        with _gate:
            pt_id = _next_pending_id()
            if pt_id is None:
                _state["thread"] = None
                return
            _state["driving"] = pt_id
        try:
            _drive(pt_id)
        except Exception:  # noqa: BLE001 — one bad test must never kill the drainer
            log.exception("Profile test %s: driver raised; continuing with the queue", pt_id)
        finally:
            with _gate:
                _state["driving"] = None
                _state["cancel"].discard(pt_id)


def _drive(pt_id: int) -> None:
    provider = get_provider()
    # The target rides on the row: several tests can be queued at once, so it cannot live
    # in one module-level slot that the next request would overwrite.
    with session_scope() as session:
        row = session.get(ProfileTest, pt_id)
        target = list(row.target or []) if row is not None else []
    final_status = ProfileTestStatus.COMPLETE
    err: str | None = None
    if not target:
        log.error("Profile test %s: no target settings on the row; cannot run", pt_id)
        with session_scope() as session:
            row = session.get(ProfileTest, pt_id)
            if row is not None:
                row.status = ProfileTestStatus.FAILED
                row.error = "No target settings recorded for this test."
                row.stage = "Failed — no target settings recorded"
                row.finished_at = datetime.now(timezone.utc)
        return
    try:
        # Hold the coordination lock for the whole session (apply → benchmark →
        # restore). Queues behind any in-progress firewall/benchmark session.
        with coordinator.hold(f"profile-test#{pt_id}"):
            # Cancelled while it queued for the lock. Checked here, before the first
            # discover/apply, so a cancel during a long wait costs no firewall write at
            # all rather than an apply-and-restore round trip nobody asked for.
            if pt_id in _state["cancel"]:
                final_status = ProfileTestStatus.CANCELLED
                _set_stage(pt_id, "Cancelled while queued — nothing was applied")
                return
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
                    if pt_id in _state["cancel"]:
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
                            f"Profile test #{pt_id}: {iterations} iteration(s) on {target_fp} "
                            f"· part {idx}/{n_chunks}"
                        ),
                        iterations=iters,
                        teardown=False,  # keep Chromium warm across chunks; closed after the loop
                        job_group=f"profile_test-{pt_id}",  # group chunks under the parent job
                        job_group_total=iterations,
                        # Lift the browser's per-plugin cap for the test's chunks: the crown
                        # metrics (fcp/lcp/network_stall_all) are ALL browser-derived, so under
                        # the default cap of 2 a 5-iteration quick test's verdict rests on two
                        # page loads — the dominant noise term the recommendation ledger
                        # measured. A profile test is an explicit "measure this profile"
                        # action; here the browser samples are the point, and the cap's
                        # wall-clock saving belongs to monitoring runs, not to this.
                        config_overrides={"browser": {"iterations": iters}},
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
                err = describe_failure(exc)
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
        err = describe_failure(exc)
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
        "lock_owner_label": coordinator.describe(coordinator.owner()),
        # A queued test has applied nothing yet — worth stating outright, because it is the
        # difference between "cancel this" being free and it costing a restore.
        "queued": pt.status == ProfileTestStatus.PENDING,
    }


def current() -> dict | None:
    """The most recent profile test (for status polling), or None."""
    with session_scope() as session:
        pt = session.scalars(select(ProfileTest).order_by(ProfileTest.id.desc())).first()
        return _serialize(pt) if pt else None


def active_tests() -> list[dict]:
    """Every test that has not finished — the running one first, then the queue in order."""
    with session_scope() as session:
        rows = session.scalars(
            select(ProfileTest)
            .where(
                ProfileTest.status.in_([ProfileTestStatus.RUNNING, ProfileTestStatus.PENDING])
            )
            .order_by(ProfileTest.id)
        ).all()
        items = [_serialize(pt) for pt in rows]
    running = [i for i in items if i["status"] == "running"]
    pending = [i for i in items if i["status"] == "pending"]
    for position, item in enumerate(pending, start=1):
        item["queue_position"] = position
    return running + pending


def queue_status() -> dict:
    """Everything a "the pipeline is busy — queue this?" prompt needs, in one cheap read.

    The question a person is really asking before pressing a second time is *what is in
    the way and how many are already waiting*, so this answers both rather than the bare
    boolean the coordinator exposes. Deliberately no estimate of when the queue drains: a
    duel window runs for hours and a monitoring run for minutes, and a fabricated wait is
    the one number someone would plan around.
    """
    items = active_tests()
    owner = coordinator.owner()
    pending = [i for i in items if i["status"] == "pending"]
    running = next((i for i in items if i["status"] == "running"), None)
    return {
        "busy": coordinator.busy(),
        "owner": owner,
        "owner_label": coordinator.describe(owner),
        "held_for_s": coordinator.held_for(),
        "waiting": coordinator.waiting(),
        "running": running,
        "pending": pending,
        "queue_depth": len(pending),
    }


def reconcile_interrupted_profile_tests(*, now: datetime | None = None) -> int:
    """Restore the baseline for any profile test left RUNNING by a previous process, and
    decide what happens to the ones left PENDING.

    Called once at startup, like ``sweep.reconcile_interrupted_sweeps``. The driving
    thread is gone, so the firewall may be stranded on the tested profile — set it
    back to the snapshotted baseline. A *queued* test applied nothing, so it is not
    failed: one younger than ``job_queue.RESUME_WINDOW_HOURS`` stays PENDING (stamped so
    the feed says it came back) and :func:`resume_queued` starts the worker once every
    engine has restored the firewall; an older one is closed CANCELLED with the reason,
    because a queue from last week is a surprise, not a queue. Returns the rows touched.
    """
    from .job_queue import RESUME_WINDOW_HOURS

    now = now or datetime.now(timezone.utc)
    provider = None
    restored = 0
    resumed = 0
    with session_scope() as session:
        tests = session.scalars(
            select(ProfileTest).where(
                ProfileTest.status.in_([ProfileTestStatus.RUNNING, ProfileTestStatus.PENDING])
            )
        ).all()
        for pt in tests:
            queued = pt.status == ProfileTestStatus.PENDING
            baseline = pt.baseline or []
            if baseline:
                try:
                    provider = provider or get_provider()
                    changes, _ = plan_apply(baseline, provider.discover())
                    _apply_all(provider, changes)
                except Exception:  # noqa: BLE001
                    log.exception("Profile test %s: restore on reconcile failed", pt.id)
            # A queued test never applied anything, so it did not fail — it simply never
            # ran. Saying so keeps "failed" meaning something, and the restart may be hours
            # later, by which point silently running a test nobody is watching is worse
            # than making them press it again.
            if queued:
                created = pt.created_at if pt.created_at is None or pt.created_at.tzinfo else pt.created_at.replace(tzinfo=timezone.utc)
                age_h = (now - created).total_seconds() / 3600.0 if created else float("inf")
                if age_h <= RESUME_WINDOW_HOURS:
                    pt.stage = "Queued — resumed after a restart"
                    resumed += 1
                    continue
                pt.status = ProfileTestStatus.CANCELLED
                pt.error = None
                pt.stage = (
                    f"Not started — queued {age_h:.0f} h ago, before a restart; older than the "
                    f"{RESUME_WINDOW_HOURS:g} h resume window"
                )
            else:
                pt.status = ProfileTestStatus.FAILED
                pt.error = (
                    "Interrupted — service restarted mid-test; baseline restored (best-effort)."
                )
            pt.finished_at = datetime.now(timezone.utc)
            restored += 1
    if restored:
        log.warning("Reconciled %s interrupted profile test(s); baseline restored", restored)
    if resumed:
        log.warning("%s queued profile test(s) kept pending across the restart", resumed)
    return restored + resumed


def resume_queued() -> int:
    """Start the worker for any tests still PENDING after startup reconciliation.

    Separate from the reconcile on purpose: it runs from the lifespan **after** every
    engine has restored the firewall, so the first resumed test snapshots the real
    baseline rather than whatever profile a dead duel left behind. Returns how many are
    waiting; the worker drains them oldest-first exactly as if they had just been queued.
    """
    with session_scope() as session:
        waiting = int(
            session.scalar(
                select(func.count()).select_from(ProfileTest).where(ProfileTest.status == ProfileTestStatus.PENDING)
            )
            or 0
        )
    if waiting:
        _ensure_worker()
        log.info("Resuming %s queued profile test(s) after a restart", waiting)
    return waiting


__all__ = [
    "start",
    "active",
    "cancel",
    "current",
    "active_tests",
    "queue_depth",
    "queue_status",
    "reconcile_interrupted_profile_tests",
]
