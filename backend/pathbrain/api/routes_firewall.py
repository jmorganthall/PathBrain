"""The firewall guard's API: state, ledger, arm, hands-off."""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from .. import firewall_guard
from ..logging_config import get_logger

router = APIRouter()
log = get_logger("api.firewall")


class HandsOffBody(BaseModel):
    reason: str | None = None


@router.get("/firewall/guard")
def guard_status(limit: int = Query(default=25, ge=1, le=200)) -> dict:
    """Hands-off and why, the write budget, the reconfigure rate, and the newest ledger
    rows — what the top-bar chip and the Plugins card render."""
    out = firewall_guard.summary()
    out["writes"] = firewall_guard.recent_writes(limit)
    return out


@router.get("/firewall/writes")
def writes(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict]:
    return firewall_guard.recent_writes(limit)


@router.post("/firewall/guard/arm")
def arm() -> dict:
    """Clear hands-off and stamp the running build as armed. The only way writes resume."""
    return firewall_guard.arm(by="user")


@router.post("/firewall/guard/hands-off")
def hands_off(body: HandsOffBody | None = None) -> dict:
    """Refuse every firewall write until armed again. Sessions in flight fail their next
    write and stop; nothing is restored — the firewall stays exactly as it is."""
    return firewall_guard.hands_off((body.reason if body else None) or "set by hand from the top bar", by="user")
