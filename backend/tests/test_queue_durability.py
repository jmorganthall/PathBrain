"""The queue survives a restart — all three layers, one window.

Reported as "cancelled a duel job and all my queued jobs were cancelled too, even the
non-duel ones": a container recreate followed the cancel, and every queueing layer dropped
its work at once (in-memory tickets gone, pending profile tests marked cancelled, pending
manual runs marked failed). These pin the replacement: a ticket is written to disk with a
spec its starter rebuilds the call from, pending rows inside ``RESUME_WINDOW_HOURS`` come
back, older ones are closed with the reason, and nothing resumes before the reconciles.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from pathbrain import coordinator, job_queue, profile_test as pt_mod, runner
from pathbrain.api import routes_run
from pathbrain.database import session_scope
from pathbrain.models import ProfileTest, ProfileTestStatus, QueuedJob, Run, RunStatus

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def setup_function() -> None:
    job_queue._reset_for_tests()
    job_queue.START_GRACE_S = 0.05
    with session_scope() as s:
        s.execute(delete(QueuedJob))


def teardown_function() -> None:
    job_queue._reset_for_tests()
    job_queue.START_GRACE_S = 5.0


def _rows() -> list[QueuedJob]:
    with session_scope() as s:
        rows = s.scalars(select(QueuedJob).order_by(QueuedJob.id)).all()
        s.expunge_all()
        return rows


def test_a_queued_ticket_with_a_spec_is_written_to_disk_and_settled_when_it_starts():
    calls: list[dict] = []
    job_queue.register("fake", lambda: False)
    job_queue.register_starter("fake", lambda spec: calls.append(spec) or 7)
    with coordinator.hold("duel#1"):
        sub = job_queue.submit("fake", "a fake job", spec={"minutes": 3})
        assert sub.started is False and sub.ticket.row_id is not None
        (row,) = _rows()
        assert row.state == "pending" and row.kind == "fake" and row.spec == {"minutes": 3} and row.resumed is False
    # Pipeline freed: the dispatcher starts it through the starter and settles the row.
    assert _wait(lambda: calls == [{"minutes": 3}])
    assert _wait(lambda: _rows()[0].state == "started")
    assert _rows()[0].started_at is not None


def test_a_cancelled_ticket_settles_its_row_so_a_restart_never_resurrects_it():
    job_queue.register("fake", lambda: False)
    job_queue.register_starter("fake", lambda spec: 1)
    with coordinator.hold("duel#1"):
        sub = job_queue.submit("fake", "doomed", spec={})
        assert job_queue.cancel(sub.ticket.id) is True
        assert _rows()[0].state == "cancelled"
    out = job_queue.restore(now=NOW)
    assert out["tickets"] == 0 and out["expired"] == 0 and job_queue.pending() == []


def test_restore_requeues_young_pending_rows_and_expires_old_ones():
    started: list[dict] = []
    job_queue.register("fake", lambda: False)
    job_queue.register_starter("fake", lambda spec: started.append(spec) or 1)
    with session_scope() as s:
        s.add(QueuedJob(kind="fake", label="last night's bet", spec={"n": 1}, submitted_at=(NOW - timedelta(hours=6)).replace(tzinfo=None)))
        s.add(QueuedJob(kind="fake", label="last week's bet", spec={"n": 2}, submitted_at=(NOW - timedelta(days=3)).replace(tzinfo=None)))
        s.add(QueuedJob(kind="unknown-kind", label="nobody can start this", spec={}, submitted_at=NOW.replace(tzinfo=None)))
    with coordinator.hold("startup-reconcile"):  # nothing may start until the restore returns
        out = job_queue.restore(now=NOW)
        assert out == {"at": NOW.isoformat(), "tickets": 1, "expired": 2}
        pending = job_queue.pending()
        assert [p["label"] for p in pending] == ["last night's bet"] and pending[0]["resumed"] is True
        rows = {r.label: r for r in _rows()}
        assert rows["last night's bet"].state == "pending" and rows["last night's bet"].resumed is True
        assert rows["last week's bet"].state == "expired" and "resume window" in rows["last week's bet"].error
        assert rows["nobody can start this"].state == "expired" and "unknown-kind" in rows["nobody can start this"].error
        assert job_queue.status()["restored"]["tickets"] == 1
    # Once the pipeline is free the restored ticket starts through its starter, with its spec.
    assert _wait(lambda: started == [{"n": 1}])
    assert _wait(lambda: {r.label: r.state for r in _rows()}["last night's bet"] == "started")


def test_restored_tickets_run_ahead_of_anything_submitted_since():
    order: list[str] = []
    job_queue.register("fake", lambda: False)
    job_queue.register_starter("fake", lambda spec: order.append(spec["who"]) or 1)
    with session_scope() as s:
        s.add(QueuedJob(kind="fake", label="from before", spec={"who": "before"}, submitted_at=NOW.replace(tzinfo=None)))
    with coordinator.hold("busy"):
        job_queue.submit("fake", "from after", spec={"who": "after"})
        job_queue.restore(now=NOW)
        assert [p["label"] for p in job_queue.pending()] == ["from before", "from after"]
    assert _wait(lambda: order == ["before", "after"])


def test_the_route_persists_the_real_spec_the_starter_needs(client, monkeypatch):
    job_queue.register_engines()
    with coordinator.hold("duel#9"):
        body = client.post("/api/duel/start", json={"duration_minutes": 45}).json()
        assert body["queued"] is True and body["ticket_id"]
        (row,) = _rows()
        assert row.kind == "duel" and row.spec == {"duration_minutes": 45, "trigger": "manual"}
        job_queue.cancel(body["ticket_id"])  # never let a real duel start in the suite


def test_profile_test_reconcile_keeps_young_queued_rows_and_closes_old_ones():
    with session_scope() as s:
        young = ProfileTest(status=ProfileTestStatus.PENDING, fingerprint="fp-young", target_label="wan", iterations=5,
                            target=[{"label": "wan"}], created_at=(NOW - timedelta(hours=2)).replace(tzinfo=None))
        old = ProfileTest(status=ProfileTestStatus.PENDING, fingerprint="fp-old", target_label="wan", iterations=5,
                          target=[{"label": "wan"}], created_at=(NOW - timedelta(days=2)).replace(tzinfo=None))
        s.add_all([young, old])
        s.flush()
        young_id, old_id = young.id, old.id
    try:
        assert pt_mod.reconcile_interrupted_profile_tests(now=NOW) >= 2
        with session_scope() as s:
            y, o = s.get(ProfileTest, young_id), s.get(ProfileTest, old_id)
            assert y.status == ProfileTestStatus.PENDING and "resumed after a restart" in y.stage
            assert o.status == ProfileTestStatus.CANCELLED and "resume window" in o.stage and o.finished_at is not None
    finally:
        # Leave no pending row behind for another test's worker to pick up.
        with session_scope() as s:
            for rid in (young_id, old_id):
                row = s.get(ProfileTest, rid)
                row.status = ProfileTestStatus.CANCELLED


def test_resume_queued_starts_the_worker_only_when_something_is_waiting(monkeypatch):
    kicks: list[int] = []
    monkeypatch.setattr(pt_mod, "_ensure_worker", lambda: kicks.append(1))
    with session_scope() as s:
        s.execute(delete(ProfileTest).where(ProfileTest.status == ProfileTestStatus.PENDING))
    assert pt_mod.resume_queued() == 0 and kicks == []
    with session_scope() as s:
        row = ProfileTest(status=ProfileTestStatus.PENDING, fingerprint="fp-w", target_label="wan", iterations=5, target=[{"label": "wan"}])
        s.add(row)
        s.flush()
        rid = row.id
    try:
        assert pt_mod.resume_queued() == 1 and kicks == [1]
    finally:
        with session_scope() as s:
            s.get(ProfileTest, rid).status = ProfileTestStatus.CANCELLED


def test_run_reconcile_keeps_young_pending_runs_and_resume_redispatches_them(monkeypatch):
    with session_scope() as s:
        s.execute(delete(Run).where(Run.status.in_([RunStatus.PENDING, RunStatus.RUNNING])))
        running = Run(status=RunStatus.RUNNING, created_at=(NOW - timedelta(minutes=5)).replace(tzinfo=None))
        old = Run(status=RunStatus.PENDING, created_at=(NOW - timedelta(days=2)).replace(tzinfo=None))
        single = Run(status=RunStatus.PENDING, created_at=(NOW - timedelta(minutes=30)).replace(tzinfo=None), iterations=2)
        s.add_all([running, old, single])
        s.flush()
        series = Run(status=RunStatus.PENDING, created_at=(NOW - timedelta(minutes=20)).replace(tzinfo=None),
                     iterations=5, label="big", notes="Manual run · part 1/4")
        s.add(series)
        s.flush()
        series.job_group = f"run-series-{series.id}"
        series.job_group_total = 20
        ids = {"running": running.id, "old": old.id, "single": single.id, "series": series.id}

    assert runner.reconcile_interrupted_runs(now=NOW) == 2
    with session_scope() as s:
        assert s.get(Run, ids["running"]).status == RunStatus.FAILED
        o = s.get(Run, ids["old"])
        assert o.status == RunStatus.FAILED and "resume window" in o.error
        assert s.get(Run, ids["single"]).status == RunStatus.PENDING
        assert s.get(Run, ids["series"]).status == RunStatus.PENDING

    calls: list[tuple] = []
    done = threading.Event()

    def _single(rid):
        calls.append(("single", rid))
        if len(calls) == 2:
            done.set()

    def _series(first, total, label, notes, group):
        calls.append(("series", first, total, label, notes, group))
        if len(calls) == 2:
            done.set()

    monkeypatch.setattr(routes_run, "_locked_execute", _single)
    monkeypatch.setattr(routes_run, "_locked_execute_series", _series)
    try:
        assert routes_run.resume_pending_runs() == 2
        assert done.wait(5.0)
        assert ("single", ids["single"]) in calls
        # The series resumes as a series: its total and group, with the part suffix stripped
        # from the notes so the driver's own "· part N/M" does not stack.
        assert ("series", ids["series"], 20, "big", "Manual run", f"run-series-{ids['series']}") in calls
    finally:
        with session_scope() as s:
            for rid in (ids["single"], ids["series"]):
                s.get(Run, rid).status = RunStatus.FAILED
