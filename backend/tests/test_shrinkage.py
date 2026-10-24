"""The field as a prior: a thin profile's Overall is shrunk toward it, and its ceiling is a
posterior quantile rather than the p75 of one or two runs.

Pins the properties that make the ceiling an admission gate instead of a coin flip:

* a one-run profile's ceiling is finite, above its shrunk reading, and below the raw reading
  plus the raw bar — the old p75 (== the point estimate) is gone;
* evidence moves the estimate: the same reading on more runs shrinks less and carries a
  tighter band, and a well-measured profile is left where it reads;
* the winner's curse is priced: a lucky one-run 80 is pulled further than a thirty-run 80;
* the SE floor: no profile under `OWN_SE_MIN_RUNS` runs reads its own IQR as its noise;
* too small a field → a flat prior, which is the raw reading plus `CEILING_Z` SEs, stated.
"""
from __future__ import annotations

import math

import pytest

from pathbrain import shrinkage as sh


def _field(centre: float = 70.0, n: int = 20, spread: float = 2.0, runs: int = 30, iqr: float = 2.0):
    # A well-measured field: n profiles spread evenly ±spread about the centre.
    return [(centre + spread * (2 * i / (n - 1) - 1), iqr, runs) for i in range(n)]


def test_a_one_run_profile_is_shrunk_and_its_ceiling_is_no_longer_its_point_estimate():
    prior = sh.fit(_field())
    assert prior.basis == "fitted" and prior.noise_basis == "fitted"
    g = sh.grade(prior, 80.0, 0.0, 1)  # one run: IQR 0, the old p75 == 80.0
    assert g is not None
    assert prior.mean < g["shrunk"] < 80.0, "pulled toward the field, not onto it"
    assert g["ceiling"] > g["shrunk"], "the ceiling is above the shrunk reading"
    assert g["se"] == pytest.approx(prior.run_noise, abs=1e-3), "its noise is the field's, not zero"
    assert g["weight"] < 1.0
    # The old gate: with one run `_spread` returns p75 == median, so `optimistic` was 80.0 exactly.
    assert g["ceiling"] != 80.0


def test_more_runs_move_the_estimate_toward_the_reading_and_tighten_the_band():
    prior = sh.fit(_field())
    one = sh.grade(prior, 78.0, 0.0, 1)
    three = sh.grade(prior, 78.0, 1.0, 3)
    thirty = sh.grade(prior, 78.0, 1.0, 30)
    assert one["shrunk"] < three["shrunk"] < thirty["shrunk"] <= 78.0
    assert one["shrunk_se"] > three["shrunk_se"] > thirty["shrunk_se"]
    assert one["weight"] < three["weight"] < thirty["weight"]
    # A well-measured profile is left (nearly) where it reads.
    assert abs(thirty["shrunk"] - 78.0) < 0.3


def test_the_winners_curse_is_priced_a_lucky_thin_80_is_pulled_further_than_a_measured_80():
    prior = sh.fit(_field())
    thin = sh.grade(prior, 80.0, 0.0, 1)
    solid = sh.grade(prior, 80.0, 1.5, 30)
    assert thin["shrunk"] < solid["shrunk"]
    # And a thin reading at the centre is not moved at all: shrinkage pulls toward, never past.
    centred = sh.grade(prior, prior.mean, 0.0, 1)
    assert centred["shrunk"] == pytest.approx(prior.mean, abs=1e-9)


def test_the_se_floor_never_reads_a_thin_profiles_own_iqr_as_its_noise():
    noise = 2.0
    assert sh.profile_se(0.0, 1, noise) == pytest.approx(2.0)
    assert sh.profile_se(0.0, 4, noise) == pytest.approx(1.0)
    assert sh.profile_se(9.0, 4, noise) == pytest.approx(1.0), "under the minimum the own IQR is ignored either way"
    # At and above the minimum the profile's own IQR/√n is read, floored at half the pooled SE.
    own = 3.0 / math.sqrt(9)
    assert sh.profile_se(3.0, 9, noise) == pytest.approx(own)
    assert sh.profile_se(0.0, 9, noise) == pytest.approx(sh.OWN_SE_FLOOR_SHARE * noise / 3)
    assert sh.profile_se(1.0, 0, noise) is None and sh.profile_se(1.0, None, noise) is None


def test_the_per_run_noise_is_pooled_over_well_measured_profiles_only():
    points = [(2.698, 30), (1.349, 10), (4.047, 5), (40.0, 2), (0.0, 1), (None, 30)]
    noise, used, basis = sh.run_noise(points)
    assert basis == "fitted" and used == 3
    assert noise == pytest.approx(2.0, abs=1e-3), "median of IQR/1.349 over the three with ≥5 runs"
    noise, used, basis = sh.run_noise([(2.698, 30), (40.0, 2)])
    assert basis == "default" and noise == sh.DEFAULT_RUN_NOISE


def test_too_small_a_field_is_a_flat_prior_that_shrinks_nothing_and_says_so():
    prior = sh.fit([(70.0, 2.0, 30), (72.0, 2.0, 30), (68.0, 2.0, 30)])
    assert prior.basis == "flat" and prior.flat
    assert prior.as_dict()["tau"] is None
    g = sh.grade(prior, 80.0, 0.0, 1)
    assert g["shrunk"] == 80.0 and g["weight"] == 1.0
    assert g["ceiling"] == pytest.approx(min(100.0, 80.0 + sh.CEILING_Z * prior.run_noise), abs=0.01)


def test_tau_is_what_is_left_of_the_spread_once_measurement_error_is_subtracted():
    # Profiles that read identically carry the floor, not zero; a field genuinely 2 points
    # wide with ~0.37-point SEs fits a τ close to its true spread.
    flat = sh.fit([(70.0, 2.0, 30)] * 8)
    assert flat.tau == pytest.approx(sh.TAU_FLOOR)
    wide = sh.fit(_field(spread=2.0))
    assert 0.9 < wide.tau < 1.4  # uniform on ±2 has SD 1.15


def test_the_ceiling_and_floor_stay_on_the_overall_scale():
    prior = sh.fit(_field(centre=97.0, spread=1.0))
    g = sh.grade(prior, 99.5, 0.0, 1)
    assert g["ceiling"] <= 100.0 and g["floor"] >= 0.0
    assert sh.grade(prior, None, 0.0, 1) is None
    assert sh.grade(prior, 90.0, 0.0, 0) is None
