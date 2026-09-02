"""Bounded execution for benchmark probes — no measurement may park the pipeline.

A plugin call is the one place PathBrain hands control to something it does not own:
a socket, a subprocess, a Chromium that answers the Playwright protocol until it
doesn't. Most of what a probe does is already bounded (nav timeouts, socket timeouts),
but a handful of the calls the browser plugin makes have **no timeout to set** —
``browser.new_context()``, ``page.evaluate()``, ``context.close()`` — and a wedged
Chromium leaves the calling thread blocked in one of them for as long as the process
lives.

That thread is the pipeline. It holds the ``coordinator`` lock for the whole
apply → benchmark → restore session, so a single unanswered protocol call stops
monitoring, the duel ladder, and everything the user presses. The symptom this module
was written for: a duel session wedged mid-round at 23:00 was still "running" at 07:30,
holding the lock the entire time, with the run watchdog dutifully marking its run FAILED
every 15 seconds and nothing whatsoever changing — the watchdog could *see* the stall
and had no way to act on it.

Python cannot interrupt a thread blocked in a C call, so the only honest response is to
stop waiting on it. Probes run on a **dedicated, long-lived worker thread** and the
caller waits with a deadline. On expiry the probe is reported as a failed measurement,
the worker is **abandoned** — left blocked, daemon, never joined — and the next call
gets a fresh one.

The worker is dedicated rather than pooled for a specific reason: Playwright's sync API
is bound to the thread that created it, so keeping every probe on one thread is what
lets the browser plugin reuse a warm Chromium across a run's iterations (the whole point
of ``teardown=False``). It also means a fresh worker implies a fresh Chromium — so an
abandoned worker's plugin is told to **drop** the handles it can no longer touch
(:meth:`BenchmarkPlugin.abandon`) rather than close them, since closing them would mean
calling into the same wedged browser from the wrong thread.

The cost of a stall is therefore one leaked thread and one leaked browser process, which
is the deliberate trade: a wedged probe becomes one failed measurement instead of a dead
platform. Leaks are counted (:func:`stats`) so "this keeps happening" is a number rather
than an impression.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

from .logging_config import get_logger
from .plugins.base import BenchmarkPlugin, PluginResult

log = get_logger("probes")

#: How long a single plugin call may take before it is abandoned. Deliberately far above
#: any legitimate probe (the browser's own budget is a few seconds per URL) and far below
#: "all night". Overridable per call from ``monitoring.probe_timeout_minutes``.
DEFAULT_TIMEOUT_S = 600.0

#: Teardown is a close, not a measurement — it should take milliseconds. A wedged close is
#: exactly as fatal as a wedged probe, so it gets the same treatment on a shorter fuse.
TEARDOWN_TIMEOUT_S = 60.0


class ProbeTimeout(RuntimeError):
    """A probe did not return within its deadline; its worker has been abandoned."""


class _Worker:
    """One thread that runs probe calls, one at a time, in submission order.

    Never runs a second job while a first is outstanding: a timed-out job is still
    occupying the thread, so the worker is discarded rather than queued behind.
    """

    def __init__(self, seq: int) -> None:
        self.seq = seq
        self.started_at = time.monotonic()
        self.busy_label: str | None = None
        self.busy_since: float | None = None
        self._jobs: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._loop, name=f"pathbrain-probe-{seq}", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, box = job
            try:
                box["value"] = fn()
                box["ok"] = True
            except BaseException as exc:  # noqa: BLE001 — relayed to the caller verbatim
                box["error"] = exc
            finally:
                box["done"].set()

    def submit(self, fn: Callable[[], Any], timeout_s: float, label: str) -> Any:
        """Run ``fn`` on the worker; return its value, re-raise its exception, or raise
        :class:`ProbeTimeout` if it hasn't finished within ``timeout_s``."""
        box: dict[str, Any] = {"done": threading.Event(), "ok": False}
        self.busy_label, self.busy_since = label, time.monotonic()
        self._jobs.put((fn, box))
        finished = box["done"].wait(timeout_s)
        if not finished:
            raise ProbeTimeout(
                f"{label} did not return within {timeout_s / 60.0:.0f} min"
            )
        self.busy_label, self.busy_since = None, None
        if box.get("ok"):
            return box.get("value")
        raise box["error"]

    def retire(self) -> None:
        """Ask an *idle* worker to exit (used when nothing is wedged)."""
        self._jobs.put(None)


_worker: _Worker | None = None
_worker_lock = threading.Lock()
_seq = 0
_abandoned = 0
_last_abandon: dict | None = None
#: Cleanups that did not complete: an ``abandon()`` that raised, or a plugin that reported
#: its own close had failed. Every one of these is a process tree we know we did not close,
#: so it is a number the health endpoint reports rather than a line in a log nobody reads.
_cleanup_failures = 0
_last_cleanup_failure: dict | None = None


def _note_cleanup_failure(where: str, detail: str) -> None:
    global _cleanup_failures, _last_cleanup_failure
    with _worker_lock:
        _cleanup_failures += 1
        _last_cleanup_failure = {"where": where, "detail": detail, "at": time.time()}


def _current() -> _Worker:
    global _worker, _seq
    with _worker_lock:
        if _worker is None:
            _seq += 1
            _worker = _Worker(_seq)
            log.info("Probe worker %s started", _seq)
        return _worker


def _abandon(worker: _Worker, label: str, timeout_s: float) -> None:
    """Drop a wedged worker so the next call gets a fresh thread (and a fresh browser)."""
    global _worker, _abandoned, _last_abandon
    with _worker_lock:
        if _worker is worker:
            _worker = None
        _abandoned += 1
        _last_abandon = {
            "worker": worker.seq,
            "label": label,
            "timeout_s": timeout_s,
            "at": time.time(),
        }
    log.error(
        "Probe worker %s abandoned: %s exceeded %.0fs. The thread is left blocked "
        "(it cannot be killed); the next probe starts a fresh worker. Abandoned so far: %s",
        worker.seq, label, timeout_s, _abandoned,
    )


def _abandon_all_plugins(primary: BenchmarkPlugin | None = None) -> None:
    """Tell **every** registered plugin to drop its thread-affine handles.

    The worker is shared by the whole suite, so when it is abandoned every plugin that
    ever ran on it is holding objects bound to a thread that is never coming back — not
    just the one that happened to blow its deadline. Abandoning only the timed-out plugin
    left the browser plugin still holding a Chromium it could no longer close: the next
    run's ``is_connected()`` probe raised cross-thread, the swallowed close dropped the
    handles without closing anything, and the process tree leaked. A cheap network probe
    timing out must not cost a browser.

    ``primary`` is abandoned first (it is the one we know is wedged); the rest follow.
    Never raises — a failure here is counted, not propagated.
    """
    from .plugins import iter_plugins

    ordered: list[BenchmarkPlugin] = []
    if primary is not None:
        ordered.append(primary)
    for plugin in iter_plugins():
        if plugin is not primary:
            ordered.append(plugin)
    for plugin in ordered:
        try:
            plugin.abandon()
        except Exception as exc:  # noqa: BLE001 — abandoning must never raise
            _note_cleanup_failure(f"abandon '{plugin.name}'", f"{type(exc).__name__}: {exc}")
            log.warning("Plugin '%s' abandon() failed", plugin.name, exc_info=True)


def run_plugin(
    plugin: BenchmarkPlugin, section: dict, *, timeout_s: float = DEFAULT_TIMEOUT_S
) -> PluginResult:
    """Execute ``plugin.run(section)`` under a deadline.

    Returns the plugin's own result, or — when the deadline passes — a
    ``success=False`` result describing the stall, exactly as a plugin reporting its own
    measurement failure would. Plugin exceptions are re-raised unchanged, so the runner's
    existing error handling is untouched.
    """
    worker = _current()
    label = f"probe '{plugin.name}'"
    try:
        return worker.submit(lambda: plugin.run(section), timeout_s, label)
    except ProbeTimeout as exc:
        _abandon(worker, label, timeout_s)
        # The cached handles of every plugin that ran on this worker belong to a thread
        # that is never coming back. They must be let go of without being touched —
        # closing a wedged browser from the wrong thread is the same hang again, one
        # frame further out.
        _abandon_all_plugins(plugin)
        return PluginResult(plugin.name, success=False, error=str(exc))


def teardown(plugin: BenchmarkPlugin, *, timeout_s: float = TEARDOWN_TIMEOUT_S) -> None:
    """Tear a plugin down **on the probe worker**, under a deadline.

    Teardown has to run on the thread that created the resources — Playwright's sync
    objects belong to their creating thread, so closing Chromium from the runner's thread
    would silently fail and leak a browser process per run.
    """
    worker = _current()
    label = f"teardown '{plugin.name}'"
    try:
        worker.submit(plugin.teardown, timeout_s, label)
    except ProbeTimeout:
        _abandon(worker, label, timeout_s)
        _abandon_all_plugins(plugin)
    except Exception as exc:  # noqa: BLE001 — teardown must never break the caller
        _note_cleanup_failure(f"teardown '{plugin.name}'", f"{type(exc).__name__}: {exc}")
        log.warning("Plugin '%s' teardown failed", plugin.name, exc_info=True)


def stats() -> dict:
    """Probe-worker health, for the stall diagnostics endpoint."""
    with _worker_lock:
        worker = _worker
        abandoned, last = _abandoned, _last_abandon
        failures, last_failure = _cleanup_failures, _last_cleanup_failure
    busy_for = None
    if worker is not None and worker.busy_since is not None:
        busy_for = round(time.monotonic() - worker.busy_since, 1)
    return {
        "worker": None if worker is None else worker.seq,
        "running": None if worker is None else worker.busy_label,
        "running_for_s": busy_for,
        "abandoned": abandoned,
        "last_abandoned": last,
        "cleanup_failures": failures,
        "last_cleanup_failure": last_failure,
        "threads": [t.name for t in threading.enumerate() if t.name.startswith("pathbrain-probe")],
    }


def _reset_for_tests() -> None:
    """Drop the worker between tests so each starts from a clean thread."""
    global _worker, _abandoned, _last_abandon, _cleanup_failures, _last_cleanup_failure
    with _worker_lock:
        worker, _worker = _worker, None
        _abandoned, _last_abandon = 0, None
        _cleanup_failures, _last_cleanup_failure = 0, None
    if worker is not None and worker.busy_since is None:
        worker.retire()
