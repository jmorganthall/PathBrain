"""Run endpoints: trigger a benchmark suite."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import coordinator, current_test
from ..config_store import get_config
from ..database import get_session, session_scope
from ..logging_config import get_logger
from ..models import Run, RunStatus
from ..runner import CHUNK_ITERATIONS, MAX_ITERATIONS, create_run, execute_run, run_chunk
from ..schemas import CurrentTestStart, RunCreate, RunDetail
from .routes_results import _serialize_run

router = APIRouter()
log = get_logger("api.run")


def _locked_execute(run_id: int) -> None:
    """Execute a run holding the coordination lock, so a manual run queues behind
    (and never overlaps) any other firewall/benchmark session."""
    with coordinator.hold(f"run#{run_id}"):
        execute_run(run_id)


def _run_completed(run_id: int) -> bool:
    """Did ``run_id`` finish COMPLETE? (Cancelling the active chunk marks it FAILED, which
    is how a user stops an in-flight series.)"""
    with session_scope() as session:
        run = session.get(Run, run_id)
        return bool(run and run.status == RunStatus.COMPLETE)


def _locked_execute_series(first_run_id: int, total: int, label: str | None, notes: str | None) -> None:
    """Execute a large manual run as a series of <=CHUNK_ITERATIONS runs under one held
    lock, so partial completion still persists each chunk. The first chunk row is created
    up front (and returned to the caller); the rest are created as we go. Stops early if a
    chunk fails (the environment isn't stable, or the user cancelled the active chunk) —
    every completed chunk is already persisted."""
    n_chunks = (total + CHUNK_ITERATIONS - 1) // CHUNK_ITERATIONS
    with coordinator.hold(f"run-series#{first_run_id}"):
        execute_run(first_run_id)
        prev_ok = _run_completed(first_run_id)
        done = CHUNK_ITERATIONS
        idx = 1
        while prev_ok and done < total:
            idx += 1
            iters = min(CHUNK_ITERATIONS, total - done)
            _run_id, prev_ok, _completed = run_chunk(
                label=label,
                notes=f"{notes or 'Manual run'} · part {idx}/{n_chunks}",
                iterations=iters,
            )
            done += iters
            if not prev_ok:
                log.warning("Run series (from #%s): part %s failed; stopping early", first_run_id, idx)
                break


@router.get("/runs/{run_id}/verify-derivation")
def verify_run_derivation(run_id: int, session: Session = Depends(get_session)) -> dict:
    """Read-only integrity audit for one run: re-derive every metric from its immutable raw and
    diff against the stored value. ``consistent: true`` means the stored metrics reproduce exactly
    from raw under the current derivation (like-for-like preserved); ``drift`` lists any metric
    whose stored value predates the current derivation and was never re-derived."""
    from ..config import get_settings
    from ..runner import verify_run_derivation as _verify

    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if not run.results:
        raise HTTPException(status_code=400, detail=f"Run {run_id} has no stored raw to verify")
    return _verify(run, get_settings().artifact_dir)


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
    """Create a run and execute it in the background. Returns the pending (first) run.

    A request over ``CHUNK_ITERATIONS`` is executed as a **series** of runs of at most
    ``CHUNK_ITERATIONS`` iterations each — so if a long series is interrupted, every
    completed chunk is still persisted instead of the whole thing being lost. The first
    chunk's row is returned here; the rest run sequentially under one held lock. The
    Dashboard tracks the *latest* run, so it follows the series as each chunk starts.
    """
    config = get_config(session)
    total = payload.iterations or int(config.get("iterations", 1) or 1)
    total = max(1, min(int(total), MAX_ITERATIONS))
    if total > CHUNK_ITERATIONS:
        first_id = create_run(
            label=payload.label,
            notes=f"{payload.notes or 'Manual run'} · part 1/"
            f"{(total + CHUNK_ITERATIONS - 1) // CHUNK_ITERATIONS}",
            iterations=CHUNK_ITERATIONS,
        )
        background.add_task(_locked_execute_series, first_id, total, payload.label, payload.notes)
        run = session.get(Run, first_id)
    else:
        run_id = create_run(label=payload.label, notes=payload.notes, iterations=total)
        background.add_task(_locked_execute, run_id)
        run = session.get(Run, run_id)
    if run is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail="Run could not be created")
    return _serialize_run(run)


@router.post("/current/test", status_code=202)
def start_current_test(payload: CurrentTestStart) -> dict:
    """Start a "test the current settings for X minutes" session: a time-boxed loop that
    benchmarks the live profile as-is (no firewall write) in short chunks until the timer
    is up. Returns the session status. 409 if one is already running."""
    try:
        ct_id = current_test.start(payload.minutes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    status = current_test.current()
    log.info("Test-current %s requested (%s min)", ct_id, payload.minutes)
    return status or {"id": ct_id, "status": "pending"}


@router.get("/current/test")
def get_current_test() -> dict:
    """The most recent test-current session (for status polling), or an empty payload."""
    return current_test.current() or {"status": None}


@router.post("/current/test/cancel")
def cancel_current_test() -> dict:
    """Ask the running test-current session to stop after its current chunk (data kept)."""
    cancelled = current_test.cancel()
    return {"cancelled": cancelled, "status": (current_test.current() or {}).get("status")}


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
