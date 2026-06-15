"""Run endpoints: trigger a benchmark suite."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config_store import get_config
from ..database import get_session
from ..models import Run, RunStatus
from ..runner import MAX_ITERATIONS, create_run, execute_run
from ..schemas import RunCreate, RunDetail
from .routes_results import _serialize_run

router = APIRouter()


@router.post("/runs/{run_id}/cancel", response_model=RunDetail)
def cancel_run(run_id: int, session: Session = Depends(get_session)) -> RunDetail:
    """Mark an in-progress run as failed (manual stop / unstick a hung run)."""
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if run.status in (RunStatus.RUNNING, RunStatus.PENDING):
        run.status = RunStatus.FAILED
        run.error = "Cancelled by user."
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
    return _serialize_run(run)


@router.post("/run", response_model=RunDetail, status_code=202)
def trigger_run(
    payload: RunCreate,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> RunDetail:
    """Create a run and execute it in the background. Returns the pending run."""
    run_id = create_run(
        label=payload.label, notes=payload.notes, iterations=payload.iterations
    )
    background.add_task(execute_run, run_id)
    run = session.get(Run, run_id)
    if run is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail="Run could not be created")
    return _serialize_run(run)


@router.get("/runs/estimate")
def estimate_run(session: Session = Depends(get_session)) -> dict:
    """Estimate per-iteration duration (ms) from recent completed runs.

    The UI multiplies this by the chosen iteration count to show an ETA.
    Returns ``per_iteration_ms = null`` until at least one timed run exists.
    """
    rows = session.scalars(
        select(Run)
        .where(Run.status == RunStatus.COMPLETE, Run.per_iteration_ms.is_not(None))
        .order_by(Run.created_at.desc())
        .limit(5)
    ).all()
    values = [r.per_iteration_ms for r in rows if r.per_iteration_ms]
    avg = round(sum(values) / len(values), 3) if values else None
    config = get_config(session)
    return {
        "per_iteration_ms": avg,
        "based_on_runs": len(values),
        "default_iterations": int(config.get("iterations", 1) or 1),
        "max_iterations": MAX_ITERATIONS,
    }
