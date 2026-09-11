"""Acknowledging a diagnostic banner: clear it, list what is cleared, bring one back.

See ``alerts`` for why an acknowledgement is keyed on a *situation* rather than on the
contents of the payload. These endpoints change what a page displays and nothing else — no
score, gate or measurement reads an acknowledgement.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from .. import alerts
from ..logging_config import get_logger

router = APIRouter()
log = get_logger("api.alerts")


class AckBody(BaseModel):
    signature: str
    state: dict | None = None


@router.get("/alerts")
def list_acks() -> dict:
    """Every standing acknowledgement. A page shows this so a cleared alert is muted rather
    than invisible — "3 alerts cleared" with a way back is honest; silence is not."""
    return {"acks": alerts.acks()}


@router.post("/alerts/{key}/ack")
def ack(key: str, body: AckBody = Body(...)) -> dict:
    """Clear an alert for the situation the caller was actually shown.

    The caller passes back the ``signature`` from the payload it rendered, deliberately
    rather than the server re-deriving it: if the situation moved between the render and the
    press, the acknowledgement records what the person actually read and the next poll shows
    them what changed — which is the right outcome, not a race to paper over.
    """
    if not body.signature:
        raise HTTPException(status_code=400, detail="An acknowledgement needs the signature it is for.")
    return alerts.acknowledge(key, body.signature, body.state, by="user")


@router.delete("/alerts/{key}/ack")
def unack(key: str) -> dict:
    """Bring an alert back. Not an error when it wasn't cleared — the caller asked for a
    state that already holds."""
    return {"key": key, "cleared": alerts.clear(key)}
