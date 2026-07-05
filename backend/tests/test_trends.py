"""Unit tests for historical-trend aggregation + the trends API."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from pathbrain.trends import (
    RunPoint,
    current_values,
    local_bucket,
    metric_grid,
    profile_relative,
    relative_reading,
    run_metric_values,
)


def _pt(dt: datetime, **values) -> RunPoint:
    return RunPoint(created_at=dt, values=values)


def test_local_bucket_shifts_into_viewer_timezone():
    # 2024-01-01 is a Monday. 01:30 UTC, shifted -120min, lands Sunday 23:00.
    dt = datetime(2024, 1, 1, 1, 30)
    assert local_bucket(dt, 0) == (0, 1)            # Mon 01:00 UTC
    assert local_bucket(dt, -120) == (6, 23)        # Sun 23:00 local (UTC-2)
    assert local_bucket(dt, 60) == (0, 2)           # Mon 02:00 local (UTC+1)


def test_run_metric_values_pulls_axes_and_registry_metrics():
    axes = {"speed": 88.0, "smoothness": 54.0, "stability": 90.0, "completion": 70.0}
    results = {
        "icmp": SimpleNamespace(metrics={"latency_ms": 12.0, "jitter_ms": 1.5}),
        "http": SimpleNamespace(metrics={"transfer_mbps": 300.0}),
    }
    vals = run_metric_values(None, results, axes)
    assert vals["speed"] == 88.0 and vals["smoothness"] == 54.0
    assert vals["stability"] == 90.0 and vals["completion"] == 70.0
    assert vals["latency"] == 12.0       # display-only, from icmp
    assert vals["jitter"] == 1.5
    assert vals["transfer"] == 300.0     # display-only, from http
    assert vals["dns"] is None           # no dns result this run


def test_run_metric_values_no_axes_when_not_comparable():
    # No axis scores (run not comparable under current methodology) => axes are None,
    # so they're excluded from the baseline; infra metrics remain.
    vals = run_metric_values(None, {"icmp": SimpleNamespace(metrics={"jitter_ms": 2.0})}, None)
    assert vals["speed"] is None and vals["smoothness"] is None
    assert vals["jitter"] == 2.0


def test_metric_grid_buckets_and_marginals():
    mon10 = datetime(2024, 1, 1, 10, 0)   # Monday 10:00
    tue10 = datetime(2024, 1, 2, 10, 0)   # Tuesday 10:00
    points = [
        _pt(mon10, latency=10.0),
        _pt(mon10, latency=20.0),
        _pt(tue10, latency=30.0),
    ]
    grid = metric_grid(points, "latency", 0)
    assert grid["total"] == 3
    cell = next(c for c in grid["cells"] if c["weekday"] == 0 and c["hour"] == 10)
    assert cell["median"] == 15.0 and cell["count"] == 2
    # Hour-of-day marginal pools across both days at hour 10.
    by_hour = next(h for h in grid["by_hour"] if h["hour"] == 10)
    assert by_hour["count"] == 3 and by_hour["median"] == 20.0
    assert grid["unit"] == "ms" and grid["higher_is_better"] is False


def test_relative_reading_lower_is_better_direction():
    mon14 = datetime(2024, 1, 1, 14, 0)
    points = [_pt(mon14, latency=v) for v in (20.0, 22.0, 24.0, 26.0, 28.0)]
    # Current latency 18 (below the bucket median 24) => better for a ping metric.
    r = relative_reading(points, "latency", 18.0, 0, weekday=0, hour=14, min_samples=3)
    assert r["baseline"] == 24.0
    assert r["baseline_source"] == "exact"
    assert r["delta"] == -6.0
    assert r["better"] is True
    assert r["percentile"] == 0  # 18 is below every sample


def test_relative_reading_higher_is_better_direction():
    mon14 = datetime(2024, 1, 1, 14, 0)
    points = [_pt(mon14, speed=v) for v in (60.0, 62.0, 64.0, 66.0, 68.0)]
    r = relative_reading(points, "speed", 72.0, 0, weekday=0, hour=14, min_samples=3)
    assert r["delta"] == 8.0
    assert r["better"] is True       # higher Speed than typical = better
    assert r["percentile"] == 100


def test_relative_reading_sparse_fallback_widens_context():
    # Plenty of Monday data, but none in the exact (Mon, 9) cell.
    points = [_pt(datetime(2024, 1, 1, 14, 0), latency=v) for v in (10.0, 11.0, 12.0, 13.0)]
    r = relative_reading(points, "latency", 10.0, 0, weekday=0, hour=9, min_samples=3)
    assert r is not None
    assert r["baseline_source"] == "weekday"   # widened from exact/hour to weekday
    assert r["count"] == 4


def test_relative_reading_none_without_history():
    assert relative_reading([], "latency", 10.0, 0, 0, 9, 3) is None


def test_relative_reading_no_current_value_returns_baseline_only():
    points = [_pt(datetime(2024, 1, 1, 14, 0), latency=v) for v in (10.0, 12.0, 14.0)]
    r = relative_reading(points, "latency", None, 0, 0, 14, 3)
    assert r["current"] is None
    assert r["delta"] is None
    assert r["band"] == "unknown"
    assert r["baseline"] == 12.0


def test_current_values_medians_recent_window():
    now = datetime(2024, 1, 8, 12, 0)
    points = [
        _pt(datetime(2024, 1, 8, 11, 30), latency=10.0),   # within 2h
        _pt(datetime(2024, 1, 8, 11, 0), latency=20.0),    # within 2h
        _pt(datetime(2024, 1, 1, 12, 0), latency=99.0),    # a week ago, excluded
    ]
    out = current_values(points, ["latency", "sops"], window_hours=2, now_utc=now)
    assert out["latency"] == 15.0
    assert out["sops"] is None


def test_profile_relative_strips_time_of_day_confound():
    good = datetime(2024, 1, 1, 10, 0)  # Mon 10:00 — easy environment
    hard = datetime(2024, 1, 1, 22, 0)  # Mon 22:00 — congested environment
    # Config-blind baseline filler so each bucket's norm isn't defined by A/B alone.
    base = [_pt(good, sops=70.0) for _ in range(4)] + [_pt(hard, sops=40.0) for _ in range(4)]
    a_points = [_pt(good, sops=75.0) for _ in range(3)]  # +5 over its (easy) norm
    b_points = [_pt(hard, sops=50.0) for _ in range(3)]  # +10 over its (hard) norm
    baseline = base + a_points + b_points

    a = profile_relative(baseline, a_points, "sops", 0, min_samples=3)
    b = profile_relative(baseline, b_points, "sops", 0, min_samples=3)
    # Raw SOPS ranks A (75) above B (50)…
    assert a["delta_median"] == 5.0 and a["count"] == 3
    # …but time-adjusted, B beat its tougher environment by more — the confound the
    # whole feature exists to strip out.
    assert b["delta_median"] == 10.0
    assert b["delta_median"] > a["delta_median"]


def test_profile_relative_none_without_values():
    pts = [_pt(datetime(2024, 1, 1, 10, 0), sops=None)]
    assert profile_relative(pts, pts, "sops", 0, 3) is None


def _wpt(dt: datetime, overall: float, fp: str) -> RunPoint:
    return RunPoint(created_at=dt, values={"overall": overall}, fingerprint=fp)


def test_weather_relative_uses_contemporaneous_window_not_day_hour():
    from pathbrain.trends import profile_weather_relative

    t1 = datetime(2024, 1, 1, 12, 0)   # Mon 12:00 — an easy moment
    t2 = datetime(2024, 1, 15, 12, 0)  # Mon 12:00 two weeks later — a degraded moment
    # Note: t1 and t2 fall in the SAME (weekday, hour) cell, so the day×hour baseline pools them.
    env1 = [_wpt(t1, 50.0, "env") for _ in range(4)]
    env2 = [_wpt(t2, 30.0, "env") for _ in range(4)]
    a_pts = [_wpt(t1, 55.0, "A") for _ in range(3)]   # +5 over its own moment's weather
    b_pts = [_wpt(t2, 33.0, "B") for _ in range(3)]   # +3 over its own moment's weather
    baseline = env1 + env2 + a_pts + b_pts

    a = profile_weather_relative(baseline, a_pts, "overall", exclude_fingerprint="A", min_samples=3)
    b = profile_weather_relative(baseline, b_pts, "overall", exclude_fingerprint="B", min_samples=3)
    # Each profile is judged against the ±2h window in ABSOLUTE time (drift-neutralizing),
    # excluding its own runs: A beat its moment by 5, B by 3.
    assert a["delta_median"] == 5.0 and a["count"] == 3
    assert b["delta_median"] == 3.0
    # The two eras share one (weekday, hour) cell, so day×hour would conflate them; the ±2h
    # weather window keeps them separate and correctly ranks A's edge above B's.
    assert a["delta_median"] > b["delta_median"]


def test_weather_relative_excludes_self_and_requires_a_window():
    from pathbrain.trends import profile_weather_relative

    t = datetime(2024, 1, 1, 12, 0)
    # A profile alone in its window has no other runs to compare against → no reading.
    solo = [_wpt(t, 80.0, "solo") for _ in range(5)]
    assert (
        profile_weather_relative(solo, solo, "overall", exclude_fingerprint="solo", min_samples=3)
        is None
    )
    # With other-profile runs present, the profile is kept out of its own baseline: 80 vs the
    # 60-median of the *other* runs = +20 (not diluted toward 0 by its own 80s).
    others = [_wpt(t, 60.0, "env") for _ in range(4)]
    res = profile_weather_relative(
        others + solo, solo, "overall", exclude_fingerprint="solo", min_samples=3
    )
    assert res is not None and res["delta_median"] == 20.0


# ── API smoke tests (no live network) ───────────────────────────────────────


def test_trends_heatmap_endpoint(client):
    resp = client.get("/api/trends/heatmap?metric=latency&tz_offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["metric"] == "latency"
    assert "cells" in body and "by_hour" in body and "by_weekday" in body
    assert body["window_days"] >= 1


def test_trends_heatmap_unknown_metric_404(client):
    resp = client.get("/api/trends/heatmap?metric=not_a_metric")
    assert resp.status_code == 404


def test_trends_relative_endpoint(client):
    resp = client.get("/api/trends/relative?tz_offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert "metrics" in body
    assert 0 <= body["weekday"] <= 6
    assert 0 <= body["hour"] <= 23
    # The response advertises the current methodology's crown measurements so the UI can
    # feature the day×hour "vs typical" matrix for them (v10: fcp/lcp/stall_energy).
    assert set(body["crown_metrics"]) == {"fcp", "lcp", "stall_energy"}
    # Every crown measurement is a trendable metric, so the same matrix applies to it.
    from pathbrain.trends import TREND_METRICS
    assert all(m in TREND_METRICS for m in body["crown_metrics"])
