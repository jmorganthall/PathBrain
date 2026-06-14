"""Run endpoints: trigger a benchmark suite."""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_session
from ..models import Run
from ..runner import create_run, execute_run
from ..schemas import RunCreate, RunDetail
from .routes_results import _serialize_run

router = APIRouter()


@router.post("/run", response_model=RunDetail, status_code=202)
def trigger_run(
    payload: RunCreate,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> RunDetail:
    """Create a run and execute it in the background. Returns the pending run."""
    run_id = create_run(label=payload.label, notes=payload.notes)
    background.add_task(execute_run, run_id)
    run = session.get(Run, run_id)
    if run is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail="Run could not be created")
    return _serialize_run(run)
