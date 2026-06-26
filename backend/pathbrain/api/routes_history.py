"""History endpoints: list past runs and time-series data for charts."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config_store import get_config
from ..database import get_session
from ..methodology import ensure_current_methodology
from ..models import Run, Score
from ..schemas import RunSummary

router = APIRouter()


def _current_scores(session: Session, run_ids: list[int]) -> dict[int, Score]:
    """Map run_id → its Score under the current methodology (any comparability)."""
    if not run_ids:
        return {}
    methodology = ensure_current_methodology(session, get_config(session))
    rows = session.scalars(
        select(Score).where(
            Score.run_id.in_(run_ids), Score.methodology_version == methodology.version
        )
    ).all()
    return {r.run_id: r for r in rows}


def _axes(score: Score | None) -> dict | None:
    """A run's axis scores, or None when it isn't comparable under the current
    methodology (the new 'legacy' — its raw can't supply a required metric)."""
    if score is None or score.comparability == "incomparable":
        return None
    return score.axis_scores or {}


@router.get("/history/count")
def history_count(session: Session = Depends(get_session)) -> dict:
    """Total number of runs, for paginating the history list."""
    return {"count": session.scalar(select(func.count()).select_from(Run)) or 0}


@router.get("/history", response_model=list[RunSummary])
def list_history(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(get_session),
) -> list[RunSummary]:
    runs = session.scalars(
        select(Run).order_by(Run.created_at.desc()).limit(limit).offset(offset)
    ).all()
    scores = _current_scores(session, [r.id for r in runs])
    out = []
    for run in runs:
        axes = _axes(scores.get(run.id))
        out.append(
            RunSummary(
                id=run.id,
                created_at=run.created_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
                status=run.status.value if hasattr(run.status, "value") else str(run.status),
                label=run.label,
                speed=(axes or {}).get("speed"),
                smoothness=(axes or {}).get("smoothness"),
                # "legacy" now = not comparable under the current methodology.
                legacy=run.score is not None and axes is None,
                iterations=run.iterations,
                iterations_completed=run.iterations_completed,
                per_iteration_ms=run.per_iteration_ms,
            )
        )
    return out


@router.get("/history/series")
def history_series(
    limit: int = Query(100, ge=1, le=1000),
    include_legacy: bool = Query(
        False, description="Include runs not comparable under the current methodology."
    ),
    session: Session = Depends(get_session),
) -> dict:
    """Time-series of Speed/Smoothness + key metrics for charting (oldest → newest).

    Runs that aren't comparable under the current methodology are excluded by
    default so the trend isn't built on non-comparable scores.
    """
    runs = session.scalars(
        select(Run).order_by(Run.created_at.desc()).limit(limit)
    ).all()
    runs = list(reversed(runs))  # chronological for charts
    scores = _current_scores(session, [r.id for r in runs])

    points = []
    for run in runs:
        score = scores.get(run.id)
        axes = _axes(score)
        if axes is None and not include_legacy:
            continue
        axes = axes or {}
        values = (score.metric_values if score else {}) or {}
        points.append(
            {
                "run_id": run.id,
                "timestamp": run.created_at.isoformat(),
                "label": run.label,
                "speed": axes.get("speed"),
                "smoothness": axes.get("smoothness"),
                "stability": axes.get("stability"),
                "completion": axes.get("completion"),
                "dns_ms": values.get("dns"),
                "tcp_ms": values.get("tcp"),
                "tls_ms": values.get("tls"),
                "ttfb_ms": values.get("ttfb"),
                "jitter_ms": values.get("jitter"),
                "packet_loss_pct": values.get("packet_loss"),
            }
        )
    return {"points": points}
