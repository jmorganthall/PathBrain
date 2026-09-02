"""The browser probe must not leak a process tree per run.

The failure these tests are written against, from a live host capture: 224 node
processes (13.25 GiB) and 887 Chrome processes (388 of them zombies, 499 live at
6.65 GiB) against 593 MiB free on a 32 GiB NAS, with the kernel OOM-killing Chrome
and nginx as collateral. The shape — one node driver with ~4 Chrome children per run —
was one leaked Playwright tree for every standalone benchmark run.

The cause was a thread, not a missing ``close()``. Playwright's sync objects are bound
to the thread that created them, and probes run on a dedicated worker thread
(``probes``). ``teardown_plugins`` routes the close back onto that worker for exactly
that reason; ``execute_run``'s own ``finally`` called ``plugin.teardown()`` directly on
the runner's thread instead. From Python that is indistinguishable from success: the
cross-thread call raises, the plugin's best-effort handler swallows it, the handles are
dropped, and the browser keeps running with nothing left to close it.

So the load-bearing assertion here is about *which thread* the close happens on — a
counted-handles test alone passes against the bug, because the buggy code did call
``teardown()``, just from the wrong place.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

import pytest

from pathbrain import browser_procs, probes, runner
from pathbrain.plugins.base import BenchmarkPlugin, PluginResult


@pytest.fixture(autouse=True)
def _clean_worker():
    probes._reset_for_tests()
    yield
    probes._reset_for_tests()


class _ThreadBoundPlugin(BenchmarkPlugin):
    """A plugin whose resource behaves like Playwright's: usable only from its own thread.

    ``open_trees`` is the stand-in for live node/Chromium process trees. It goes up on
    acquire and down only on a close made from the acquiring thread — a close from any
    other thread raises, exactly as Playwright's greenlet dispatcher does, and leaves the
    tree running.
    """

    name = "threadbound"

    def __init__(self) -> None:
        self.open_trees = 0
        self.leaked = 0
        self._owner: int | None = None
        self.teardown_threads: list[int] = []

    def run(self, config: dict) -> PluginResult:
        if self._owner is None:
            self._owner = threading.get_ident()
            self.open_trees += 1
        elif self._owner != threading.get_ident():
            raise AssertionError("probe ran on a thread that does not own the browser")
        return PluginResult(self.name, success=True, raw={})

    def teardown(self) -> None:
        self.teardown_threads.append(threading.get_ident())
        if self._owner is None:
            return
        if self._owner != threading.get_ident():
            # The bug: swallowed by the plugin's best-effort handler, handles dropped,
            # process left running.
            self.leaked += 1
            self._owner = None
            return
        self.open_trees -= 1
        self._owner = None

    def abandon(self) -> None:
        self._owner = None


def _register(monkeypatch, *plugins) -> None:
    from pathbrain import plugins as plugins_pkg

    monkeypatch.setattr(runner, "iter_plugins", lambda: list(plugins))
    monkeypatch.setattr(plugins_pkg, "iter_plugins", lambda: list(plugins))


# -- the regression ---------------------------------------------------------


def test_a_standalone_run_closes_the_browser_on_the_thread_that_opened_it(monkeypatch):
    """``execute_run``'s own teardown must go through the probe worker, not run inline.

    This is the whole bug in one assertion. Both the buggy and the fixed code call
    ``teardown()``; only the fixed code calls it from the thread that can actually close
    anything.
    """
    plugin = _ThreadBoundPlugin()
    _register(monkeypatch, plugin)

    run_id = runner.create_run(label="leak-test", iterations=1)
    runner.execute_run(run_id)

    assert plugin.teardown_threads, "teardown never ran"
    assert threading.get_ident() not in plugin.teardown_threads, (
        "teardown ran on the caller's thread — a cross-thread Playwright close raises, "
        "is swallowed, and leaks the browser process tree"
    )
    assert plugin.leaked == 0
    assert plugin.open_trees == 0


def test_hundreds_of_runs_return_the_tree_count_to_baseline(monkeypatch):
    """The soak: many measurements, no accumulation.

    Run count is the axis the leak scaled on — one tree per run, so 200 runs meant 200
    trees. Both the peak and the end state are asserted: peaking at 1 is what says the
    browser is being reused within a run rather than relaunched, and ending at 0 is what
    says every one of them was closed.
    """
    plugin = _ThreadBoundPlugin()
    _register(monkeypatch, plugin)

    peak = 0
    for _ in range(200):
        run_id = runner.create_run(label="leak-soak", iterations=1)
        runner.execute_run(run_id)
        peak = max(peak, plugin.open_trees)

    assert plugin.leaked == 0, f"{plugin.leaked} browser trees leaked over 200 runs"
    assert plugin.open_trees == 0
    assert peak <= 1


def test_a_chunked_series_keeps_one_browser_warm_and_closes_it_once(monkeypatch):
    """``teardown=False`` must reuse, and the series' single teardown must still close."""
    plugin = _ThreadBoundPlugin()
    _register(monkeypatch, plugin)

    for _ in range(50):
        run_id = runner.create_run(label="leak-chunk", iterations=1)
        runner.execute_run(run_id, teardown=False)
        assert plugin.open_trees == 1  # warm across chunks, never a second tree

    runner.teardown_plugins()
    assert plugin.open_trees == 0
    assert plugin.leaked == 0


# -- abandonment ------------------------------------------------------------


class _Wedged(BenchmarkPlugin):
    name = "wedged"

    def __init__(self) -> None:
        self.release = threading.Event()

    def run(self, config: dict) -> PluginResult:
        self.release.wait(30)
        return PluginResult(self.name, success=True)


class _Counting(BenchmarkPlugin):
    name = "counting"

    def __init__(self) -> None:
        self.abandoned = 0

    def run(self, config: dict) -> PluginResult:
        return PluginResult(self.name, success=True)

    def abandon(self) -> None:
        self.abandoned += 1


def test_abandoning_a_wedged_worker_abandons_every_plugin_on_it(monkeypatch):
    """A cheap probe timing out must not strand the browser's handles.

    The worker is shared by the whole suite, so when it is abandoned *every* plugin that
    ran on it is holding objects bound to a dead thread. Abandoning only the plugin that
    timed out left the browser still holding a Chromium it could no longer close — the
    next run's cross-thread ``is_connected()`` raised, the swallowed close dropped the
    handles, and the tree leaked.
    """
    wedged, other = _Wedged(), _Counting()
    _register(monkeypatch, wedged, other)
    try:
        result = probes.run_plugin(wedged, {}, timeout_s=0.2)
        assert result.success is False
        assert other.abandoned == 1, "a bystander plugin's stranded handles were not dropped"
    finally:
        wedged.release.set()


def test_an_abandon_that_raises_is_counted_not_propagated(monkeypatch):
    class _Bad(_Counting):
        name = "bad"

        def abandon(self) -> None:
            raise RuntimeError("boom")

    wedged, bad = _Wedged(), _Bad()
    _register(monkeypatch, wedged, bad)
    try:
        before = probes.stats()["cleanup_failures"]
        probes.run_plugin(wedged, {}, timeout_s=0.2)
        assert probes.stats()["cleanup_failures"] == before + 1
    finally:
        wedged.release.set()


# -- process accounting -----------------------------------------------------


@pytest.mark.skipif(not browser_procs.available(), reason="no /proc on this platform")
def test_snapshot_reports_this_process_and_its_children():
    snap = browser_procs.snapshot()
    assert snap["available"] is True
    assert snap["pid"] == os.getpid()
    assert snap["self_rss_mb"] > 0
    for key in ("drivers", "node", "chrome", "zombies", "children_rss_mb"):
        assert key in snap


@pytest.mark.skipif(not browser_procs.available(), reason="no /proc on this platform")
def test_kill_tree_reaches_grandchildren_and_leaves_everything_else_alone():
    """The reaper's safety property: it walks *our* descendants and only those."""
    parent = subprocess.Popen(
        ["sh", "-c", "sleep 30 & sleep 30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    bystander = subprocess.Popen(["sleep", "30"])
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not browser_procs.descendants(parent.pid):
            time.sleep(0.05)
        assert browser_procs.descendants(parent.pid), "child never started"

        browser_procs.kill_tree(parent.pid)
        parent.wait(timeout=5)
        assert bystander.poll() is None, "the reaper killed a process outside the tree"
    finally:
        for proc in (parent, bystander):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(not browser_procs.available(), reason="no /proc on this platform")
def test_reaping_with_no_drivers_is_a_no_op():
    assert browser_procs.reap_orphans() == {"reaped": 0, "pids": []}


# -- the real thing (opt-in) ------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("PATHBRAIN_BROWSER_SOAK"),
    reason="set PATHBRAIN_BROWSER_SOAK=1 to soak a real Chromium (slow; needs Playwright)",
)
def test_real_chromium_soak_returns_process_counts_and_rss_to_baseline():
    """The end-to-end proof, run against an actual Chromium.

    Kept opt-in because it needs Playwright plus a browser binary and takes minutes; the
    fake-plugin soak above is what runs in CI, and it pins the same property (the close
    happens where it can work) without the dependency.
    """
    pytest.importorskip("playwright")
    from pathbrain.plugins.benchmark_browser import BrowserBenchmark

    plugin = BrowserBenchmark()
    section = {"urls": ["about:blank"], "timeout_s": 30.0, "networkidle_timeout_s": 1.0}

    probes.run_plugin(plugin, section, timeout_s=120)  # warm up, then measure from here
    probes.teardown(plugin)
    base = browser_procs.snapshot()

    for _ in range(50):
        result = probes.run_plugin(plugin, section, timeout_s=120)
        assert result.success, result.error
        probes.teardown(plugin)

    time.sleep(2)  # let the kernel finish tearing the last tree down
    after = browser_procs.snapshot()
    assert after["drivers"] == 0, f"leaked {after['drivers']} Playwright driver tree(s)"
    assert after["chrome"] <= base["chrome"]
    assert after["node"] <= base["node"]
    assert after["zombies"] == 0
    assert plugin.cleanup_stats()["cleanup_failures"] == 0
