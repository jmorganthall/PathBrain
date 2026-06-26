"""Process-wide coordination for firewall-write + benchmark sessions.

Three subsystems can apply firewall settings *and* run benchmarks — the
autonomous experiment engine (``experiment.py``), the Shotgun Sweep
(``sweep.py``), and the on-demand profile test (``profile_test.py``) — plus the
ordinary monitoring/manual runs. If two of these overlap, one session's apply can
land on top of another's measurement, so "what we tested" stops matching "what we
thought we tested".

This module is the single in-process gate that makes those sessions mutually
exclusive. It is *complementary* to the per-run read-before/read-after fingerprint
check in ``runner.execute_run`` (which catches drift from *outside* PathBrain, e.g.
someone editing OPNsense directly): the lock prevents internal races; the integrity
check backstops external ones.

Usage::

    with coordinator.hold("sweep#7"):       # blocks (queues) until free
        ...apply → benchmark → restore...

    with coordinator.try_hold("monitoring"):  # non-blocking; raises if busy
        ...

User-triggered, long-running sessions (sweep, profile test, manual run) **queue**
(blocking ``hold``). Periodic/autonomous work (monitoring, experiment) uses
``try_hold`` and simply defers to the next tick when busy, so the scheduler
watchdog never stalls behind a long session.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

from .logging_config import get_logger

log = get_logger("coordinator")

# A single mutex guards the "exclusive firewall/benchmark session". Locking is done
# at call sites (never inside execute_run), so a plain non-reentrant Lock is safe.
_lock = threading.Lock()
# Best-effort label of the current holder, for status display. Guarded by its own
# lock so a status read never blocks on the (possibly long-held) session lock.
_owner: str | None = None
_owner_lock = threading.Lock()


class CoordinatorBusy(RuntimeError):
    """Raised when the coordination lock could not be acquired in time.

    ``owner`` is the best-effort label of the session currently holding it.
    """

    def __init__(self, owner: str | None) -> None:
        self.owner = owner
        super().__init__(
            f"Another firewall/benchmark operation is in progress{f' ({owner})' if owner else ''}."
        )


def busy() -> bool:
    """True if a session currently holds the lock."""
    return _lock.locked()


def owner() -> str | None:
    """Best-effort label of the current holder (``None`` if idle)."""
    with _owner_lock:
        return _owner


def _set_owner(value: str | None) -> None:
    global _owner
    with _owner_lock:
        _owner = value


@contextmanager
def hold(owner_label: str, *, timeout: float | None = None):
    """Acquire the coordination lock for the duration of the block (blocking).

    Queues behind any in-progress session. With ``timeout`` set, raises
    ``CoordinatorBusy`` if the lock can't be acquired within that many seconds.
    """
    acquired = _lock.acquire(timeout=timeout if timeout is not None else -1)
    if not acquired:
        raise CoordinatorBusy(owner())
    _set_owner(owner_label)
    log.info("Coordinator acquired by %s", owner_label)
    try:
        yield
    finally:
        _set_owner(None)
        _lock.release()
        log.info("Coordinator released by %s", owner_label)


@contextmanager
def try_hold(owner_label: str):
    """Acquire the lock without blocking; raise ``CoordinatorBusy`` if held.

    For periodic/autonomous callers that should defer (try again later) rather
    than queue, so they never block the thread they run on.
    """
    if not _lock.acquire(blocking=False):
        raise CoordinatorBusy(owner())
    _set_owner(owner_label)
    log.info("Coordinator acquired by %s", owner_label)
    try:
        yield
    finally:
        _set_owner(None)
        _lock.release()
        log.info("Coordinator released by %s", owner_label)
