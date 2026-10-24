"""Explore stops manufacturing profiles the ring cannot use.

Two leaks fed the field with thin, noisy fingerprints: a refine move that bisected down to
one-unit steps (q5798 beside q5797 and q5800 — a bout `decidability.cannot_differ` refuses
and no instrument resolves), and an upper-confidence-bound search that found open space on
levers the mechanism model predicts inert (target, interval, limit, ecn) maximally
attractive precisely because nobody had been there.
"""
from __future__ import annotations

import pathbrain.api.routes_settings as rs
from pathbrain import explore


def _profile(fp: str, *, quantum: int, target: int, overall: float, up_quantum: int = 1514,
             iterations: int = 30, confident: bool = True) -> dict:
    return {
        "fingerprint": fp,
        "name": fp.title(),
        "label": f"q{quantum} t{target}ms",
        "overall": overall,
        "iterations": iterations,
        "confident": confident,
        "settings": [
            {"label": "Download", "quantum": quantum, "target": f"{target}ms",
             "scheduler": "fq_codel", "queues": 1},
            {"label": "Upload", "quantum": up_quantum, "target": f"{target}ms",
             "scheduler": "fq_codel", "queues": 1},
        ],
    }


def _landscape(monkeypatch, profiles, ring=None, **kw):
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: {"profiles": profiles})
    monkeypatch.setattr(explore, "ring_transitions", lambda session: dict(ring or {}))
    return explore.landscape(None, **kw)


def _moves(out: dict, key: str) -> list[dict]:
    return [ch for c in out["candidates"] for ch in c["changes"] if ch["key"] == key]


# ── the step floor ───────────────────────────────────────────────────────────────────


def test_a_refine_never_proposes_an_immaterial_sibling(monkeypatch):
    """The best quantum sits three units from each neighbour on a range of ~5000: the old
    bisection offered 5798 and 5802, which differ from measured profiles by one part in two
    thousand. Nothing proposed may sit within `MIN_STEP_FRACTION` of the range from a
    measured value unless it IS a measured value (a transplant)."""
    profiles = [
        _profile("a", quantum=1000, target=5, overall=60.0),
        _profile("b", quantum=5797, target=5, overall=71.0),
        _profile("c", quantum=5800, target=5, overall=74.0),
        _profile("d", quantum=5803, target=5, overall=70.0),
    ]
    out = _landscape(monkeypatch, profiles, suggestions=12)
    tested = {1000.0, 5797.0, 5800.0, 5803.0}
    span = 5803 - 1000
    moves = _moves(out, "Download::quantum")
    assert moves, "the quantum axis still proposes something (the hole and the edge)"
    for ch in moves:
        to = float(ch["to"])
        nearest = min(abs(to - t) for t in tested)
        assert to in tested or nearest >= explore.MIN_STEP_FRACTION * span, ch
    # Direct pin on the helper, so the rule is not only visible through the ranking.
    assert explore._immaterial(5798.0, [1000.0, 5797.0, 5800.0], span) is True
    assert explore._immaterial(3400.0, [1000.0, 5797.0, 5800.0], span) is False
    assert explore._immaterial(5798.0, [1000.0], 0.0) is False  # no span, no judgement


# ── the mechanism prior ──────────────────────────────────────────────────────────────


def _mixed_field() -> list[dict]:
    # quantum and target both vary, with the same values at both pipes held still.
    return [
        _profile("a", quantum=1000, target=3, overall=60.0),
        _profile("b", quantum=2000, target=5, overall=70.0),
        _profile("c", quantum=3000, target=8, overall=80.0),
        _profile("d", quantum=4000, target=12, overall=70.0),
        _profile("e", quantum=5000, target=20, overall=60.0),
    ]


def test_a_move_on_an_inert_lever_keeps_only_a_quarter_of_its_exploration_credit(monkeypatch):
    out = _landscape(monkeypatch, _mixed_field(), suggestions=40)
    target_moves = [c for c in out["candidates"]
                    if [ch["key"] for ch in c["changes"]] == ["Download::target"]]
    quantum_moves = [c for c in out["candidates"]
                     if [ch["key"] for ch in c["changes"]] == ["Download::quantum"]]
    assert target_moves and quantum_moves
    for c in target_moves:
        assert c["inert_levers"] == ["Download::target"]
        assert c["exploration_credit"] == explore.INERT_LEVER_CREDIT
        assert any("mechanism predicts no effect" in e for e in c["evidence"])
        # The prediction is untouched; only the opportunity bonus is discounted.
        assert c["upside"] >= c["predicted"]
    for c in quantum_moves:
        assert c["inert_levers"] == [] and c["exploration_credit"] == 1.0
    # A two-lever move that touches a live lever is not discounted: the credit is cut only
    # when EVERY moved lever is inert.
    for c in out["candidates"]:
        keys = {ch["key"] for ch in c["changes"]}
        if "Download::quantum" in keys and "Download::target" in keys:
            assert c["exploration_credit"] == 1.0 and c["inert_levers"] == ["Download::target"]


def test_the_ring_finding_an_inert_lever_moving_restores_the_credit(monkeypatch):
    """A `surprise` — the ring found the target moving the Overall — says the model of the
    link is wrong, not the lever, so the search looks there again in full."""
    ring = {
        "Download::target": {
            "key": "Download::target", "pipe": "Download", "field": "target",
            "field_label": "Target", "unit": "ms", "total_rounds": 12,
            "transitions": [{
                "from": 5.0, "to": 8.0, "margin_up": 1.4, "rounds": 12, "paired_rounds": 12,
                "matches": 1, "sign_p": 0.01, "paired_p": 0.01, "margin_se": 0.3,
                "direction": "higher", "significant": True, "null": False, "thin": False,
            }],
        },
    }
    out = _landscape(monkeypatch, _mixed_field(), ring=ring, suggestions=40)
    target_moves = [c for c in out["candidates"]
                    if [ch["key"] for ch in c["changes"]] == ["Download::target"]]
    assert target_moves
    assert all(c["inert_levers"] == [] and c["exploration_credit"] == 1.0 for c in target_moves)


def test_inert_moves_rank_below_a_live_move_of_equal_promise():
    """The pure function: identical parent, identical open space, one move on quantum and
    one on target — the inert one's upside is lower by exactly the withheld credit."""
    axes = {
        "Download::quantum": {"key": "Download::quantum", "pipe": "Download", "field": "quantum",
                              "field_label": "Quantum", "unit": "B", "min": 1000.0, "max": 5000.0,
                              "values": [1000.0, 3000.0, 5000.0]},
        "Download::target": {"key": "Download::target", "pipe": "Download", "field": "target",
                             "field_label": "Target", "unit": "ms", "min": 3.0, "max": 20.0,
                             "values": [3.0, 8.0, 20.0]},
    }
    points = [
        {"fingerprint": f"p{i}", "name": f"P{i}", "label": f"p{i}", "overall": o, "iterations": 30,
         "confident": True, "weight": 1.0, "coords": {"Download::quantum": q, "Download::target": t},
         "settings": []}
        for i, (q, t, o) in enumerate([(1000.0, 3.0, 60.0), (3000.0, 8.0, 80.0), (5000.0, 20.0, 60.0)])
    ]
    parent = points[1]
    q = explore._candidate_dict(parent, {"Download::quantum": (2000.0, "why")}, axes, points, {}, {}, None)
    t = explore._candidate_dict(parent, {"Download::target": (5.0, "why")}, axes, points, {}, {}, None)
    assert q["exploration_credit"] == 1.0 and t["exploration_credit"] == explore.INERT_LEVER_CREDIT
    assert q["predicted"] == t["predicted"]  # no curves handed in: both sit on the anchor
    bonus_q, bonus_t = q["upside"] - q["predicted"], t["upside"] - t["predicted"]
    assert bonus_q > 0 and abs(bonus_t - bonus_q * explore.INERT_LEVER_CREDIT) < 0.02


# ── R23: an edge step is a value the firewall can hold ─────────────────────────────────


def test_an_edge_step_never_walks_past_the_levers_declared_maximum(monkeypatch):
    """E10: the edge step was ``max + step`` with no ceiling, so a quantum edge walked past
    the registry's own 10000 one sweep step at a time. The step is clamped to the declared
    sweep range, and an edge already at the clamp proposes nothing — the finding stays on
    the curve with the reason."""
    from pathbrain.shaper_fields import field as shaper_field

    top = shaper_field("quantum").sweep_default["max"]
    profiles = [
        _profile("a", quantum=1000, target=5, overall=60.0),
        _profile("b", quantum=4000, target=5, overall=70.0),
        _profile("c", quantum=7000, target=5, overall=80.0),
        _profile("d", quantum=top, target=5, overall=90.0),
    ]
    out = _landscape(monkeypatch, profiles)
    curve = next(c for c in out["curves"] if c["key"] == "Download::quantum")
    assert curve["best_at_edge"] is True
    assert "declared maximum" in (curve.get("edge_blocked") or "")
    assert not [g for g in out["gaps"] if g["key"] == "Download::quantum" and g["kind"] == "edge"]
    for c in out["candidates"]:
        for ch in c["changes"]:
            if ch["key"] == "Download::quantum":
                assert ch["to"] <= top, ch
    # A step short of the ceiling is clamped TO it rather than past it.
    near = [
        _profile("a", quantum=1000, target=5, overall=60.0),
        _profile("b", quantum=4000, target=5, overall=70.0),
        _profile("c", quantum=7000, target=5, overall=80.0),
        _profile("d", quantum=top - 100, target=5, overall=90.0),
    ]
    out2 = _landscape(monkeypatch, near)
    edge = next(g for g in out2["gaps"] if g["key"] == "Download::quantum" and g["kind"] == "edge")
    assert edge["suggest"] == top


def test_a_select_lever_without_its_option_list_offers_no_untested_value(monkeypatch):
    """A CoDel target/interval is a select; a value off its option list is accepted and
    ignored by the firewall, and the benchmark measures a profile nobody proposed. Without
    the provider's list nothing can check that an untested value exists, so the hole is
    reported with the reason and no button, no edge step is offered, and only values the
    firewall has already held (transplants) are proposed on that lever."""
    profiles = [
        _profile("a", quantum=1000, target=3, overall=60.0),
        _profile("b", quantum=1200, target=4, overall=62.0),
        _profile("c", quantum=1400, target=5, overall=64.0),
        _profile("d", quantum=1600, target=40, overall=66.0),
    ]
    out = _landscape(monkeypatch, profiles, suggestions=20)
    curve = next(c for c in out["curves"] if c["key"] == "Download::target")
    assert curve["best_at_edge"] is True and "option list" in (curve.get("edge_blocked") or "")
    gaps = [g for g in out["gaps"] if g["key"] == "Download::target"]
    assert gaps and all(g["kind"] == "gap" for g in gaps)
    assert all(g.get("blocked") and "candidate" not in g for g in gaps)
    assert all(float(g["suggest"]) == int(g["suggest"]) for g in gaps), "still the field's own form"
    for c in out["candidates"]:
        for ch in c["changes"]:
            if ch["key"] == "Download::target":
                assert ch["to"] in (3.0, 4.0, 5.0, 40.0), ch
                assert "proven elsewhere" in ch["why"]
    # With the option list attached the edge steps to the NEXT OPTION, never an invented value.
    out2 = _landscape(monkeypatch, profiles, suggestions=20, allowed_values={"target": [3, 4, 5, 40, 60]})
    edge = next(g for g in out2["gaps"] if g["key"] == "Download::target" and g["kind"] == "edge")
    assert edge["suggest"] == 60 and edge.get("candidate") is not None
    out3 = _landscape(monkeypatch, profiles, suggestions=20, allowed_values={"target": [3, 4, 5, 40]})
    curve3 = next(c for c in out3["curves"] if c["key"] == "Download::target")
    assert "last option" in (curve3.get("edge_blocked") or "")
