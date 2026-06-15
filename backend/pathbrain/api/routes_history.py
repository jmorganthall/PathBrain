"""History endpoints: list past runs and time-series data for charts."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_session
from ..models import Run, ScoreResult
from ..schemas import RunSummary

router = APIRouter()


@router.get("/history", response_model=list[RunSummary])
def list_history(
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[RunSummary]:
    runs = session.scalars(
        select(Run).order_by(Run.created_at.desc()).limit(limit)
    ).all()
    return [
        RunSummary(
            id=run.id,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            status=run.status.value if hasattr(run.status, "value") else str(run.status),
            label=run.label,
            sops=run.score.sops if run.score else None,
            iterations=run.iterations,
            iterations_completed=run.iterations_completed,
            per_iteration_ms=run.per_iteration_ms,
        )
        for run in runs
    ]


@router.get("/history/series")
def history_series(
    limit: int = Query(100, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> dict:
    """Time-series of SOPS and key metrics for charting (oldest → newest)."""
    rows = session.execute(
        select(Run, ScoreResult)
        .join(ScoreResult, ScoreResult.run_id == Run.id)
        .order_by(Run.created_at.desc())
        .limit(limit)
    ).all()
    rows = list(reversed(rows))  # chronological for charts

    points = []
    for run, score in rows:
        values = score.metric_values or {}
        points.append(
            {
                "run_id": run.id,
                "timestamp": run.created_at.isoformat(),
                "label": run.label,
                "sops": score.sops,
                "dns_ms": values.get("dns"),
                "tcp_ms": values.get("tcp"),
                "tls_ms": values.get("tls"),
                "ttfb_ms": values.get("ttfb"),
                "jitter_ms": values.get("jitter"),
                "packet_loss_pct": values.get("packet_loss"),
            }
        )
    return {"points": points}
