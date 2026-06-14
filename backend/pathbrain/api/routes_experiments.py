"""Experiments endpoint (stub).

The experiment engine — apply a candidate setting, wait, benchmark, store,
rollback, repeat — lands in a later phase. The endpoint exists now so the API
surface from the PRD is stable and the UI can advertise the capability.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/experiments")
def list_experiments() -> dict:
    return {
        "experiments": [],
        "status": "not_implemented",
        "message": (
            "The experiment engine (apply → wait → benchmark → store → rollback) "
            "is planned for a later phase. Safety: changes will always be preceded "
            "by a configuration snapshot."
        ),
    }
