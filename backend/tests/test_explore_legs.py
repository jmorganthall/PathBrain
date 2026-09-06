"""Leaders per crown leg: who leads each crown metric, where the best profile stands on it,
and the "move the best profile their way" proposals — named when they already exist,
runnable when they don't."""
from __future__ import annotations

from pathbrain.explore import (
    _numeric_axes,
    _response_curves,
    crown_legs,
    matched_pairs,
)


def _profile(fp: str, quantum: int, target: int, *, overall: float, fcp: float, stall: float,
             iterations: int = 20, name: str | None = None) -> dict:
    return {
        "fingerprint": fp,
        "name": name or fp,
        "label": f"q{quantum} t{target}",
        "overall": overall,
        "overall_p25": overall - 1.0,
        "overall_p75": overall + 1.0,
        "count": iterations // 3,
        "iterations": iterations,
        "confident": iterations >= 15,
        "settings": [{"label": "wan", "quantum": quantum, "target": target, "enabled": True}],
        "metrics": {"fcp": fcp, "network_stall_all": stall},
        "crown_norm": {},
    }


def _points(profiles: list[dict], axes: dict) -> list[dict]:
    from pathbrain.explore import _coords

    return [
        {
            "fingerprint": p["fingerprint"], "name": p["name"], "label": p["label"],
            "overall": p["overall"], "iterations": p["iterations"], "confident": p["confident"],
            "weight": 1.0, "trusted": p["overall_p25"], "runs": p["count"],
            "overall_iqr": 2.0, "coords": _coords(p, axes),
        }
        for p in profiles
    ]


def _field():
    # The best profile (q1500 t5) leads FCP but sits last on the stall leg. Every profile
    # that leads the stall leg runs a HIGHER quantum — the signature the section must find.
    profiles = [
        _profile("best0000000", 1500, 5, overall=90, fcp=250, stall=200, name="Best"),
        _profile("a0000000000", 3000, 5, overall=85, fcp=300, stall=100),
        _profile("b0000000000", 4000, 5, overall=84, fcp=310, stall=90),
        _profile("c0000000000", 3000, 10, overall=80, fcp=330, stall=110),
        _profile("d0000000000", 5000, 5, overall=78, fcp=340, stall=95),
        _profile("e0000000000", 1000, 5, overall=70, fcp=400, stall=250),
    ]
    axes = _numeric_axes(profiles)
    points = _points(profiles, axes)
    field = {"profiles": profiles, "overall_metrics": ["fcp", "network_stall_all"]}
    return field, points, axes


def test_leaders_and_the_reference_standing_follow_the_crown_metric_set():
    field, points, axes = _field()
    curves = _response_curves(points, axes)
    out = crown_legs(field, points, axes, curves, matched_pairs(field["profiles"], axes),
                     set(tuple(sorted(p["coords"].items())) for p in points), 90.0,
                     field["overall_metrics"])
    assert out is not None and out["reference"]["fingerprint"] == "best0000000"
    legs = {l["key"]: l for l in out["legs"]}
    assert set(legs) == {"fcp", "network_stall_all"}  # exactly the crown, in crown order
    assert legs["fcp"]["reference"]["leads"] is True
    assert legs["fcp"]["leaders"][0]["fingerprint"] == "best0000000"
    stall = legs["network_stall_all"]
    assert stall["reference"]["leads"] is False and stall["reference"]["rank"] == 5
    assert stall["leaders"][0]["fingerprint"] == "b0000000000"  # lowest stall leads
    assert out["weakest"] == "network_stall_all"


def test_the_move_toward_the_stall_leaders_raises_quantum_and_is_runnable_when_untested():
    field, points, axes = _field()
    curves = _response_curves(points, axes)
    tried = set(tuple(sorted(p["coords"].items())) for p in points)
    out = crown_legs(field, points, axes, curves, matched_pairs(field["profiles"], axes),
                     tried, 90.0, field["overall_metrics"])
    stall = next(l for l in out["legs"] if l["key"] == "network_stall_all")
    moves = {m["key"]: m for m in stall["moves"]}
    q = moves["wan::quantum"]
    # Four of the four compared leaders run quantum above the best profile's 1500 → "up",
    # to their median value, and Best-with-that-quantum is a profile nobody has measured.
    assert q["direction"] == "up" and q["from"] == 1500 and q["to"] > 1500
    assert q["agreement"] == 1.0
    assert "candidate" in q and q["candidate"]["parent"]["fingerprint"] == "best0000000"
    assert q["candidate"]["changes"][0]["to"] == q["to"]
    assert q["candidate"]["settings"] == [{"label": "wan", "quantum": q["to"]}]
    # The CoDel target: the leaders split (three at 5, one at 10) — the majority side has the
    # SAME value as the best profile, so there is nothing to move and no target move.
    assert "wan::target" not in moves


def test_an_existing_profile_is_named_instead_of_proposed_again():
    field, points, axes = _field()
    # Add the profile the quantum move would create (Best with q3500) — now the move must
    # point at it with its measured Overall rather than propose it.
    extra = _profile("exists00000", 3500, 5, overall=87, fcp=280, stall=120, name="Exists")
    field["profiles"].append(extra)
    axes = _numeric_axes(field["profiles"])
    points = _points(field["profiles"], axes)
    curves = _response_curves(points, axes)
    tried = set(tuple(sorted(p["coords"].items())) for p in points)
    out = crown_legs(field, points, axes, curves, matched_pairs(field["profiles"], axes),
                     tried, 90.0, field["overall_metrics"])
    stall = next(l for l in out["legs"] if l["key"] == "network_stall_all")
    q = next(m for m in stall["moves"] if m["key"] == "wan::quantum")
    if q["to"] == 3500:
        assert q["existing"]["fingerprint"] == "exists00000"
        assert q["existing"]["delta"] == -3.0 and "candidate" not in q
    else:
        # The leaders' median landed elsewhere; the move is still runnable and never names
        # a profile it doesn't reproduce.
        assert "candidate" in q and "existing" not in q


def test_nothing_is_reported_without_a_crown_or_enough_profiles():
    field, points, axes = _field()
    curves = _response_curves(points, axes)
    assert crown_legs(field, points, axes, curves, [], set(), 90.0, []) is None
    assert crown_legs({"profiles": []}, [], axes, curves, [], set(), 90.0, ["fcp"]) is None
