"""A wedged probe must cost one measurement, not the pipeline.

The failure these tests are written against: a duel session went into a browser call at
23:00 and was still in it at 07:30, holding the coordination lock the whole time. Every
run, test and race started overnight simply queued behind a thread that was never coming
back, and the run watchdog marked the run FAILED every 15 seconds without changing
anything — it could see the stall and had no way to act on it.
"""
from __future__ import annotations

import threading
import time

import pytest

from pathbrain import probes
from pathbrain.plugins.base import BenchmarkPlugin, PluginResult


@pytest.fixture(autouse=True)
def _clean_worker():
    probes._reset_for_tests()
    yield
    probes._reset_for_tests()


class _Wedged(BenchmarkPlugin):
    """A probe that never returns — a Chromium that stopped answering the protocol."""

    name = "wedged"

    def __init__(self) -> None:
        self.release = threading.Event()
        self.abandoned = False
        self.entered = threading.Event()

    def run(self, config: dict) -> PluginResult:
        self.entered.set()
        self.release.wait(30)  # bounded only so a failing test can't hang the suite
        return PluginResult(self.name, success=True)

    def abandon(self) -> None:
        self.abandoned = True


class _Quick(BenchmarkPlugin):
    name = "quick"

    def __init__(self) -> None:
        self.threads: list[int] = []

    def run(self, config: dict) -> PluginResult:
        self.threads.append(threading.get_ident())
        return PluginResult(self.name, success=True, raw={"ok": True})


def test_a_wedged_probe_comes_back_as_a_failed_measurement():
    """It cannot be killed, so it is stopped being waited on — and reported like any
    other probe that didn't work, so nothing downstream needs to know about threads."""
    plugin = _Wedged()
    started = time.monotonic()
    result = probes.run_plugin(plugin, {}, timeout_s=0.3)
    elapsed = time.monotonic() - started

    assert result.success is False
    assert "did not return" in (result.error or "")
    assert elapsed < 5, "the caller waited on the deadline, not on the probe"
    plugin.release.set()


def test_the_wedged_plugin_is_told_to_let_go_of_its_handles():
    """Its Chromium belongs to a thread that is never coming back, so it must drop the
    handles rather than close them — closing means calling into the same wedged browser
    from a thread that doesn't own it, which is the original hang one frame out."""
    plugin = _Wedged()
    probes.run_plugin(plugin, {}, timeout_s=0.2)
    assert plugin.abandoned is True
    plugin.release.set()


def test_the_next_probe_runs_on_a_fresh_worker_and_succeeds():
    """The recovery that matters: the pipeline keeps measuring afterwards."""
    wedged = _Wedged()
    probes.run_plugin(wedged, {}, timeout_s=0.2)

    quick = _Quick()
    result = probes.run_plugin(quick, {}, timeout_s=5)
    assert result.success is True, "a stalled probe must not poison the ones after it"
    assert probes.stats()["abandoned"] == 1
    wedged.release.set()


def test_probes_share_one_thread_so_a_warm_browser_survives_iterations():
    """Playwright's sync API is bound to its creating thread, so 'one dedicated worker'
    is not an implementation detail — it is what lets a run reuse one Chromium across
    every iteration instead of paying the cold start each time."""
    quick = _Quick()
    for _ in range(3):
        probes.run_plugin(quick, {}, timeout_s=5)
    assert len(set(quick.threads)) == 1
    assert quick.threads[0] != threading.get_ident(), "probes run off the caller's thread"


def test_a_plugin_that_raises_still_raises_to_the_caller():
    """Only the *timeout* is new behaviour; the runner's existing error handling for a
    plugin that blows up must be untouched."""

    class _Boom(BenchmarkPlugin):
        name = "boom"

        def run(self, config: dict) -> PluginResult:
            raise ValueError("nope")

    with pytest.raises(ValueError):
        probes.run_plugin(_Boom(), {}, timeout_s=5)


def test_a_wedged_teardown_is_abandoned_too():
    """Closing the browser is the other unbounded call — a hang there is exactly as
    fatal, and it happens in a ``finally`` where nobody is looking."""

    class _WedgedClose(BenchmarkPlugin):
        name = "wedged-close"

        def __init__(self) -> None:
            self.release = threading.Event()
            self.abandoned = False

        def run(self, config: dict) -> PluginResult:
            return PluginResult(self.name, success=True)

        def teardown(self) -> None:
            self.release.wait(30)

        def abandon(self) -> None:
            self.abandoned = True

    plugin = _WedgedClose()
    started = time.monotonic()
    probes.teardown(plugin, timeout_s=0.2)
    assert time.monotonic() - started < 5
    assert plugin.abandoned is True
    plugin.release.set()
