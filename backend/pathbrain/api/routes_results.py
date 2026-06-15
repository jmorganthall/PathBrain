"""Results endpoints: fetch a run's full detail (metrics + score)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_session
from ..models import Run, RunStatus
from ..schemas import BenchmarkResultOut, RunDetail, ScoreOut

router = APIRouter()


def _serialize_run(run: Run) -> RunDetail:
    return RunDetail(
        id=run.id,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        status=run.status.value if hasattr(run.status, "value") else str(run.status),
        label=run.label,
        notes=run.notes,
        error=run.error,
        iterations=run.iterations,
        iterations_completed=run.iterations_completed,
        per_iteration_ms=run.per_iteration_ms,
        settings_fingerprint=run.settings_fingerprint,
        settings=run.settings,
        config_used=run.config_used,
        results=[BenchmarkResultOut.model_validate(r) for r in run.results],
        score=ScoreOut.model_validate(run.score) if run.score else None,
    )


@router.get("/results/latest", response_model=RunDetail)
def latest_result(session: Session = Depends(get_session)) -> RunDetail:
    run = session.scalars(
        select(Run)
        .where(Run.status == RunStatus.COMPLETE)
        .order_by(Run.created_at.desc())
        .limit(1)
    ).first()
    if run is None:
        raise HTTPException(status_code=404, detail="No completed runs yet")
    return _serialize_run(run)


@router.get("/results/{run_id}", response_model=RunDetail)
def get_result(run_id: int, session: Session = Depends(get_session)) -> RunDetail:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return _serialize_run(run)
