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
    eta = _eta_ms(started_at=started, budget_s=20 * 60)
    assert eta.basis == "scheduled"
    # A window burns clock, not units — there is no step for a bar to advance through.
    assert eta.unit_ms is None
    assert 14 * 60_000 < eta.ms <= 15 * 60_000      # ~15 minutes of the window left
    # …but the window's own length IS reported, because it is the bar's denominator. This
    # is the whole of what a time-boxed job could not previously say: with no unit total
    # the client had only the indeterminate sweep, identical at minute one of a six-hour
    # duel and at minute three hundred.
    assert eta.window_ms == 20 * 60_000

    # …and it wins over the other two even when both are available.
    eta2 = _eta_ms(
        started_at=started, budget_s=20 * 60,
        remaining_units=100, per_unit_ms=60_000, current=1, total=500,
    )
    assert eta2.basis == "scheduled" and abs(eta2.ms - eta.ms) < 1_000


def test_a_measured_unit_cost_beats_extrapolating_this_jobs_own_rate():
    """Iterations left x how long an iteration has actually been taking is a better answer
    than this job's rate so far, which on its first unit is barely evidence at all."""
    from datetime import datetime, timedelta, timezone

    from pathbrain.api.routes_jobs import _eta_ms

    started = datetime.now(timezone.utc) - timedelta(seconds=30)
    eta = _eta_ms(
        started_at=started, remaining_units=4, per_unit_ms=15_000, current=1, total=5
    )
    assert eta.basis == "measured" and eta.ms == 60_000
    # The same number the bar steps through: one iteration is 15s of the 60s left.
    assert eta.unit_ms == 15_000
    # A job made of units has no window; its bar counts them, not the clock.
    assert eta.window_ms is None


def test_a_job_with_no_deadline_and_no_unit_cost_extrapolates_its_own_rate():
    """The universal fallback — a re-grade counting rows has neither a deadline nor a priced
    unit, but once it has done some it knows how fast it is going."""
    from datetime import datetime, timedelta, timezone

    from pathbrain.api.routes_jobs import _eta_ms

    started = datetime.now(timezone.utc) - timedelta(seconds=10)
    eta = _eta_ms(started_at=started, current=100, total=400)
    assert eta.basis == "observed"
    # 10s / 100 rows = 100ms a row, which is also how fast the bar may creep.
    assert 95 < eta.unit_ms < 105
    assert 29_000 < eta.ms < 31_000    # 10s bought 100 of 400 → ~30s for the remaining 300
    assert eta.window_ms is None


def test_an_unestimatable_job_says_nothing_rather_than_guessing():
    """A fabricated countdown is worse than none: it's the one number a user plans around."""
    from pathbrain.api.routes_jobs import _eta_ms

    assert _eta_ms() == (None, None, None, None)
    # Progress that hasn't completed a single unit can't imply a rate.
    from datetime import datetime, timezone
    assert _eta_ms(started_at=datetime.now(timezone.utc), current=0, total=40) == (
        None, None, None, None
    )


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
        # The bar reads `unit_ms` on every row for the same reason the countdown reads
        # `eta_ms` on every row: a missing key and a null one look identical until
        # something tries to divide by it.
        assert "unit_ms" in entry
        assert entry["unit_ms"] is None or entry["unit_ms"] > 0
        # A finished job has neither — there is nothing left to count down or step through.
        if entry["status"] != "running":
            assert entry["eta_ms"] is None and entry["unit_ms"] is None


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
    ms, basis = _eta_ms(started_at=None, budget_s=20 * 60, queued=True)[:2]
    assert basis == "queued" and ms == 20 * 60_000
    # …and the wait itself doesn't eat into it, however long it's been; the queued reading
    # carries no window either: a bar drawn from one would start
    # creeping across a window whose clock has not begun, which is the same lie the
    # countdown refuses to tell.
    assert _eta_ms(started_at=joined, budget_s=20 * 60, queued=True) == (
        20 * 60_000, "queued", None, None
    )

    # A job whose work is priced by the unit: all of it is still ahead.
    # No unit cost while queued: nothing is running, so the bar must not creep either.
    assert _eta_ms(remaining_units=10, per_unit_ms=6_000, queued=True) == (
        60_000, "queued", None, None
    )

    # Nothing known about the work → say nothing, same as any other unestimatable job.
    assert _eta_ms(current=0, total=40, queued=True) == (None, None, None, None)


def test_the_countdown_starts_when_the_job_does():
    """The moment it takes the lock its clock is real, and the estimate becomes a deadline."""
    from datetime import datetime, timedelta, timezone

    from pathbrain.api.routes_jobs import _eta_ms

    started = datetime.now(timezone.utc) - timedelta(minutes=5)
    ms, basis = _eta_ms(started_at=started, budget_s=20 * 60, queued=False)[:2]
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


def test_a_running_run_prices_one_iteration_so_the_bar_can_cross_it():
    """The bar's whole complaint in one entry: `current`/`total` moves once an iteration, so
    between ticks it stands still and then jumps — on a benchmark run, tens of seconds of a
    bar that reads as hung. `unit_ms` is what the client crosses that gap with, so a running
    run has to carry the price of the iteration it is *in*, not just the count of the ones
    behind it. A queued run carries none: nothing is running, so nothing may creep."""
    from datetime import datetime, timezone

    from pathbrain.api import routes_jobs

    with session_scope() as s:
        priced = Run(status=RunStatus.COMPLETE, iterations=1, iterations_completed=1,
                     per_iteration_ms=6_000)
        live = Run(status=RunStatus.RUNNING, iterations=10, iterations_completed=3,
                   started_at=datetime.now(timezone.utc))
        waiting = Run(status=RunStatus.PENDING, iterations=10, iterations_completed=0)
        s.add_all([priced, live, waiting])
        s.flush()
        ids = (live.id, waiting.id, priced.id)

    try:
        with session_scope() as s:
            entries = {e["id"]: e for e in routes_jobs._active_run_jobs(s)}
            est = routes_jobs._per_iteration_estimate(s)
        running, queued = entries[f"run-{ids[0]}"], entries[f"run-{ids[1]}"]

        assert running["eta_basis"] == "measured"
        # One iteration, priced by what an iteration has actually been costing — the same
        # number the countdown multiplies out, never a second estimate of its own.
        assert running["unit_ms"] == round(est)
        assert running["eta_ms"] == round(7 * est)
        # Three of ten done: the bar starts the unit at 30% and may cross to 40%, no further.
        assert (running["current"], running["total"]) == (3, 10)

        assert queued["queued"] is True and queued["unit_ms"] is None
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


# ── Call signs: a job says WHICH profile, not which settings ───────────────────────────
#
# Reported from a phone: the challenger race read "leader Download: 880Mbit q3550 t3 i60 ecn
# | Upload: 880Mbit q500 t3 i60 ecn" — three wrapped lines that never say which profile is
# leading. Every other view leads with the call sign; the jobs feed was the last place still
# printing raw settings at people.


def test_a_job_about_a_profile_is_named_by_its_call_sign():
    from pathbrain import profile_names
    from pathbrain.api import routes_jobs

    with session_scope() as s:
        name = profile_names.names_for(s, ["feedfacecafe"])["feedfacecafe"]

    routes_jobs.challenger.active = lambda: True                     # type: ignore[assignment]
    routes_jobs.challenger.current = lambda: {                       # type: ignore[assignment]
        "id": 9, "status": "running", "iterations_run": 1, "eliminated": [],
        "leader_fingerprint": "feedfacecafe",
        "leader_label": "Download: 880Mbit q3550 t3 i60 ecn | Upload: 880Mbit q500 t3 i60 ecn",
        "started_at": None, "created_at": None, "time_budget_s": 7200,
    }
    try:
        with session_scope() as s:
            entry = routes_jobs._active_challenger_job(s)[0]
        assert f"leader {name}" in entry["message"]
        assert "q3550" not in entry["message"]        # the settings string is gone from the line
        assert "q3550" in (entry["detail"] or "")     # …and kept as the hover detail
    finally:
        importlib.reload(routes_jobs)


def test_a_profile_with_no_call_sign_falls_back_to_its_label():
    """Naming is best-effort and must never blank a job line: a fingerprint the feed can't
    resolve (or an engine that never recorded one) still reads as something."""
    from pathbrain.api import routes_jobs

    routes_jobs.challenger.active = lambda: True                     # type: ignore[assignment]
    routes_jobs.challenger.current = lambda: {                       # type: ignore[assignment]
        "id": 9, "status": "running", "iterations_run": 1, "eliminated": [],
        "leader_fingerprint": None, "leader_label": "q1514 t5ms",
        "started_at": None, "created_at": None, "time_budget_s": 7200,
    }
    try:
        with session_scope() as s:
            entry = routes_jobs._active_challenger_job(s)[0]
        assert "leader q1514 t5ms" in entry["message"]
    finally:
        importlib.reload(routes_jobs)


# ── "it says it's finishing, and nothing is happening" ─────────────────────────────────


def test_a_silent_holder_is_reported_as_stalled_rather_than_finishing():
    """A time-boxed job past its deadline floors at "finishing…" — which is what a duel
    wedged in an unanswered browser call read as, all night, while nothing happened. When
    the job holding the pipeline has shown no progress, the feed says so instead."""
    from pathbrain import coordinator
    from pathbrain.api import routes_jobs

    assert routes_jobs._stalled_ms("duel#7") is None, "nobody holds it"
    with coordinator.hold("duel#7") as lease:
        assert routes_jobs._stalled_ms("duel#7") is None, "a live holder is not stalled"
        assert routes_jobs._stalled_ms("run#3") is None, "…and it is only about the holder"
        lease.last_beat -= routes_jobs.STALL_REPORT_S + 60
        stalled = routes_jobs._stalled_ms("duel#7")
        assert stalled is not None and stalled >= routes_jobs.STALL_REPORT_S * 1000


def test_the_feed_reports_the_state_of_the_pipeline_itself(client):
    """Eight rows saying "waiting to start" describe the queue; only this describes what
    they are queued on."""
    body = client.get("/api/jobs").json()
    assert "pipeline" in body
    assert set(body["pipeline"]) >= {"busy", "owner", "stalled_for_s", "waiting"}
    assert body["pipeline"]["busy"] is False


# ── Time-boxed jobs: the bar is the window ─────────────────────────────────────────────
#
# The duel ladder runs longer than anything else PathBrain does — a nightly window is a
# night — and it was the one job whose bar could say nothing at all. It has no unit total
# (how many rounds a window buys depends on how fast each match settles), so `total` was
# None and the client fell back to the indeterminate sweep: an animation that reads exactly
# the same at minute one and at minute three hundred.


def test_a_time_boxed_job_carries_the_window_its_bar_is_drawn_against():
    """`eta_ms` alone is a countdown and nothing else. The bar needs the denominator too —
    what the remainder is a remainder *of* — and it has to be the same window the countdown
    is anchored on, or the two readings start disagreeing about one deadline."""
    from datetime import datetime, timedelta, timezone

    from pathbrain.api.routes_jobs import Eta, _eta_ms, _with_eta

    started = datetime.now(timezone.utc) - timedelta(minutes=90)
    entry = _with_eta({}, _eta_ms(started_at=started, budget_s=6 * 3600))
    assert entry["eta_basis"] == "scheduled"
    assert entry["window_ms"] == 6 * 3600 * 1000
    # 90 minutes of a six-hour window: the client draws a quarter of a bar, not a sweep.
    spent = entry["window_ms"] - entry["eta_ms"]
    assert 0.24 < spent / entry["window_ms"] < 0.26
    # A window is not a unit — there is no step to interpolate across.
    assert entry["unit_ms"] is None

    # And the shape stays uniform where there is no window at all.
    assert _with_eta({}, Eta(None, None))["window_ms"] is None


def test_every_job_entry_carries_the_window_field(client):
    """Same rule as `eta_ms` and `unit_ms`: present on every row, null where it doesn't
    apply. A missing key and a null one look identical until something divides by it."""
    def work(job):
        job.set_progress(1, 10, "working")
        time.sleep(0.05)

    jobs.start("unit-window", "window job", work)
    body = client.get("/api/jobs").json()
    assert body["jobs"]
    for entry in body["jobs"]:
        assert "window_ms" in entry
        assert entry["window_ms"] is None or entry["window_ms"] > 0


def test_the_duel_row_says_how_much_of_the_session_is_done(monkeypatch):
    """The two facts a duel row was missing, and the two the engine already records after
    every round: how many matches have been decided, and how many iterations measured. The
    stage sentence names the bout in the ring — which is right, and says nothing about the
    session, so five hours in looked identical to five minutes in."""
    from datetime import datetime, timedelta, timezone

    from pathbrain.api import routes_jobs

    started = datetime.now(timezone.utc) - timedelta(hours=2)
    session = {
        "id": 7,
        "status": "running",
        "stage": "Match 4 · Speedy Sloth (belt) defends vs Tall Garland (contender) — round 3 (1-1)",
        "duration_s": 6 * 3600,
        "matchups": [{}, {}, {}],
        "iterations_run": 96,
        "started_at": started.isoformat(),
        "created_at": started.isoformat(),
    }
    monkeypatch.setattr(routes_jobs.duel, "active", lambda: True)
    monkeypatch.setattr(routes_jobs.duel, "current", lambda: session)

    (entry,) = routes_jobs._active_duel_job()
    assert "Match 4" in entry["message"], "the bout in the ring still leads"
    assert "3 matches decided" in entry["message"]
    assert "96 iteration(s)" in entry["message"]
    # …and the bar has a real denominator, so it is determinate for the first time.
    assert entry["eta_basis"] == "scheduled"
    assert entry["window_ms"] == 6 * 3600 * 1000
    assert 3.9 * 3600_000 < entry["eta_ms"] < 4.1 * 3600_000


def test_a_duel_that_has_not_started_a_match_still_reports_a_session():
    """Nothing decided, nothing measured, no stage yet — the row still says so rather than
    rendering an empty message beside a sweeping bar."""
    from pathbrain.api.routes_jobs import _duel_message

    assert _duel_message({}) == "0 matches decided · 0 iteration(s)"
    assert _duel_message({"matchups": [{}], "iterations_run": 6}) == (
        "1 match decided · 6 iteration(s)"
    )
