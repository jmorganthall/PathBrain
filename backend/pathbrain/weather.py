"""Measured-weather severity + cohort residuals ("wins above the weather").

The problem this solves: profiles are not measured at random times. A 50-iteration
block during a busy afternoon faces worse ambient conditions than an overnight run,
so its raw stats are worse *by definition* — and because the firewall usually sits on
one profile for hours, a contemporaneous (±time-window) cohort contains almost nothing
but that profile. Time was only ever a proxy for conditions; every run already
*measures* its conditions directly through the weather instruments it carries
(probe DNS/TCP/TLS/latency + the load's own connection-setup phases).

So: weather is defined **in the context of each run's own raw stats**.

* :func:`run_severities` — per-run **weather severity** (0–100): each clean covariate's
  value is ranked against its own all-history distribution (mid-rank ECDF), and the
  run's severity is the median of those percentiles. "DNS at 4 ms when the median is
  1 ms" *is* a high percentile.
* :func:`cohort_residuals` — band runs into severity quintiles and compare each run's
  Overall against the median of **other profiles' runs in the same band** ("everyone
  else with this weather"). Per profile: median residual + IQR + coverage. A profile
  that is mid-pack raw but strongly positive here delivered average outcomes in
  weather where the field delivered below-average ones — "there may be something
  here".

This reading is deliberately **flag-and-steer only**: it never enters the crown.
The verdict stays the raw measurements (the gospel that accrues into the macro
picture); a contaminated or noisy flag can at worst trigger an unnecessary race,
which resolves with clean head-to-head data. Covariate cleanliness (shaper-moved
signals must never define weather) is decided by ``routes_settings._weather_covariates``
and validated empirically by ``GET /settings/weather-sensitivity``.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from statistics import median

from .logging_config import get_logger

log = get_logger("weather")

# A run needs at least this many clean covariates present to get a severity at all —
# fewer and the "conditions" read is too thin to band on.
WEATHER_MIN_COVARIATES = 3
# Severity bands (equal-count quantile bins over the field's severity distribution).
WEATHER_BANDS = 5
# A run's residual only counts when its band holds at least this many OTHER-profile
# readings — no cohort, no claim.
WEATHER_COHORT_MIN = 3


def _percentile_of(sorted_vals: list[float], v: float) -> float:
    """Mid-rank empirical percentile (0–100) of ``v`` within ``sorted_vals``."""
    n = len(sorted_vals)
    if n == 0:
        return 50.0
    below = bisect_left(sorted_vals, v)
    equal = bisect_right(sorted_vals, v) - below
    return (below + equal / 2.0) / n * 100.0


def run_severities(
    covariate_values: list[dict[str, float]], covariates: list[str]
) -> list[float | None]:
    """Per-run weather severity (0–100), from each run's own covariate readings.

    ``covariate_values`` is one dict per run ({covariate_key: value}); ``covariates``
    the clean covariate keys to use. Each covariate is ranked against its own
    distribution across all runs (mid-rank ECDF, higher value = worse weather for all
    current covariates — they're times/latency/loss), and the run's severity is the
    median of its available covariate percentiles. ``None`` when fewer than
    ``WEATHER_MIN_COVARIATES`` covariates were measured on that run.
    """
    dists: dict[str, list[float]] = {}
    for c in covariates:
        vals = sorted(cv[c] for cv in covariate_values if c in cv)
        if vals:
            dists[c] = vals

    out: list[float | None] = []
    for cv in covariate_values:
        pcts = [
            _percentile_of(dists[c], cv[c])
            for c in covariates
            if c in cv and c in dists and len(dists[c]) > 1
        ]
        out.append(round(median(pcts), 1) if len(pcts) >= WEATHER_MIN_COVARIATES else None)
    return out


def _quantile_edges(sorted_vals: list[float], bands: int) -> list[float]:
    """Interior quantile cut points (len bands-1) for equal-count banding."""
    n = len(sorted_vals)
    edges = []
    for i in range(1, bands):
        idx = min(n - 1, max(0, round(i * n / bands) - 1))
        edges.append(sorted_vals[idx])
    return edges


def _band_of(edges: list[float], v: float) -> int:
    for i, e in enumerate(edges):
        if v <= e:
            return i
    return len(edges)


def cohort_residuals(
    fingerprints: list[str],
    overalls: list[float],
    severities: list[float | None],
) -> tuple[dict[str, dict], dict[str, float]]:
    """Per-profile "wins above the weather" from parallel per-run arrays.

    Bands runs into severity quintiles; each run's residual is its Overall minus the
    median Overall of **other profiles'** runs in the same band (own runs excluded, so
    a burst can't define its own "typical"; bands with fewer than
    ``WEATHER_COHORT_MIN`` other-profile readings contribute nothing).

    Returns ``(residual_by_fp, severity_median_by_fp)`` where each residual entry is
    ``{delta_median, p25, p75, count, coverage}`` — ``coverage`` the fraction of the
    profile's severity-scored runs that had a usable cohort.
    """
    scored = [
        (fp, ov, sev)
        for fp, ov, sev in zip(fingerprints, overalls, severities)
        if sev is not None
    ]
    if not scored:
        return {}, {}

    edges = _quantile_edges(sorted(s for _, _, s in scored), WEATHER_BANDS)
    bands: dict[int, list[tuple[str, float]]] = {}
    runs_by_fp: dict[str, list[tuple[int, float]]] = {}  # fp -> [(band, overall)]
    sevs_by_fp: dict[str, list[float]] = {}
    for fp, ov, sev in scored:
        b = _band_of(edges, sev)
        bands.setdefault(b, []).append((fp, ov))
        runs_by_fp.setdefault(fp, []).append((b, ov))
        sevs_by_fp.setdefault(fp, []).append(sev)

    # Cohort median per (band, fp): the band minus the profile's own runs — computed once
    # per pair, not per run, so the whole pass stays O(bands × profiles × band_size).
    cohort_median: dict[tuple[int, str], float | None] = {}
    for b, pairs in bands.items():
        fps_in_band = {fp for fp, _ in pairs}
        for fp in fps_in_band:
            others = [ov for f, ov in pairs if f != fp]
            cohort_median[(b, fp)] = median(others) if len(others) >= WEATHER_COHORT_MIN else None
        # Profiles not in this band would use the full band; computed lazily below.
        cohort_median[(b, "")] = (
            median([ov for _, ov in pairs]) if len(pairs) >= WEATHER_COHORT_MIN else None
        )

    residual_by_fp: dict[str, dict] = {}
    for fp, brs in runs_by_fp.items():
        deltas = []
        for b, ov in brs:
            cm = cohort_median.get((b, fp), cohort_median.get((b, "")))
            if cm is not None:
                deltas.append(ov - cm)
        if deltas:
            s = sorted(deltas)
            n = len(s)
            residual_by_fp[fp] = {
                "delta_median": round(median(s), 2),
                "p25": round(s[max(0, round(0.25 * (n - 1)))], 2),
                "p75": round(s[min(n - 1, round(0.75 * (n - 1)))], 2),
                "count": n,
                "coverage": round(n / len(brs), 2),
            }
    severity_median_by_fp = {
        fp: round(median(sevs), 1) for fp, sevs in sevs_by_fp.items()
    }
    return residual_by_fp, severity_median_by_fp


__all__ = [
    "WEATHER_BANDS",
    "WEATHER_COHORT_MIN",
    "WEATHER_MIN_COVARIATES",
    "cohort_residuals",
    "run_severities",
]
