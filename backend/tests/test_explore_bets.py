"""The bet ranking: which recommendations are worth actually running, and why.

The landscape ranks candidates by an upper confidence bound, which is right for *exploring*
— uncertainty is an attraction when the question is "where might we beat everything?". It is
the wrong order for "queue five of these tonight", which is a question about what to back.
So bets are scored at the pessimistic end of a band widened by what that evidence class has
historically missed by, measured from the recommendation ledger.
"""
from __future__ import annotations

from pathbrain import explore
from pathbrain.explore import rank_bets


def _candidate(name, predicted, uncertainty, evidence):
    return {
        "changes": [{"key": name, "pipe": "Download", "field_label": "quantum", "to": 1}],
        "predicted": predicted,
        "uncertainty": uncertainty,
        "evidence": evidence,
        "upside": predicted + uncertainty,
        "settings": {},
        "parent": {"fingerprint": "p", "name": "P", "label": "p", "overall": 50.0},
    }


def test_a_bet_is_ranked_on_its_floor_not_its_ceiling():
    """Two candidates, same ceiling. The exploring order likes the vague one (it might be
    anything); the betting order takes the one that still wins if the model is wrong."""
    vague = _candidate("vague", 60.0, 6.0, ["from the marginal curve"])      # upside 66
    solid = _candidate("solid", 65.0, 1.0, ["from a matched pair"])          # upside 66

    # Exploring: the two are indistinguishable on upside, which is the point of the UCB.
    assert vague["upside"] == solid["upside"]

    bets = rank_bets([vague, solid])
    assert [b["changes"][0]["key"] for b in bets] == ["solid", "vague"]
    assert bets[0]["confidence_score"] == 64.0   # 65 - 1*1
    assert bets[1]["confidence_score"] == 54.0   # 60 - 1*6
    assert bets[0]["confidence"] == "high" and bets[1]["confidence"] == "low"


def test_the_ledgers_track_record_widens_an_overconfident_band():
    """The model states its own uncertainty; the ledger measures what that class actually
    missed by. A class claiming +/-1.0 while missing by 4.0 is overconfident, and the
    measured number is what the bet is judged on."""
    c = _candidate("x", 70.0, 1.0, ["from a confounded marginal curve"])

    uncalibrated = rank_bets([c])[0]
    assert uncalibrated["confidence_score"] == 69.0
    assert uncalibrated["calibration_basis"] == "stated"

    calibrated = rank_bets([c], {
        "confounded": {"graded": 9, "mean_abs_error": 4.0, "trusted": True},
    })[0]
    assert calibrated["confidence_band"] == 4.0
    assert calibrated["confidence_score"] == 66.0
    assert calibrated["calibration_basis"] == "measured"


def test_a_thin_track_record_is_reported_but_does_not_steer():
    """One unlucky measurement must not bury a whole class of proposals — below the minimum
    the class is untrusted and the model's own band stands."""
    c = _candidate("x", 70.0, 1.0, ["from a matched pair"])
    out = rank_bets([c], {
        "matched_pair": {"graded": 1, "mean_abs_error": 9.0, "trusted": False},
    })[0]
    assert out["confidence_band"] == 1.0
    assert out["calibration_basis"] == "stated"


def test_the_band_is_the_wider_of_the_two_never_a_blend():
    """A model that states a band WIDER than the class's measured miss keeps its own — the
    rule is 'never narrower than the track record', not 'always the track record'."""
    c = _candidate("x", 70.0, 5.0, ["from a matched pair"])
    out = rank_bets([c], {
        "matched_pair": {"graded": 20, "mean_abs_error": 1.0, "trusted": True},
    })[0]
    assert out["confidence_band"] == 5.0
    assert out["calibration_basis"] == "stated (wider than measured)"


def test_clearing_the_bar_is_the_floor_beating_the_best_measured():
    """The strong claim: 'even wrong by its usual amount, this still beats the best
    profile we have' — as opposed to the upside claim the exploring order makes."""
    strong = _candidate("strong", 70.0, 1.0, ["from a matched pair"])   # floor 69
    weak = _candidate("weak", 70.0, 8.0, ["from the marginal curve"])   # floor 62

    bets = rank_bets([strong, weak], None, best_overall=65.0)
    by_key = {b["changes"][0]["key"]: b for b in bets}
    assert by_key["strong"]["clears_bar"] is True
    assert by_key["weak"]["clears_bar"] is False
    # Both still make the optimistic claim, which is why the two rankings disagree.
    assert strong["upside"] > 65.0 and weak["upside"] > 65.0


def test_a_bet_score_never_leaves_the_overall_scale():
    """The Overall is 0-100; a huge band must floor at 0 rather than going negative."""
    out = rank_bets([_candidate("x", 3.0, 40.0, ["from the marginal curve"])])[0]
    assert out["confidence_score"] == explore.OVERALL_MIN


def test_ranking_does_not_mutate_the_candidates_it_was_given():
    """The landscape returns both orders over the same underlying list; annotating in place
    would leak bet fields into the exploring view and make the two disagree about what a
    candidate is."""
    c = _candidate("x", 70.0, 1.0, ["from a matched pair"])
    rank_bets([c])
    assert "confidence_score" not in c


# ── The bets pool is every runnable proposal, not just the headline list ────────────────


def _with_candidate(key, to, parent="p"):
    return {
        "changes": [{"key": key, "pipe": "Download", "field_label": "quantum", "to": to}],
        "predicted": 60.0, "uncertainty": 1.0, "evidence": ["from a matched pair"],
        "upside": 61.0, "settings": [{"label": "Download", "quantum": to}],
        "parent": {"fingerprint": parent, "name": "P", "label": "p", "overall": 50.0},
    }


def test_coverage_holes_and_crown_leg_moves_are_runnable_bets_too():
    """Explore proposes in three places — the ranked list, the variant attached to each hole
    in coverage, and the crown-leg "move the best profile the leaders' way". All three are
    built by the same `_candidate_dict` and post the same payload, so a batch that only saw
    the headline list was ignoring most of what the page had already worked out."""
    from pathbrain.explore import runnable_candidates

    out = runnable_candidates(
        [_with_candidate("Download::quantum", 5000)],
        [{"candidate": _with_candidate("Download::target", 6)}],
        {"legs": [{
            "moves": [{"candidate": _with_candidate("Upload::quantum", 900)}],
            "combined": _with_candidate("Download::interval", 40),
        }]},
    )
    assert {c["changes"][0]["key"] for c in out} == {
        "Download::quantum", "Download::target", "Upload::quantum", "Download::interval",
    }


def test_the_same_move_proposed_twice_is_one_bet():
    """A lever move genuinely appears both as a ranked candidate and as the variant that
    fills a hole. Queueing it twice spends two benchmarks answering one question."""
    from pathbrain.explore import runnable_candidates

    same = _with_candidate("Download::quantum", 5000)
    out = runnable_candidates([same], [{"candidate": _with_candidate("Download::quantum", 5000)}])
    assert len(out) == 1


def test_a_hole_with_no_runnable_variant_contributes_nothing():
    """`already_measured` holes and legs whose move is an existing profile carry no
    `candidate` — they are answers, not proposals."""
    from pathbrain.explore import runnable_candidates

    out = runnable_candidates(
        [],
        [{"already_measured": True}, {"existing": {"fingerprint": "x"}}],
        {"legs": [{"moves": [{"existing": {"fingerprint": "y"}}], "combined": None}]},
    )
    assert out == []
