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
from ..runner import rescore_run
from ..schemas import ScoreOut
from ..scoring import compute_score

router = APIRouter()


@router.post("/score/rescore")
def rescore_history(session: Session = Depends(get_session)) -> dict:
    """Re-grade every completed run with the current scoring rubric.

    Run this after changing thresholds/weights so historical scores stay
    comparable (no discontinuity in the SOPS timeline at the change).
    """
    cfg = get_config(session)
    weights = cfg.get("weights", {})
    thresholds = cfg.get("thresholds", {})
    rubric_version = cfg.get("rubric_version")
    p_weights = cfg.get("perceptual_weights", {})
    p_thresholds = cfg.get("perceptual_thresholds", {})
    runs = session.scalars(select(Run).where(Run.status == RunStatus.COMPLETE)).all()
    rescored = sum(
        1
        for run in runs
        if rescore_run(run, weights, thresholds, rubric_version, p_weights, p_thresholds)
    )
    session.commit()
    return {"rescored": rescored, "rubric_version": rubric_version}


@router.get("/score/rolling")
def rolling_score(
    hours: int = Query(24, ge=1, le=720),
    session: Session = Depends(get_session),
) -> dict:
    """Windowed SOPS over completed runs in the last ``hours`` hours.

    This is the stable "current responsiveness" figure: a median over many runs,
    with an interquartile band, so it doesn't swing on point-in-time noise. Also
    returns the median per-metric subscore + metric value over the window (and the
    most recent weights) so the dashboard can show an aggregated breakdown.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    rows = (
        session.execute(
            select(ScoreResult)
            .join(Run, Run.id == ScoreResult.run_id)
            .where(Run.status == RunStatus.COMPLETE, Run.created_at >= cutoff)
            .order_by(Run.created_at)
        )
        .scalars()
        .all()
    )
    if not rows:
        return {
            "window_hours": hours,
            "count": 0,
            "median": None,
            "p25": None,
            "p75": None,
            "min": None,
            "max": None,
            "subscores": {},
            "metric_values": {},
            "weights": {},
        }

    def median_by_key(dicts: list[dict]) -> dict:
        keys: set[str] = set()
        for d in dicts:
            keys.update((d or {}).keys())
        out: dict[str, float] = {}
        for k in keys:
            vals = [d[k] for d in dicts if (d or {}).get(k) is not None]
            if vals:
                out[k] = round(median(vals), 2)
        return out

    vals = sorted(r.sops for r in rows)
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
        "subscores": median_by_key([r.subscores or {} for r in rows]),
        "metric_values": median_by_key([r.metric_values or {} for r in rows]),
        "weights": rows[-1].weights_used or {},
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
