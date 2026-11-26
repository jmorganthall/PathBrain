"""``GET /api/verdict`` — the crown, who named it and why, and when it last changed.

The one read every view can make instead of resolving the crown for itself (`verdict`).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import crown_follower, verdict
from ..database import get_session

router = APIRouter()


@router.get("/verdict")
def get_verdict(
    session: Session = Depends(get_session),
    history: int = Query(20, ge=1, le=200, description="How many past answers to include."),
) -> dict:
    """The Verdict now (resolved live, as Auto-apply resolves it) and its history, newest
    first. Read-only: the history is written by the crown check, never by this read."""
    pooled = crown_follower.current_crown(session)
    now = verdict.current(session, (pooled or {}).get("fingerprint"))
    return {"verdict": now.as_dict(), "history": verdict.history(session, history)}
