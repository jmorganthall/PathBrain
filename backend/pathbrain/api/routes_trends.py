"""Historical-trend endpoints: per-metric baselines by day-of-week × hour-of-day.

These power the "weather forecast" view — not just "here's the score" but "here's
the score *relative to what's normal for this day and time*". Aggregation lives in
``pathbrain.trends`` (pure, unit-tested); this layer loads runs and extracts each
run's metric values.

Buckets are computed in the *viewer's* local time: the frontend passes
``tz_offset`` = minutes to add to UTC to reach local
(``-new Date().getTimezoneOffset()``). DST across the lookback window is
approximated by the single current offset — fine for an MVP, since the point is a
coarse day/hour pattern, not minute-accurate alignment.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, defer, selectinload

from ..config_store import get_config
from ..database import get_session
from ..methodology import ensure_current_methodology
from ..models import BenchmarkResult, Run, RunStatus, Score
from ..trends import (
    TREND_METRICS,
    RunPoint,
    current_values,
    local_bucket,
    metric_grid,
    relative_reading,
    run_metric_values,
)
from ..logging_config import get_logger

router = APIRouter()
logger = get_logger(__name__)


def _trends_cfg(session: Session) -> dict:
    return get_config(session).get("trends", {})


def _load_points(session: Session, days: int) -> list[RunPoint]:
    """All completed runs in the lookback window as ``RunPoint`` records.

    Axis scores (Speed/Smoothness/…) come from the (run × methodology) Score under
    the current methodology; infra metrics come from the runs' plugin results.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    runs = (
        session.execute(
            select(Run)
            # Only ``metrics`` is read per result; skip the heavy raw/details JSON blobs.
            .options(selectinload(Run.results).options(defer(BenchmarkResult.raw), defer(BenchmarkResult.details)))
            .where(Run.status == RunStatus.COMPLETE, Run.created_at >= cutoff)
            .order_by(Run.created_at)
        )
        .scalars()
        .all()
    )
    methodology = ensure_current_methodology(session, get_config(session))
    score_rows = session.scalars(
        select(Score).where(
            Score.run_id.in_([r.id for r in runs]),
            Score.methodology_version == methodology.version,
        )
    ).all()
    axes_by_run = {
        s.run_id: (s.axis_scores or {}) for s in score_rows if s.comparability != "incomparable"
    }
    points: list[RunPoint] = []
    for run in runs:
        results_by_plugin = {r.plugin: r for r in run.results}
        points.append(
            RunPoint(
                created_at=run.created_at,
                values=run_metric_values(None, results_by_plugin, axes_by_run.get(run.id)),
            )
        )
    return points


@router.get("/trends/heatmap")
def trends_heatmap(
    metric: str = Query("smoothness", description="Metric key, e.g. speed/smoothness/latency/jitter."),
    tz_offset: int = Query(0, description="Minutes to add to UTC for viewer-local time."),
    days: int | None = Query(None, ge=1, le=365, description="Lookback window (days)."),
    session: Session = Depends(get_session),
) -> dict:
    """Day-of-week × hour-of-day baseline grid (median + IQR + n) for one metric."""
    if metric not in TREND_METRICS:
        raise HTTPException(status_code=404, detail=f"Unknown metric '{metric}'")
    cfg = _trends_cfg(session)
    days = days or int(cfg.get("lookback_days", 90))
    points = _load_points(session, days)
    grid = metric_grid(points, metric, tz_offset)
    grid["window_days"] = days
    return grid


@router.get("/trends/relative")
def trends_relative(
    tz_offset: int = Query(0, description="Minutes to add to UTC for viewer-local time."),
    window_hours: float | None = Query(
        None, ge=0.25, le=168, description="Window for the 'current' reading (hours)."
    ),
    days: int | None = Query(None, ge=1, le=365, description="Lookback window (days)."),
    session: Session = Depends(get_session),
) -> dict:
    """Current reading vs. its historical baseline for the current weekday+hour.

    Returns a per-metric map (SOPS, Completion, and every measured metric) with the
    signed delta, robust z-score, percentile, direction-aware ``better`` flag, and
    the fallback context the baseline came from — the "wins above replacement"
    figure for right now.
    """
    cfg = _trends_cfg(session)
    days = days or int(cfg.get("lookback_days", 90))
    window_hours = window_hours if window_hours is not None else float(cfg.get("window_hours", 2))
    min_samples = int(cfg.get("min_samples", 3))

    points = _load_points(session, days)
    now = datetime.now(timezone.utc)
    weekday, hour = local_bucket(now, tz_offset)
    keys = list(TREND_METRICS.keys())
    currents = current_values(points, keys, window_hours, now)

    metrics: dict[str, dict] = {}
    for key in keys:
        reading = relative_reading(
            points, key, currents.get(key), tz_offset, weekday, hour, min_samples
        )
        if reading is not None:
            metrics[key] = reading

    return {
        "weekday": weekday,
        "hour": hour,
        "window_hours": window_hours,
        "window_days": days,
        "min_samples": min_samples,
        "metrics": metrics,
    }
