"""Historical trend aggregation: baselines by day-of-week × hour-of-day.

The premise (a weather forecast for the network): a run's score is only half the
story — the other half is *what's normal for this day and time*. Network quality
swings on a diurnal/weekly cycle (ISP congestion, upstream load) that the firewall
config doesn't control. By bucketing history into ``(weekday, hour)`` cells we get a
baseline for that **environment**, and a current reading can be read *relative* to
it — "wins above replacement": ``observed − expected_for_this_time``.

The environment is best characterised by the infra/completion metrics (latency,
jitter, loss, throughput, DNS/TCP/TLS): they track internet conditions and are
largely config-insensitive, so a plain all-history baseline reflects the
environment rather than the config under test. SOPS (paint) is what the config
optimises, so its relative reading isolates the config's contribution.

This module is pure aggregation over ``RunPoint`` records (no DB / FastAPI), so it
is straightforward to unit-test. The API layer (``api/routes_trends.py``) is
responsible for loading runs and extracting per-run metric values.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from .metrics import METRICS

# Robust σ ≈ IQR / 1.349 (the IQR of a normal distribution spans 1.349σ).
_IQR_TO_SIGMA = 1.349

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass(frozen=True)
class TrendMetric:
    """Display metadata for a metric we can trend (registry metrics + the two axes)."""

    key: str
    label: str
    unit: str
    higher_is_better: bool


def _trend_metrics() -> dict[str, TrendMetric]:
    # The score axes are synthetic (not in the metric registry); everything else is
    # derived straight from the single source of truth in ``metrics.py``.
    out: dict[str, TrendMetric] = {
        "speed": TrendMetric("speed", "Speed", "", higher_is_better=True),
        "smoothness": TrendMetric("smoothness", "Smoothness", "", higher_is_better=True),
        "stability": TrendMetric("stability", "Stability & Interactivity", "", higher_is_better=True),
        "completion": TrendMetric("completion", "Completion", "", higher_is_better=True),
    }
    for m in METRICS:
        out[m.key] = TrendMetric(m.key, m.label, m.unit, m.higher_is_better)
    return out


# The methodology score axes (synthetic, not registry metrics).
AXIS_KEYS = ("responsiveness", "speed", "smoothness", "stability", "completion")


TREND_METRICS: dict[str, TrendMetric] = _trend_metrics()


@dataclass
class RunPoint:
    """A single run's timestamp + its metric values, ready for bucketing."""

    created_at: datetime  # UTC (treated as UTC whether naive or aware)
    values: dict[str, float | None] = field(default_factory=dict)


def run_metric_values(score, results_by_plugin: dict, axis_scores: dict | None = None) -> dict[str, float | None]:
    """Pull every trendable metric for one run: the methodology axis scores (Speed/
    Smoothness/Stability/Completion) plus the infra metrics from the plugin results.

    ``axis_scores`` is the run's axis-score dict under the current methodology (empty
    for runs not comparable, so the baseline isn't built on non-comparable scores).
    ``score`` is accepted for call-site compatibility but no longer read. Only
    attribute/dict access — no queries — so it's cheap and easily stubbed in tests.
    """
    vals: dict[str, float | None] = {}
    axes = axis_scores or {}
    for axis in AXIS_KEYS:
        vals[axis] = axes.get(axis)
    for m in METRICS:
        res = results_by_plugin.get(m.plugin)
        metrics = getattr(res, "metrics", None) if res is not None else None
        vals[m.key] = metrics.get(m.source_key) if metrics else None
    return vals


def _naive_utc(dt: datetime) -> datetime:
    """Drop tz info, treating the value as UTC (matches how runs are stored)."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def local_bucket(dt: datetime, tz_offset_min: int) -> tuple[int, int]:
    """``(weekday 0=Mon, hour 0-23)`` for ``dt`` shifted into the viewer's local time.

    ``tz_offset_min`` is the minutes to add to UTC to reach local time
    (the frontend sends ``-new Date().getTimezoneOffset()``).
    """
    local = _naive_utc(dt) + timedelta(minutes=tz_offset_min)
    return local.weekday(), local.hour


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    """Linear-interpolated percentile (``q`` in 0..100); safe for tiny samples."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * (q / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _stats(vals: list[float]) -> dict:
    """Median + IQR bounds + sample count for one bucket."""
    s = sorted(vals)
    return {
        "median": round(median(s), 2),
        "p25": round(_percentile(s, 25), 2),
        "p75": round(_percentile(s, 75), 2),
        "count": len(s),
    }


def bucket_values(
    points: list[RunPoint], metric_key: str, tz_offset_min: int
) -> dict[tuple[int, int], list[float]]:
    """Group a metric's non-null readings by ``(weekday, hour)`` local cell."""
    buckets: dict[tuple[int, int], list[float]] = {}
    for p in points:
        v = p.values.get(metric_key)
        if v is None:
            continue
        buckets.setdefault(local_bucket(p.created_at, tz_offset_min), []).append(v)
    return buckets


def metric_grid(points: list[RunPoint], metric_key: str, tz_offset_min: int) -> dict:
    """Full 7×24 heatmap cells + hour-of-day / day-of-week marginals for a metric."""
    buckets = bucket_values(points, metric_key, tz_offset_min)
    cells = [
        {"weekday": wd, "hour": hr, **_stats(vals)}
        for (wd, hr), vals in sorted(buckets.items())
    ]

    by_hour_acc: dict[int, list[float]] = {}
    by_wd_acc: dict[int, list[float]] = {}
    for (wd, hr), vals in buckets.items():
        by_hour_acc.setdefault(hr, []).extend(vals)
        by_wd_acc.setdefault(wd, []).extend(vals)
    by_hour = [{"hour": h, **_stats(v)} for h, v in sorted(by_hour_acc.items())]
    by_weekday = [{"weekday": w, **_stats(v)} for w, v in sorted(by_wd_acc.items())]

    meta = TREND_METRICS.get(metric_key)
    return {
        "metric": metric_key,
        "label": meta.label if meta else metric_key,
        "unit": meta.unit if meta else "",
        "higher_is_better": meta.higher_is_better if meta else False,
        "total": sum(len(v) for v in buckets.values()),
        "cells": cells,
        "by_hour": by_hour,
        "by_weekday": by_weekday,
    }


# Order matters: try the most specific context first, widen until we have samples.
_FALLBACK_LADDER = ("exact", "hour", "weekday", "global")


def _baseline_pool(
    buckets: dict[tuple[int, int], list[float]],
    weekday: int,
    hour: int,
    min_samples: int,
) -> tuple[str, list[float]] | None:
    """Pick a baseline sample pool for ``(weekday, hour)`` via the fallback ladder.

    Widen the context — exact cell → same hour any day → same day any hour →
    global — until a pool clears ``min_samples``; if none does, fall back to the
    most specific *non-empty* pool. Returns ``(source, values)`` or None if there's
    no history at all.
    """
    pools: dict[str, list[float]] = {
        "exact": list(buckets.get((weekday, hour), [])),
        "hour": [x for (w, h), vs in buckets.items() if h == hour for x in vs],
        "weekday": [x for (w, h), vs in buckets.items() if w == weekday for x in vs],
        "global": [x for vs in buckets.values() for x in vs],
    }
    for source in _FALLBACK_LADDER:
        if len(pools[source]) >= min_samples:
            return source, pools[source]
    for source in _FALLBACK_LADDER:
        if pools[source]:
            return source, pools[source]
    return None


def relative_reading(
    points: list[RunPoint],
    metric_key: str,
    current_value: float | None,
    tz_offset_min: int,
    weekday: int,
    hour: int,
    min_samples: int,
) -> dict | None:
    """How a current reading compares to its historical baseline for this time.

    Returns the baseline (median + IQR + sample count + which fallback context it
    came from) and, when ``current_value`` is known, a direction-aware delta: the
    signed difference, a robust z-score (delta / (IQR/1.349)), the current value's
    percentile within the bucket, a ``better`` flag (respecting whether higher is
    better for this metric), and a coarse ``band`` (typical / mild / strong). None
    if there's no usable history for the metric at all.
    """
    buckets = bucket_values(points, metric_key, tz_offset_min)
    pool = _baseline_pool(buckets, weekday, hour, min_samples)
    if pool is None:
        return None
    source, vals = pool
    stats = _stats(vals)
    meta = TREND_METRICS.get(metric_key)
    higher = meta.higher_is_better if meta else False

    out: dict = {
        "metric": metric_key,
        "label": meta.label if meta else metric_key,
        "unit": meta.unit if meta else "",
        "higher_is_better": higher,
        "current": round(current_value, 2) if current_value is not None else None,
        "baseline": stats["median"],
        "p25": stats["p25"],
        "p75": stats["p75"],
        "count": stats["count"],
        "baseline_source": source,
        "delta": None,
        "delta_pct": None,
        "z": None,
        "percentile": None,
        "better": None,
        "band": "unknown",
    }
    if current_value is None:
        return out

    md = stats["median"]
    delta = round(current_value - md, 2)
    out["delta"] = delta
    if md != 0:
        out["delta_pct"] = round(delta / abs(md) * 100, 1)
    sigma = (stats["p75"] - stats["p25"]) / _IQR_TO_SIGMA
    if sigma > 0:
        z = delta / sigma
        out["z"] = round(z, 2)
        out["band"] = "typical" if abs(z) < 0.5 else ("mild" if abs(z) < 1.5 else "strong")
    else:
        out["band"] = "typical" if delta == 0 else "mild"
    s = sorted(vals)
    out["percentile"] = round(sum(1 for v in s if v <= current_value) / len(s) * 100)
    out["better"] = (delta >= 0) if higher else (delta <= 0)
    return out


def relative_deltas(
    baseline_points: list[RunPoint],
    target_points: list[RunPoint],
    metric_key: str,
    tz_offset_min: int,
    min_samples: int,
) -> list[float]:
    """Per-run ``value − baseline_median(its weekday,hour)`` for ``target_points``.

    The baseline is built from ``baseline_points`` (the comparison universe), using
    the same fallback ladder as a live relative reading so each run is judged
    against the most specific time context that has enough history. This is the core
    of *time-adjusting* a settings profile: it removes the day/hour environment a run
    happened to land in, leaving the config's own contribution.
    """
    buckets = bucket_values(baseline_points, metric_key, tz_offset_min)
    deltas: list[float] = []
    for p in target_points:
        v = p.values.get(metric_key)
        if v is None:
            continue
        wd, hr = local_bucket(p.created_at, tz_offset_min)
        pool = _baseline_pool(buckets, wd, hr, min_samples)
        if pool is None:
            continue
        deltas.append(v - median(pool[1]))
    return deltas


def profile_relative(
    baseline_points: list[RunPoint],
    target_points: list[RunPoint],
    metric_key: str,
    tz_offset_min: int,
    min_samples: int,
) -> dict | None:
    """Time-adjusted summary for a set of runs (e.g. one settings profile).

    ``delta_median`` > 0 means runs under this profile beat the time-of-day norm by
    that many points on average (for a higher-is-better metric like SOPS) — "this
    config performs above its historical environment". None when no run had a
    usable baseline.
    """
    deltas = relative_deltas(baseline_points, target_points, metric_key, tz_offset_min, min_samples)
    if not deltas:
        return None
    s = sorted(deltas)
    return {
        "delta_median": round(median(s), 2),
        "p25": round(_percentile(s, 25), 2),
        "p75": round(_percentile(s, 75), 2),
        "count": len(s),
    }


def current_values(
    points: list[RunPoint],
    metric_keys: list[str],
    window_hours: float,
    now_utc: datetime,
) -> dict[str, float | None]:
    """Median of each metric over the last ``window_hours`` (the 'current' reading)."""
    cutoff = _naive_utc(now_utc) - timedelta(hours=window_hours)
    recent = [p for p in points if _naive_utc(p.created_at) >= cutoff]
    out: dict[str, float | None] = {}
    for k in metric_keys:
        vals = [p.values[k] for p in recent if p.values.get(k) is not None]
        out[k] = round(median(vals), 2) if vals else None
    return out
