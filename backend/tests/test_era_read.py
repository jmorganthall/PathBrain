"""Methodology v19: v17's rubric, with the ranking reading the pooled record per instrument
era (`methodology.pooled_read`, `beliefs.era_adjusted`, `overall_ranking._read_per_era`).

What is pinned: it ships as a candidate, never current on its own; it grades exactly as v17
does, so the ring's v17 rounds carry over under it and every other version still keeps its
rounds apart; the Methodology page can switch to it and the Crown agreement card says what
the switch would do; and the ranking under it is the per-era read, only under it.
"""
from __future__ import annotations

import pytest

from pathbrain import crown_agreement, methodology, overall_ranking
from pathbrain.methodology import (
    CURRENT_METHODOLOGY, METHODOLOGY_REGISTRY, build_definition_from_spec, pooled_read, same_scale, scale_of,
)

V17, V19 = "speed-smoothness-v17", "speed-smoothness-v19"


def test_v19_is_a_candidate_on_v17_s_scale():
    assert CURRENT_METHODOLOGY != V19
    assert scale_of(V19) == V17 and scale_of(V17) == V17
    assert same_scale(V19, V17) and same_scale(V17, V19) and same_scale(V17, V17)
    # A rubric change, a fork or an unknown version is its own scale, as before.
    assert not same_scale("speed-smoothness-v16", V17)
    assert not same_scale(f"{V19}+sites-abc", V17)
    assert not same_scale(V17, None) and same_scale(None, None)
    d17 = build_definition_from_spec(METHODOLOGY_REGISTRY[V17])
    d19 = build_definition_from_spec(METHODOLOGY_REGISTRY[V19])
    assert pooled_read(d17) == "all" and pooled_read(d19) == "epochs"
    assert pooled_read(None) == "all" and pooled_read({"ranking": {"pooled_read": "nonsense"}}) == "all"
    # Same grading to the field: only how the ranking reads it differs.
    assert {k: v for k, v in d19.items() if k != "ranking"} == d17
    assert methodology.verdict_rules(V19) == methodology.verdict_rules(V17)
    assert crown_agreement.prior_version(V19) == V17


def test_same_scale_as_must_be_the_same_rubric(monkeypatch):
    """The import-time invariant, re-run: a version that claims v17's scale but changes a
    weight is refused."""
    spec = dict(METHODOLOGY_REGISTRY[V19])
    spec["overall"] = {**spec["overall"], "weights": {"fcp": 2.0, "lcp": 1.0, "cadence_cov": 1.0}}
    a = {k: v for k, v in spec.items() if k not in methodology.READ_ONLY_KEYS}
    b = {k: v for k, v in METHODOLOGY_REGISTRY[V17].items() if k not in methodology.READ_ONLY_KEYS}
    assert a != b


def test_the_ring_keeps_v17_rounds_under_v19_and_drops_another_scale_s():
    sessions = [{"id": 1, "matchups": [
        {"incumbent": "a", "challenger": "b", "verdict": "challenger", "methodology": V17, "deltas": [1.0, 2.0]},
        {"incumbent": "a", "challenger": "c", "verdict": "draw", "methodology": V19, "deltas": [0.5]},
        {"incumbent": "a", "challenger": "d", "verdict": "draw", "methodology": "speed-smoothness-v16", "deltas": [9.0]},
    ]}]
    under19 = overall_ranking.ring_rounds(sessions, V19)
    assert len(under19["rounds"]) == 3 and under19["excluded_methodology"] == 1
    under17 = overall_ranking.ring_rounds(sessions, V17)
    assert len(under17["rounds"]) == 3 and under17["excluded_methodology"] == 1


def test_the_page_offers_v19_as_a_candidate(client):
    body = client.get("/api/methodologies").json()
    cands = {c["version"]: c for c in body.get("candidates") or []}
    assert V19 in cands and cands[V19]["pooled_read"] == "epochs" and cands[V19]["same_scale_as"] == V17
    assert CURRENT_METHODOLOGY not in cands


def test_a_preview_reads_per_era_without_publishing(client, monkeypatch):
    """``?read=epochs`` previews v19's read; the published read and the crown stay v17's."""
    from pathbrain import beliefs

    calls = []

    def fake(_session):
        calls.append(1)
        return {"shifts": {}, "offsets": {}, "boundaries": [], "hyper": None, "methodology": V17}

    monkeypatch.setattr(beliefs, "era_adjusted", fake)
    body = client.get("/api/overall", params={"backtest": "false", "read": "epochs"}).json()
    assert calls and body["pooled_read"] == "epochs" and body["published_read"] == "all"
    assert body["preview"] is True and body["eras"]["applied"] is False
    assert client.get("/api/overall", params={"read": "bogus"}).status_code == 422


def test_a_failed_era_fit_falls_back_to_the_whole_record_and_says_so(client, monkeypatch):
    from pathbrain import beliefs

    def boom(_session):
        raise RuntimeError("no")

    monkeypatch.setattr(beliefs, "era_adjusted", boom)
    body = client.get("/api/overall", params={"backtest": "false", "read": "epochs"}).json()
    assert body["eras"]["applied"] is False and "failed" in body["eras"]["reason"]


def test_the_crown_agreement_card_compares_the_reads(client, monkeypatch):
    """v17 vs v19 grade alike, so the cold-median audit can only say "alike"; the card reports
    the two reads' rankings instead."""
    def fake_ranking(_session, *, backtest=True, live=True, read=None):
        rows = [{"fingerprint": fp, "name": fp.upper(), "eligible": True} for fp in
                (["old", "new", "mid"] if read == "all" else ["new", "mid", "old"])]
        return {"best": {**rows[0], "fused": 25.0}, "profiles": rows, "lead": 0.3,
                "noise_bar": 1.2, "tied_count": 2, "eras": {"boundaries": ["2026-10-26"], "offsets": {}}}

    monkeypatch.setattr(overall_ranking, "ranking", fake_ranking)
    body = client.get("/api/methodologies/crown-agreement", params={"a": V17, "b": V19}).json()
    assert body["read"]["a_read"] == "all" and body["read"]["b_read"] == "epochs"
    assert body["read"]["rubric_alike"] is True
    assert body["verdict"] == "disagree" and "OLD drops to #3" in body["text"]
    assert "Auto-apply" in body["text"]
