"""The methodology's judgement block (one chain, S13): every number that decides the crown.

A version that states a judgement block is read from it alone; a version that states none
(v20 and earlier) is read exactly as before, from the settings and constants."""
from __future__ import annotations

import copy

import pytest

from pathbrain import crowning, duel, methodology
from pathbrain.config_store import DEFAULT_CONFIG
from pathbrain.overall_ranking import MIN_RING_ROUNDS_FOR_CROWN

V21_LIKE = {
    "crowned_by": "ring",
    "ring_crown_rule": "lineal",
    "ring_min_margin": 0.0,
    "tie_sigma": 2.0,
    "tie_min_margin": 0.5,
    "min_ring_rounds": 8,
    "speed_budget": {"enabled": True, "lcp_pct": 20.0, "fcp_pct": 25.0},
    "budget_judged_on": "ring",
    "allow_unshaped_crown": False,
}


def _cfg():
    return copy.deepcopy(DEFAULT_CONFIG)


@pytest.mark.parametrize("version", [
    "speed-smoothness-v17", "speed-smoothness-v18", "speed-smoothness-v19", "speed-smoothness-v20",
])
def test_versions_before_the_block_read_exactly_what_they_always_read(version):
    cfg = _cfg()
    j = methodology.judgement(version, methodology.METHODOLOGY_REGISTRY[version], cfg)
    assert j.source == "settings"
    assert j.crowned_by == "policy"
    assert j.ring_crown_rule == duel.crown_rule(cfg["duel"])
    assert j.tie_sigma == float(cfg["correlation"]["crown_tie_sigma"] or 2.0)
    assert j.tie_min_margin == float(cfg["correlation"]["crown_tie_min_margin"] or 0.0)
    assert j.min_ring_rounds == MIN_RING_ROUNDS_FOR_CROWN
    budget = crowning.speed_budget(cfg)
    assert (j.speed_budget.enabled, j.speed_budget.lcp_pct, j.speed_budget.fcp_pct) == (
        budget["enabled"], budget["lcp_pct"], budget["fcp_pct"])
    assert j.budget_judged_on == "pooled"
    assert j.allow_unshaped_crown is False


def test_without_a_block_a_setting_still_moves_the_number():
    cfg = _cfg()
    cfg["crown_follow"]["speed_budget"] = {"enabled": True, "lcp_pct": 40.0, "fcp_pct": 30.0}
    cfg["correlation"]["crown_tie_sigma"] = 3.0
    j = methodology.judgement("speed-smoothness-v20", {}, cfg)
    assert j.speed_budget.lcp_pct == 40.0
    assert j.tie_sigma == 3.0


def test_with_a_block_no_setting_can_move_the_crown():
    cfg = _cfg()
    cfg["crown_follow"]["speed_budget"] = {"enabled": False, "lcp_pct": 90.0, "fcp_pct": 90.0}
    cfg["correlation"]["crown_tie_sigma"] = 9.0
    cfg["duel"]["min_margin"] = 5.0
    j = methodology.judgement("v21-test", {"judgement": V21_LIKE}, cfg)
    assert j.source == "methodology"
    assert j.crowned_by == "ring"
    assert j.ring_min_margin == 0.0  # a win is a win
    assert j.tie_sigma == 2.0
    assert j.speed_budget.enabled is True and j.speed_budget.lcp_pct == 20.0
    assert j.budget_judged_on == "ring"


def test_a_block_must_state_every_number():
    partial = {k: v for k, v in V21_LIKE.items() if k != "tie_sigma"}
    assert methodology.judgement_problems(partial) == ["missing tie_sigma"]
    with pytest.raises(ValueError, match="missing tie_sigma"):
        methodology.judgement("v21-test", {"judgement": partial}, _cfg())
    assert "unknown hold_rule" in methodology.judgement_problems({**V21_LIKE, "hold_rule": True})


@pytest.mark.parametrize("key,value", [
    ("crowned_by", "pooled"),
    ("budget_judged_on", "vibes"),
    ("ring_crown_rule", "coin"),
    ("tie_sigma", -1),
    ("min_ring_rounds", 0),
    ("allow_unshaped_crown", "no"),
    ("speed_budget", {"enabled": True, "lcp_pct": 20.0}),
    ("speed_budget", {"enabled": True, "lcp_pct": 500.0, "fcp_pct": 25.0}),
])
def test_a_block_with_a_bad_number_is_refused(key, value):
    assert methodology.judgement_problems({**V21_LIKE, key: value})


def test_the_judgement_is_a_reading_so_it_keeps_the_scale():
    """A version that changes only its judgement grades every run alike, so ring rounds and
    scores carry over (`same_scale`) and publishing it needs no re-grade."""
    assert "judgement" in methodology.READ_ONLY_KEYS


def test_every_registry_block_is_whole():
    for version, spec in methodology.METHODOLOGY_REGISTRY.items():
        if "judgement" in spec:
            assert not methodology.judgement_problems(spec["judgement"]), version
