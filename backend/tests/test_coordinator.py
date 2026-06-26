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
