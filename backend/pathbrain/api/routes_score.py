"""Score endpoints: fetch a run's score, preview scoring, inspect weights."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config_store import get_config
from ..database import get_session
from ..models import Run
from ..schemas import ScoreOut
from ..scoring import compute_score

router = APIRouter()


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
