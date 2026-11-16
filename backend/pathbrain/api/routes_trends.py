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

import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config_store import get_config
from ..database import get_session
from ..methodology import ensure_current_methodology, latest_score, overall_metrics
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


def _read_points(session: Session, days: int) -> list[RunPoint]:
    """All completed runs in the lookback window as ``RunPoint`` records.

    Axis scores (Speed/Smoothness/…) come from the (run × methodology) Score under
    the current methodology; infra metrics come from the runs' plugin results.

    Reads only the columns it uses (run id and time, each result's plugin and
    ``metrics``, each score's axes). Loading whole ``Run`` rows also parsed every
    run's ``config_used``/``settings`` JSON, which made a 90-day window (about 11k
    runs) take 12 to 15 seconds on the NAS.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    in_window = (Run.status == RunStatus.COMPLETE, Run.created_at >= cutoff)
    runs = session.execute(
        select(Run.id, Run.created_at).where(*in_window).order_by(Run.created_at, Run.id)
    ).all()
    if not runs:
        return []

    results: dict[int, dict[str, SimpleNamespace]] = {}
    for run_id, plugin, metrics in session.execute(
        select(BenchmarkResult.run_id, BenchmarkResult.plugin, BenchmarkResult.metrics)
        .join(Run, Run.id == BenchmarkResult.run_id)
        .where(*in_window)
        .order_by(BenchmarkResult.id)
    ):
        # Later rows win, as ``{r.plugin: r for r in run.results}`` did.
        results.setdefault(run_id, {})[plugin] = SimpleNamespace(metrics=metrics)

    methodology = ensure_current_methodology(session, get_config(session))
    axes_by_run = {
        run_id: (axis_scores or {})
        for run_id, axis_scores, comparability in session.execute(
            select(Score.run_id, Score.axis_scores, Score.comparability)
            .join(Run, Run.id == Score.run_id)
            .where(*in_window, Score.methodology_version == methodology.version, latest_score())
        )
        if comparability != "incomparable"
    }

    return [
        RunPoint(
            created_at=created_at,
            values=run_metric_values(None, results.get(run_id, {}), axes_by_run.get(run_id)),
        )
        for run_id, created_at in runs
    ]


# The heatmap, the "right now" panel and the sweep page all read the same points, and the
# Trends page asks for a fresh heatmap each time the metric changes. Loading them is the
# whole cost of those requests (several seconds on the NAS), so they are kept between
# requests while nothing they are built from has changed.
_POINTS_TTL_S = 600.0
_POINTS_LOCK = threading.Lock()
_POINTS_CACHE: dict[int, tuple[tuple, float, list[RunPoint]]] = {}


def _points_stamp(session: Session, days: int) -> tuple:
    """The cheap identity of everything ``_read_points`` reads.

    New or finished runs, new results, new or re-graded scores (new ``Score`` rows) and a
    change of methodology all move it. The window's start is included to the hour, so a
    run ageing out of the window drops out within the hour. In-place rewrites that create
    no row (a re-derive, a re-grade that updates rows) are covered by
    ``invalidate_points_cache`` where the path calls it, and otherwise by the TTL.
    """
    run_max, run_count = session.execute(
        select(func.max(Run.id), func.count(Run.id)).where(Run.status == RunStatus.COMPLETE)
    ).one()
    result_max, result_count = session.execute(
        select(func.max(BenchmarkResult.id), func.count(BenchmarkResult.id))
    ).one()
    score_max, score_count = session.execute(select(func.max(Score.id), func.count(Score.id))).one()
    methodology = ensure_current_methodology(session, get_config(session))
    cutoff_hour = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H")
    return (
        run_max, run_count, result_max, result_count, score_max, score_count,
        methodology.version, cutoff_hour,
    )


def invalidate_points_cache() -> None:
    """Drop the cached trend points (called where scores are rewritten in place)."""
    with _POINTS_LOCK:
        _POINTS_CACHE.clear()


def _load_points(session: Session, days: int) -> list[RunPoint]:
    """``_read_points``, kept between requests while its stamp is unchanged.

    Requests that arrive together (the Trends page asks for the heatmap and the panel at
    once) wait for one load instead of each doing it. Callers only read the points; each
    gets its own list.
    """
    with _POINTS_LOCK:
        stamp = _points_stamp(session, days)
        hit = _POINTS_CACHE.get(days)
        if hit is not None and hit[0] == stamp and time.monotonic() - hit[1] < _POINTS_TTL_S:
            return list(hit[2])
        points = _read_points(session, days)
        if len(_POINTS_CACHE) >= 4:
            _POINTS_CACHE.clear()
        _POINTS_CACHE[days] = (stamp, time.monotonic(), points)
        return list(points)


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

    # The current methodology's crown measurements (what we rank on today) — so the UI can
    # feature the same "vs typical" matrix for them + the Overall without hardcoding a set
    # that drifts when the methodology changes.
    methodology = ensure_current_methodology(session, get_config(session))
    crown, _ = overall_metrics(methodology.definition or {})

    return {
        "weekday": weekday,
        "hour": hour,
        "window_hours": window_hours,
        "window_days": days,
        "min_samples": min_samples,
        "crown_metrics": list(crown),
        "metrics": metrics,
    }
