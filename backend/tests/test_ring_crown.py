"""One chain, S18: under a methodology whose judgement says the ring crowns, the crown is the
ring's champion, kept within the speed budget on paired ring milliseconds. No policy, no
fallback chain, no pooled stand-in while the ring has an answer."""
from __future__ import annotations

from dataclasses import replace

import pytest

from pathbrain import crowning, duel as duel_mod, methodology, verdict
from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.domain.judgement import SpeedBudget
from pathbrain.models import RingRoundMs
from pathbrain.ring_budget import MIN_PAIRED_ROUNDS, ring_budget_checks
from pathbrain.settings_profile import SQM_OFF_FINGERPRINT


def _judged(s, **over):
    cfg = get_config(s)
    row = methodology.ensure_current_methodology(s, cfg)
    today = methodology.judgement(row.version, row.definition or {}, cfg)
    return replace(today, source="methodology", crowned_by="ring", ring_crown_rule="lineal",
                   ring_min_margin=0.0, tie_sigma=2.0,
                   speed_budget=SpeedBudget(enabled=True, lcp_pct=20.0, fcp_pct=25.0), **over)


def _rounds(s, version, a, b, metric, a_ms, b_ms, n=MIN_PAIRED_ROUNDS + 1, jitter=1.0):
    """``n`` rounds where ``a`` (challenger) measured about ``a_ms`` and ``b`` about ``b_ms``."""
    for i in range(n):
        s.add(RingRoundMs(duel_id=9000, round_index=i, methodology=version, incumbent=b, challenger=a,
                          metric=metric, challenger_ms=a_ms + (i % 3 - 1) * jitter,
                          incumbent_ms=b_ms, challenger_run_id=1))


@pytest.fixture()
def ring(monkeypatch, client):
    """A ring-crowning judgement, a ring whose champion and standings the test sets."""
    with session_scope() as s:
        s.query(RingRoundMs).delete()
        judged = _judged(s)
    state = {"judged": judged, "champion": None, "order": [], "sitting": None}
    monkeypatch.setattr(methodology, "current_judgement", lambda session: state["judged"])
    monkeypatch.setattr(duel_mod, "latest_champion", lambda session, max_age_days: state["champion"])
    monkeypatch.setattr(duel_mod, "ring_order", lambda session: list(state["order"]))
    monkeypatch.setattr(verdict, "crown_fingerprint", lambda session: state["sitting"])
    yield state
    with session_scope() as s:
        s.query(RingRoundMs).delete()


def _champ(fp):
    return {"fingerprint": fp, "decisive": True, "duel_id": 1, "finished_at": "2026-11-26"}


def test_the_ring_champion_is_the_crown_whatever_the_policy(ring, monkeypatch):
    ring["champion"], ring["order"] = _champ("champ"), ["champ", "second"]
    monkeypatch.setattr(crowning, "active_policy", lambda session: "math")
    with session_scope() as s:
        out = crowning.resolve(s, "pooledbest")
    assert (out["fingerprint"], out["source"], out["policy"]) == ("champ", "ring", "ring")
    assert "lineal rule" in out["detail"]


def test_with_no_decided_match_the_crown_stays_where_it_is(ring):
    ring["champion"] = {**_champ("champ"), "decisive": False}
    ring["sitting"] = "sitting"
    with session_scope() as s:
        out = crowning.resolve(s, "pooledbest")
    assert (out["fingerprint"], out["source"]) == ("sitting", "ring")
    assert "stays where it is" in out["detail"]
    ring["sitting"] = None  # a fresh install: nothing decided yet, the pooled crown stands in
    with session_scope() as s:
        out = crowning.resolve(s, "pooledbest")
    assert (out["fingerprint"], out["source"]) == ("pooledbest", "pooled")


def test_sqm_off_may_hold_the_crown_only_when_the_methodology_allows(ring):
    ring["champion"], ring["order"] = _champ(SQM_OFF_FINGERPRINT), [SQM_OFF_FINGERPRINT, "shaped"]
    with session_scope() as s:
        out = crowning.resolve(s, "pooledbest")
    assert out["fingerprint"] == "shaped"
    assert "SQM off holds the ring's title" in out["detail"]
    ring["judged"] = replace(ring["judged"], allow_unshaped_crown=True)
    with session_scope() as s:
        assert crowning.resolve(s, "pooledbest")["fingerprint"] == SQM_OFF_FINGERPRINT


def test_a_champion_clearly_over_budget_back_to_back_passes_the_crown_on(ring):
    ring["champion"], ring["order"] = _champ("slow"), ["slow", "quick", "third"]
    v = ring["judged"].version
    with session_scope() as s:
        _rounds(s, v, "slow", "quick", "lcp", 1500.0, 1000.0)   # 50% slower on LCP, budget 20%
        _rounds(s, v, "quick", "third", "lcp", 1000.0, 990.0)   # quick is within
    with session_scope() as s:
        out = crowning.resolve(s, "pooledbest")
    assert out["fingerprint"] == "quick"
    assert out["speed_budget"]["judged_on"] == "ring"
    assert out["speed_budget"]["replaced"] == {"fingerprint": "slow", "source": "ring"}
    assert "LCP +50% slower than quick" in out["detail"]


def test_ring_budget_needs_enough_rounds_and_clears_the_noise_rule(client):
    budget = SpeedBudget(enabled=True, lcp_pct=20.0, fcp_pct=25.0)
    with session_scope() as s:
        s.query(RingRoundMs).delete()
        v = methodology.ensure_current_methodology(s, get_config(s)).version
        _rounds(s, v, "thin", "x", "lcp", 2000.0, 1000.0, n=MIN_PAIRED_ROUNDS - 1)
        _rounds(s, v, "noisy", "x", "lcp", 1250.0, 1000.0, jitter=400.0)  # 25% but wild
        _rounds(s, v, "clear", "x", "fcp", 1400.0, 1000.0)                # 40%, budget 25%
    with session_scope() as s:
        checks = ring_budget_checks(s, ["thin", "noisy", "clear", "x"], budget, v, sigma=2.0)
        s.query(RingRoundMs).delete()
    assert checks["thin"] is None            # cannot tell yet, and that never refuses a crown
    assert checks["noisy"].within             # over the limit on average, not clearly
    assert not checks["clear"].within
    assert checks["clear"].over[0].leg == "fcp" and checks["clear"].over[0].against == "x"
    assert checks["x"].within                 # the faster side of every pair
