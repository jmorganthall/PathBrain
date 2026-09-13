"""The firewall write path's API: the ledger, and the link watch.

Arm and hands-off used to live here. They are gone with the state they controlled — see
``firewall_guard`` for why the rate valve was rolled back. What is left only reports.
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from .. import firewall_guard, link_watch
from ..logging_config import get_logger

router = APIRouter()
log = get_logger("api.firewall")


class WatchBody(BaseModel):
    enabled: bool


@router.get("/firewall/guard")
def guard_status(limit: int = Query(default=25, ge=1, le=200)) -> dict:
    """The write rate and the newest ledger rows — what the Firewall page renders.

    The path keeps its name so an existing client does not break; there is no longer any
    guard state to report, only what PathBrain has written.
    """
    out = firewall_guard.summary()
    out["writes"] = firewall_guard.recent_writes(limit)
    return out


@router.get("/firewall/writes")
def writes(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict]:
    return firewall_guard.recent_writes(limit)


@router.get("/firewall/watch")
def watch(limit: int = Query(default=100, ge=1, le=500),
          hours: float = Query(default=24.0, gt=0, le=720)) -> dict:
    """The continuous ping beside every write: what it is watching, and every gap it saw.

    ``summary`` is the reading that matters — of the gaps in the window, how many had a
    firewall write in flight and how many did not. A count of gaps alone says the link is
    unstable; only the split says whether PathBrain is why.
    """
    return {
        "status": link_watch.status(),
        "summary": link_watch.gap_summary(hours),
        "gaps": link_watch.recent_gaps(limit),
    }


@router.post("/firewall/watch")
def set_watch(body: WatchBody) -> dict:
    """Start or stop the watch now, and remember the choice."""
    from ..config_store import save_config
    from ..database import session_scope

    with session_scope() as s:
        save_config(s, {"firewall": {"watch_enabled": bool(body.enabled)}})
    if body.enabled:
        link_watch.start()
    else:
        link_watch.stop()
    return link_watch.status()
