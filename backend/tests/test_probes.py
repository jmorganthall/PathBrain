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


# ── One bad browser session must not poison every later one ──────────────────────────
#
# Playwright's sync API parks a running asyncio loop on its creating thread for as long
# as the started playwright lives; the probe worker deliberately lives forever. A
# playwright that died without stop() (a launch that raised, an abandon) therefore left
# the thread-local running-loop marker set, and every later sync_playwright().start() on
# the worker refused with "Sync API inside the asyncio loop" — the "all runs stopped
# reporting" incident: one Chromium hiccup and every subsequent run scored
# incomparable/legacy until the process restarted.


class _LoopLeaker(BenchmarkPlugin):
    """A probe that dies the way a failed sync Playwright start dies: leaving the
    thread's running-loop marker set behind it."""

    name = "leaker"

    def run(self, config: dict) -> PluginResult:  # noqa: ARG002
        import asyncio

        asyncio.events._set_running_loop(asyncio.new_event_loop())
        return PluginResult(self.name, success=True)


class _NextProbe(BenchmarkPlugin):
    """The probe after the leak: reports whether the thread is clean after running the
    browser plugin's guard — i.e. whether a fresh sync_playwright().start() would work."""

    name = "next"

    def run(self, config: dict) -> PluginResult:  # noqa: ARG002
        import asyncio

        from pathbrain.plugins.benchmark_browser import _clear_stale_asyncio_loop

        _clear_stale_asyncio_loop()
        try:
            asyncio.get_running_loop()
            clean = False  # a running loop would make sync Playwright refuse to start
        except RuntimeError:
            clean = True
        return PluginResult(self.name, success=clean)


def test_a_leaked_asyncio_loop_on_the_worker_is_cleared_not_fatal():
    """The marker genuinely persists across jobs on the long-lived worker (that's the
    poison), and the browser plugin's guard clears it (that's the antidote)."""
    probes.run_plugin(_LoopLeaker(), {}, timeout_s=5)
    result = probes.run_plugin(_NextProbe(), {}, timeout_s=5)
    assert result.success, "the guard must leave the worker thread startable again"


def test_a_failed_chromium_launch_stops_the_playwright_it_started(monkeypatch):
    """The root cause: start() succeeded, launch() raised, and the started playwright —
    whose stop() is what clears the thread's loop marker — was simply forgotten."""
    import sys
    import types

    from pathbrain.plugins.benchmark_browser import BrowserBenchmark

    stopped: list[bool] = []

    class _FakePw:
        class chromium:  # noqa: N801 — mirrors playwright's attribute
            @staticmethod
            def launch(**kwargs):  # noqa: ARG004
                raise RuntimeError("chromium exploded")

        def stop(self):
            stopped.append(True)

    class _FakeCtx:
        def start(self):
            return _FakePw()

    fake = types.ModuleType("playwright.sync_api")
    fake.sync_playwright = lambda: _FakeCtx()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)

    plugin = BrowserBenchmark()
    with pytest.raises(RuntimeError, match="chromium exploded"):
        plugin._ensure_browser({})
    assert stopped == [True], "the started playwright must be stopped on the failure path"
    assert plugin._pw is None and plugin._browser is None
