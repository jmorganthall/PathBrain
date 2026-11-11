"""The speed budget: Auto-apply never follows a crown that is clearly much slower than the
field's fastest LCP or FCP; it follows the first crown that fits, and says so."""
from __future__ import annotations

import pytest

from pathbrain import crowning
from pathbrain.config_store import get_config, save_config
from pathbrain.database import session_scope

FASTEST = {"profiles": 3,
           "fcp": {"fingerprint": "f", "name": None, "label": None, "ms": 200.0, "iterations": 40},
           "lcp": {"fingerprint": "l", "name": None, "label": None, "ms": 500.0, "iterations": 40}}


def _gap(pct: float, clear: bool = True) -> dict:
    return {"ms": 0.0, "fastest_ms": 0.0, "delta_ms": pct * 5, "pct": pct, "bar_ms": 10.0,
            "clear": clear, "is_fastest": pct == 0}


@pytest.fixture
def field(monkeypatch):
    """Gaps per fingerprint, and what each policy names."""
    gaps: dict[str, dict] = {}
    names: dict[str, str | None] = {}

    from pathbrain import pooled_field

    monkeypatch.setattr(pooled_field, "fastest_legs", lambda *a, **k: FASTEST)
    monkeypatch.setattr(pooled_field, "leg_gaps",
                        lambda s, v, f, fps, **k: {fp: gaps[fp] for fp in fps if fp in gaps})

    def _resolve(session, pooled_best_fp, policy):
        fp = names.get(policy)
        return {"policy": policy, "fingerprint": fp, "source": policy,
                "detail": f"{policy} detail", "duel_champion": None}

    monkeypatch.setattr(crowning, "_resolve_policy", _resolve)
    with session_scope() as s:
        save_config(s, {"crown_follow": {"policy": "fused"}})
    yield gaps, names
    with session_scope() as s:
        save_config(s, {"crown_follow": {"policy": "fused",
                                         "speed_budget": dict(crowning.DEFAULT_SPEED_BUDGET)}})


def test_the_default_budget_is_twenty_on_lcp_and_twenty_five_on_fcp():
    with session_scope() as s:
        assert crowning.speed_budget(get_config(s)) == {"enabled": True, "lcp_pct": 20.0, "fcp_pct": 25.0}


def test_a_crown_twice_as_slow_is_passed_over_for_the_first_crown_within_budget(field):
    gaps, names = field
    names.update({"fused": "slow", "lcp_first": "quick", "pooled": "slow"})
    gaps["slow"] = {"lcp": _gap(100.0), "fcp": _gap(10.0)}
    gaps["quick"] = {"lcp": _gap(5.0), "fcp": _gap(20.0)}
    with session_scope() as s:
        out = crowning.resolve(s, "slow")
    assert out["fingerprint"] == "quick" and out["source"] == "lcp_first"
    assert out["policy"] == "fused"
    assert out["speed_budget"]["replaced"] == {"fingerprint": "slow", "source": "fused"}
    assert "LCP +100% slower than the fastest (budget 20%)" in out["detail"]


def test_a_gap_inside_the_noise_never_trips_the_budget(field):
    gaps, names = field
    names["fused"] = "noisy"
    gaps["noisy"] = {"lcp": _gap(60.0, clear=False), "fcp": _gap(0.0)}
    with session_scope() as s:
        out = crowning.resolve(s, "noisy")
    assert out["fingerprint"] == "noisy" and out["speed_budget"]["within"] is True


def test_fcp_has_its_own_limit(field):
    gaps, names = field
    names.update({"fused": "a", "lcp_first": "b"})
    gaps["a"] = {"lcp": _gap(0.0), "fcp": _gap(24.0)}       # under 25: within
    with session_scope() as s:
        assert crowning.resolve(s, "a")["fingerprint"] == "a"
    gaps["a"]["fcp"] = _gap(30.0)                              # over 25: replaced
    gaps["b"] = {"lcp": _gap(15.0), "fcp": _gap(5.0)}
    with session_scope() as s:
        assert crowning.resolve(s, "a")["fingerprint"] == "b"


def test_when_nothing_fits_the_policys_choice_stands_and_says_so(field):
    gaps, names = field
    names.update({"fused": "slow", "lcp_first": "slower"})
    gaps["slow"] = {"lcp": _gap(80.0), "fcp": _gap(0.0)}
    gaps["slower"] = {"lcp": _gap(90.0), "fcp": _gap(0.0)}
    with session_scope() as s:
        out = crowning.resolve(s, "slow")
    assert out["fingerprint"] == "slow"
    assert "no other crown fits it either" in out["detail"]
    assert out["speed_budget"]["replaced"] is None


def test_a_disabled_budget_changes_nothing(field):
    gaps, names = field
    names.update({"fused": "slow", "lcp_first": "quick"})
    gaps["slow"] = {"lcp": _gap(100.0), "fcp": _gap(0.0)}
    with session_scope() as s:
        save_config(s, {"crown_follow": {"speed_budget": {"enabled": False, "lcp_pct": 20, "fcp_pct": 25}}})
        assert crowning.resolve(s, "slow")["fingerprint"] == "slow"


def test_the_budget_is_settable_through_the_api(client):
    body = client.post("/api/settings/crown-follow",
                       json={"speed_budget": {"lcp_pct": 15, "fcp_pct": 30}}).json()
    assert body["config"]["speed_budget"] == {"enabled": True, "lcp_pct": 15.0, "fcp_pct": 30.0}
    assert client.post("/api/settings/crown-follow",
                       json={"speed_budget": {"lcp_pct": -1}}).status_code == 400
    client.post("/api/settings/crown-follow", json={"speed_budget": dict(crowning.DEFAULT_SPEED_BUDGET)})
