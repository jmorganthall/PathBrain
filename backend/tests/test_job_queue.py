"""The universal "add a job" queue.

Pressing a Run button while the pipeline is busy used to do one of three things depending
only on which button it was: refuse with a 409, queue silently while the toast claimed the
session had started, or start. Three behaviours for one intent is not a policy. These tests
pin the policy: submitting always succeeds, and the caller is always told which of the two
things happened.
"""
from __future__ import annotations

import time

from pathbrain import coordinator, job_queue


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def setup_function() -> None:
    job_queue._reset_for_tests()
    # These fakes never take the coordinator lock, so the dispatcher's post-start handoff
    # grace would be spent in full between every job. Real engines take it in microseconds.
    job_queue.START_GRACE_S = 0.05


def teardown_function() -> None:
    job_queue._reset_for_tests()
    job_queue.START_GRACE_S = 5.0


def test_a_free_pipeline_starts_the_job_synchronously():
    """The caller still gets the engine's real session id back, so every existing response
    shape survives — queueing is added, nothing is taken away."""
    out = job_queue.submit("sweep", "a sweep", lambda: 42)
    assert out.started is True and out.result == 42
    assert out.placement() == {
        "queued": False, "ticket_id": None, "queue_position": None,
        "queue_ahead": 0, "blocked_by": None,
    }


def test_a_busy_pipeline_queues_and_names_what_is_in_the_way():
    """The reported problem: a busy pipeline must not be a dead end, and must not pretend
    the job started either."""
    started: list[str] = []
    with coordinator.hold("duel#5"):
        out = job_queue.submit("sweep", "a sweep", lambda: started.append("sweep"))
        assert out.started is False
        placement = out.placement()
        assert placement["queued"] is True
        assert placement["queue_position"] == 1
        assert placement["blocked_by"] == "a duel session"
        assert placement["ticket_id"] is not None
        # It is listed as waiting, and nothing has run.
        assert [p["kind"] for p in job_queue.pending()] == ["sweep"]
        assert started == []

    # Released: the dispatcher starts it.
    assert _wait(lambda: started == ["sweep"])
    assert job_queue.pending() == []


def test_jobs_start_in_the_order_they_were_submitted():
    order: list[str] = []
    with coordinator.hold("duel#5"):
        for name in ("first", "second", "third"):
            job_queue.submit("sweep", name, lambda n=name: order.append(n))
        assert [p["queue_position"] for p in job_queue.pending()] == [1, 2, 3]
    assert _wait(lambda: len(order) == 3)
    assert order == ["first", "second", "third"]


def test_a_queued_job_can_be_dropped_before_it_starts():
    """Free by construction: a queued job has applied nothing, so it just leaves the line."""
    ran: list[str] = []
    with coordinator.hold("duel#5"):
        keep = job_queue.submit("sweep", "keep", lambda: ran.append("keep"))
        drop = job_queue.submit("sweep", "drop", lambda: ran.append("drop"))
        assert job_queue.cancel(drop.ticket.id) is True
        # Cancelling something that already left is not an error, just False.
        assert job_queue.cancel(drop.ticket.id) is False
        assert [p["ticket_id"] for p in job_queue.pending()] == [keep.ticket.id]
    assert _wait(lambda: ran == ["keep"])
    assert "drop" not in ran


def test_the_gate_is_per_kind_so_one_stuck_engine_cannot_stall_everything():
    """Cross-kind exclusion is the coordinator's job — it has a lease and an eviction path.
    A module-level ``active()`` flag has neither, so gating the queue on "is ANY engine
    active" would let one engine that died with its flag set stall every job forever, with
    nothing able to clear it. That would be strictly worse than the refusals this replaces.
    """
    job_queue.register("duel", lambda: True)      # a duel that never clears its flag
    job_queue.register("sweep", lambda: False)

    # A sweep is unaffected by the wedged duel and starts immediately.
    out = job_queue.submit("sweep", "a sweep", lambda: "ran")
    assert out.started is True and out.result == "ran"

    # Another duel, though, still waits for the one that claims to be running.
    queued = job_queue.submit("duel", "another duel", lambda: "nope")
    assert queued.started is False


def test_a_job_that_fails_when_its_turn_comes_records_why():
    """Validation moves with the job: "nothing to race" can only be discovered at start
    time, and by then the HTTP caller is long gone. The error has to land somewhere."""
    def explode():
        raise RuntimeError("Nothing to race")

    with coordinator.hold("duel#5"):
        out = job_queue.submit("race", "a race", explode)
        assert out.started is False

    assert _wait(lambda: bool(job_queue.recent_failures()))
    failure = job_queue.recent_failures()[0]
    assert failure["kind"] == "race"
    assert "Nothing to race" in failure["error"]
    assert job_queue.pending() == []


def test_an_immediate_start_still_raises_so_callers_keep_their_error_handling():
    """When it runs inline, an engine's own validation error must reach the HTTP caller
    exactly as it always did — that is what keeps the 400s meaningful."""
    def explode():
        raise ValueError("bad spec")

    try:
        job_queue.submit("sweep", "a sweep", explode)
    except ValueError as exc:
        assert "bad spec" in str(exc)
    else:
        raise AssertionError("the engine's error should propagate on an immediate start")


def test_status_reports_the_holder_and_the_whole_line():
    with coordinator.hold("refresh#2"):
        job_queue.submit("sweep", "a sweep", lambda: None)
        status = job_queue.status()
        assert status["busy"] is True
        assert status["blocked_by"] == "a profile re-run"
        assert status["queue_depth"] == 1
        assert status["pending"][0]["label"] == "a sweep"
        job_queue.cancel(status["pending"][0]["ticket_id"])
