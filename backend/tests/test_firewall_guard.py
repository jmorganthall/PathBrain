"""The firewall guard: every write ledgered, paced, budgeted, refusable — and a write that
times out is never reissued. Written after the reload-storm incident; each test pins one of
the rules that would have contained it."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from pathbrain import firewall_guard as fg
from pathbrain import session_runtime
from pathbrain.database import session_scope
from pathbrain.models import FirewallGuardState, FirewallWrite
from pathbrain.providers import get_provider
from pathbrain.providers.mock import _OVERRIDES, _PIPE_ENABLED
from pathbrain.session_runtime import FirewallUnavailable, ResilientProvider

from .faults import FaultyProvider


@pytest.fixture()
def guard(monkeypatch):
    """A clean guard with pacing off and a known config; tests override ``cfg`` as needed."""
    cfg = dict(fg.DEFAULTS, min_reconfigure_gap_s=0, max_reconfigures_per_hour=0, cooldown_after_outage_s=0)
    monkeypatch.setattr(fg, "config", lambda: dict(cfg))
    monkeypatch.setattr(fg, "_build_sha", lambda: "")
    monkeypatch.setattr(session_runtime, "FIREWALL_BACKOFF_S", (0.0, 0.0))   # reads still retry, without the wait
    def reset():
        with session_scope() as s:
            for r in s.scalars(select(FirewallWrite)).all():
                s.delete(r)
            row = s.get(FirewallGuardState, 1)
            if row is not None:
                s.delete(row)
        fg._last_ok_stamp = 0.0
        fg._outage_pending = False

    # The mock provider's state is shared by the whole suite: save it and put it back, so a
    # value written here never changes what a later test discovers.
    saved = dict(_OVERRIDES)
    saved_enabled = dict(_PIPE_ENABLED)
    reset()
    _OVERRIDES.clear()
    yield cfg
    # Leave the guard armed and clean: a hands-off left behind would refuse every later
    # test's writes, which is exactly what it is for and exactly what a suite must not see.
    reset()
    _OVERRIDES.clear()
    _OVERRIDES.update(saved)
    _PIPE_ENABLED.clear()
    _PIPE_ENABLED.update(saved_enabled)


def _ledger() -> list[dict]:
    return list(reversed(fg.recent_writes(1000)))


def _provider() -> tuple[ResilientProvider, FaultyProvider]:
    inner = FaultyProvider()
    return ResilientProvider(inner), inner


CH = {"pipe_uuid": None, "param": "quantum", "value": 1514}


# ── 1. the ledger ──────────────────────────────────────────────────────────────


def test_every_write_lands_on_the_ledger_with_its_cost(guard):
    p, inner = _provider()
    p.apply(CH)
    p.apply_many([CH, {"pipe_uuid": None, "param": "limit", "value": 1000}])
    p.set_pipe_enabled(None, False)
    rows = _ledger()
    assert [r["op"] for r in rows] == ["apply", "apply_many", "set_pipe_enabled"]
    assert all(r["outcome"] == "ok" for r in rows)
    assert [r["reconfigures"] for r in rows] == [1, 1, 1]      # a switch costs ONE reload
    assert rows[1]["field"] == "limit,quantum" and rows[0]["value"] == "1514"
    assert all(r["latency_ms"] is not None for r in rows)
    assert inner.reconfigures == 3


def test_a_wrong_request_is_recorded_as_failed_and_never_trips(guard):
    p, inner = _provider()
    inner.fail_next("400")
    with pytest.raises(httpx.HTTPStatusError):
        p.apply(CH)
    assert _ledger()[-1]["outcome"] == "failed"
    assert fg.state()["hands_off"] is False


# ── 2. hands-off is persistent, universal, and refuses restores too ────────────


def test_hands_off_refuses_every_write_and_records_the_refusal(guard):
    p, inner = _provider()
    fg.hands_off("network under investigation")
    for call in (lambda: p.apply(CH), lambda: p.apply_many([CH]), lambda: p.set_pipe_enabled(None, True)):
        with pytest.raises(fg.FirewallHandsOff) as ei:
            call()
        assert "network under investigation" in ei.value.reason
    assert inner.reconfigures == 0 and not [c for c in inner.calls if c != "discover"]
    st = fg.state()
    assert st["hands_off"] and st["kind"] == "manual" and st["refused_count"] == 3
    assert [r["outcome"] for r in _ledger()] == ["refused"] * 3
    # Reads still work — measurement is never what hands-off stops.
    assert p.discover()
    # Only arming clears it, and arming stamps the build.
    fg.arm()
    assert fg.state()["hands_off"] is False
    p.apply(CH)
    assert inner.reconfigures == 1


def test_hands_off_survives_a_restart(guard):
    fg.hands_off("set before a restart")
    # A fresh read of the state row (what a new process would do) still says hands-off.
    assert fg.state()["hands_off"] and fg.state()["reason"] == "set before a restart"
    assert fg.startup_check()["hands_off"]


def test_describe_failure_says_nothing_was_written(guard):
    text = session_runtime.describe_failure(fg.FirewallHandsOff("hands-off: because", kind="manual"))
    assert "Nothing was written" in text and "restore" in text and "because" in text


# ── 3. outage → trip; cooldown after the firewall returns ─────────────────────


def test_an_outage_on_a_write_trips_hands_off_and_no_write_is_reissued(guard):
    p, inner = _provider()
    inner.fail_next("connect")            # the box is rebooting: the write cannot reach it
    inner.dark(2)                          # ...and the verifying re-read fails too
    with pytest.raises(FirewallUnavailable):
        p.apply({"pipe_uuid": None, "param": "quantum", "value": 777})   # a value the mock is NOT on
    assert inner.calls.count("apply") == 1                   # ONE attempt, never a retry
    st = fg.state()
    assert st["hands_off"] and st["kind"] == "outage" and st["unreachable_since"]
    assert _ledger()[-1]["outcome"] == "failed"
    # Every later write is refused until a person arms — a baseline restore included.
    with pytest.raises(fg.FirewallHandsOff):
        p.apply_many([CH])
    assert inner.reconfigures == 0


def test_an_outage_seen_on_a_read_trips_too(guard, monkeypatch):
    monkeypatch.setattr(session_runtime, "FIREWALL_BACKOFF_S", (0.0, 0.0))
    p, inner = _provider()
    inner.dark(10)
    with pytest.raises(FirewallUnavailable):
        p.discover()
    assert fg.state()["hands_off"] and fg.state()["kind"] == "outage"


def test_writes_cool_down_after_the_firewall_comes_back(guard):
    guard["cooldown_after_outage_s"] = 300
    p, inner = _provider()
    inner.dark(3)                            # exactly the read's three attempts
    with pytest.raises(FirewallUnavailable):
        p.discover()
    fg.arm()                                # a person arms straight after the reboot
    assert p.discover()                     # reachable again → reachable_since stamped
    with pytest.raises(fg.FirewallHandsOff) as ei:
        p.apply(CH)
    assert ei.value.kind == "cooldown" and "cooldown" in ei.value.reason
    assert inner.reconfigures == 0
    # ...and once the cooldown has elapsed the same write goes through.
    with session_scope() as s:
        row = s.get(FirewallGuardState, 1)
        row.reachable_since = datetime.now(timezone.utc) - timedelta(seconds=301)
    p.apply(CH)
    assert inner.reconfigures == 1


# ── 4. a timed-out write is verified by re-reading, never reissued ────────────


def test_a_timed_out_write_that_took_is_verified_not_reissued(guard):
    p, inner = _provider()
    inner.fail_next("timeout", land=True)   # the reconfigure ran; only the answer was late
    out = p.apply({"pipe_uuid": None, "param": "quantum", "value": 300})
    assert out["ok"] and out.get("verified_after_timeout") is True
    assert inner.calls.count("apply") == 1 and inner.calls.count("discover") >= 1
    assert inner.reconfigures == 1                            # exactly one reload reached the box
    assert _ledger()[-1]["outcome"] == "verified"
    assert fg.state()["hands_off"] is False


def test_a_timed_out_write_that_did_not_take_is_an_outage(guard):
    p, inner = _provider()
    inner.fail_next("timeout", land=False)
    with pytest.raises(FirewallUnavailable):
        p.apply({"pipe_uuid": None, "param": "quantum", "value": 300})
    assert inner.calls.count("apply") == 1
    assert fg.state()["hands_off"] and fg.state()["kind"] == "outage"


def test_a_502_on_a_write_is_never_retried_either(guard):
    p, inner = _provider()
    inner.fail_next("502", land=False)
    with pytest.raises(FirewallUnavailable):
        p.set_pipe_enabled(None, False)
    assert inner.calls.count("set_pipe_enabled") == 1


# ── 5. pacing and budget ───────────────────────────────────────────────────────


def test_the_minimum_gap_is_waited_out_not_skipped(guard, monkeypatch):
    guard["min_reconfigure_gap_s"] = 20
    waited: list[float] = []
    p, inner = _provider()
    p.apply(CH)
    fg.before_write("apply", [CH], reconfigures=1, sleep=waited.append)
    assert len(waited) == 1 and 18 < waited[0] <= 20
    # A gap that would mean minutes on the pipeline is refused instead of slept.
    guard["min_reconfigure_gap_s"] = fg.MAX_GAP_WAIT_S + 60
    with pytest.raises(fg.FirewallHandsOff) as ei:
        fg.before_write("apply", [CH], reconfigures=1, sleep=waited.append)
    assert ei.value.kind == "gap" and len(waited) == 1


def test_the_hourly_budget_trips_hands_off_and_stops_the_session(guard):
    guard["max_reconfigures_per_hour"] = 3
    p, inner = _provider()
    for _ in range(3):
        p.apply(CH)
    with pytest.raises(fg.FirewallHandsOff) as ei:
        p.apply(CH)
    assert ei.value.kind == "budget" and "3 reconfigures" in ei.value.reason
    st = fg.state()
    assert st["hands_off"] and st["kind"] == "budget"
    assert inner.reconfigures == 3
    assert fg.summary()["reconfigures_last_hour"] == 3


# ── 6. a new build starts hands-off ───────────────────────────────────────────


def test_a_new_build_starts_hands_off_until_armed(guard, monkeypatch):
    monkeypatch.setattr(fg, "_build_sha", lambda: "abc1234def")
    st = fg.startup_check()
    assert st["hands_off"] and st["kind"] == "deploy" and "abc1234" in st["reason"]
    p, inner = _provider()
    with pytest.raises(fg.FirewallHandsOff):
        p.apply(CH)
    fg.arm()
    assert fg.state()["armed_sha"] == "abc1234def"
    # The same build restarting (a crash, a compose restart) does NOT trip again.
    assert fg.startup_check()["hands_off"] is False
    # A different build does.
    monkeypatch.setattr(fg, "_build_sha", lambda: "fedcba9876")
    assert fg.startup_check()["hands_off"]


def test_a_dev_build_with_no_sha_is_left_alone(guard, monkeypatch):
    monkeypatch.setattr(fg, "_build_sha", lambda: "")
    assert fg.startup_check()["hands_off"] is False


def test_the_deploy_gate_can_be_switched_off(guard, monkeypatch):
    guard["arm_required_after_deploy"] = False
    monkeypatch.setattr(fg, "_build_sha", lambda: "abc1234def")
    assert fg.startup_check()["hands_off"] is False


# ── 7. the chokepoint: every provider is the guarded one ───────────────────────


def test_get_provider_is_always_the_guarded_wrapper():
    assert isinstance(get_provider(), ResilientProvider)


def test_no_module_builds_a_raw_provider_or_bypasses_apply_many():
    """A regression on the write path has to go through ``session_runtime``: nothing outside
    the providers package constructs a provider directly, and no engine writes a profile
    switch as a per-field loop."""
    root = Path(__file__).resolve().parents[1] / "pathbrain"
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        text = path.read_text()
        if not rel.startswith("providers/") and re.search(r"\b(OPNsenseProvider|MockProvider)\(", text):
            offenders.append(f"{rel}: constructs a provider directly")
        if rel not in ("profile_test.py",) and re.search(r"for \w+ in \w+:\s*\n\s*provider\.apply\(", text):
            offenders.append(f"{rel}: applies a change list one field at a time")
    assert not offenders, offenders


# ── 8. the API ─────────────────────────────────────────────────────────────────


def test_api_guard_status_arm_and_hands_off(client, guard):
    body = client.get("/api/firewall/guard").json()
    assert body["hands_off"] is False and "reconfigures_last_hour" in body and body["writes"] == []
    r = client.post("/api/firewall/guard/hands-off", json={"reason": "testing"}).json()
    assert r["hands_off"] and r["reason"] == "testing"
    assert client.get("/api/health/pipeline").json()["firewall"]["hands_off"] is True
    assert client.post("/api/firewall/guard/arm").json()["hands_off"] is False


# ── 9. OPNsense: one reconfigure per switch, however many fields ───────────────


def test_opnsense_apply_many_reconfigures_once(monkeypatch):
    from pathbrain.providers.opnsense import OPNsenseProvider

    posts: list[str] = []
    settings = {"ts": {"pipes": {"pipe": {
        "u-down": {"fqcodel_quantum": "1514", "fqcodel_limit": "10240", "codel_target": {"5": {"value": "5ms", "selected": 1}}},
        "u-up": {"fqcodel_quantum": "300", "fqcodel_limit": "10240", "codel_target": {"5": {"value": "5ms", "selected": 1}}},
    }}}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=settings)
        posts.append(request.url.path)
        return httpx.Response(200, json={"result": "saved"})

    prov = OPNsenseProvider(base_url="https://fw.test", api_key="k", api_secret="s")
    monkeypatch.setattr(prov, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fw.test"))
    out = prov.apply_many([
        {"pipe_uuid": "u-down", "param": "quantum", "value": 1000},
        {"pipe_uuid": "u-down", "param": "limit", "value": 2000},
        {"pipe_uuid": "u-up", "param": "quantum", "value": 500},
    ])
    assert out["reconfigures"] == 1 and len(out["applied"]) == 3
    assert posts.count("/api/trafficshaper/service/reconfigure") == 1
    assert sorted(p for p in posts if "setPipe" in p) == ["/api/trafficshaper/settings/setPipe/u-down", "/api/trafficshaper/settings/setPipe/u-up"]
    # The single-field apply is the same path with one change: still one reload.
    posts.clear()
    prov.apply({"pipe_uuid": "u-up", "param": "quantum", "value": 600})
    assert posts.count("/api/trafficshaper/service/reconfigure") == 1
