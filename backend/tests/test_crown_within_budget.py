"""Methodology v20: v19 with the crown kept within the speed budget.

Before it, the crown was the fused #1 however slow it was, and the speed budget only told
Auto-apply to follow something else, so the dashboard could crown one profile while the
firewall ran another. What is pinned: v20 ships as a candidate on v17's scale; under its
rule a profile clearly over the budget is not eligible for the crown, the crown is the best
within it and Auto-apply follows that same profile; "cannot tell" never refuses a crown;
when nobody is within the budget the fused #1 stands; and without the rule nothing changes.
"""
from __future__ import annotations

import copy

import pytest

from pathbrain import crowning, methodology, overall_ranking as o
from pathbrain.config_store import save_config
from pathbrain.database import session_scope
from pathbrain.methodology import (
    CURRENT_METHODOLOGY, METHODOLOGY_REGISTRY, build_definition_from_spec, crown_within_speed_budget,
    pooled_read, same_scale, scale_of,
)
from pathbrain.models import Methodology
from tests.test_pooled_field import VERSION, _add, _fp, _own_field  # noqa: F401

V17, V19, V20 = "speed-smoothness-v17", "speed-smoothness-v19", "speed-smoothness-v20"


def test_v20_is_a_candidate_on_v17_s_scale_that_keeps_the_crown_within_budget():
    assert CURRENT_METHODOLOGY != V20
    assert scale_of(V20) == V17 and same_scale(V20, V19) and same_scale(V20, V17)
    d19 = build_definition_from_spec(METHODOLOGY_REGISTRY[V19])
    d20 = build_definition_from_spec(METHODOLOGY_REGISTRY[V20])
    assert crown_within_speed_budget(d20) and not crown_within_speed_budget(d19)
    assert not crown_within_speed_budget(None) and not crown_within_speed_budget({})
    assert pooled_read(d20) == "epochs"
    # Same grading as v19: only the ranking block differs.
    assert {k: v for k, v in d20.items() if k != "ranking"} == {k: v for k, v in d19.items() if k != "ranking"}
    assert methodology.verdict_rules(V20) == methodology.verdict_rules(V19)


def test_the_page_offers_v20_and_says_what_it_does(client):
    cands = {c["version"]: c for c in client.get("/api/methodologies").json().get("candidates") or []}
    assert V20 in cands and cands[V20]["crown_within_speed_budget"] is True
    assert cands[V20]["pooled_read"] == "epochs" and cands[V20]["same_scale_as"] == V17


def _check(over_pct: float | None) -> dict | None:
    if over_pct is None:
        return None
    if over_pct <= 0:
        return {"within": True, "over": [], "gaps": {}}
    return {"within": False, "over": [{"leg": "lcp", "pct": over_pct, "limit_pct": 10.0,
                                       "delta_ms": over_pct * 10}], "gaps": {}}


@pytest.fixture
def budget(monkeypatch):
    """Which profiles the speed budget says are over it, by name; and this module's version
    keeping the crown within the budget, as v20 does."""
    over: dict[str, float | None] = {}

    def fake(session, fps, budget=None):
        return {fp: _check(over.get(fp.removeprefix("verdict-test-"), 0.0)) for fp in fps}

    monkeypatch.setattr(crowning, "budget_checks", fake)
    with session_scope() as s:
        save_config(s, {"crown_follow": {"policy": "fused", "speed_budget": {"enabled": True, "lcp_pct": 10, "fcp_pct": 20}}})
        row = s.get(Methodology, VERSION)
        saved = copy.deepcopy(row.definition)
        row.definition = {**copy.deepcopy(row.definition), "ranking": {"crown_within_speed_budget": True}}
    yield over
    with session_scope() as s:
        s.get(Methodology, VERSION).definition = saved
        save_config(s, {"crown_follow": {"policy": "fused",
                                         "speed_budget": dict(crowning.DEFAULT_SPEED_BUDGET)}})


def test_a_profile_over_the_budget_cannot_wear_the_crown(budget):
    _add("slow_best", 80.0, runs=8, spread=1.0)
    _add("quick", 75.0, runs=8, spread=1.0)
    _add("quicker_but_worse", 70.0, runs=8, spread=1.0)
    budget.update({"slow_best": 145.0})
    with session_scope() as s:
        r = o.ranking(s, backtest=False, live=False)
        rows = {p["fingerprint"]: p for p in r["profiles"]}
        assert r["crown_within_budget"] is True and r["preview"] is False
        assert r["best"]["fingerprint"] == _fp("quick")
        assert rows[_fp("slow_best")]["eligible"] is False
        assert rows[_fp("slow_best")]["over_speed_budget"][0]["pct"] == 145.0
        assert [p["fingerprint"] for p in r["crown_budget"]["passed_over"]] == [_fp("slow_best")]
        assert "over the speed budget" in r["verdict"]
        # The crown and Auto-apply name one profile, and Auto-apply needs no fallback.
        assert o.crown(s)["fingerprint"] == _fp("quick")
        followed = crowning.resolve(s, pooled_best_fp=_fp("slow_best"))
        assert followed["fingerprint"] == _fp("quick") and followed["source"] == "fused"


def test_cannot_tell_never_refuses_the_crown(budget):
    _add("unread", 80.0, runs=8, spread=1.0)
    _add("other", 75.0, runs=8, spread=1.0)
    budget.update({"unread": None})
    with session_scope() as s:
        r = o.ranking(s, backtest=False, live=False)
    assert r["best"]["fingerprint"] == _fp("unread") and not r["crown_budget"]["passed_over"]


def test_when_nobody_is_within_the_budget_the_fused_number_one_stands(budget):
    _add("a", 80.0, runs=8, spread=1.0)
    _add("b", 75.0, runs=8, spread=1.0)
    budget.update({"a": 50.0, "b": 60.0})
    with session_scope() as s:
        r = o.ranking(s, backtest=False, live=False)
    assert r["best"]["fingerprint"] == _fp("a")
    assert r["crown_budget"]["none_within"] is True and r["crown_budget"]["applied"] is False


def test_without_the_rule_the_crown_is_the_fused_number_one_and_a_preview_can_apply_it(budget):
    _add("slow_best", 80.0, runs=8, spread=1.0)
    _add("quick", 75.0, runs=8, spread=1.0)
    budget.update({"slow_best": 145.0})
    with session_scope() as s:
        row = s.get(Methodology, VERSION)
        row.definition = {k: v for k, v in row.definition.items() if k != "ranking"}
    with session_scope() as s:
        r = o.ranking(s, backtest=False, live=False)
        assert r["crown_within_budget"] is False and r["crown_budget"] is None
        assert r["best"]["fingerprint"] == _fp("slow_best")
        p = o.ranking(s, backtest=False, live=False, budget=True)
        assert p["preview"] is True and p["best"]["fingerprint"] == _fp("quick")
        # A preview never moves the crown Auto-apply follows.
        assert o.crown(s)["fingerprint"] == _fp("slow_best")


def test_the_budget_check_reads_the_field_once_for_many_profiles(monkeypatch):
    from pathbrain import pooled_field

    calls = []
    fastest = {"profiles": 2, "fcp": {"fingerprint": "f", "ms": 200.0},
               "lcp": {"fingerprint": "l", "ms": 500.0}}

    def gap(pct):
        return {"pct": pct, "clear": True, "delta_ms": pct * 5}

    monkeypatch.setattr(pooled_field, "fastest_legs", lambda *a, **k: calls.append(1) or fastest)
    monkeypatch.setattr(pooled_field, "leg_gaps", lambda s, v, f, fps, **k: {
        "x": {"lcp": gap(5.0), "fcp": gap(0.0)}, "y": {"lcp": gap(40.0), "fcp": gap(0.0)}})
    with session_scope() as s:
        got = crowning.budget_checks(s, ["x", "y", "z"], {"enabled": True, "lcp_pct": 10.0, "fcp_pct": 20.0})
    assert len(calls) == 1
    assert got["x"]["within"] and not got["y"]["within"] and got["z"] is None


def test_the_crown_agreement_card_says_what_switching_to_v20_would_change(client, monkeypatch):
    """v19 and v20 read the record alike and grade alike; the card compares the crown with
    and without the budget rule, and says Auto-apply would follow the change."""
    def fake_ranking(_session, *, backtest=True, live=True, read=None, budget=None):
        names = ["quick", "slow"] if budget else ["slow", "quick"]
        rows = [{"fingerprint": fp, "name": fp.upper(), "eligible": True} for fp in names]
        return {"best": {**rows[0], "fused": 25.0}, "profiles": rows, "lead": 0.3,
                "noise_bar": 1.2, "tied_count": 0, "eras": {}}

    monkeypatch.setattr(o, "ranking", fake_ranking)
    body = client.get("/api/methodologies/crown-agreement", params={"a": V19, "b": V20}).json()
    assert body["read"]["a_crown_within_budget"] is False and body["read"]["b_crown_within_budget"] is True
    assert body["verdict"] == "disagree" and "within the speed budget" in body["text"]
    assert "QUICK" in body["text"] and "Auto-apply" in body["text"]
