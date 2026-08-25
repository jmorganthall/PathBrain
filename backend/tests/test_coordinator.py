"""Tests for the firewall/benchmark coordination lock."""
from __future__ import annotations

import threading
import time

import pytest

from pathbrain import coordinator
from pathbrain.coordinator import CoordinatorBusy


def test_hold_sets_and_clears_owner():
    assert not coordinator.busy()
    with coordinator.hold("alpha"):
        assert coordinator.busy()
        assert coordinator.owner() == "alpha"
    assert not coordinator.busy()
    assert coordinator.owner() is None


def test_try_hold_raises_while_held():
    with coordinator.hold("alpha"):
        with pytest.raises(CoordinatorBusy) as exc:
            with coordinator.try_hold("beta"):
                pass
        # The error reports who holds it, so the UI can explain the wait.
        assert exc.value.owner == "alpha"
    # Free again once released.
    with coordinator.try_hold("beta"):
        assert coordinator.owner() == "beta"


def test_hold_blocks_then_proceeds_when_released():
    order: list[str] = []
    started = threading.Event()

    def holder():
        with coordinator.hold("holder"):
            started.set()
            time.sleep(0.2)
            order.append("holder-release")

    t = threading.Thread(target=holder)
    t.start()
    assert started.wait(2.0)
    # This blocks until the holder releases, then runs.
    with coordinator.hold("waiter", timeout=5.0):
        order.append("waiter-acquire")
    t.join(2.0)
    assert order == ["holder-release", "waiter-acquire"]


def test_hold_timeout_raises_when_not_released():
    with coordinator.hold("holder"):
        with pytest.raises(CoordinatorBusy):
            with coordinator.hold("waiter", timeout=0.1):
                pass


# ── the zipper merge ───────────────────────────────────────────────────────────────────
#
# The duel ladder holds the lock for its whole window — hours — so without a way to step
# aside, anything a user starts meanwhile queues behind the entire night.


def test_a_holder_can_see_that_someone_is_queued():
    """The merge lane has to be visible before anyone can be let into it."""
    import threading

    assert coordinator.waiting() == 0
    with coordinator.hold("long-session"):
        assert coordinator.waiting() == 0
        got = threading.Event()

        def queued():
            with coordinator.hold("queued-session"):
                got.set()

        t = threading.Thread(target=queued, daemon=True)
        t.start()
        # Give the thread a moment to block on acquire.
        for _ in range(200):
            if coordinator.waiting() > 0:
                break
            time.sleep(0.005)
        assert coordinator.waiting() == 1
        assert not got.is_set(), "it must be waiting, not running"
    t.join(timeout=5)
    assert got.is_set()
    assert coordinator.waiting() == 0


def test_yielding_actually_hands_the_lock_over_rather_than_taking_it_straight_back():
    """The observable contract: the queued session runs BEFORE the yielder resumes.

    On CPython a naive release-then-reacquire usually satisfies this already — releasing a
    lock wakes a blocked waiter and it typically wins the race. But "usually" is the
    problem: ``threading.Lock`` is documented as unfair, nothing promises the woken thread
    beats the releasing thread's fresh ``acquire``, and the odds get worse with several
    waiters or another implementation. So the yield waits for the lock to actually be taken
    before queueing for it again, which makes the handoff a guarantee rather than a
    tendency. This test pins the behaviour either way; the loop is what stops it being
    luck.
    """
    import threading

    order: list[str] = []
    done = threading.Event()

    with coordinator.hold("ladder"):
        def queued():
            with coordinator.hold("test-now"):
                order.append("queued-ran")
                time.sleep(0.05)
            done.set()

        t = threading.Thread(target=queued, daemon=True)
        t.start()
        for _ in range(200):          # wait until it is genuinely blocked
            if coordinator.waiting() > 0:
                break
            time.sleep(0.005)

        yielded = coordinator.yield_if_waiting("ladder")
        # We are back in control, and the queued session went first and finished.
        order.append("ladder-resumed")
        assert yielded > 0
        assert coordinator.busy(), "the yielder holds the lock again on return"

    t.join(timeout=5)
    assert done.is_set()
    assert order == ["queued-ran", "ladder-resumed"], order


def test_yielding_with_an_empty_lane_is_a_no_op():
    """Called between every pair, so the common case has to cost nothing and never let go
    of the lock — dropping it when nobody wants it would invite a race for no reason."""
    with coordinator.hold("ladder"):
        assert coordinator.yield_if_waiting("ladder") == 0.0
        assert coordinator.busy() and coordinator.owner() == "ladder"


# ── the lease ──────────────────────────────────────────────────────────────────────────
#
# Holding the lock for hours is normal — a duel window is a night — so age proves nothing
# and only silence distinguishes a session from a wedge. The case these pin: a duel that
# went into an unanswered browser call at 23:00 still owned the pipeline at 07:30, so
# everything started overnight queued behind a thread that was never coming back.


def test_a_long_holder_that_keeps_beating_is_left_alone():
    """The first thing eviction must not do is interrupt work that is going fine."""
    with coordinator.hold("duel#1") as lease:
        lease.acquired_at -= 10_000  # held for hours…
        coordinator.beat()           # …but alive right now
        assert coordinator.evict_if_stalled(threshold_s=60) is False
        assert lease.alive
        assert coordinator.held_for() > 9_000
        assert coordinator.stalled_for() < 5


def test_a_holder_that_stops_beating_is_evicted_and_the_pipeline_is_free_again():
    with coordinator.hold("duel#1") as lease:
        lease.last_beat -= 10_000
        assert coordinator.evict_if_stalled(threshold_s=60) is True
        assert not lease.alive
        # The lock is genuinely free — someone else can run.
        with coordinator.try_hold("monitoring"):
            assert coordinator.owner() == "monitoring"
    assert not coordinator.busy()


def test_an_evicted_session_stops_at_its_next_seam():
    """It cannot be killed, so it is disowned: it finds out at the seam it was going to
    write the firewall from, and unwinds instead of applying settings over the top of
    whoever holds the pipeline now."""
    with coordinator.hold("duel#1") as lease:
        lease.check()  # fine while it holds
        lease.last_beat -= 10_000
        coordinator.evict_if_stalled(threshold_s=60)
        with pytest.raises(coordinator.LeaseRevoked):
            lease.check()


def test_an_evicted_session_does_not_release_a_lock_someone_else_now_holds():
    """The dangerous version of coming back from the dead: a stale release would hand a
    live session's lock away and let two firewall writers run at once."""
    evicted_done = threading.Event()
    proceed = threading.Event()

    gone_quiet = threading.Event()

    def wedged():
        with coordinator.hold("duel#1") as lease:
            lease.last_beat -= 10_000
            gone_quiet.set()
            proceed.wait(5)  # stands in for the unanswered call
        evicted_done.set()

    t = threading.Thread(target=wedged, daemon=True)
    t.start()
    assert gone_quiet.wait(5)
    assert coordinator.evict_if_stalled(threshold_s=60) is True

    with coordinator.hold("monitoring", timeout=5) as live:
        proceed.set()                      # the wedged thread finally returns…
        assert evicted_done.wait(5)
        assert coordinator.busy()          # …and did not release our lock
        assert coordinator.owner() == "monitoring"
        assert live.alive
    t.join(timeout=5)


def test_a_waiter_is_freed_by_the_eviction_rather_than_queueing_forever():
    """The user-visible harm was never the stalled duel — it was everything else waiting
    on it. A queued session must get the pipeline once the holder is declared dead."""
    ran = threading.Event()
    proceed = threading.Event()

    gone_quiet = threading.Event()

    def wedged():
        with coordinator.hold("duel#1") as lease:
            lease.last_beat -= 10_000
            gone_quiet.set()
            proceed.wait(5)

    t = threading.Thread(target=wedged, daemon=True)
    t.start()
    assert gone_quiet.wait(5)

    def queued():
        with coordinator.hold("test-now", timeout=5):
            ran.set()

    w = threading.Thread(target=queued, daemon=True)
    w.start()
    for _ in range(200):
        if coordinator.waiting() > 0:
            break
        time.sleep(0.005)
    assert not ran.is_set(), "it is waiting on a holder that will never finish"

    coordinator.evict_if_stalled(threshold_s=60)  # what the scheduler watchdog does
    assert ran.wait(5), "the queued session must get the pipeline"
    proceed.set()
    t.join(timeout=5)
    w.join(timeout=5)


def test_status_reports_who_holds_it_and_how_long_they_have_been_quiet():
    """'It seems stuck' has to become answerable in one read: the jobs feed and the
    pipeline-health endpoint both render this."""
    assert coordinator.status()["owner"] is None
    with coordinator.hold("duel#1") as lease:
        lease.last_beat -= 600
        st = coordinator.status()
        assert st["owner"] == "duel#1"
        assert st["busy"] is True
        assert st["stalled_for_s"] >= 600
        assert st["stale_after_s"] == coordinator.STALE_HOLDER_S
