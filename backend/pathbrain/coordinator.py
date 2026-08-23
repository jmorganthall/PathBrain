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
import time
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
# How many sessions are queued on ``hold`` right now — the merge lane. A long session can
# read this and step aside for one of them (see ``yield_if_waiting``); without it, a holder
# has no way to know anyone is waiting and simply runs to completion.
_waiters = 0
_waiters_lock = threading.Lock()
# How long to wait for a queued session to actually pick the lock up after we let go. This
# is not a guess about how long their work takes — only about how long the handoff takes,
# and a thread already blocked in ``acquire`` is woken immediately.
HANDOFF_GRACE_S = 2.0


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


def waiting() -> int:
    """How many sessions are queued behind the current holder."""
    with _waiters_lock:
        return _waiters


def yield_if_waiting(owner_label: str, *, grace: float = HANDOFF_GRACE_S) -> float:
    """Let **one** queued session through, then take the lock back. Returns seconds yielded.

    A zipper merge, not a free-for-all. A long session (the duel ladder runs for hours)
    otherwise holds the lock end to end, so pressing "Test now" queues behind the whole
    night. Calling this at a natural seam — between pairs, where nothing is in flight —
    lets one waiting session in and then resumes.

    The subtlety worth stating: ``threading.Lock`` is documented as **unfair**. Releasing
    it does wake a blocked waiter, and on CPython that waiter usually beats the releasing
    thread's fresh ``acquire`` — but "usually" is not a handoff, and the odds worsen with
    several waiters or a different implementation. So after releasing we wait for the lock
    to actually be taken before queueing for it again, which turns an alternating merge
    from a tendency into a guarantee. If nobody picks it up within ``grace`` (the waiter
    gave up, or was a ``try_hold`` that already deferred), we simply carry on.

    Returns 0.0 when nothing was waiting, so callers can cheaply do this every iteration.
    """
    if waiting() <= 0:
        return 0.0
    started = time.monotonic()
    _set_owner(None)
    _lock.release()
    log.info("Coordinator: %s yielding to %s queued session(s)", owner_label, waiting())
    deadline = started + max(grace, 0.0)
    while time.monotonic() < deadline and not _lock.locked():
        time.sleep(0.01)
    # Queue behind whoever took it (or re-take it immediately if nobody did).
    _lock.acquire()
    _set_owner(owner_label)
    yielded = time.monotonic() - started
    log.info("Coordinator: %s resumed after yielding %.1fs", owner_label, yielded)
    return yielded


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
    global _waiters
    with _waiters_lock:
        _waiters += 1
    try:
        acquired = _lock.acquire(timeout=timeout if timeout is not None else -1)
    finally:
        with _waiters_lock:
            _waiters -= 1
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
