"""The job runtime: a firewall call is retried before it fails anything, a failure is
described in words, and a run of failures is counted where it happens."""
from __future__ import annotations

import httpx
import pytest

from pathbrain import coordinator, session_runtime as rt
from pathbrain.plugins.benchmark_browser import warm_load_wanted
from pathbrain.providers import get_provider
from pathbrain.providers.mock import MockProvider


def _timeout(msg: str = "timed out") -> httpx.ReadTimeout:
    return httpx.ReadTimeout(msg, request=httpx.Request("GET", "https://fw/api"))


def test_a_transient_error_is_retried_and_the_call_then_succeeds():
    calls: list[int] = []
    slept: list[float] = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise _timeout()
        return {"ok": True}

    out = rt.call_firewall("apply", flaky, sleep=slept.append)
    assert out == {"ok": True}
    assert len(calls) == 3
    assert slept == list(rt.FIREWALL_BACKOFF_S[:2])


def test_spent_attempts_raise_a_typed_error_that_says_what_was_tried():
    calls: list[int] = []

    def dead():
        calls.append(1)
        raise _timeout()

    with pytest.raises(rt.FirewallUnavailable) as info:
        rt.call_firewall("apply", dead, sleep=lambda s: None)
    assert len(calls) == rt.FIREWALL_ATTEMPTS
    err = info.value
    assert err.op == "apply" and err.attempts == rt.FIREWALL_ATTEMPTS
    assert isinstance(err.last, httpx.ReadTimeout)
    assert "did not answer 'apply'" in str(err) and "ReadTimeout" in str(err)


def test_a_wrong_request_is_never_retried():
    calls: list[int] = []

    def wrong():
        calls.append(1)
        raise ValueError("Unknown/unsupported param 'bogus'")

    with pytest.raises(ValueError):
        rt.call_firewall("apply", wrong, sleep=lambda s: None)
    assert len(calls) == 1
    # A 4xx is the request being wrong; a 5xx is the firewall mid-reconfigure.
    req = httpx.Request("POST", "https://fw/api")
    bad = httpx.HTTPStatusError("400", request=req, response=httpx.Response(400, request=req))
    busy = httpx.HTTPStatusError("503", request=req, response=httpx.Response(503, request=req))
    assert not rt.is_transient(bad) and rt.is_transient(busy)
    assert rt.is_transient(httpx.ConnectError("refused", request=req))


def test_every_engine_gets_the_resilient_provider_through_the_one_seam():
    provider = get_provider()
    assert isinstance(provider, rt.ResilientProvider)
    assert isinstance(provider.inner, MockProvider)
    assert provider.name == provider.inner.name
    assert rt.resilient(provider) is provider  # idempotent
    # Behaves as the provider it wraps: reads through, writes through.
    assert provider.discover() == provider.inner.discover()
    assert provider.writable_fields() == provider.inner.writable_fields()


def test_the_wrapped_provider_retries_reads_but_never_reissues_a_write(monkeypatch):
    """Reads are retried. A WRITE is attempted once: a timed-out reconfigure may still be
    running inside the firewall, and reissuing it put two shaper reloads in flight at once
    (the reload-storm incident). The wrapper re-reads instead and reports the write as
    verified when it took."""
    from pathbrain import firewall_guard as fg

    monkeypatch.setattr(fg, "config", lambda: dict(fg.DEFAULTS, min_reconfigure_gap_s=0,
                                                   max_reconfigures_per_hour=0, cooldown_after_outage_s=0))
    fg.arm()
    inner = MockProvider()
    attempts: list[int] = []
    real_apply = inner.apply

    def apply_late_but_landed(changes):
        attempts.append(1)
        real_apply(changes)          # the write reached the firewall...
        raise _timeout()             # ...only the answer was late

    monkeypatch.setattr(inner, "apply", apply_late_but_landed)
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    wrapped = rt.resilient(inner)
    pipe = inner.discover()[0]
    out = wrapped.apply({"pipe_uuid": pipe.extra.get("uuid"), "param": "quantum", "value": 1234})
    assert out.get("ok") is True and out.get("verified_after_timeout") is True
    assert len(attempts) == 1                     # ONE attempt — never a second reload

    # A read still gets the retry policy.
    reads: list[int] = []
    real_discover = inner.discover

    def discover_once_late():
        reads.append(1)
        if len(reads) == 1:
            raise _timeout()
        return real_discover()

    monkeypatch.setattr(inner, "discover", discover_once_late)
    assert wrapped.discover() and len(reads) == 2


def test_failures_are_described_in_words():
    fw = rt.FirewallUnavailable("apply", 3, _timeout())
    text = rt.describe_failure(fw)
    assert text.startswith("The firewall stopped answering") and "settings were restored" in text
    from types import SimpleNamespace

    revoked = coordinator.LeaseRevoked(SimpleNamespace(label="duel#4", revoked_reason="evicted"))
    stood = rt.describe_failure(revoked)
    assert stood.startswith("Stood down mid-session") and "duel#4" in stood
    assert rt.describe_failure(RuntimeError("Could not reach profile")) == "RuntimeError: Could not reach profile"
    assert rt.describe_failure(RuntimeError("")) == "RuntimeError"
    assert rt.describe_failure(rt.SessionAbort("3 legs in a row could not be applied.")) == (
        "3 legs in a row could not be applied."
    )


def test_a_failure_streak_trips_at_its_bar_and_clears_on_success():
    streak = rt.FailureStreak(3)
    assert streak.hit("a") is False and streak.hit("b") is False
    streak.clear()
    assert streak.count == 0 and streak.last is None
    assert [streak.hit(w) for w in ("x", "y", "z")] == [False, False, True]
    assert streak.last == "z"


def test_warm_loads_run_on_the_first_iteration_only_unless_asked_for_every():
    """A warm load doubles an iteration's page loads; the default pays it once per run."""
    assert warm_load_wanted({"warm_loads": True, "_iteration": 0}) is True
    assert warm_load_wanted({"warm_loads": True, "_iteration": 1}) is False
    assert warm_load_wanted({"warm_loads": True}) is True  # unstamped = a first
    assert warm_load_wanted({"warm_loads": "every", "_iteration": 4}) is True
    assert warm_load_wanted({"warm_loads": "first", "_iteration": 2}) is False
    assert warm_load_wanted({"warm_loads": False, "_iteration": 0}) is False
    assert warm_load_wanted({"warm_loads": "off", "_iteration": 0}) is False
    assert warm_load_wanted({}) is True
