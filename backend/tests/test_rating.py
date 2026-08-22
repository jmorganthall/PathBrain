"""Bradley–Terry ratings for the duel ladder.

The property that matters, and the reason the league table was replaced: **who** you beat
has to change your standing, not just how many. These tests pin that down, along with the
regularization that stops a three-pair record from topping the table.
"""
from __future__ import annotations

from pathbrain.rating import fit_bradley_terry, win_probability


def test_beating_a_strong_profile_moves_you_more_than_beating_a_weak_one():
    """The whole point. In a points league both are worth 3; here they can't be."""
    # A field where "top" has beaten everyone and "bottom" has beaten nobody.
    base = {
        ("top", "mid1"): 12, ("mid1", "top"): 3,
        ("top", "mid2"): 12, ("mid2", "top"): 3,
        ("mid1", "bottom"): 12, ("bottom", "mid1"): 3,
        ("mid2", "bottom"): 12, ("bottom", "mid2"): 3,
        # Two identical newcomers, each with the same record against the same middle.
        ("x", "mid1"): 6, ("mid1", "x"): 6,
        ("y", "mid2"): 6, ("mid2", "y"): 6,
    }
    before = fit_bradley_terry(base)
    assert abs(before["x"]["rating"] - before["y"]["rating"]) < 1.0, "x and y start equal"

    # Now x beats the TOP profile 8-2, and y beats the BOTTOM profile 8-2. Same scoreline,
    # same number of pairs — a league table would award both exactly 3 points.
    beat_top = {**base, ("x", "top"): 8, ("top", "x"): 2}
    beat_bottom = {**base, ("y", "bottom"): 8, ("bottom", "y"): 2}
    gain_x = fit_bradley_terry(beat_top)["x"]["rating"] - before["x"]["rating"]
    gain_y = fit_bradley_terry(beat_bottom)["y"]["rating"] - before["y"]["rating"]

    assert gain_x > gain_y, "beating the best must be worth more than beating the worst"
    assert gain_x > 0 and gain_y >= 0


def test_losing_to_the_best_costs_less_than_losing_to_the_worst():
    """The mirror image, and the reason a champion isn't punished for defending."""
    base = {
        ("top", "mid"): 20, ("mid", "top"): 5,
        ("mid", "bottom"): 20, ("bottom", "mid"): 5,
        ("x", "mid"): 8, ("mid", "x"): 8,
        ("y", "mid"): 8, ("mid", "y"): 8,
    }
    before = fit_bradley_terry(base)
    lost_to_top = {**base, ("top", "x"): 8, ("x", "top"): 2}
    lost_to_bottom = {**base, ("bottom", "y"): 8, ("y", "bottom"): 2}
    drop_x = before["x"]["rating"] - fit_bradley_terry(lost_to_top)["x"]["rating"]
    drop_y = before["y"]["rating"] - fit_bradley_terry(lost_to_bottom)["y"]["rating"]
    assert drop_y > drop_x, "losing to a filler profile must hurt more than losing to the best"


def test_profiles_that_never_met_are_still_ranked_through_shared_opponents():
    """A league table cannot compare two profiles with no common result. This can."""
    ratings = fit_bradley_terry(
        {
            ("a", "shared"): 18, ("shared", "a"): 2,
            ("shared", "c"): 18, ("c", "shared"): 2,
        }
    )
    # a and c never met, but a dominated the profile that dominated c.
    assert ratings["a"]["rating"] > ratings["shared"]["rating"] > ratings["c"]["rating"]


def test_a_thin_unbeaten_record_does_not_outrank_an_established_one():
    """Plain Bradley–Terry sends an unbeaten profile to infinity; the prior holds it in
    the field until it has actually proved something."""
    ratings = fit_bradley_terry(
        {
            # A veteran that has beaten good opposition many times over, across the field.
            ("veteran", "good"): 60, ("good", "veteran"): 20,
            ("veteran", "weak"): 30, ("weak", "veteran"): 6,
            ("good", "weak"): 40, ("weak", "good"): 10,
            # A newcomer that went 3-0 in one snap bout against the weakest profile.
            ("newcomer", "weak"): 3,
        }
    )
    assert all(r["rating"] == r["rating"] for r in ratings.values()), "no NaN/inf"
    assert ratings["veteran"]["rating"] > ratings["newcomer"]["rating"]
    assert ratings["newcomer"]["provisional"] is True
    assert ratings["veteran"]["provisional"] is False
    # And the thin rating says so in its own error bar.
    assert ratings["newcomer"]["rating_se"] > ratings["veteran"]["rating_se"]


def test_the_fit_is_order_independent_and_deterministic():
    """It's a fit over the whole ledger, not a running tally — so re-deriving the table
    from the same record always gives the same answer, whatever order it's read in."""
    record = {
        ("a", "b"): 7, ("b", "a"): 3,
        ("b", "c"): 9, ("c", "b"): 4,
        ("a", "c"): 6, ("c", "a"): 6,
    }
    first = fit_bradley_terry(record)
    shuffled = dict(reversed(list(record.items())))
    second = fit_bradley_terry(shuffled)
    assert {k: v["rating"] for k, v in first.items()} == {
        k: v["rating"] for k, v in second.items()
    }


def test_expected_wins_reads_as_over_or_under_performance():
    """A profile winning more pairs than the model expects is beating its schedule."""
    ratings = fit_bradley_terry({("a", "b"): 15, ("b", "a"): 5})
    assert ratings["a"]["wins"] == 15 and ratings["a"]["losses"] == 5
    # At convergence the fit reproduces the record it was given.
    assert abs(ratings["a"]["expected_wins"] - 15) < 1.0


def test_win_probability_matches_the_elo_scale():
    assert abs(win_probability(1500, 1500) - 0.5) < 1e-9
    assert abs(win_probability(1900, 1500) - 10 / 11) < 1e-6


def test_an_empty_ledger_is_an_empty_table():
    assert fit_bradley_terry({}) == {}


def test_one_opponent_is_provisional_however_many_pairs_it_won():
    """A profile that has only ever fought one opponent has no position in the network —
    its rating is a single edge restated, however emphatic. It still ranks where the model
    puts it; it's just marked as not yet established."""
    ratings = fit_bradley_terry(
        {
            ("solo", "leader"): 40, ("leader", "solo"): 5,
            ("leader", "a"): 20, ("a", "leader"): 10,
            ("leader", "b"): 20, ("b", "leader"): 10,
            ("a", "b"): 12, ("b", "a"): 12,
        }
    )
    assert ratings["solo"]["opponents"] == 1
    assert ratings["solo"]["provisional"] is True, "one opponent is never established"
    assert ratings["leader"]["provisional"] is False
    # It is not demoted for it — the model still rates it above the profile it dominated.
    assert ratings["solo"]["rating"] > ratings["leader"]["rating"]


def test_the_prior_is_calibrated_between_the_two_failure_modes():
    """The prior is the one real tuning decision here, so pin both ends of it.

    Too light and a 3-0 sweep over the weakest profile in the field outrates a veteran
    that has beaten everyone. Too heavy and beating the strongest profile stops moving
    anyone, which defeats the purpose of rating rather than counting wins.
    """
    veterans = {
        ("veteran", "solid"): 30, ("solid", "veteran"): 10,
        ("veteran", "weak"): 18, ("weak", "veteran"): 4,
        ("solid", "weak"): 20, ("weak", "solid"): 5,
    }
    swept = fit_bradley_terry({**veterans, ("newcomer", "weak"): 3})
    assert swept["veteran"]["rating"] > swept["newcomer"]["rating"], (
        "sweeping the weakest profile must not outrate beating the whole field"
    )

    ladder = {
        ("leader", "mid"): 21, ("mid", "leader"): 7,
        ("leader", "other"): 19, ("other", "leader"): 9,
        ("mid", "tail"): 11, ("tail", "mid"): 3,
        ("other", "tail"): 10, ("tail", "other"): 3,
    }
    upset = fit_bradley_terry({**ladder, ("climber", "leader"): 9, ("leader", "climber"): 3})
    assert upset["climber"]["rating"] == max(r["rating"] for r in upset.values()), (
        "beating the ladder's leader 9-3 must take the top of the table"
    )
