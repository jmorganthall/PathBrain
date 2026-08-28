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

**The lock is a lease, not a promise.** Holding it for hours is normal (a duel window
is a night), so "held for a long time" says nothing — but a holder that has stopped
*making progress* has stopped being a session and become a wedge, and every queued
session then waits on a thread that is never coming back. That is not hypothetical: a
duel wedged inside an unanswered browser call at 23:00 still owned the pipeline at
07:30, so every run, test and race started in between simply queued, and the platform
read as dead. Python cannot kill the blocked thread, so instead the holder **beats**
(:func:`beat`, stamped by the runner around every probe) and a holder whose last beat is
older than :data:`STALE_HOLDER_S` can be **evicted**: its lease is revoked and the lock
handed on. The evicted session cannot quietly resume — :meth:`Lease.check` at its own
seams raises, and its eventual release is a no-op — so the mutual exclusion the lock
exists for survives its holder dying.

Eviction is the backstop, not the mechanism: probes are individually bounded
(``pathbrain.probes``), so a stall normally resolves as one failed measurement long
before the lease goes stale.
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
# The current holder's lease (None when idle). Guarded by its own lock so a status read
# never blocks on the (possibly long-held) session lock.
_lease: "Lease | None" = None
_state_lock = threading.Lock()
_lease_seq = 0
# How many sessions are queued on ``hold`` right now — the merge lane. A long session can
# read this and step aside for one of them (see ``yield_if_waiting``); without it, a holder
# has no way to know anyone is waiting and simply runs to completion.
_waiters = 0
_waiters_lock = threading.Lock()
# How long to wait for a queued session to actually pick the lock up after we let go. This
# is not a guess about how long their work takes — only about how long the handoff takes,
# and a thread already blocked in ``acquire`` is woken immediately.
HANDOFF_GRACE_S = 2.0

#: A holder silent for this long is treated as wedged and may be evicted. It has to sit
#: **above** the probe deadline (``runner.DEFAULT_PROBE_TIMEOUT_MINUTES``, 10 min) with
#: room to spare: a probe blowing its own deadline is a recoverable failed measurement and
#: must never be mistaken for a dead session. Below that, it is as short as the evidence
#: allows — every minute here is a minute the pipeline stays hostage.
STALE_HOLDER_S = 20 * 60.0

#: How often a queued session re-checks whether the holder has gone quiet. Cheap: it is a
#: timed ``acquire`` that would otherwise be an untimed one.
EVICT_POLL_S = 15.0

#: Evictions since start, and what the last one was — so "does this keep happening?" is a
#: number rather than an impression (surfaced by the stall diagnostics).
_evictions = 0
_last_eviction: dict | None = None


class LeaseRevoked(RuntimeError):
    """Raised at a session's own seam when its lease has been evicted.

    The evicted session is, by definition, one that stopped responding — but "stopped
    responding" is not "died", and a thread that wakes up hours later must not resume
    applying firewall settings on top of whoever holds the pipeline now. So it finds out
    at its next :meth:`Lease.check` and unwinds instead.
    """

    def __init__(self, lease: "Lease") -> None:
        self.lease = lease
        super().__init__(
            f"Coordination lease for {lease.label} was revoked "
            f"({lease.revoked_reason or 'evicted'}); this session must stop."
        )


class Lease:
    """One session's claim on the pipeline: who, since when, and last sign of life."""

    __slots__ = ("id", "label", "acquired_at", "last_beat", "revoked", "revoked_reason")

    def __init__(self, lease_id: int, label: str) -> None:
        now = time.monotonic()
        self.id = lease_id
        self.label = label
        self.acquired_at = now
        self.last_beat = now
        self.revoked = False
        self.revoked_reason: str | None = None

    @property
    def alive(self) -> bool:
        return not self.revoked

    def beat(self) -> None:
        """Record progress. Cheap enough to call in any inner loop."""
        self.last_beat = time.monotonic()

    def check(self) -> None:
        """Raise if this lease has been evicted. Call at seams where stopping is safe."""
        if self.revoked:
            raise LeaseRevoked(self)


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


def current_lease() -> Lease | None:
    """The holder's lease, or None when idle."""
    with _state_lock:
        return _lease


def owner() -> str | None:
    """Best-effort label of the current holder (``None`` if idle)."""
    lease = current_lease()
    return lease.label if lease else None


def beat() -> None:
    """Stamp the current holder as alive and progressing.

    Called by whoever is doing the work (the runner, around every probe), not by the
    session that owns the lease: progress is made in the inner loop, and that is the only
    place that knows it happened.
    """
    lease = current_lease()
    if lease is not None:
        lease.beat()


def held_for() -> float | None:
    """Seconds the current holder has held the lock (None when idle)."""
    lease = current_lease()
    return None if lease is None else round(time.monotonic() - lease.acquired_at, 1)


def stalled_for() -> float | None:
    """Seconds since the current holder last showed progress (None when idle).

    Not the same question as :func:`held_for`: a duel window is *meant* to run for hours,
    so age proves nothing. Silence is what distinguishes a session from a wedge.
    """
    lease = current_lease()
    return None if lease is None else round(time.monotonic() - lease.last_beat, 1)


def status() -> dict:
    """Everything a stall diagnosis needs about the pipeline gate, in one read."""
    lease = current_lease()
    with _state_lock:
        evictions, last = _evictions, _last_eviction
    return {
        "busy": busy(),
        "owner": lease.label if lease else None,
        "held_for_s": held_for(),
        "stalled_for_s": stalled_for(),
        "stale_after_s": STALE_HOLDER_S,
        "waiting": waiting(),
        "evictions": evictions,
        "last_eviction": last,
    }


def evict_if_stalled(threshold_s: float | None = None) -> bool:
    """Revoke the lease of a holder that has gone quiet, freeing the pipeline.

    Returns True if a holder was evicted. The wedged thread is *not* killed — Python
    cannot — it is disowned: its lease is revoked, so its own :meth:`Lease.check` seams
    raise and its eventual release is a no-op, and the lock is handed to whoever is next.
    """
    global _lease, _evictions, _last_eviction
    threshold_s = STALE_HOLDER_S if threshold_s is None else threshold_s
    with _state_lock:
        lease = _lease
        if lease is None or lease.revoked:
            return False
        quiet = time.monotonic() - lease.last_beat
        if quiet < threshold_s:
            return False
        lease.revoked = True
        lease.revoked_reason = f"no progress for {quiet / 60.0:.0f} min"
        _lease = None
        _evictions += 1
        _last_eviction = {
            "owner": lease.label,
            "quiet_s": round(quiet, 1),
            "held_s": round(time.monotonic() - lease.acquired_at, 1),
            "at": time.time(),
        }
    try:
        _lock.release()
    except RuntimeError:  # already released between the check and here
        pass
    log.error(
        "Coordinator: evicted %s — no progress for %.0f min (held %.0f min). Its thread "
        "is left blocked; the pipeline is free again.",
        lease.label, quiet / 60.0, (time.monotonic() - lease.acquired_at) / 60.0,
    )
    return True


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
    lease = current_lease()
    if lease is None or lease.revoked or waiting() <= 0:
        return 0.0
    started = time.monotonic()
    _set_lease(None)
    _lock.release()
    log.info("Coordinator: %s yielding to %s queued session(s)", owner_label, waiting())
    deadline = started + max(grace, 0.0)
    while time.monotonic() < deadline and not _lock.locked():
        time.sleep(0.01)
    # Queue behind whoever took it (or re-take it immediately if nobody did). Counted as a
    # waiter like any other, so the session we stepped aside for can see us and step aside
    # in turn — and so a stalled holder is evicted rather than trapping the yielder.
    _acquire_or_evict(None)
    # The same lease object resumes: the session never ended, and callers hold references
    # to it for their own ``check()`` seams.
    lease.beat()
    _set_lease(lease)
    yielded = time.monotonic() - started
    log.info("Coordinator: %s resumed after yielding %.1fs", owner_label, yielded)
    return yielded


def _set_lease(value: Lease | None) -> None:
    global _lease
    with _state_lock:
        _lease = value


def _new_lease(label: str) -> Lease:
    global _lease, _lease_seq
    with _state_lock:
        _lease_seq += 1
        _lease = Lease(_lease_seq, label)
        return _lease


def _acquire_or_evict(timeout: float | None) -> bool:
    """Block for the lock, evicting a holder that has gone quiet. True if acquired.

    The wait is broken into polls for one reason only: an untimed ``acquire`` on a wedged
    holder never returns, which is precisely the failure this is here to end.
    """
    global _waiters
    deadline = None if timeout is None else time.monotonic() + timeout
    with _waiters_lock:
        _waiters += 1
    try:
        while True:
            if deadline is None:
                wait = EVICT_POLL_S
            else:
                wait = min(EVICT_POLL_S, max(deadline - time.monotonic(), 0.0))
                if wait <= 0:
                    return False
            if _lock.acquire(timeout=wait):
                return True
            evict_if_stalled()
    finally:
        with _waiters_lock:
            _waiters -= 1


@contextmanager
def hold(owner_label: str, *, timeout: float | None = None):
    """Acquire the coordination lock for the duration of the block (blocking).

    Queues behind any in-progress session. With ``timeout`` set, raises
    ``CoordinatorBusy`` if the lock can't be acquired within that many seconds. Yields the
    session's :class:`Lease` — a long session should ``check()`` it at its seams so an
    eviction stops it instead of letting it resume over the top of a live session.
    """
    if not _acquire_or_evict(timeout):
        raise CoordinatorBusy(owner())
    lease = _new_lease(owner_label)
    log.info("Coordinator acquired by %s", owner_label)
    try:
        yield lease
    finally:
        _release(lease, owner_label)


@contextmanager
def try_hold(owner_label: str):
    """Acquire the lock without blocking; raise ``CoordinatorBusy`` if held.

    For periodic/autonomous callers that should defer (try again later) rather
    than queue, so they never block the thread they run on.
    """
    if not _lock.acquire(blocking=False):
        raise CoordinatorBusy(owner())
    lease = _new_lease(owner_label)
    log.info("Coordinator acquired by %s", owner_label)
    try:
        yield lease
    finally:
        _release(lease, owner_label)


def _release(lease: Lease, owner_label: str) -> None:
    """Release the lock — unless this session was evicted and no longer owns it.

    An evicted holder that finally returns must not release a lock somebody else is now
    holding: that would let two firewall sessions run at once, the exact thing this
    module exists to prevent. So the release is conditional on the lease still being the
    current one.
    """
    global _lease
    with _state_lock:
        mine = _lease is lease and not lease.revoked
        if mine:
            _lease = None
    if not mine:
        log.warning(
            "Coordinator: %s finished after being evicted (%s) — not releasing a lock it "
            "no longer holds.",
            owner_label, lease.revoked_reason or "superseded",
        )
        return
    _lock.release()
    log.info("Coordinator released by %s", owner_label)


def _reset_for_tests() -> None:
    """Drop all lock state between tests."""
    global _evictions, _last_eviction
    _set_lease(None)
    with _state_lock:
        _evictions, _last_eviction = 0, None
    if _lock.locked():
        try:
            _lock.release()
        except RuntimeError:
            pass
