"""Monitoring status endpoint.

Enable/disable and interval are part of the benchmark config (edited via
``/api/config``); this exposes the scheduler's live runtime status.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..scheduler import scheduler_status

router = APIRouter()


@router.get("/monitoring")
def monitoring_status() -> dict:
    return scheduler_status()
