"""Metric catalog endpoint — the registry's metadata, for the UI.

Exposes the single metric registry so the frontend renders labels, descriptions,
units and direction from one authoritative source instead of duplicating them.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_session
from ..drift import MIN_SAMPLES, metric_time_drift
from ..metrics import catalog

router = APIRouter()


@router.get("/metrics")
def list_metrics() -> dict:
    """All known metrics with their display metadata, axis, ledger role, and rubric."""
    return {"metrics": catalog()}


@router.get("/metrics/drift")
def metric_drift(
    min_samples: int = MIN_SAMPLES, session: Session = Depends(get_session)
) -> dict:
    """Campaign drift per metric — Spearman ρ of value vs ``created_at`` over history.

    The receipts for whether a metric is time-stationary (rankable raw) or drifting (needs
    the weather lens). Ratio shape stats (``jank_fraction``/``delivery_gini``/``cadence_cov``)
    should sit near 0; the absolute stall metrics and setup-phase timings should not. Run a
    re-derive first so display-only metrics are present in each run's cached values.
    """
    rows = metric_time_drift(session, min_samples=min_samples)
    return {"drift": rows, "min_samples": min_samples, "n_metrics": len(rows)}
