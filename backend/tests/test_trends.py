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


# (The rolling ±window "weather" baseline and its tests were retired: the firewall usually
# sits on one profile for hours, so a time window rarely held a real cohort. Its successor —
# the measured-weather severity cohort — lives in pathbrain.weather; see test_weather.py.)


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
    # feature the day×hour "vs typical" matrix for them (v17: fcp/lcp/cadence_cov).
    assert set(body["crown_metrics"]) == {"fcp", "lcp", "cadence_cov"}
    # Every crown measurement is a trendable metric, so the same matrix applies to it.
    from pathbrain.trends import TREND_METRICS
    assert all(m in TREND_METRICS for m in body["crown_metrics"])


def test_load_points_matches_full_row_load():
    """The column-only loader returns exactly what loading whole ``Run`` rows did:
    same runs in the same order, later results winning per plugin, axis scores only
    from comparable scores under the current methodology."""
    from datetime import timedelta, timezone

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from pathbrain.api.routes_trends import _load_points
    from pathbrain.config_store import get_config
    from pathbrain.database import session_scope
    from pathbrain.methodology import ensure_current_methodology, latest_score
    from pathbrain.models import BenchmarkResult, Run, RunStatus, Score

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        version = ensure_current_methodology(s, get_config(s)).version

        def add(status, age_h, results, comparability=None, axes=None):
            run = Run(status=status, created_at=now - timedelta(hours=age_h), iterations=1,
                      config_used={"big": list(range(50))})
            s.add(run)
            s.flush()
            for plugin, metrics in results:
                s.add(BenchmarkResult(run_id=run.id, plugin=plugin, success=True, metrics=metrics))
            if comparability:
                s.add(Score(run_id=run.id, methodology_version=version, comparability=comparability,
                            axis_scores=axes, subscores={}, metric_values={}))
            return run.id

        add(RunStatus.COMPLETE, 3, [("browser", {"fcp_ms": 500.0}), ("browser", {"fcp_ms": 700.0})],
            "exact", {"speed": 61.0})
        add(RunStatus.COMPLETE, 2, [("browser", {"lcp_ms": 900.0})], "incomparable", {"speed": 10.0})
        add(RunStatus.COMPLETE, 1, [], None)
        add(RunStatus.FAILED, 1, [("browser", {"fcp_ms": 1.0})], "exact", {"speed": 1.0})
        add(RunStatus.COMPLETE, 24 * 40, [("browser", {"fcp_ms": 2.0})], "exact", {"speed": 2.0})
        s.commit()

    with session_scope() as s:
        got = _load_points(s, 30)

        cutoff = now - timedelta(days=30)
        runs = s.execute(
            select(Run).options(selectinload(Run.results))
            .where(Run.status == RunStatus.COMPLETE, Run.created_at >= cutoff)
            .order_by(Run.created_at, Run.id)
        ).scalars().all()
        scores = s.scalars(select(Score).where(
            Score.run_id.in_([r.id for r in runs]), Score.methodology_version == version, latest_score(),
        )).all()
        axes = {sc.run_id: (sc.axis_scores or {}) for sc in scores if sc.comparability != "incomparable"}
        want = [
            RunPoint(created_at=r.created_at,
                     values=run_metric_values(None, {x.plugin: x for x in r.results}, axes.get(r.id)))
            for r in runs
        ]

    assert [(p.created_at, p.values) for p in got] == [(p.created_at, p.values) for p in want]
    assert len(got) >= 3
    fcp_vals = [p.values.get("fcp") for p in got]
    assert 700.0 in fcp_vals and 500.0 not in fcp_vals and 1.0 not in fcp_vals and 2.0 not in fcp_vals
