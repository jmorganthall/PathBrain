"""Methodology endpoints — the versioned interpretation layer.

Read access to the published methodologies: how raw becomes a score at each point
in time. "Here's the methodology used when this was collected." Snapshots are
created by the scoring path / startup; these endpoints are read-only (plus a lazy
ensure so the current methodology always appears, even on a fresh database).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config_store import get_config
from ..database import get_session
from ..methodology import ensure_current_methodology, serialize, summarize
from ..models import Methodology

router = APIRouter()


@router.get("/methodologies")
def list_methodologies(session: Session = Depends(get_session)) -> dict:
    """All published methodologies (newest current first), compact view.

    Lazily records the current methodology so a fresh instance still shows the
    interpretation in play before any re-grade has happened.
    """
    ensure_current_methodology(session, get_config(session))
    rows = session.scalars(
        select(Methodology).order_by(Methodology.is_current.desc(), Methodology.created_at.desc())
    ).all()
    return {"methodologies": [summarize(r) for r in rows], "count": len(rows)}


@router.get("/methodologies/current")
def current_methodology(session: Session = Depends(get_session)) -> dict:
    """The published-now methodology, with its full frozen definition."""
    row = ensure_current_methodology(session, get_config(session))
    return serialize(row)


@router.get("/methodologies/{version}")
def get_methodology(version: str, session: Session = Depends(get_session)) -> dict:
    """One methodology's full definition (axes + every metric's weight/thresholds)."""
    ensure_current_methodology(session, get_config(session))
    row = session.get(Methodology, version)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No methodology '{version}'")
    return serialize(row)
