"""Results endpoints: fetch a run's full detail (metrics + score)."""
from __future__ import annotations

from statistics import mean, median

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config_store import get_config
from ..database import get_session
from ..models import Run, RunStatus, ScoreResult
from ..schemas import BenchmarkResultOut, RunBaselineOut, RunDetail, ScoreOut
from ..settings_profile import summarize

router = APIRouter()

# How many recent runs to average into a baseline. Keeps the comparison anchored
# to recent typical behavior rather than a profile's entire history.
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


def _average_metrics(runs: list[Run], exclude_run_id: int) -> tuple[dict, int]:
    """Mean of each numeric plugin metric across ``runs`` (excluding one run).

    Returns ``(metrics, run_count)`` where ``metrics`` maps plugin -> {key: mean}
    and ``run_count`` is the number of runs that contributed at least one value.
    """
    samples: dict[str, dict[str, list[float]]] = {}
    contributing: set[int] = set()
    for r in runs:
        if r.id == exclude_run_id:
            continue
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
    return metrics, len(contributing)


@router.get("/results/{run_id}/baseline", response_model=RunBaselineOut)
def get_result_baseline(
    run_id: int, session: Session = Depends(get_session)
) -> RunBaselineOut:
    """Average plugin metrics for the *best-scoring* settings profile, for comparison.

    The useful question on a run isn't "how does this compare to its own profile"
    (that's circular) but "how far is it from the best configuration I've found".
    So the baseline is the settings profile with the highest median SOPS: each
    metric arrow then shows whether this run beats — or trails — the best profile.

    Confident profiles (>= ``correlation.min_runs`` runs) are preferred when any
    exist, so a single fluky run can't define "best". When no profiles with
    captured settings exist, falls back to the most recent completed runs. The
    viewed run is always excluded so it isn't compared against itself.
    """
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    min_runs = int((get_config(session).get("correlation", {}) or {}).get("min_runs", 5) or 5)

    # Group completed, scored runs by settings profile.
    rows = session.execute(
        select(Run, ScoreResult.sops)
        .join(ScoreResult, ScoreResult.run_id == Run.id)
        .where(Run.status == RunStatus.COMPLETE, Run.settings_fingerprint.is_not(None))
        .order_by(Run.created_at.desc())
    ).all()
    groups: dict[str, dict] = {}
    for r, sops in rows:
        g = groups.setdefault(
            r.settings_fingerprint, {"runs": [], "sops": [], "settings": r.settings}
        )
        if len(g["runs"]) < BASELINE_RUN_LIMIT:
            g["runs"].append(r)
        g["sops"].append(sops)

    # Prefer confident profiles when we have any; otherwise consider them all.
    confident = {fp: g for fp, g in groups.items() if len(g["sops"]) >= min_runs}
    candidates = confident or groups

    best_fp: str | None = None
    best_median: float | None = None
    for fp, g in candidates.items():
        med = median(g["sops"])
        if best_median is None or med > best_median:
            best_fp, best_median = fp, med

    if best_fp is not None:
        best = groups[best_fp]
        metrics, count = _average_metrics(best["runs"], exclude_run_id=run_id)
        if count > 0:
            return RunBaselineOut(
                run_id=run_id,
                scope="best_profile",
                profile_fingerprint=best_fp,
                profile_label=summarize(best["settings"]),
                profile_median_sops=round(best_median, 2),
                is_best_profile=run.settings_fingerprint == best_fp,
                run_count=count,
                metrics=metrics,
            )

    # Fallback: no usable profile (settings never captured, or the best profile
    # only contains this run) — compare against recent completed runs instead.
    recent = list(
        session.scalars(
            select(Run)
            .where(Run.status == RunStatus.COMPLETE, Run.id != run_id)
            .order_by(Run.created_at.desc())
            .limit(BASELINE_RUN_LIMIT)
        ).all()
    )
    metrics, count = _average_metrics(recent, exclude_run_id=run_id)
    return RunBaselineOut(
        run_id=run_id,
        scope="all",
        profile_fingerprint=None,
        profile_label=None,
        profile_median_sops=None,
        is_best_profile=False,
        run_count=count,
        metrics=metrics,
    )
