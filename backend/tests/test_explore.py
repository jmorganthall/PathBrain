"""The exploration landscape: response curves, interactions, coverage gaps, candidates.

The properties that matter are all "does it find the thing a human would find by staring
at the table for an hour" — a sweet spot in the middle of a lever, two levers that have to
be chosen together, the hole nobody sampled, and the untested profile most likely to beat
everything measured. Each test builds a field where the right answer is known by
construction.
"""
from __future__ import annotations

import time

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
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: {"profiles": profiles})
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


def test_a_thin_profile_informs_the_model_without_steering_it(monkeypatch):
    """The policy this replaces excluded thin profiles outright, which meant a quick test
    taught the model nothing until it crossed the confidence bar — most of the way to never.
    A five-iteration reading is a real *initial placement*: the only reading anyone has of
    that point. So it is never excluded, and never allowed to drive either.

    Three protections, because inclusion alone would have made the page worse:
    its measurement is weighted down, it cannot become the trunk candidates branch from,
    and it cannot set the bar those candidates are judged against."""
    profiles = [
        _profile("a", quantum=1000, target=5, overall=60.0),
        _profile("b", quantum=2000, target=5, overall=62.0),
        _profile("c", quantum=3000, target=5, overall=64.0),
        _profile("d", quantum=4000, target=5, overall=66.0),
        _profile("lucky", quantum=5000, target=5, overall=99.0, iterations=2, confident=False),
    ]
    out = _landscape(monkeypatch, profiles)
    points = {p["fingerprint"]: p for p in out["points"]}

    assert "lucky" in points, "a measured profile is never dropped from the model"
    assert points["lucky"]["weight"] < points["a"]["weight"]
    assert points["lucky"]["weight"] == explore.THIN_WEIGHT_FLOOR

    # It does not become the trunk: every candidate branches from a settled profile.
    assert all(c["parent"]["fingerprint"] != "lucky" for c in out["candidates"])
    # …and it does not set the bar. "Best measured" stays the best SETTLED profile, which is
    # the same one the crown names; a fluke would make every real candidate look hopeless.
    assert out["best_overall"] == 66.0


def test_evidence_weight_is_flat_once_a_profile_is_settled():
    """Linear in iterations up to the bar, then flat — twice the minimum is not twice as
    believable — and floored above zero so a thin reading is never worth literally nothing."""
    assert explore.evidence_weight(15, 15) == 1.0
    assert explore.evidence_weight(150, 15) == 1.0
    assert explore.evidence_weight(5, 50) == 0.15
    assert explore.evidence_weight(0, 50) == explore.THIN_WEIGHT_FLOOR
    assert explore.evidence_weight(25, 50) == 0.5


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
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: {"profiles": profiles})
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


# ── Confounding: the marginal curve answers the wrong question ───────────────────────
#
# These are built from the failure that motivated them: a field where the 1-D marginal
# curve names a value "best" while every controlled comparison says changing to that value
# makes things worse. That is not a bug in the curve — it's the difference between "profiles
# with this value score well" and "changing this profile's value to that helps".


def _pair(fp: str, *, dl_q: int, dl_t: int, ul_q: int, overall: float) -> dict:
    return {
        "fingerprint": fp,
        "name": fp.title(),
        "label": f"dl q{dl_q} t{dl_t} / ul q{ul_q}",
        "overall": overall,
        "iterations": 30,
        "confident": True,
        "settings": [
            {"label": "Download", "quantum": dl_q, "target": f"{dl_t}ms", "interval": "60ms",
             "flows": 1024, "limit": 10240, "ecn": True, "scheduler": "fq_codel", "queues": 1},
            {"label": "Upload", "quantum": ul_q, "target": "3ms", "interval": "60ms",
             "flows": 1024, "limit": 10240, "ecn": True, "scheduler": "fq_codel", "queues": 1},
        ],
    }


def _confounded_field():
    """Upload quantum 475 looks best marginally, but is worse than 500 wherever the two are
    compared with everything else held equal — because 475 mostly co-occurs with the strong
    download quantum and 500 mostly with the weak one."""
    return [
        # High-scoring download family, mostly paired with UL 475.
        _pair("a", dl_q=7313, dl_t=5, ul_q=475, overall=80.0),
        _pair("b", dl_q=7313, dl_t=5, ul_q=500, overall=82.0),  # matched sibling: 500 wins
        _pair("c", dl_q=7313, dl_t=4, ul_q=475, overall=79.0),
        _pair("d", dl_q=7313, dl_t=4, ul_q=500, overall=81.0),  # matched sibling: 500 wins
        # Weak download family, only ever paired with UL 500 — dragging 500's average down.
        _pair("e", dl_q=1257, dl_t=5, ul_q=500, overall=60.0),
        _pair("f", dl_q=1257, dl_t=4, ul_q=500, overall=61.0),
        _pair("g", dl_q=1257, dl_t=6, ul_q=500, overall=59.0),
    ]


def test_the_marginal_curve_and_the_matched_pairs_disagree_and_both_are_reported(monkeypatch):
    """The whole point. The curve says 475 is the best upload quantum; every controlled
    comparison says moving 475 → 500 gains two points. Both are true statements about
    different questions, and the tool has to show the second one."""
    out = _landscape(monkeypatch, _confounded_field())

    curve = next(c for c in out["curves"] if c["key"] == "Upload::quantum")
    assert curve["best_value"] == 475, "marginally, 475 looks best — that's the trap"

    ul = next(m for m in out["matched_pairs"] if m["key"] == "Upload::quantum")
    move = next(t for t in ul["transitions"] if (t["from"], t["to"]) == (475, 500))
    assert move["pairs"] == 2, "two profiles differ ONLY in upload quantum"
    assert move["median_delta"] == 2.0, "…and both say 500 is two points better"
    assert move["consistent"] is True, "every matched pair agrees — this is not noise"


def test_the_confounded_curve_point_names_the_lever_it_is_confounded_with(monkeypatch):
    """Saying "don't trust this point" is only half an answer; the useful half is *why*."""
    out = _landscape(monkeypatch, _confounded_field())
    curve = next(c for c in out["curves"] if c["key"] == "Upload::quantum")
    assert curve["confounded"] is True
    bad = next(r for r in curve["imbalance"] if r["value"] == 475)
    assert bad["other"] == "Download::quantum", "475 co-occurs with the strong download quantum"
    assert bad["shift"] > 0
    assert "measuring both levers at once" in bad["detail"]


def test_matched_pairs_require_everything_else_to_be_identical(monkeypatch):
    """A "sibling" that differs in two levers is not a controlled comparison, and counting
    it would reintroduce exactly the confounding this is here to remove."""
    profiles = [
        _pair("a", dl_q=7313, dl_t=5, ul_q=475, overall=80.0),
        # Differs in BOTH download target and upload quantum — not a sibling of "a" on
        # either axis.
        _pair("b", dl_q=7313, dl_t=4, ul_q=500, overall=90.0),
        _pair("c", dl_q=1257, dl_t=5, ul_q=475, overall=60.0),
        _pair("d", dl_q=1257, dl_t=5, ul_q=500, overall=62.0),
    ]
    out = _landscape(monkeypatch, profiles)
    ul = next(m for m in out["matched_pairs"] if m["key"] == "Upload::quantum")
    move = next(t for t in ul["transitions"] if (t["from"], t["to"]) == (475, 500))
    assert move["pairs"] == 1, "only c→d is a true one-lever pair; a→b changes two things"
    assert move["median_delta"] == 2.0


def test_conditioned_curves_describe_the_reference_profile_s_own_neighbourhood(monkeypatch):
    """The direct fix for "the marginal view doesn't describe the crown's neighbourhood":
    hold everything else near the crown and let only the plotted lever move."""
    out = _landscape(monkeypatch, _confounded_field())
    assert out["reference"]["overall"] == 82.0, "defaults to the best measured profile"
    cond = next(c for c in out["conditioned_curves"] if c["key"] == "Upload::quantum")
    best = max(cond["curve"], key=lambda p: p["overall"])
    assert best["value"] == 500, "near the crown, 500 beats 475 — the opposite of the marginal"
    # And it is honest about how thin each point is.
    assert all(p["profiles"] >= 1 for p in cond["curve"])


def test_separate_basins_are_counted(monkeypatch):
    """If the surface had one optimum, marginal curves would be enough and hill-climbing
    from anywhere would find it. Two separated local maxima say the levers are coupled."""
    out = _landscape(monkeypatch, _confounded_field())
    names = {b["fingerprint"] for b in out["basins"]}
    assert "b" in names, "the 7313 family's peak"
    assert len(out["basins"]) >= 1
    # The top basin is the crown, and each other basin records how far it sits from a
    # better one — the valley you'd have to cross to escape it.
    assert out["basins"][0]["overall"] == 82.0
    assert all(
        b["levers_from_better"] is None or b["levers_from_better"] >= 1 for b in out["basins"]
    )


def test_a_lever_with_no_siblings_reports_nothing_rather_than_guessing(monkeypatch):
    """Where the record holds no controlled comparison, the honest output is silence — the
    answer is to run the experiment, not to model harder."""
    profiles = [
        _pair("a", dl_q=7313, dl_t=5, ul_q=500, overall=80.0),
        _pair("b", dl_q=1257, dl_t=4, ul_q=475, overall=70.0),
        _pair("c", dl_q=3000, dl_t=6, ul_q=625, overall=65.0),
        _pair("d", dl_q=900, dl_t=3, ul_q=750, overall=60.0),
    ]
    out = _landscape(monkeypatch, profiles)
    # Every profile differs from every other in all three levers at once.
    assert out["matched_pairs"] == []


def test_coupled_basins_are_two_levers_apart(monkeypatch):
    """The structure that makes one-lever-at-a-time tuning fail.

    Two optima that need *both* levers moved together: (t5, q500) and (t4, q750). Each
    mixed corner is worse than either, so from one basin every single-lever step goes
    downhill and a coordinate-wise search stops there — while the marginal curve, averaging
    the two basins and the two bad corners, describes neither.
    """
    profiles = [
        _pair("t5q500", dl_q=7313, dl_t=5, ul_q=500, overall=82.0),  # basin 1
        _pair("t4q750", dl_q=7313, dl_t=4, ul_q=750, overall=81.0),  # basin 2
        _pair("t5q750", dl_q=7313, dl_t=5, ul_q=750, overall=64.0),  # mixed: LCP collapses
        _pair("t4q500", dl_q=7313, dl_t=4, ul_q=500, overall=70.0),  # mixed: weak FCP
    ]
    out = _landscape(monkeypatch, profiles)
    basins = {b["fingerprint"]: b for b in out["basins"]}
    assert set(basins) == {"t5q500", "t4q750"}, "both corners are local optima"
    # …and you cannot walk from one to the other one lever at a time.
    assert basins["t4q750"]["levers_from_better"] == 2

    # The marginal curve on upload quantum splits the difference and describes neither
    # basin: 500 averages 82 and 70, 750 averages 81 and 64.
    curve = next(c for c in out["curves"] if c["key"] == "Upload::quantum")
    assert curve["best_value"] == 500
    assert curve["best_overall"] == 76.0, "an average of a basin and a bad corner"


def test_matched_pairs_lead_with_the_best_evidence_not_the_biggest_number(monkeypatch):
    """A transition backed by several agreeing pairs is a finding; one backed by a single
    pair with a dramatic number is an anecdote. Sorting on the number alone buries the
    first under the second, which is how you end up chasing an outlier."""
    profiles = [
        # Four matched pairs that all agree on a modest effect (5 → 4 costs ~1).
        _pair("a1", dl_q=7313, dl_t=5, ul_q=500, overall=82.0),
        _pair("a2", dl_q=7313, dl_t=4, ul_q=500, overall=81.0),
        _pair("b1", dl_q=1257, dl_t=5, ul_q=500, overall=60.0),
        _pair("b2", dl_q=1257, dl_t=4, ul_q=500, overall=59.0),
        # …and one lone pair with a huge swing (5 → 6 on a single comparison).
        _pair("c1", dl_q=3000, dl_t=5, ul_q=475, overall=70.0),
        _pair("c2", dl_q=3000, dl_t=6, ul_q=475, overall=30.0),
    ]
    out = _landscape(monkeypatch, profiles)
    target = next(m for m in out["matched_pairs"] if m["key"] == "Download::target")
    first = target["transitions"][0]
    assert (first["from"], first["to"]) == (4, 5), "the well-evidenced move leads"
    assert first["pairs"] == 2 and first["consistent"] is True
    # The dramatic single-pair result is still reported, just not led with.
    assert any((t["from"], t["to"]) == (5, 6) for t in target["transitions"])


def test_a_candidate_prefers_controlled_evidence_over_the_marginal_curve(monkeypatch):
    """When the record already contains this exact move made under controlled conditions,
    that number beats any curve — the curve is an average over profiles that differ in
    other ways, which is the whole problem being worked around."""
    profiles = [
        # Matched pairs say 4 → 5 gains exactly 1.0, every time.
        _pair("a1", dl_q=7313, dl_t=4, ul_q=500, overall=81.0),
        _pair("a2", dl_q=7313, dl_t=5, ul_q=500, overall=82.0),
        _pair("b1", dl_q=1257, dl_t=4, ul_q=500, overall=59.0),
        _pair("b2", dl_q=1257, dl_t=5, ul_q=500, overall=60.0),
        # …while the marginal curve is dragged around by an unrelated family.
        _pair("c1", dl_q=3000, dl_t=4, ul_q=750, overall=90.0),
        _pair("c2", dl_q=3000, dl_t=6, ul_q=750, overall=20.0),
    ]
    out = _landscape(monkeypatch, profiles, suggestions=12)
    moved = [
        c for c in out["candidates"]
        if len(c["changes"]) == 1 and c["changes"][0]["key"] == "Download::target"
    ]
    for c in moved:
        assert c["evidence"], "every candidate says how its number was arrived at"
    # Any candidate whose exact move exists as a matched pair must say so.
    assert any("matched pair" in n for c in moved for n in c["evidence"]) or not moved


def test_two_lever_candidates_carry_a_wider_band_than_one_lever_ones(monkeypatch):
    """The prediction adds the two effects; the basin structure is the demonstration that
    they don't add. The honest response is a wider band, not a confident number."""
    profiles = [
        _pair("a", dl_q=1000, dl_t=3, ul_q=300, overall=60.0),
        _pair("b", dl_q=3000, dl_t=5, ul_q=500, overall=70.0),
        _pair("c", dl_q=6000, dl_t=8, ul_q=750, overall=65.0),
        _pair("d", dl_q=9000, dl_t=15, ul_q=900, overall=55.0),
    ]
    out = _landscape(monkeypatch, profiles, suggestions=12)
    singles = [c for c in out["candidates"] if not c["multi_lever"]]
    multis = [c for c in out["candidates"] if c["multi_lever"]]
    if multis and singles:
        assert max(c["uncertainty"] for c in multis) > min(c["uncertainty"] for c in singles)
    assert all(len(c["changes"]) > 1 for c in multis)


def test_no_candidate_or_gap_ever_suggests_a_non_positive_value(monkeypatch):
    """A declared sweep step can exceed a lever's minimum, and "try 0" is not a setting."""
    profiles = [
        _pair("a", dl_q=7313, dl_t=5, ul_q=475, overall=80.0),
        _pair("b", dl_q=7313, dl_t=5, ul_q=500, overall=78.0),
        _pair("c", dl_q=7313, dl_t=5, ul_q=625, overall=60.0),
        _pair("d", dl_q=7313, dl_t=5, ul_q=750, overall=55.0),
    ]
    out = _landscape(monkeypatch, profiles, suggestions=12)
    assert all(g["suggest"] > 0 for g in out["gaps"])
    for c in out["candidates"]:
        assert all(ch["to"] > 0 for ch in c["changes"])


def test_the_multi_lever_penalty_is_a_cost_not_a_bonus(monkeypatch):
    """An upper confidence bound rewards uncertainty — that's what makes it explore. So
    widening the band for a two-lever move, without subtracting it again, would push
    exactly the candidates we trust least to the top of the list."""
    profiles = [
        _pair("a", dl_q=1000, dl_t=3, ul_q=300, overall=60.0),
        _pair("b", dl_q=3000, dl_t=5, ul_q=500, overall=70.0),
        _pair("c", dl_q=6000, dl_t=8, ul_q=750, overall=65.0),
        _pair("d", dl_q=9000, dl_t=15, ul_q=900, overall=55.0),
    ]
    out = _landscape(monkeypatch, profiles, suggestions=12)
    for c in out["candidates"]:
        # The band is wider for a two-lever move…
        if c["multi_lever"]:
            # …but the score it earns is *lower* than the band alone would imply.
            assert c["upside"] < c["predicted"] + c["uncertainty"]
        assert 0.0 <= c["predicted"] <= 100.0, "the Overall scale is 0-100"
        assert 0.0 <= c["upside"] <= 100.0, "and an upside outside it is broken, not bold"


def test_a_proven_value_is_transplanted_onto_a_profile_that_has_never_run_it(monkeypatch):
    """The move that coupled levers make interesting: take the crown and give it the value
    from the *other* basin. The value is old, the combination is new — and it is the only
    kind of candidate the matched-pair record can price directly, since every other kind
    proposes a value nobody has measured at all.
    """
    profiles = [
        _pair("crown", dl_q=7313, dl_t=5, ul_q=500, overall=82.0),
        _pair("alt", dl_q=7313, dl_t=4, ul_q=750, overall=81.0),
        _pair("mix", dl_q=7313, dl_t=5, ul_q=750, overall=64.0),
        _pair("low", dl_q=1257, dl_t=5, ul_q=500, overall=58.0),
        _pair("low4", dl_q=1257, dl_t=4, ul_q=500, overall=57.0),
    ]
    out = _landscape(monkeypatch, profiles, suggestions=12)
    transplants = [
        c for c in out["candidates"]
        if any("proven elsewhere" in ch["why"] for ch in c["changes"])
    ]
    assert transplants, "a value that works elsewhere must be offered to profiles missing it"
    # And where a controlled comparison for that exact move exists, it prices it.
    priced = [
        c for c in transplants
        if any("measured directly on a matched pair" in n for n in c["evidence"])
    ]
    assert priced, "a transplant whose move is a matched pair is priced from that pair"


def test_a_confounded_curve_is_discounted_when_it_drives_a_prediction(monkeypatch):
    """Believing a confounded average whole is exactly how it becomes a confident
    prediction — the failure this whole section exists to prevent. Where nothing better
    is available the delta is used, but shrunk, and the candidate says so."""
    out = _landscape(monkeypatch, _confounded_field(), suggestions=12)
    confounded_keys = {c["key"] for c in out["curves"] if c["confounded"]}
    assert confounded_keys, "the fixture is built to confound the upload-quantum curve"
    from_confounded = [
        c for c in out["candidates"]
        if any("confounded marginal curve" in n for n in c["evidence"])
    ]
    for c in from_confounded:
        assert any(ch["key"] in confounded_keys for ch in c["changes"])
    # And a prediction never leaves the Overall's scale, however enthusiastic the curve.
    assert all(0.0 <= c["predicted"] <= 100.0 for c in out["candidates"])


# ── A hole in coverage you can actually press ────────────────────────────────────────
#
# A gap names a *value* nobody has measured, which is a finding and not something anyone
# can run: the obvious way to turn one into a profile is what a person does by hand — take
# the best profile measured and move that one lever. Until it carried that variant the
# section could say "nothing measured between 1400 and 9000" and offer no way to go and
# measure it.


def test_a_gap_carries_a_runnable_variant_of_the_best_profile(monkeypatch):
    profiles = [
        _profile("a", quantum=1000, target=5, overall=60.0),
        _profile("b", quantum=1200, target=5, overall=62.0),
        _profile("c", quantum=1400, target=5, overall=64.0),
        _profile("d", quantum=9000, target=5, overall=66.0),
    ]
    out = _landscape(monkeypatch, profiles)
    gap = next(g for g in out["gaps"] if g["key"] == "Download::quantum" and g["kind"] == "gap")
    cand = gap["candidate"]
    # Branched from the best measured profile, with exactly the one untested lever moved.
    assert cand["parent"]["fingerprint"] == "d"
    assert [c["key"] for c in cand["changes"]] == ["Download::quantum"]
    assert cand["changes"][0]["to"] == gap["suggest"] == 5200
    assert cand["multi_lever"] is False
    # …and priced like any other candidate, so the page can post it unchanged.
    assert cand["predicted"] and cand["uncertainty"] and cand["settings"]
    assert cand["already_measured"] is False


def test_an_edge_proposes_going_beyond_the_range_from_the_best_profile(monkeypatch):
    profiles = [
        _profile("a", quantum=1000, target=5, overall=60.0),
        _profile("b", quantum=2000, target=5, overall=70.0),
        _profile("c", quantum=3000, target=5, overall=80.0),
        _profile("d", quantum=4000, target=5, overall=90.0),
    ]
    out = _landscape(monkeypatch, profiles)
    edge = next(g for g in out["gaps"] if g["key"] == "Download::quantum" and g["kind"] == "edge")
    cand = edge["candidate"]
    assert cand["parent"]["fingerprint"] == "d", "the best profile is the one to extend"
    assert cand["changes"][0]["to"] > 4000
    assert "steps past" in cand["changes"][0]["why"]


def _gap_parts(monkeypatch, profiles):
    """The landscape's own pieces, for calling ``attach_gap_candidates`` directly with a
    chosen "already tried" set — which is the only way to express "the winner has been
    here", since adding a profile at the suggested value moves the gap somewhere else."""
    out = _landscape(monkeypatch, profiles)
    axes = {a["key"]: a for a in out["axes"]}
    gap = next(g for g in out["gaps"] if g["key"] == "Download::quantum" and g["kind"] == "gap")
    return out, axes, dict(gap)


def _variant_of(point: dict, key: str, value: float) -> tuple:
    coords = dict(point["coords"])
    coords[key] = float(value)
    return tuple(sorted(coords.items()))


# A hole between 1400 and 9000 on the download quantum, with the upload quantum varying
# too — so each profile is a distinct point and "this parent has already been there" is a
# statement about one profile rather than about the whole field.
_HOLE = [
    ("a", 1000, 1514, 60.0),
    ("b", 1200, 1600, 62.0),
    ("c", 1400, 1700, 64.0),
    ("d", 9000, 1800, 66.0),
]


def _hole_profiles():
    return [
        _profile(fp, quantum=q, target=5, up_quantum=uq, overall=o)
        for fp, q, uq, o in _HOLE
    ]


def test_the_gap_variant_falls_to_the_next_best_parent_when_the_winner_has_been_there(monkeypatch):
    """The winner already having run the suggested value can't answer the question; the
    runner-up can. Walking down the ranking beats dropping the finding."""
    profiles = _hole_profiles()
    out, axes, gap = _gap_parts(monkeypatch, profiles)
    best = max(out["points"], key=lambda p: p["overall"])
    gaps = [gap]
    explore.attach_gap_candidates(
        gaps, out["points"], axes, out["curves"],
        already_tried={_variant_of(best, gap["key"], gap["suggest"])},
    )
    cand = gaps[0]["candidate"]
    assert cand["parent"]["fingerprint"] != best["fingerprint"]
    assert cand["already_measured"] is False
    assert cand["changes"][0]["to"] == gap["suggest"]


def test_a_closed_hole_says_so_rather_than_losing_its_button(monkeypatch):
    """When every candidate parent has already been to the suggested value the entry is
    flagged, not dropped — "this hole is closed" is a truthful answer and a silently
    missing button is not."""
    profiles = _hole_profiles()
    out, axes, gap = _gap_parts(monkeypatch, profiles)
    gaps = [gap]
    explore.attach_gap_candidates(
        gaps, out["points"], axes, out["curves"],
        already_tried={_variant_of(p, gap["key"], gap["suggest"]) for p in out["points"]},
    )
    assert gaps[0]["candidate"]["already_measured"] is True


def test_a_gap_suggestion_is_the_value_the_firewall_can_actually_hold(monkeypatch):
    """The number on screen has to be the number that runs. CoDel target is a select keyed
    by a bare integer, so a midpoint of 6.5ms is a suggestion that cannot exist — the apply
    quantizes it and the ledger then grades the claim against a profile that isn't it."""
    profiles = [
        _profile("a", quantum=1000, target=3, overall=60.0),
        _profile("b", quantum=1200, target=4, overall=62.0),
        _profile("c", quantum=1400, target=5, overall=64.0),
        _profile("d", quantum=1600, target=40, overall=66.0),
    ]
    out = _landscape(monkeypatch, profiles)
    gap = next(
        (g for g in out["gaps"] if g["field"] == "target" and g["kind"] == "gap"), None
    )
    if gap is None:  # the target axis produced no wide hole in this field
        return
    assert float(gap["suggest"]) == int(gap["suggest"])
    assert gap["candidate"]["changes"][0]["to"] == float(gap["suggest"])


def _spread(p: dict, *, iqr: float, runs: int) -> dict:
    """Give a fixture profile the spread/sample fields the anchor shrinkage reads."""
    p["overall_p25"] = p["overall"] - iqr / 2
    p["overall_p75"] = p["overall"] + iqr / 2
    p["count"] = runs
    return p


def test_the_anchor_is_shrunk_on_a_packed_field_and_left_alone_on_a_spread_one(monkeypatch):
    """The winner's-curse correction the recommendation ledger demanded.

    On a packed field (profiles a fraction of a run's noise apart) the top Overall is the
    max of many noisy medians — biased high by construction, and anchoring predictions on
    it produced the ledger's measured -2 to -3 bias. On a genuinely spread field the same
    machinery must be a no-op: a 20-point gap is not sampling noise, and shrinking it away
    would just re-import the global-regression flaw the anchor exists to avoid.
    """
    # Packed: six settled profiles inside ~1 point, per-run IQR 2.5 over 25 runs each —
    # the observed spread is entirely explainable as sampling noise.
    packed = [
        _spread(_pair(f"p{i}", dl_q=1000 + i * 1000, dl_t=3 + (i % 3), ul_q=500,
                      overall=79.7 + 0.1 * i), iqr=2.5, runs=25)
        for i in range(6)
    ]
    packed.append(_spread(_pair("top", dl_q=7313, dl_t=5, ul_q=500, overall=81.0),
                          iqr=2.5, runs=25))
    out = _landscape(monkeypatch, packed, suggestions=30)
    assert out["candidates"], "a packed field still proposes candidates"
    for c in out["candidates"]:
        assert c["anchor"] < 81.0 - 0.3, (
            "on a packed field no anchor takes the top profile's raw Overall at face value"
        )

    # Spread: the same shapes 10+ points apart — the anchor must stay put.
    spread = [
        _spread(_pair(f"s{i}", dl_q=1000 + i * 1000, dl_t=3 + (i % 3), ul_q=500,
                      overall=20.0 + 12.0 * i), iqr=2.5, runs=25)
        for i in range(6)
    ]
    out = _landscape(monkeypatch, spread, suggestions=30)
    best = max(p["overall"] for p in spread)
    from_best = [c for c in out["candidates"] if c["parent"]["overall"] == best]
    assert from_best, "candidates still branch from the best profile"
    for c in from_best:
        assert abs(c["anchor"] - best) < 1.0, (
            "a real 12-point-per-step spread is not shrunk away"
        )


def test_a_single_matched_pair_is_not_believed_whole(monkeypatch):
    """A one-pair delta is the difference of two noisy medians; the ledger measured the
    class at 0%% in band with the miss growing in the claimed magnitude. Half of one
    pair's claim, not all of it."""
    profiles = [
        _pair("a4", dl_q=7313, dl_t=4, ul_q=500, overall=81.0),
        _pair("a5", dl_q=7313, dl_t=5, ul_q=500, overall=85.0),  # the pair claims +4
        _pair("b4", dl_q=1257, dl_t=4, ul_q=500, overall=60.0),
        _pair("c4", dl_q=3000, dl_t=4, ul_q=500, overall=61.0),
    ]
    out = _landscape(monkeypatch, profiles, suggestions=16)
    priced = [
        c for c in out["candidates"]
        if c["parent"]["fingerprint"] == "b4"
        and len(c["changes"]) == 1
        and c["changes"][0]["key"] == "Download::target"
        and c["changes"][0]["to"] == 5
    ]
    assert priced, "the transplant of the proven value is offered to b4"
    c = priced[0]
    # Anchor is untouched here (no spread info in the fixture), so predicted - parent
    # is exactly the shrunk pair delta: +4 x 1/(1+1) = +2.
    assert abs(c["predicted"] - (60.0 + 2.0)) < 0.25, c["predicted"]


def test_the_noise_floor_says_when_no_candidate_is_distinguishable(monkeypatch):
    """On a field whose profiles differ by less than run-to-run noise, ranking candidates
    by predicted score is theatre — the honest output is 'nothing here clears the floor,
    measure the coverage gaps'. The payload must say which situation the reader is in."""
    packed = [
        _spread(_pair(f"n{i}", dl_q=1000 + i * 1000, dl_t=3 + (i % 3), ul_q=500,
                      overall=79.7 + 0.05 * i), iqr=2.5, runs=25)
        for i in range(7)
    ]
    out = _landscape(monkeypatch, packed, suggestions=8)
    assert out["noise_floor"] is not None and out["noise_floor"] > 0
    assert out["candidates_clear_noise"] is False, (
        "a packed field must not report any candidate as clearing the noise floor"
    )
    for c in out["candidates"]:
        assert c.get("beats_noise") is False


def test_proposed_values_snap_to_the_firewalls_own_option_list(monkeypatch):
    """A CoDel target/interval is a select with a fixed option list; a value off it is
    silently not written and the benchmark measures a profile nobody proposed (three of
    the four 'not fully reachable' ledger rows proposed exactly that). With the provider's
    option list attached, every proposed value for that lever is on it."""
    profiles = [
        _pair(f"snap{i}", dl_q=7313, dl_t=t, ul_q=500, overall=70.0 + t)
        for i, t in enumerate((2, 5, 9, 12))
    ]
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: {"profiles": profiles})
    out = explore.landscape(
        None, suggestions=20, allowed_values={"target": [2, 5, 9, 12, 15]}
    )
    for c in out["candidates"]:
        for ch in c["changes"]:
            if ch["field"] == "target":
                assert ch["to"] in (2, 5, 9, 12, 15), ch
    for g in out["gaps"]:
        if g["field"] == "target" and g.get("candidate"):
            for ch in g["candidate"]["changes"]:
                if ch["field"] == "target":
                    assert ch["to"] in (2, 5, 9, 12, 15), ch


# ── "Run the smartest bets": queue the top N recommendations in one press ───────────────


def test_the_landscape_carries_both_orders(monkeypatch):
    """Exploring and betting are different questions over the same candidates, so the
    landscape answers both rather than making the page re-rank and risk disagreeing."""
    profiles = [
        _profile("a", quantum=1000, target=5, overall=60.0),
        _profile("b", quantum=2000, target=5, overall=70.0),
        _profile("c", quantum=3000, target=5, overall=80.0),
        _profile("d", quantum=9000, target=5, overall=65.0),
    ]
    out = _landscape(monkeypatch, profiles, suggestions=5)
    assert len(out["bets"]) == len(out["candidates"])
    # Bets are sorted by their floor, and every one carries what that floor rests on.
    floors = [b["confidence_score"] for b in out["bets"]]
    assert floors == sorted(floors, reverse=True)
    for bet in out["bets"]:
        assert bet["confidence"] in ("high", "medium", "low")
        assert bet["calibration_basis"] is not None
        assert bet["confidence_score"] <= bet["predicted"]


def _wide_field() -> list[dict]:
    """A field with enough variety that the candidate pool is bigger than any batch asks
    for — so a batch test is exercising the *ranking and queueing*, not the pool's size."""
    return [
        _profile("a", quantum=1000, target=3, overall=60.0, up_quantum=500),
        _profile("b", quantum=2000, target=5, overall=70.0, up_quantum=1000),
        _profile("c", quantum=3000, target=8, overall=80.0, up_quantum=1514),
        _profile("d", quantum=5000, target=10, overall=65.0, up_quantum=2000),
        _profile("e", quantum=7000, target=15, overall=55.0, up_quantum=3000),
        _profile("f", quantum=9000, target=20, overall=50.0, up_quantum=4000),
    ]


def _stub_start(monkeypatch, fail_first: bool = False):
    """Stand in for the apply → benchmark → restore session, recording what was asked for."""
    import pathbrain.api.routes_settings as rs_mod
    from fastapi import HTTPException

    started: list[dict] = []

    def fake_start(session, settings, label, iterations):
        started.append({"label": label, "iterations": iterations})
        n = len(started)
        if fail_first and n == 1:
            raise HTTPException(status_code=400, detail="Unreachable: non-writable field.")
        return {
            "id": n,
            "fingerprint": f"fp{n}",
            "iterations": iterations,
            "label": label,
            "existing_iterations": 0,
            "warnings": [],
            # Tests queue behind one another; only the first starts straight away.
            "queued": n > 1,
            "queue_position": n,
            "queue_ahead": n - 1,
            "blocked_by": None,
        }

    monkeypatch.setattr(rs_mod, "start_settings_test", fake_start)
    return started


def test_batch_queues_the_top_n_bets_at_the_requested_iterations(client, monkeypatch):
    """The ask: "grab the top X and test Y iterations each" — one press instead of X.

    Each goes through the ordinary single-test path, so they queue behind each other and
    land in the recommendation ledger exactly as an individually pressed test does.
    """
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: {"profiles": _wide_field()})
    started = _stub_start(monkeypatch)

    body = client.post("/api/explore/test-batch", json={"count": 3, "iterations": 4}).json()

    assert len(body["queued"]) == 3 and not body["skipped"]
    assert body["rank"] == "confidence" and body["iterations"] == 4
    assert [s["iterations"] for s in started] == [4, 4, 4]
    # Each queued row carries the reasoning that got it picked, not just an id.
    for row in body["queued"]:
        assert row["confidence_score"] is not None and row["predicted"] is not None
    # They line up rather than racing each other.
    assert [r["queue_position"] for r in body["queued"]] == [1, 2, 3]
    # And they are the *best bets*, in bet order — not the page's exploring order.
    floors = [r["confidence_score"] for r in body["queued"]]
    assert floors == sorted(floors, reverse=True)


def test_batch_can_still_be_asked_for_the_exploring_order(client, monkeypatch):
    """Betting and exploring are different questions, so the caller can pick. `upside`
    reproduces the page's own ranking — drawn to what we know least about."""
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: {"profiles": _wide_field()})
    _stub_start(monkeypatch)

    body = client.post(
        "/api/explore/test-batch", json={"count": 2, "rank": "upside"}
    ).json()
    assert body["rank"] == "upside" and len(body["queued"]) == 2


def test_batch_skips_a_bad_candidate_instead_of_failing_the_whole_run(client, monkeypatch):
    """One unreachable proposal must not cost the others their night — the same discipline
    `refresh` applies to a bad profile."""
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: {"profiles": _wide_field()})
    _stub_start(monkeypatch, fail_first=True)

    body = client.post("/api/explore/test-batch", json={"count": 3}).json()
    assert len(body["skipped"]) == 1
    assert "Unreachable" in body["skipped"][0]["reason"]
    assert len(body["queued"]) == 2


def test_batch_refuses_when_there_is_nothing_to_propose(client, monkeypatch):
    """An empty batch should say why rather than reporting a successful run of nothing."""
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: {"profiles": []})
    resp = client.post("/api/explore/test-batch", json={"count": 3})
    assert resp.status_code == 400
    assert "enough comparable profiles" in resp.json()["detail"].lower() or resp.json()["detail"]


def test_a_batch_really_lines_up_queued_profile_tests(client, monkeypatch):
    """End to end, with nothing stubbed between the button and the queue.

    This is the join between the two features: batching is only possible *because* profile
    tests queue. Under the old singleton the second candidate would have been refused, so a
    "queue the top three" button could only ever have started one. Here the pipeline is held
    so nothing can run, and all three must be sitting in the queue in bet order, having
    applied nothing.
    """
    from sqlalchemy import select as sa_select

    from pathbrain import coordinator, profile_test
    from pathbrain.database import session_scope
    from pathbrain.models import ProfileTest, ProfileTestStatus
    from pathbrain.providers import get_provider
    from pathbrain.providers import mock as mock_mod
    from pathbrain.settings_profile import fingerprint, normalize

    mock_mod._OVERRIDES.clear()
    # Built on the live firewall's OWN pipe labels: a candidate's overrides are matched to
    # live pipes by label, so a field labelled anything else proposes changes to pipes that
    # do not exist and every one collapses to "nothing to change".
    live = normalize(get_provider().discover())
    labels = [p["label"] for p in live]
    field = [
        {
            "fingerprint": fp,
            "name": fp.title(),
            "label": f"q{quantum}",
            "overall": overall,
            "iterations": 30,
            "confident": True,
            "settings": [
                {"label": labels[0], "quantum": quantum, "target": f"{target}ms",
                 "scheduler": "fq_codel", "queues": 1},
                {"label": labels[1], "quantum": up_quantum, "target": f"{target}ms",
                 "scheduler": "fq_codel", "queues": 1},
            ],
        }
        for fp, quantum, target, overall, up_quantum in [
            ("ea", 1000, 3, 60.0, 500), ("eb", 2000, 5, 70.0, 1000),
            ("ec", 3000, 8, 80.0, 1514), ("ed", 5000, 10, 65.0, 2000),
            ("ee", 7000, 15, 55.0, 3000), ("ef", 9000, 20, 50.0, 4000),
        ]
    ]
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: {"profiles": field})

    with coordinator.hold("duel#99"):
        body = client.post(
            "/api/explore/test-batch", json={"count": 3, "iterations": 2}
        ).json()
        ids = [r["id"] for r in body["queued"]]
        try:
            # Not all three necessarily queue — a candidate the firewall cannot write is
            # reverted and becomes a no-op, which is the reachability check doing its job —
            # but a batch must line up more than one, which is the whole point of it.
            assert len(ids) >= 2, body.get("skipped")
            assert all(s["reason"] for s in body["skipped"])
            # Every queued row says it is behind the duel rather than claiming to run.
            assert all(r["queued"] for r in body["queued"])
            assert {r["blocked_by"] for r in body["queued"]} == {"a duel session"}
            assert [r["queue_position"] for r in body["queued"]] == list(
                range(1, len(ids) + 1)
            )

            with session_scope() as s_:
                rows = s_.scalars(
                    sa_select(ProfileTest)
                    .where(ProfileTest.id.in_(ids))
                    .order_by(ProfileTest.id)
                ).all()
                assert [r.id for r in rows] == sorted(ids)
                assert all(r.status == ProfileTestStatus.PENDING for r in rows)
                assert all(r.iterations == 2 for r in rows)
                # Each carries its OWN target — the whole reason the target moved onto the
                # row rather than staying in one module-level slot.
                assert all(r.target for r in rows)
                assert len({fingerprint(r.target) for r in rows}) == len(rows)
                # Nothing applied: a queued test has not touched the firewall.
                assert all(r.baseline is None for r in rows)
        finally:
            # Always drop them, or a failed assertion leaves real benchmarks to run at
            # teardown against the shared mock firewall.
            for test_id in ids:
                client.post(f"/api/settings/test-profile/{test_id}/cancel")

    for _ in range(200):
        if not profile_test.active():
            break
        time.sleep(0.02)
    mock_mod._OVERRIDES.clear()


# ── The ring prices a move before any matched pair ───────────────────────────────
#
# The duel ring's lever ledger is paired, interleaved, same-weather evidence on an exact
# single-lever move — controlled by design, where a matched pair is controlled by
# coincidence and measured on different nights. Explore reads it once per landscape and
# prices a move from it first; the ledger grades those claims as their own class.


def _ledger_with(monkeypatch, *levers: dict) -> None:
    """Stand in for the duel ledger's book (``levers.lever_ledger``) with lever rows in its
    own shape, so ``ring_transitions`` does the keying and the state reading for real."""
    import pathbrain.levers as levers_mod

    monkeypatch.setattr(
        levers_mod, "lever_ledger", lambda session, limit_sessions=50: {"levers": list(levers)}
    )


def _fought(pipe: str, field: str, lo: float, hi: float, margin_up: float, rounds: int,
            *, p: float | None = 0.01) -> dict:
    """One lever with one transition, as ``lever_ledger`` reports it: ``lo → hi`` with the
    margin signed as moving UP."""
    return {
        "pipe": pipe, "field": field, "field_label": field, "unit": None,
        "transitions": [{
            "from": lo, "to": hi, "median_margin_up": margin_up, "rounds": rounds,
            "paired_rounds": rounds, "matches": 1, "sign_p": p, "paired_p": p,
            "direction": "higher" if margin_up > 0 else "lower", "margin_se": 0.4,
        }],
    }


def _book(monkeypatch) -> dict:
    """A significant, a null and a thin transition on three different levers."""
    _ledger_with(
        monkeypatch,
        _fought("Download", "target", 4, 5, 2.0, 12, p=0.01),        # significant
        _fought("Upload", "limit", 512, 1024, 0.2, 16, p=0.6),        # null: 16 rounds inside the floor
        _fought("Download", "quantum", 1400, 5200, 1.5, 3, p=None),  # thin: under MIN_ROUNDS
        {"pipe": "Download", "field": "ecn", "transitions": []},      # never fought: no key
    )
    return explore.ring_transitions(None)


def test_ring_transitions_are_keyed_like_the_axes_and_carry_their_state(monkeypatch):
    book = _book(monkeypatch)
    assert set(book) == {"Download::target", "Upload::limit", "Download::quantum"}
    t = book["Download::target"]["transitions"][0]
    assert (t["from"], t["to"], t["margin_up"]) == (4.0, 5.0, 2.0)
    assert t["significant"] and not t["null"] and not t["thin"]
    u = book["Upload::limit"]["transitions"][0]
    assert u["null"] and not u["significant"] and not u["thin"]
    q = book["Download::quantum"]["transitions"][0]
    assert q["thin"] and not q["significant"] and not q["null"]
    assert book["Download::target"]["total_rounds"] == 12


def test_an_unreadable_ledger_is_an_empty_book_never_an_error(monkeypatch):
    import pathbrain.levers as levers_mod

    def boom(*a, **k):
        raise RuntimeError("no database here")

    monkeypatch.setattr(levers_mod, "lever_ledger", boom)
    assert explore.ring_transitions(None) == {}
    # And the landscape still prices from the pooled record alone.
    out = _landscape(monkeypatch, _hole_profiles())
    assert out["ring_transitions"] == [] and out["candidates"]


def test_the_rings_three_states_price_a_move_three_ways(monkeypatch):
    """Significant: the margin shrunk by rounds/(rounds+2). Null: nothing added, said in
    words. Thin: informs without steering, shrunk against the bar it hasn't reached."""
    book = _book(monkeypatch)
    parent = {
        "overall": 50.0,
        "coords": {"Download::target": 4.0, "Upload::limit": 512.0, "Download::quantum": 1400.0},
    }
    pred, notes = explore._predict(parent, {"Download::target": 5.0}, {}, ring_by_key=book)
    assert abs(pred - (50.0 + 2.0 * 12 / 14)) < 1e-9
    assert notes[0].startswith("measured in the ring") and "gained 2.00" in notes[0] and "86%" in notes[0]

    pred, notes = explore._predict(parent, {"Upload::limit": 1024.0}, {}, ring_by_key=book)
    assert pred == 50.0 and "no gain" in notes[0] and "nothing added" in notes[0]

    pred, notes = explore._predict(parent, {"Download::quantum": 5200.0}, {}, ring_by_key=book)
    assert abs(pred - (50.0 + 1.5 * 3 / (3 + explore.RING_MIN_ROUNDS))) < 1e-9
    assert "informs without steering" in notes[0]


def test_a_move_down_takes_the_ring_margin_with_its_sign_flipped(monkeypatch):
    """The book records moving UP; a candidate moving the lever DOWN pays the same margin
    the other way — the move is the same experiment read from the other end."""
    book = _book(monkeypatch)
    parent = {"overall": 50.0, "coords": {"Download::target": 5.0}}
    pred, notes = explore._predict(parent, {"Download::target": 4.0}, {}, ring_by_key=book)
    assert abs(pred - (50.0 - 2.0 * 12 / 14)) < 1e-9
    assert "cost 2.00" in notes[0]


def test_the_ring_prices_a_move_before_any_matched_pair(monkeypatch):
    """The pairs say 4 → 5 is +1.0; the ring fought the same move and says +2.0. The ring
    prices it, the candidate says so, and the pair is not consulted for that leg."""
    profiles = [
        _pair("a1", dl_q=7313, dl_t=4, ul_q=500, overall=81.0),
        _pair("a2", dl_q=7313, dl_t=5, ul_q=500, overall=82.0),
        _pair("b1", dl_q=1257, dl_t=4, ul_q=500, overall=59.0),
        _pair("b2", dl_q=1257, dl_t=5, ul_q=500, overall=60.0),
        _pair("c1", dl_q=3000, dl_t=4, ul_q=750, overall=90.0),
        _pair("c2", dl_q=3000, dl_t=6, ul_q=750, overall=20.0),
    ]
    out = _landscape(monkeypatch, profiles)
    axes = {a["key"]: a for a in out["axes"]}
    by_key = {c["key"]: c for c in out["curves"]}
    pairs_by_key = {m["key"]: m for m in out["matched_pairs"]}
    assert "Download::target" in pairs_by_key, "the fixture must carry the matched pair"
    parent = next(p for p in out["points"] if p["fingerprint"] == "c1")  # target 4, never at 5
    move = {"Download::target": (5.0, "test")}
    book = _book(monkeypatch)

    priced = explore._candidate_dict(
        parent, move, axes, out["points"], by_key, pairs_by_key, None, ring_by_key=book
    )
    assert priced["evidence"][0].startswith("measured in the ring")
    assert not any("matched pair" in n for n in priced["evidence"])
    assert priced["ring"]["rounds"] == 12 and priced["ring"]["state"] == "measured"
    assert priced["ring"]["margin"] == 2.0 and priced["ring"]["legs"][0]["key"] == "Download::target"
    assert abs(priced["predicted"] - (priced["anchor"] + 2.0 * 12 / 14)) < 0.02

    plain = explore._candidate_dict(parent, move, axes, out["points"], by_key, pairs_by_key, None)
    assert any("matched pair" in n for n in plain["evidence"]) and plain["ring"] is None
    assert priced["predicted"] > plain["predicted"]


def test_the_landscape_threads_the_ring_into_every_priced_proposal(monkeypatch):
    """A gap's runnable variant is priced by the same machinery as a headline candidate, so
    it must read the ring too; and the payload carries the ring's rows only for levers the
    axes actually have."""
    profiles = _hole_profiles()
    first = _landscape(monkeypatch, profiles)
    gap = next(g for g in first["gaps"] if g["key"] == "Download::quantum" and g["kind"] == "gap")
    parent_value = gap["candidate"]["changes"][0]["from"]
    suggest = gap["suggest"]
    lo, hi = sorted((parent_value, suggest))
    # The book records moving UP; the variant should GAIN 1.2 whichever way it moves.
    _ledger_with(
        monkeypatch,
        _fought("Download", "quantum", lo, hi, 1.2 if suggest == hi else -1.2, 10, p=0.02),
        _fought("Upload", "flows", 1024, 2048, 0.5, 9),  # a lever these profiles don't carry
    )
    out = _landscape(monkeypatch, profiles)
    gap = next(g for g in out["gaps"] if g["key"] == "Download::quantum" and g["kind"] == "gap")
    cand = gap["candidate"]
    assert cand["evidence"][0].startswith("measured in the ring")
    assert cand["ring"]["rounds"] == 10 and cand["ring"]["margin"] == 1.2
    assert [r["key"] for r in out["ring_transitions"]] == ["Download::quantum"]
    assert out["ring_transitions"][0]["transitions"][0]["rounds"] == 10
