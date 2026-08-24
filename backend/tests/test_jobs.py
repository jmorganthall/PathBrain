"""Tests for the background-job registry and the unified /api/jobs feed."""
from __future__ import annotations

import importlib
import threading
import time

from pathbrain import jobs
from pathbrain.database import session_scope
from pathbrain.models import Run, RunStatus


def _wait(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def _find(job_id: str) -> dict | None:
    return next((j for j in jobs.list_jobs() if j["id"] == job_id), None)


def test_job_runs_to_success_with_progress():
    gate = threading.Event()

    def work(job):
        job.set_progress(1, 2, "halfway")
        gate.wait(2.0)
        job.set_progress(2, 2, "done")
        return {"scored": 7}

    job_id = jobs.start("unit-success", "unit job", work)
    # Visible as running with progress before we release the gate.
    _wait(lambda: (_find(job_id) or {}).get("current") == 1)
    assert _find(job_id)["status"] == "running"
    gate.set()
    _wait(lambda: (_find(job_id) or {}).get("status") == "succeeded")
    done = _find(job_id)
    assert done["current"] == 2 and done["total"] == 2
    assert "scored 7" in (done["message"] or "")  # summary derived from the returned dict


def test_job_failure_is_recorded():
    def boom(job):
        raise ValueError("nope")

    job_id = jobs.start("unit-fail", "boom", boom)
    _wait(lambda: (_find(job_id) or {}).get("status") == "failed")
    assert "nope" in (_find(job_id)["error"] or "")


def test_same_kind_is_not_started_twice():
    gate = threading.Event()

    def work(job):
        gate.wait(2.0)

    first = jobs.start("unit-dedupe", "first", work)
    second = jobs.start("unit-dedupe", "second", work)  # should reuse the running one
    assert first == second
    gate.set()
    _wait(lambda: (_find(first) or {}).get("status") == "succeeded")


def test_jobs_endpoint_merges_registry_and_run_adapter(client):
    # An in-process job appears in the feed...
    gate = threading.Event()
    job_id = jobs.start("unit-feed", "feed job", lambda job: gate.wait(2.0))

    # ...and a live benchmark run is synthesized as an adapter entry.
    with session_scope() as s:
        run = Run(status=RunStatus.RUNNING, label="live run", iterations=3, iterations_completed=1)
        s.add(run)
        s.flush()
        rid = run.id

    body = client.get("/api/jobs").json()
    ids = {j["id"] for j in body["jobs"]}
    assert job_id in ids
    assert f"run-{rid}" in ids
    run_entry = next(j for j in body["jobs"] if j["id"] == f"run-{rid}")
    assert run_entry["kind"] == "run"
    assert run_entry["total"] == 3 and run_entry["current"] == 1
    assert body["running"] >= 2

    gate.set()
    # Clean up: fully remove the seeded run so it can't leak into other tests'
    # aggregations or "active" checks.
    with session_scope() as s:
        s.delete(s.get(Run, rid))


def test_chunked_series_nests_under_a_parent_with_aggregate_progress(client):
    """Chunks of a manual run series carry a job_group; the feed synthesizes one parent line
    with aggregate progress, nests the active chunk under it, and offers cancel at both levels."""
    group = "run-series-90001"
    rids = []
    with session_scope() as s:
        # Two completed chunks + one running chunk, all tagged with the same group; the series
        # total is 12 iterations (5 + 5 + 2).
        for done, total, status in [
            (5, 5, RunStatus.COMPLETE),
            (5, 5, RunStatus.COMPLETE),
            (1, 2, RunStatus.RUNNING),
        ]:
            r = Run(
                status=status,
                label="Big run",
                iterations=total,
                iterations_completed=done,
                job_group=group,
                job_group_total=12,
            )
            s.add(r)
            s.flush()
            rids.append(r.id)

    body = client.get("/api/jobs").json()
    by_id = {j["id"]: j for j in body["jobs"]}

    # The synthesized parent exists, is top-level, and shows aggregate progress (11/12 done).
    parent = by_id.get(group)
    assert parent is not None
    assert parent["parent_id"] is None
    assert parent["current"] == 11 and parent["total"] == 12
    assert parent["cancel_url"] == f"/runs/{rids[2]}/cancel"  # cancels the active chunk → series

    # The active chunk is nested under the parent and cancellable on its own.
    child = by_id.get(f"run-{rids[2]}")
    assert child is not None
    assert child["parent_id"] == group
    assert child["cancel_url"] == f"/runs/{rids[2]}/cancel"

    # Completed chunks aren't listed as separate top-level runs (they're done); the parent
    # counts once toward the running badge, not once per chunk.
    top_level_running = [j for j in body["jobs"] if j["status"] == "running" and not j["parent_id"]]
    assert group in {j["id"] for j in top_level_running}
    assert f"run-{rids[0]}" not in by_id  # a finished chunk isn't surfaced individually

    with session_scope() as s:
        for rid in rids:
            row = s.get(Run, rid)
            if row is not None:
                s.delete(row)


# ── ETAs: every job answers "when is this done?" ───────────────────────────────────────
#
# A progress bar stands in for that question and usually can't answer it — 3/40 says nothing
# about whether to wait or walk away. Three ways to answer, and the display says which was
# used, because they are not equally trustworthy.


def test_a_time_boxed_job_reports_its_deadline_not_an_estimate():
    """A duel window, a challenger race, "test current for 20 minutes" — these don't need
    estimating. The finish time is a fact, and it must outrank any extrapolation."""
    from datetime import datetime, timedelta, timezone

    from pathbrain.api.routes_jobs import _eta_ms

    started = datetime.now(timezone.utc) - timedelta(minutes=5)
    ms, basis = _eta_ms(started_at=started, budget_s=20 * 60)
    assert basis == "scheduled"
    assert 14 * 60_000 < ms <= 15 * 60_000          # ~15 minutes of the window left

    # …and it wins over the other two even when both are available.
    ms2, basis2 = _eta_ms(
        started_at=started, budget_s=20 * 60,
        remaining_units=100, per_unit_ms=60_000, current=1, total=500,
    )
    assert basis2 == "scheduled" and abs(ms2 - ms) < 1_000


def test_a_measured_unit_cost_beats_extrapolating_this_jobs_own_rate():
    """Iterations left x how long an iteration has actually been taking is a better answer
    than this job's rate so far, which on its first unit is barely evidence at all."""
    from datetime import datetime, timedelta, timezone

    from pathbrain.api.routes_jobs import _eta_ms

    started = datetime.now(timezone.utc) - timedelta(seconds=30)
    ms, basis = _eta_ms(
        started_at=started, remaining_units=4, per_unit_ms=15_000, current=1, total=5
    )
    assert basis == "measured" and ms == 60_000


def test_a_job_with_no_deadline_and_no_unit_cost_extrapolates_its_own_rate():
    """The universal fallback — a re-grade counting rows has neither a deadline nor a priced
    unit, but once it has done some it knows how fast it is going."""
    from datetime import datetime, timedelta, timezone

    from pathbrain.api.routes_jobs import _eta_ms

    started = datetime.now(timezone.utc) - timedelta(seconds=10)
    ms, basis = _eta_ms(started_at=started, current=100, total=400)
    assert basis == "observed"
    assert 29_000 < ms < 31_000        # 10s bought 100 of 400 → ~30s for the remaining 300


def test_an_unestimatable_job_says_nothing_rather_than_guessing():
    """A fabricated countdown is worse than none: it's the one number a user plans around."""
    from pathbrain.api.routes_jobs import _eta_ms

    assert _eta_ms() == (None, None)
    # Progress that hasn't completed a single unit can't imply a rate.
    from datetime import datetime, timezone
    assert _eta_ms(started_at=datetime.now(timezone.utc), current=0, total=40) == (None, None)


def test_every_job_entry_carries_the_eta_fields(client):
    """The dropdown renders one countdown for every kind, so the shape has to be uniform —
    present on every entry, null only when genuinely unknown."""
    def work(job):
        job.set_progress(1, 10, "working")
        time.sleep(0.05)
        return {}

    jobs.start("unit-eta", "eta job", work)
    body = client.get("/api/jobs").json()
    assert body["jobs"], "expected at least the job just started"
    for entry in body["jobs"]:
        assert "eta_ms" in entry and "eta_basis" in entry
        assert entry["eta_ms"] is None or entry["eta_ms"] >= 0


def test_a_running_profile_test_reports_real_progress_from_its_chunks():
    """Its progress used to live only in the stage sentence ("part 1/1 (0/5 done)"), so the
    bar was indeterminate and there was no ETA at all. The chunks carry the counts."""
    from pathbrain.api import routes_jobs

    with session_scope() as s:
        run = Run(status=RunStatus.COMPLETE, iterations=5, iterations_completed=3,
                  job_group="profile_test-4321")
        s.add(run)

    routes_jobs.profile_test.current = lambda: {          # type: ignore[assignment]
        "id": 4321, "status": "running", "iterations": 5, "label": "x",
        "stage": "Benchmarking", "started_at": None, "created_at": None,
    }
    try:
        with session_scope() as s:
            entry = routes_jobs._active_profile_test_job(s)[0]
        assert entry["current"] == 3 and entry["total"] == 5
    finally:
        importlib.reload(routes_jobs)


# ── Queued jobs: the clock starts when the job does ────────────────────────────────────
#
# A job can sit in the feed for an hour before it runs a single iteration, waiting for the
# coordination lock. Timing it from the moment it was *created* makes the estimate wrong in
# the one direction that matters — it silently drains while the job is still standing still,
# and a long enough queue counts a never-started job all the way down to "finishing…".


def test_a_queued_job_reports_the_size_of_the_work_not_a_countdown():
    """It hasn't started, so "when does it finish?" has no answer — nobody knows when the
    lock frees. "How long is this once it starts?" does, and that's what it reports."""
    from datetime import datetime, timedelta, timezone

    from pathbrain.api.routes_jobs import _eta_ms

    joined = datetime.now(timezone.utc) - timedelta(minutes=45)

    # A time-boxed job waiting its turn: the full window, undiminished by the wait — the
    # 20 minutes are counted from when it *starts*, which is exactly what queuing defers.
    ms, basis = _eta_ms(started_at=None, budget_s=20 * 60, queued=True)
    assert basis == "queued" and ms == 20 * 60_000
    # …and the wait itself doesn't eat into it, however long it's been.
    assert _eta_ms(started_at=joined, budget_s=20 * 60, queued=True) == (20 * 60_000, "queued")

    # A job whose work is priced by the unit: all of it is still ahead.
    assert _eta_ms(remaining_units=10, per_unit_ms=6_000, queued=True) == (60_000, "queued")

    # Nothing known about the work → say nothing, same as any other unestimatable job.
    assert _eta_ms(current=0, total=40, queued=True) == (None, None)


def test_the_countdown_starts_when_the_job_does():
    """The moment it takes the lock its clock is real, and the estimate becomes a deadline."""
    from datetime import datetime, timedelta, timezone

    from pathbrain.api.routes_jobs import _eta_ms

    started = datetime.now(timezone.utc) - timedelta(minutes=5)
    ms, basis = _eta_ms(started_at=started, budget_s=20 * 60, queued=False)
    assert basis == "scheduled"
    assert 14 * 60_000 < ms <= 15 * 60_000


def test_a_queued_run_is_not_timed_from_when_it_joined_the_queue():
    """End to end through the feed: a PENDING run has waited an hour behind the pipeline and
    still reports its whole cost, flagged so the dropdown shows it standing still."""
    from datetime import datetime, timedelta, timezone

    from pathbrain.api import routes_jobs

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        # One finished run so an iteration has a measured price at all (what the estimate
        # averages over is whatever history holds, so the assertions read it back rather
        # than assuming this one is alone).
        priced = Run(status=RunStatus.COMPLETE, iterations=1, iterations_completed=1,
                     per_iteration_ms=6_000)
        fresh = Run(status=RunStatus.PENDING, iterations=10, iterations_completed=0,
                    created_at=now)
        stale = Run(status=RunStatus.PENDING, iterations=10, iterations_completed=0,
                    created_at=now - timedelta(hours=1))
        s.add_all([priced, fresh, stale])
        s.flush()
        ids = (fresh.id, stale.id, priced.id)

    try:
        with session_scope() as s:
            entries = {e["id"]: e for e in routes_jobs._active_run_jobs(s)}
            est = routes_jobs._per_iteration_estimate(s)
        just_queued, waited_an_hour = entries[f"run-{ids[0]}"], entries[f"run-{ids[1]}"]

        for entry in (just_queued, waited_an_hour):
            assert entry["queued"] is True
            assert entry["eta_basis"] == "queued"
            # All ten iterations are still ahead of it, priced at what an iteration costs.
            assert entry["eta_ms"] == round(10 * est)
        # The whole point: an hour in the queue took nothing off the estimate.
        assert waited_an_hour["eta_ms"] == just_queued["eta_ms"]
    finally:
        with session_scope() as s:
            for rid in ids:
                row = s.get(Run, rid)
                if row is not None:
                    s.delete(row)


def test_every_job_entry_says_whether_it_has_started(client):
    """`queued` is part of the uniform shape, not something an adapter may forget: the
    client reads it to decide whether the number ticks, and a missing key reads as False."""
    def work(job):
        job.set_progress(1, 10, "working")
        return {}

    jobs.start("unit-queued-shape", "queued shape", work)
    body = client.get("/api/jobs").json()
    assert body["jobs"]
    for entry in body["jobs"]:
        assert "queued" in entry
        # An in-process job never queues — it gets its own thread immediately.
        if entry["queued"]:
            assert entry["eta_basis"] == "queued"
