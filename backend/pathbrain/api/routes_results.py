"""Results endpoints: fetch a run's full detail (metrics + score)."""
from __future__ import annotations

from statistics import mean

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_session
from ..models import Run, RunStatus
from ..schemas import BenchmarkResultOut, RunBaselineOut, RunDetail, ScoreOut
from ..settings_profile import summarize

router = APIRouter()

# How many recent runs to average into a baseline. Keeps the comparison anchored
# to the profile's *recent* typical behavior rather than its entire history.
BASELINE_RUN_LIMIT = 50


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


@router.get("/results/{run_id}/baseline", response_model=RunBaselineOut)
def get_result_baseline(
    run_id: int, session: Session = Depends(get_session)
) -> RunBaselineOut:
    """Average plugin metrics for the run's settings profile, for comparison.

    Prefers other completed runs that share this run's settings fingerprint (the
    same firewall/SQM "profile"); when there are none, falls back to the most
    recent completed runs so a comparison is still available. The current run is
    always excluded so it isn't compared against itself.
    """
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    fp = run.settings_fingerprint
    others = select(Run).where(Run.status == RunStatus.COMPLETE, Run.id != run_id)

    scope = "all"
    label: str | None = None
    runs: list[Run] = []
    if fp:
        runs = list(
            session.scalars(
                others.where(Run.settings_fingerprint == fp)
                .order_by(Run.created_at.desc())
                .limit(BASELINE_RUN_LIMIT)
            ).all()
        )
        if runs:
            scope = "profile"
            label = summarize(run.settings)
    if not runs:
        scope = "all"
        runs = list(
            session.scalars(
                others.order_by(Run.created_at.desc()).limit(BASELINE_RUN_LIMIT)
            ).all()
        )

    # Collect each numeric plugin metric across the baseline runs, then average.
    samples: dict[str, dict[str, list[float]]] = {}
    contributing: set[int] = set()
    for r in runs:
        for res in r.results:
            for key, value in (res.metrics or {}).items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                samples.setdefault(res.plugin, {}).setdefault(key, []).append(float(value))
                contributing.add(r.id)

    metrics = {
        plugin: {key: round(mean(vals), 3) for key, vals in keyed.items() if vals}
        for plugin, keyed in samples.items()
    }
    return RunBaselineOut(
        run_id=run_id,
        scope=scope,
        profile_fingerprint=fp,
        profile_label=label,
        run_count=len(contributing),
        metrics=metrics,
    )
