"""Score endpoints: fetch a run's score, preview scoring, inspect weights."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median, quantiles

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config_store import get_config
from ..database import get_session
from ..models import Run, RunStatus, ScoreResult
from ..schemas import ScoreOut
from ..scoring import compute_score

router = APIRouter()


@router.get("/score/rolling")
def rolling_score(
    hours: int = Query(24, ge=1, le=720),
    session: Session = Depends(get_session),
) -> dict:
    """Windowed SOPS over completed runs in the last ``hours`` hours.

    This is the stable "current responsiveness" figure: a median over many runs,
    with an interquartile band, so it doesn't swing on point-in-time noise.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    rows = session.execute(
        select(ScoreResult.sops)
        .join(Run, Run.id == ScoreResult.run_id)
        .where(Run.status == RunStatus.COMPLETE, Run.created_at >= cutoff)
    ).all()
    vals = sorted(r[0] for r in rows)
    if not vals:
        return {
            "window_hours": hours,
            "count": 0,
            "median": None,
            "p25": None,
            "p75": None,
            "min": None,
            "max": None,
        }
    med = round(median(vals), 2)
    if len(vals) >= 2:
        q = quantiles(vals, n=4)  # [p25, p50, p75]
        p25, p75 = round(q[0], 2), round(q[2], 2)
    else:
        p25 = p75 = med
    return {
        "window_hours": hours,
        "count": len(vals),
        "median": med,
        "p25": p25,
        "p75": p75,
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
    }


@router.get("/score/weights")
def get_weights(session: Session = Depends(get_session)) -> dict:
    """Current SOPS weights and normalization thresholds."""
    config = get_config(session)
    return {"weights": config["weights"], "thresholds": config["thresholds"]}


@router.post("/score/preview", response_model=ScoreOut)
def preview_score(
    plugin_metrics: dict = Body(..., description="plugin -> metrics, e.g. {'dns': {'lookup_ms': 12}}"),
    session: Session = Depends(get_session),
) -> ScoreOut:
    """Compute a SOPS for ad-hoc metrics using the current weights/thresholds."""
    config = get_config(session)
    breakdown = compute_score(
        plugin_metrics, weights=config["weights"], thresholds=config["thresholds"]
    )
    return ScoreOut(
        sops=breakdown.sops,
        subscores=breakdown.subscores,
        weights_used=breakdown.weights_used,
        metric_values=breakdown.metric_values,
    )


@router.get("/score/{run_id}", response_model=ScoreOut)
def get_score(run_id: int, session: Session = Depends(get_session)) -> ScoreOut:
    run = session.get(Run, run_id)
    if run is None or run.score is None:
        raise HTTPException(status_code=404, detail=f"No score for run {run_id}")
    return ScoreOut.model_validate(run.score)
