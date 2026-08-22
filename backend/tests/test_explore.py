"""The exploration landscape: response curves, interactions, coverage gaps, candidates.

The properties that matter are all "does it find the thing a human would find by staring
at the table for an hour" — a sweet spot in the middle of a lever, two levers that have to
be chosen together, the hole nobody sampled, and the untested profile most likely to beat
everything measured. Each test builds a field where the right answer is known by
construction.
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


def _landscape(monkeypatch, profiles, **kw):
    monkeypatch.setattr(rs, "compute_profiles", lambda session: {"profiles": profiles})
    return explore.landscape(None, **kw)


def test_a_sweet_spot_is_found_where_a_correlation_would_miss_it(monkeypatch):
    """The reason the page draws curves instead of printing coefficients.

    Overall peaks in the middle of the quantum range, so the rank correlation across the
    whole axis is ~0 — a lever that a sensitivity table reports as "no relationship" while
    it is in fact the strongest thing in the field.
    """
    # Symmetric peak at 3000: 60, 70, 80, 70, 60.
    profiles = [
        _profile("a", quantum=1000, target=5, overall=60.0),
        _profile("b", quantum=2000, target=5, overall=70.0),
        _profile("c", quantum=3000, target=5, overall=80.0),
        _profile("d", quantum=4000, target=5, overall=70.0),
        _profile("e", quantum=5000, target=5, overall=60.0),
    ]
    out = _landscape(monkeypatch, profiles)
    curve = next(c for c in out["curves"] if c["key"] == "Download::quantum")
    assert curve["best_value"] == 3000
    assert curve["best_at_edge"] is False
    assert curve["shape"] == "sweet spot"
    # A monotonic coefficient sees nothing here — which is the point.
    assert abs(curve["spearman"] or 0.0) < 0.3
    assert [c["value"] for c in curve["curve"]] == [1000, 2000, 3000, 4000, 5000]


def test_the_two_pipes_are_separate_levers(monkeypatch):
    """Download and Upload quantum are different knobs and must never be pooled into one
    axis — averaging them would hide whichever one actually matters."""
    profiles = [
        _profile("a", quantum=1000, target=5, overall=60.0, up_quantum=300),
        _profile("b", quantum=2000, target=5, overall=65.0, up_quantum=600),
        _profile("c", quantum=3000, target=5, overall=70.0, up_quantum=900),
        _profile("d", quantum=4000, target=5, overall=75.0, up_quantum=1200),
    ]
    out = _landscape(monkeypatch, profiles)
    keys = {a["key"] for a in out["axes"]}
    assert "Download::quantum" in keys and "Upload::quantum" in keys
    pipes = {a["pipe"] for a in out["axes"]}
    assert pipes == {"Download", "Upload"}


def test_a_constant_field_is_not_an_axis(monkeypatch):
    """A lever every profile shares isn't a lever — showing it as an axis with one value
    puts a meaningless panel on every chart."""
    profiles = [
        _profile("a", quantum=1000, target=5, overall=60.0),
        _profile("b", quantum=2000, target=5, overall=65.0),
        _profile("c", quantum=3000, target=5, overall=70.0),
        _profile("d", quantum=4000, target=5, overall=75.0),
    ]
    out = _landscape(monkeypatch, profiles)
    assert not any(a["field"] == "target" for a in out["axes"]), "target never varies here"
    assert any(a["field"] == "quantum" for a in out["axes"])


def test_the_widest_untested_interval_is_reported_as_a_gap(monkeypatch):
    """A hole between two tested values is bracketed — one run resolves it."""
    profiles = [
        _profile("a", quantum=1000, target=5, overall=60.0),
        _profile("b", quantum=1200, target=5, overall=62.0),
        _profile("c", quantum=1400, target=5, overall=64.0),
        # …then nothing at all until 9000.
        _profile("d", quantum=9000, target=5, overall=66.0),
    ]
    out = _landscape(monkeypatch, profiles)
    gap = next(
        g for g in out["gaps"] if g["key"] == "Download::quantum" and g["kind"] == "gap"
    )
    assert (gap["from"], gap["to"]) == (1400, 9000)
    assert gap["suggest"] == 5200  # the middle of the hole
    assert gap["width_fraction"] > 0.9


def test_a_best_value_at_the_edge_says_look_further_out(monkeypatch):
    """An edge-best isn't a gap — the answer isn't bracketed at all, and the right move is
    to step past the end of what's been tried."""
    profiles = [
        _profile("a", quantum=1000, target=5, overall=60.0),
        _profile("b", quantum=2000, target=5, overall=70.0),
        _profile("c", quantum=3000, target=5, overall=80.0),
        _profile("d", quantum=4000, target=5, overall=90.0),  # best, and the highest tried
    ]
    out = _landscape(monkeypatch, profiles)
    edge = next(
        g for g in out["gaps"] if g["key"] == "Download::quantum" and g["kind"] == "edge"
    )
    assert edge["suggest"] > 4000, "must propose a value beyond the tested range"
    assert "beyond" in edge["detail"]


def test_interacting_levers_are_surfaced(monkeypatch):
    """The question no per-lever view can answer: does the best download quantum depend on
    the upload quantum? Here it flatly does — high/high and low/low win, the mixed corners
    lose — so the interaction contrast has to be large."""
    profiles = []
    for i, (dq, uq, score) in enumerate([
        (1000, 300, 80.0), (1100, 320, 82.0),     # low/low  → good
        (1000, 3000, 50.0), (1100, 3200, 52.0),   # low/high → bad
        (5000, 300, 50.0), (5100, 320, 52.0),     # high/low → bad
        (5000, 3000, 80.0), (5100, 3200, 82.0),   # high/high → good
    ]):
        profiles.append(_profile(f"p{i}", quantum=dq, target=5, overall=score, up_quantum=uq))
    out = _landscape(monkeypatch, profiles)
    top = out["interactions"][0]
    assert {top["a"], top["b"]} == {"Download::quantum", "Upload::quantum"}
    assert abs(top["contrast"]) > 20, "a flat sign flip must read as a strong interaction"
    assert "interact" in top["summary"]


def test_candidates_are_untested_reachable_and_explained(monkeypatch):
    profiles = [
        _profile("a", quantum=1000, target=5, overall=60.0, up_quantum=300),
        _profile("b", quantum=1200, target=5, overall=64.0, up_quantum=600),
        _profile("c", quantum=1400, target=5, overall=68.0, up_quantum=900),
        _profile("d", quantum=9000, target=5, overall=72.0, up_quantum=4000),
    ]
    out = _landscape(monkeypatch, profiles, suggestions=3)
    assert len(out["candidates"]) == 3
    measured = {tuple(sorted(p["coords"].items())) for p in out["points"]}
    for c in out["candidates"]:
        # Never propose something already measured.
        assert tuple(sorted(c["coords"].items())) not in measured
        assert c["changes"], "every candidate states what it changes and why"
        assert c["changes"][0]["why"]
        assert c["parent"]["fingerprint"]
        # And it's directly runnable: per-pipe overrides of writable fields only.
        assert c["settings"] and all("label" in s for s in c["settings"])
        assert c["upside"] >= c["predicted"], "upside includes the uncertainty"


def test_candidates_are_ranked_by_upside_not_by_prediction(monkeypatch):
    """The question is "where might we beat everything we have?", not "where do we expect
    to score well?" — those give different answers, and only the first one explores."""
    profiles = [
        _profile("a", quantum=1000, target=5, overall=70.0),
        _profile("b", quantum=1100, target=5, overall=71.0),
        _profile("c", quantum=1200, target=5, overall=72.0),
        _profile("d", quantum=9000, target=5, overall=69.0),
    ]
    out = _landscape(monkeypatch, profiles)
    ups = [c["upside"] for c in out["candidates"]]
    assert ups == sorted(ups, reverse=True)
    best = out["best_overall"]
    assert all(c["beats_best_by"] == round(c["upside"] - best, 2) for c in out["candidates"])


def test_a_point_far_from_everything_carries_more_uncertainty(monkeypatch):
    """The honest statement about open space: we don't know. A prediction that doesn't
    widen out there would send the ladder to measure things it already knows."""
    profiles = [
        _profile("a", quantum=1000, target=5, overall=60.0),
        _profile("b", quantum=1050, target=5, overall=62.0),
        _profile("c", quantum=1100, target=5, overall=64.0),
        _profile("d", quantum=9000, target=5, overall=66.0),
    ]
    out = _landscape(monkeypatch, profiles)
    far = max(out["candidates"], key=lambda c: c["nearest_measured"])
    near = min(out["candidates"], key=lambda c: c["nearest_measured"])
    assert far["uncertainty"] >= near["uncertainty"]


def test_thin_profiles_are_kept_out_of_the_model(monkeypatch):
    """A lucky Overall on two iterations is noise, and noise in the model comes back out as
    a confident-sounding prediction."""
    profiles = [
        _profile("a", quantum=1000, target=5, overall=60.0),
        _profile("b", quantum=2000, target=5, overall=62.0),
        _profile("c", quantum=3000, target=5, overall=64.0),
        _profile("d", quantum=4000, target=5, overall=66.0),
        _profile("lucky", quantum=5000, target=5, overall=99.0, iterations=2, confident=False),
    ]
    out = _landscape(monkeypatch, profiles)
    assert out["confident_only"] is True
    assert "lucky" not in {p["fingerprint"] for p in out["points"]}
    assert out["best_overall"] == 66.0

    # …but with nothing confident to model, it falls back rather than showing an empty page.
    thin = [dict(p, confident=False, iterations=2) for p in profiles]
    out2 = _landscape(monkeypatch, thin)
    assert out2["confident_only"] is False
    assert out2["profiles_modelled"] == len(thin)


def test_too_little_data_explains_itself(monkeypatch):
    out = _landscape(monkeypatch, [_profile("a", quantum=1000, target=5, overall=60.0)])
    assert out["candidates"] == []
    assert "Not enough comparable profiles" in out["reason"]


def test_endpoint_serves_the_landscape(client, monkeypatch):
    profiles = [
        _profile("a", quantum=1000, target=5, overall=60.0),
        _profile("b", quantum=2000, target=5, overall=70.0),
        _profile("c", quantum=3000, target=5, overall=80.0),
        _profile("d", quantum=9000, target=5, overall=65.0),
    ]
    monkeypatch.setattr(rs, "compute_profiles", lambda session: {"profiles": profiles})
    body = client.get("/api/explore/landscape?suggestions=2").json()
    assert body["profiles_modelled"] == 4
    assert 1 <= len(body["candidates"]) <= 2
    assert body["best_overall"] == 80.0
    assert any(a["key"] == "Download::quantum" for a in body["axes"])


def test_an_interior_peak_beats_a_strong_correlation(monkeypatch):
    """A lever whose far end is disastrous carries a strong monotonic ρ even when its best
    value is in the middle. Reading that as "lower is better" would send you to the low
    end, which the curve itself says is worse than the peak already found."""
    profiles = [
        _profile("a", quantum=1000, target=3, overall=68.0),
        _profile("b", quantum=2000, target=5, overall=72.0),   # the actual best
        _profile("c", quantum=3000, target=8, overall=60.0),
        _profile("d", quantum=4000, target=15, overall=40.0),  # far end is terrible
    ]
    out = _landscape(monkeypatch, profiles)
    curve = next(c for c in out["curves"] if c["key"] == "Download::target")
    assert curve["best_value"] == 5
    assert (curve["spearman"] or 0) < -0.4, "the correlation really is strongly monotonic"
    assert curve["shape"] == "sweet spot", "…but the peak is interior, and that's what matters"


def test_a_trivial_contrast_is_not_reported_as_an_interaction(monkeypatch):
    """"They interact" has to mean something. Every pair of levers produces *some* nonzero
    2×2 contrast; reporting them all is worse than reporting none, because it invites
    choosing two levers together for no reason."""
    profiles = []
    for i, (dq, uq, score) in enumerate([
        # A clean main effect on download quantum and essentially nothing joint.
        (1000, 300, 60.0), (1100, 320, 60.2),
        (1000, 3000, 60.1), (1100, 3200, 60.3),
        (5000, 300, 70.0), (5100, 320, 70.2),
        (5000, 3000, 70.1), (5100, 3200, 70.4),
    ]):
        profiles.append(_profile(f"p{i}", quantum=dq, target=5, overall=score, up_quantum=uq))
    out = _landscape(monkeypatch, profiles)
    assert out["interactions"] == [], "a pure main effect is not an interaction"
