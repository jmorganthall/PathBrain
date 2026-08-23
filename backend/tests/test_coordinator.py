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
