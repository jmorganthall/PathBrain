"""Monitoring status endpoint.

Enable/disable and interval are part of the benchmark config (edited via
``/api/config``); this exposes the scheduler's live runtime status.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import schedule
from ..config_store import get_config
from ..database import get_session
from ..scheduler import scheduler_status

router = APIRouter()


@router.get("/monitoring")
def monitoring_status() -> dict:
    return scheduler_status()


@router.get("/schedule")
def schedule_status(session: Session = Depends(get_session)) -> dict:
    """Everything scheduled to run, soonest first — and the one thing happening next.

    The Dashboard could say when the next *monitoring* run was due and nothing else, which
    is the wrong half of the answer: monitoring is the small, frequent, unsurprising piece,
    while a duel window opening at 03:00, a nightly unshaped baseline test or an armed
    experiment change what the platform is doing for hours and announced themselves only by
    taking the pipeline. Disarmed schedules are listed and marked rather than dropped —
    "the duel is off" and "the duel is six hours away" are different answers.
    """
    return schedule.status(get_config(session), scheduler_status())
