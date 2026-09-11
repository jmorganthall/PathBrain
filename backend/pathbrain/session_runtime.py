"""The job runtime — what every session shares, in one module.

PathBrain runs ten kinds of firewall-and-benchmark session (the duel ladder, profile
tests, the challenger race, refreshes, sweeps, the baseline test, the current test, the
experiment, monitoring, manual runs). Each owns its own thread, its own status row and its
own stage line, and that is fine; what they must not each own is the *policy* for the
things that go wrong in every one of them the same way. Three of those policies live here.

**1. A firewall call is retried before it fails anything** (:func:`call_firewall`,
:class:`ResilientProvider`). Every write goes through ``provider.apply()`` and every read
through ``provider.discover()``; both are one HTTP round trip to OPNsense, and a
reconfigure on a busy firewall can take longer than any fixed timeout. The first cut let a
single ``ReadTimeout`` on one leg's apply fail an entire lever session — the failure the
Levers page showed twice in one evening as ``ReadTimeout: timed out``, 0 rounds. A timeout
or a dropped connection is retried with a short backoff (``FIREWALL_ATTEMPTS``,
``FIREWALL_BACKOFF_S``); a reconfigure that already took effect is re-applied harmlessly
(the same params set twice is one state). Only when every attempt fails does the call raise
:class:`FirewallUnavailable`, a typed error whose message says what was tried, so the
engine that catches it can decide whether it lost a leg or a session. Errors that are not
transport (a bad param, an unknown pipe, a 4xx) are never retried — retrying a wrong
request is a slower wrong request. ``get_provider()`` hands every engine the wrapped
provider, so no engine has to remember to do this.

**2. A failure is described in words, once** (:func:`describe_failure`). ``ReadTimeout:
timed out`` on a session card told a person nothing about what stopped or what to do; the
sentence a session records now names the class of failure and what was kept.

**3. A run of failures is counted where it happens** (:class:`FailureStreak`). An engine
that measures in legs or chunks decides per unit whether to skip and continue; the streak
is the one place "how many in a row before the session itself is the problem" is written.

Nothing here starts or schedules anything: ``job_queue`` owns admission, ``coordinator``
owns exclusion and the lease, and the engines own their lifecycle. This module is what
they all reach for at the point where the firewall answers late.
"""
from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from .logging_config import get_logger
from .providers.base import ConfigProvider, FqCodelConfig

log = get_logger(__name__)

#: Attempts per firewall call before it is declared unavailable (the first plus retries).
FIREWALL_ATTEMPTS = 3
#: Seconds to wait before the second and third attempts.
FIREWALL_BACKOFF_S: tuple[float, ...] = (2.0, 5.0)
#: Server-side statuses worth one more try: the firewall answered, but not as a server that
#: had finished what it was doing (a reconfigure in flight answers 502/503 from its proxy).
_RETRY_STATUSES = {502, 503, 504}


class FirewallUnavailable(RuntimeError):
    """The firewall did not answer a call after every allowed attempt.

    ``op`` is the provider method, ``attempts`` how many were made, ``last`` the final
    exception. The message is written for a session card, not a log line.
    """

    def __init__(self, op: str, attempts: int, last: BaseException) -> None:
        self.op = op
        self.attempts = attempts
        self.last = last
        super().__init__(
            f"The firewall did not answer '{op}' in {attempts} attempts "
            f"({type(last).__name__}: {last or 'no detail'})."
        )


class SessionAbort(RuntimeError):
    """A session stopped itself for a reason it can state in a sentence (too many legs in
    a row could not be applied, nothing left to race). The message IS the card's text —
    ``describe_failure`` passes it through without a class-name prefix."""


def is_transient(exc: BaseException) -> bool:
    """Would one more try plausibly succeed? Timeouts, dropped connections and a
    server-side 5xx are transport-level; everything else is the request being wrong."""
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response is not None and exc.response.status_code in _RETRY_STATUSES
    return False


def call_firewall(
    op: str,
    fn: Callable[..., Any],
    *args: Any,
    attempts: int | None = None,
    backoff_s: tuple[float, ...] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> Any:
    """Call ``fn`` and retry it on a transient transport error, up to ``attempts`` times.

    Raises :class:`FirewallUnavailable` once the attempts are spent, and re-raises any
    non-transient error immediately and unchanged.
    """
    # Resolved at call time so the module constants can be tuned (and patched in tests).
    attempts = max(1, int(FIREWALL_ATTEMPTS if attempts is None else attempts))
    backoff_s = FIREWALL_BACKOFF_S if backoff_s is None else backoff_s
    last: BaseException | None = None
    for n in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — classified below
            if not is_transient(exc):
                raise
            last = exc
            if n >= attempts:
                break
            wait = backoff_s[min(n - 1, len(backoff_s) - 1)] if backoff_s else 0.0
            log.warning(
                "Firewall '%s' failed (attempt %d/%d, %s: %s); retrying in %.0fs",
                op, n, attempts, type(exc).__name__, exc, wait,
            )
            if wait > 0:
                sleep(wait)
    assert last is not None
    log.error("Firewall '%s' unavailable after %d attempts: %s: %s", op, attempts, type(last).__name__, last)
    raise FirewallUnavailable(op, attempts, last) from last


class ResilientProvider(ConfigProvider):
    """A provider whose every call goes through :func:`call_firewall`.

    Wraps any :class:`ConfigProvider`; the wrapped provider's ``name`` and any attribute
    this class does not define are read straight through, so callers see the same object
    they always did — only its patience changed.
    """

    def __init__(self, inner: ConfigProvider) -> None:
        self._inner = inner

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._inner.name

    @property
    def inner(self) -> ConfigProvider:
        return self._inner

    def discover(self) -> list[FqCodelConfig]:
        return self._read("discover", self._inner.discover)

    def _read(self, op: str, fn: Callable[..., Any], *args: Any) -> Any:
        from . import firewall_guard

        try:
            out = call_firewall(op, fn, *args)
        except FirewallUnavailable as exc:
            firewall_guard.note_contact(False, str(exc.last) if hasattr(exc, "last") else str(exc))
            raise
        firewall_guard.note_contact(True)
        return out

    def snapshot(self) -> dict:
        return call_firewall("snapshot", self._inner.snapshot)

    def health(self) -> dict:
        return call_firewall("health", self._inner.health)

    def writable_fields(self) -> list[str]:
        return self._inner.writable_fields()

    def field_options(self) -> dict[str, list[float]]:
        return self._inner.field_options()

    def pipe_states(self) -> list[dict]:
        return self._read("pipe_states", self._inner.pipe_states)

    # ── writes: gated, paced, ledgered, never reissued ──────────────────────────
    #
    # Reads may be retried; a write may not. A reconfigure that timed out may still be
    # running inside the firewall, and reissuing it was the new behaviour that put two
    # shaper reloads in flight at once. So a write is attempted ONCE; if the transport
    # fails, the firewall is re-read and the write is either found to have taken
    # (``verified``) or the firewall is treated as gone and the guard trips hands-off.

    def set_pipe_enabled(self, pipe_uuid: str | None, enabled: bool) -> dict:
        def verify() -> bool:
            for st in self.pipe_states():
                if (st.get("uuid") or None) == (pipe_uuid or st.get("uuid")):
                    return bool(st.get("enabled")) == bool(enabled)
            return False
        return self._write("set_pipe_enabled", lambda: self._inner.set_pipe_enabled(pipe_uuid, enabled),
                           changes=[{"pipe_uuid": pipe_uuid, "param": "enabled", "value": enabled}],
                           reconfigures=1, verify=verify)

    def apply(self, changes: dict) -> dict:
        return self._write("apply", lambda: self._inner.apply(changes), changes=[dict(changes)],
                           reconfigures=1, verify=lambda: self._verify_applied([changes]))

    def apply_many(self, changes: list[dict], *, reload: bool = True) -> dict:
        batch = [dict(c) for c in changes]
        if not batch:
            return {"provider": self.name, "ok": True, "applied": [], "reconfigures": 0}
        # ``reload=False`` costs the firewall no shaper reload, and the ledger says so: the
        # pacing gap and the hourly budget are about reconfigures, and charging one for a
        # write that did not reload would ration the cheap half out of existence.
        return self._write("apply_many" if reload else "set_fields",
                           lambda: self._inner.apply_many(batch, reload=reload), changes=batch,
                           reconfigures=1 if reload else 0,
                           verify=lambda: self._verify_applied(batch))

    def reconfigure(self) -> dict:
        """The shaper reload on its own. A write like any other — ledgered, paced, budgeted,
        refusable, attempted once. There is nothing to verify by re-reading (no field
        changed), so a timeout is treated as the firewall being gone, which is the honest
        reading: a reload that did not answer is exactly the event this guard exists for."""
        return self._write("reconfigure", self._inner.reconfigure, changes=[],
                           reconfigures=1, verify=lambda: False)

    def _verify_applied(self, changes: list[dict]) -> bool:
        """Did every change take? Read back and compare numerically (``_field_equal``), never
        by reissuing. A read that fails is "could not tell", which is False."""
        from .settings_profile import _field_equal

        try:
            live = self.discover()
        except Exception:  # noqa: BLE001
            return False
        by_uuid = {(c.extra or {}).get("uuid"): c for c in live}
        first = live[0] if live else None
        for ch in changes:
            cfg = by_uuid.get(ch.get("pipe_uuid")) if ch.get("pipe_uuid") else first
            if cfg is None:
                return False
            if not _field_equal(str(ch.get("param")), cfg.to_dict().get(str(ch.get("param"))), ch.get("value")):
                return False
        return True

    def _write(self, op: str, fn: Callable[[], Any], *, changes: list[dict], reconfigures: int,
               verify: Callable[[], bool]) -> Any:
        from . import firewall_guard

        firewall_guard.before_write(op, changes, reconfigures=reconfigures)  # raises FirewallHandsOff
        t0 = time.monotonic()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 — classified below
            elapsed = (time.monotonic() - t0) * 1000.0
            if not is_transient(exc):
                firewall_guard.record(op, changes=changes, reconfigures=reconfigures, outcome="failed",
                                      error=f"{type(exc).__name__}: {exc}", latency_ms=elapsed)
                raise
            log.warning("Firewall '%s' did not answer (%s: %s); re-reading to see whether it took — never reissuing",
                        op, type(exc).__name__, exc)
            took = False
            try:
                took = bool(verify())
            except Exception:  # noqa: BLE001
                took = False
            if took:
                firewall_guard.record(op, changes=changes, reconfigures=reconfigures, outcome="verified",
                                      error=f"{type(exc).__name__}: {exc}", latency_ms=elapsed)
                firewall_guard.note_contact(True)
                return {"provider": self.name, "ok": True, "verified_after_timeout": True, "reconfigures": reconfigures}
            firewall_guard.record(op, changes=changes, reconfigures=reconfigures, outcome="failed",
                                  error=f"{type(exc).__name__}: {exc}", latency_ms=elapsed)
            firewall_guard.note_contact(False, f"{type(exc).__name__}: {exc}")
            raise FirewallUnavailable(op, 1, exc) from exc
        firewall_guard.record(op, changes=changes, reconfigures=reconfigures, outcome="ok",
                              latency_ms=(time.monotonic() - t0) * 1000.0)
        firewall_guard.note_contact(True)
        return result

    def __getattr__(self, item: str) -> Any:
        # Anything this wrapper does not define (a provider-specific helper, ``base_url``)
        # is the inner provider's, unchanged.
        return getattr(self._inner, item)


def resilient(provider: ConfigProvider) -> ConfigProvider:
    """``provider`` behind the retry policy (idempotent: a wrapped provider is returned as is)."""
    if isinstance(provider, ResilientProvider):
        return provider
    return ResilientProvider(provider)


def describe_failure(exc: BaseException) -> str:
    """A session card's sentence for ``exc``: what stopped and what it means.

    Class-aware for the failures a session meets by design (the firewall going quiet, a
    lease handed on, a cancel); the generic tail keeps the exception's own words so an
    unexpected failure is still diagnosable from the card.
    """
    from . import coordinator

    if isinstance(exc, SessionAbort):
        return str(exc)
    from .firewall_guard import FirewallHandsOff

    if isinstance(exc, FirewallHandsOff):
        return (
            f"Stopped by the firewall guard — {exc.reason} Nothing was written, including any "
            "restore: the firewall is exactly as it was before this write. Arm writes from the "
            "top bar once the network is known good."
        )
    if isinstance(exc, FirewallUnavailable):
        return (
            f"The firewall stopped answering: {exc} The session stopped early and your "
            "settings were restored. Anything already measured or decided is kept."
        )
    if isinstance(exc, coordinator.LeaseRevoked):
        return (
            "Stood down mid-session: the pipeline watchdog saw no progress for too long and "
            f"handed the pipeline on. Detail: {exc}"
        )
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


class FailureStreak:
    """Consecutive failures of one unit of work (a leg, a chunk), with the bar at which the
    session itself should give up. ``hit(why)`` records one and returns True when the bar
    is reached; ``clear()`` on any success."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self.count = 0
        self.last: str | None = None

    def hit(self, why: str) -> bool:
        self.count += 1
        self.last = why
        return self.count >= self.limit

    def clear(self) -> None:
        self.count = 0
        self.last = None


__all__ = [
    "FIREWALL_ATTEMPTS",
    "FIREWALL_BACKOFF_S",
    "FailureStreak",
    "FirewallUnavailable",
    "ResilientProvider",
    "SessionAbort",
    "call_firewall",
    "describe_failure",
    "is_transient",
    "resilient",
]
