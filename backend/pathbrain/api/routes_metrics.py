"""Metric catalog endpoint — the registry's metadata, for the UI.

Exposes the single metric registry so the frontend renders labels, descriptions,
units and direction from one authoritative source instead of duplicating them.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..metrics import catalog

router = APIRouter()


@router.get("/metrics")
def list_metrics() -> dict:
    """All known metrics with their display metadata, axis, and rubric."""
    return {"metrics": catalog()}
