"""Write-and-ping: measure what one firewall write costs the network.

See ``write_probe`` for why the write is decomposed into its two halves and why two ping
targets are used. Every write here goes through the guarded provider like any other.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from .. import write_probe
from ..logging_config import get_logger

router = APIRouter()
log = get_logger("api.write_probe")


class ProbeBody(BaseModel):
    changes: list[dict]
    # Omitted → the address PathBrain already talks to. Only supply one to override it.
    firewall_target: str | None = None
    through_target: str = "1.1.1.1"
    baseline_s: float = write_probe.DEFAULT_BASELINE_S
    settle_s: float = write_probe.DEFAULT_SETTLE_S


@router.post("/firewall/write-probe")
def start_probe(body: ProbeBody = Body(...)) -> dict:
    """Run one write with ping running throughout. Returns the probe id.

    A 400 covers the genuinely bad request — nothing to write, no target, the firewall
    already on these values, or the guard hands-off (arm writes first: this applies a real
    change to a live firewall and is meant to be watched).
    """
    try:
        probe_id = write_probe.start(
            body.changes, firewall_target=body.firewall_target,
            through_target=body.through_target,
            baseline_s=body.baseline_s, settle_s=body.settle_s,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": probe_id, "status": "running"}


@router.get("/firewall/write-probe")
def probe_status() -> dict:
    """The running probe (with its timeline so far), the recent ones, and the defaults.

    ``defaults.firewall_target`` is read from the provider PathBrain is already configured
    with, so the page can fill it in rather than asking for an address the application
    knows perfectly well."""
    return {
        "current": write_probe.current(),
        "recent": write_probe.recent(),
        "defaults": {
            "firewall_target": write_probe.firewall_address(),
            "through_target": "1.1.1.1",
        },
    }


@router.get("/firewall/write-probe/{probe_id}")
def probe_detail(probe_id: int) -> dict:
    out = write_probe.get(probe_id)
    if out is None:
        raise HTTPException(status_code=404, detail="No such write probe.")
    return out


@router.post("/firewall/write-probe/cancel")
def cancel_probe() -> dict:
    """Stop after the current step. The original values are still restored."""
    return {"cancelled": write_probe.cancel()}
