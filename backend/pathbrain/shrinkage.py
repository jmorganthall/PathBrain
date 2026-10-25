"""Empirical-Bayes shrinkage of a profile's pooled Overall, and the posterior **ceiling**.

**The problem this solves is selection, not measurement.** A profile measured once reads its
Overall off a single run, and a single run on this link wobbles by ~2 points. Among a hundred
such profiles the best-looking one is expected to overshoot its true value by ≈2.5–3 points —
the expected maximum of a hundred draws — against real gaps between the top profiles of
0.1–0.3. So "very high Overall on five iterations" is, almost always, the lucky tail of the
noise and not a finding; and the one number PathBrain used to decide whether such a profile
was worth a night in the ring (`optimistic`, the p75 of its own runs) was *worse* than the
point estimate: with one run the p75 **is** the point estimate, with two
`statistics.quantiles` extrapolates past the larger of them, with three it equals the
maximum, so the admission gate to the duel was a coin flip on whichever run happened to land
high, and the race's "eliminate below the bar" struck off half the thin field on loop one for
the same reason.

**What this does.** The field itself is the prior. Over every profile with an Overall the
module fits, by the method of moments (as `overall_ranking.pooled_slack` fits τ), the spread
of **true** Overalls — the observed spread of the medians minus what measurement error alone
would produce — and shrinks each profile's reading toward the field's centre by exactly as
much as its own noise warrants:

    w        = τ² / (τ² + se²)
    shrunk   = mean + w · (overall − mean)
    sd       = √(w · se²)                        (= 1/√(1/se² + 1/τ²))
    ceiling  = shrunk + CEILING_Z · sd

A 3000-iteration profile has `se ≈ 0`, so `w ≈ 1` and nothing moves — its reading **is** the
estimate. A one-run profile has `se` equal to a whole run's noise and is pulled most of the way
to the mean, and its ceiling says what it *could* still be, honestly wide. `CEILING_Z` is the
same bar as `correlation.crown_tie_sigma` and `duel.tie_sigma`, so "its ceiling reaches the
crown" means the same thing as "the crown cannot call it clearly worse".

**The run-to-run noise is pooled, not read off the thin profile.** A profile's own IQR over one
or two runs is not an estimate of anything (one run has no spread; two runs have a spread that
is itself a coin flip), so under `OWN_SE_MIN_RUNS` a profile's SE is the **field's per-run
noise** over √runs — the median per-run SD over the profiles measured well enough to have one
(`run_noise`) — and a well-measured profile's own IQR-based SE is floored at half of that, so a
profile whose few runs happened to agree cannot present itself as noiseless. This is the same
floor the fused ranking's pooled anchors read (`pooled_field.graded_field`), where a one-run
profile with SE 0 used to anchor the fit as firmly as the crown.

**What it does NOT do.** The crown is still the argmax of the *raw* median among confident
profiles (`routes_settings._select_crown`), full stop: a profile better by 0.000001 is better,
and that rule is the user's own. Shrinkage changes which thin profiles are *admitted* to the
ring, raced and listed as heirs (`optimistic` is now the posterior ceiling, read identically by
the heirs card, the challenger race and the duel's `LIVE_THREAT_TIER`), and it puts a shrunk
reading (`overall_shrunk` ± `overall_shrunk_se`) beside every raw one so a five-iteration 83
reads as what it is. It never writes a score, never gates comparability and never touches the
firewall. Dependency-free and deterministic: every number re-derives from the profile table and
explains in a sentence.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from statistics import median

#: Upper quantile for the ceiling, in posterior standard deviations — the same bar as
#: `correlation.crown_tie_sigma` and `duel.tie_sigma`.
CEILING_Z = 2.0
#: Below this many runs a profile's own IQR is not an estimate of its noise; the field's is used.
OWN_SE_MIN_RUNS = 5
#: A well-measured profile's own SE is floored at this share of the pooled per-run noise over √n.
OWN_SE_FLOOR_SHARE = 0.5
#: Fewer well-measured profiles than this → the per-run noise falls back to the default.
MIN_NOISE_PROFILES = 3
#: Per-run SD of the Overall measured on the live link (the duel entry in CLAUDE.md: ~2.3).
DEFAULT_RUN_NOISE = 2.3
#: IQR of a normal → its SD.
IQR_TO_SD = 1.349
#: Fewer profiles than this and the prior is flat (no shrinkage): a mean of two is not a field.
MIN_PRIOR_PROFILES = 5
#: τ never collapses below this (points): a field that *reads* identical is not known to be
#: identical, and τ = 0 would pull every thin profile exactly onto the mean.
TAU_FLOOR = 0.25

__all__ = [
    "CEILING_Z", "DEFAULT_RUN_NOISE", "MIN_PRIOR_PROFILES", "OWN_SE_MIN_RUNS", "Prior",
    "fit", "grade", "posterior", "profile_se", "run_noise",
]


@dataclass(frozen=True)
class Prior:
    """The field as a prior: its centre, the spread of true Overalls, and the per-run noise."""

    mean: float
    tau: float
    run_noise: float
    profiles: int
    noise_profiles: int
    basis: str  # "fitted" | "flat"
    noise_basis: str  # "fitted" | "default"

    @property
    def flat(self) -> bool:
        return not math.isfinite(self.tau)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["tau"] = None if self.flat else round(self.tau, 3)
        d["mean"] = round(self.mean, 2)
        d["run_noise"] = round(self.run_noise, 3)
        d["ceiling_z"] = CEILING_Z
        return d


def run_noise(points: list[tuple[float | None, int | None]]) -> tuple[float, int, str]:
    """``(per_run_sd, profiles_used, basis)`` — the field's run-to-run noise of the Overall.

    ``points`` are ``(iqr, runs)`` per profile; only profiles with at least `OWN_SE_MIN_RUNS`
    runs contribute (a thin profile's IQR is not a noise reading), each as ``IQR / 1.349``, and
    the median is taken so one profile that sat on a hung probe cannot double everyone's bar.
    Under `MIN_NOISE_PROFILES` contributors the default is used and said so.
    """
    sds = [
        max(0.0, float(iqr)) / IQR_TO_SD
        for iqr, runs in points
        if iqr is not None and math.isfinite(float(iqr))
        and runs is not None and int(runs) >= OWN_SE_MIN_RUNS
    ]
    sds = [s for s in sds if s > 0]
    if len(sds) < MIN_NOISE_PROFILES:
        return DEFAULT_RUN_NOISE, len(sds), "default"
    return float(median(sds)), len(sds), "fitted"


def profile_se(iqr: float | None, runs: int | None, noise: float) -> float | None:
    """Standard error of one profile's median Overall, with the thin-profile floor.

    * no runs → ``None`` (nothing is known);
    * under `OWN_SE_MIN_RUNS` runs → ``noise / √runs`` (its own IQR is not an estimate);
    * otherwise its own ``IQR / √runs`` (`profile_aggregates.median_se`), floored at
      `OWN_SE_FLOOR_SHARE · noise / √runs`.
    """
    from .profile_aggregates import median_se

    if runs is None or int(runs) < 1:
        return None
    n = int(runs)
    pooled = float(noise) / math.sqrt(n)
    if n < OWN_SE_MIN_RUNS or iqr is None or not math.isfinite(float(iqr)):
        return pooled
    own = median_se(iqr, n)
    if own is None:
        return pooled
    return max(float(own), OWN_SE_FLOOR_SHARE * pooled)


def fit(points: list[tuple[float | None, float | None, int | None]]) -> Prior:
    """Fit the prior over the field. ``points`` are ``(overall, iqr, runs)`` per profile.

    Centre = the median Overall (robust to the outliers the field genuinely has — a profile
    that reads 40 beside a pack at 70 is real, and a mean would let it drag the prior).
    τ² = mean squared deviation about the centre minus the mean squared SE, floored at
    `TAU_FLOOR`²: what is left of the observed spread once measurement error is subtracted
    is the spread of true values. Under `MIN_PRIOR_PROFILES` profiles the prior is flat
    (τ = ∞ — no shrinkage, the ceiling is the raw reading plus `CEILING_Z` SEs) and says so.
    """
    noise, n_noise, noise_basis = run_noise([(iqr, runs) for _o, iqr, runs in points])
    rows = []
    for overall, iqr, runs in points:
        if overall is None:
            continue
        se = profile_se(iqr, runs, noise)
        if se is None:
            continue
        rows.append((float(overall), se))
    if len(rows) < MIN_PRIOR_PROFILES:
        centre = median([o for o, _ in rows]) if rows else 0.0
        return Prior(centre, math.inf, noise, len(rows), n_noise, "flat", noise_basis)
    centre = float(median([o for o, _ in rows]))
    var_obs = sum((o - centre) ** 2 for o, _ in rows) / len(rows)
    mean_se2 = sum(se ** 2 for _, se in rows) / len(rows)
    tau = math.sqrt(max(var_obs - mean_se2, TAU_FLOOR ** 2))
    return Prior(centre, tau, noise, len(rows), n_noise, "fitted", noise_basis)


def posterior(prior: Prior, overall: float, se: float) -> dict:
    """Shrink one reading. Returns ``{shrunk, shrunk_se, ceiling, floor, weight, se}``,
    the ceiling and floor clamped to the Overall's 0–100 scale."""
    se = max(0.0, float(se))
    if prior.flat:
        w = 1.0
    else:
        w = prior.tau ** 2 / (prior.tau ** 2 + se ** 2) if (prior.tau ** 2 + se ** 2) > 0 else 1.0
    shrunk = prior.mean + w * (float(overall) - prior.mean)
    sd = math.sqrt(w) * se
    return {
        "shrunk": round(shrunk, 2),
        "shrunk_se": round(sd, 3),
        "ceiling": round(min(100.0, max(0.0, shrunk + CEILING_Z * sd)), 2),
        "floor": round(min(100.0, max(0.0, shrunk - CEILING_Z * sd)), 2),
        "weight": round(w, 3),
        "se": round(se, 3),
    }


def grade(prior: Prior, overall: float | None, iqr: float | None, runs: int | None) -> dict | None:
    """`profile_se` + `posterior` for one profile; ``None`` when it has no Overall or no runs."""
    if overall is None:
        return None
    se = profile_se(iqr, runs, prior.run_noise)
    if se is None:
        return None
    return posterior(prior, overall, se)
