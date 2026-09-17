"""The Overall page: one ranking fitted over the pooled record and the ring together."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import overall_ranking
from ..config_store import get_config, save_config
from ..database import get_session
from ..logging_config import get_logger

log = get_logger(__name__)
router = APIRouter()


@router.get("/overall")
def overall(
    session: Session = Depends(get_session),
    slack: float | None = Query(
        None, ge=0.0, le=50.0,
        description="What-if: re-fit with this pooled slack (Overall points) instead of the "
                    "measured/configured one. Changes nothing stored.",
    ),
    backtest: bool = Query(True, description="Include the leave-one-session-out predictive check."),
) -> dict:
    """The fused ranking: every profile's Overall fitted from its pooled median AND every
    head-to-head round it fought, with an error bar from both; the crown it names; the
    three corners (pooled alone, ring alone, together); what the ring changed; and how
    well each verdict predicted past nights. Read-only."""
    return overall_ranking.ranking(session, slack_override=slack, backtest=backtest)


@router.get("/overall/config")
def overall_config(session: Session = Depends(get_session)) -> dict:
    cfg = get_config(session).get("overall_ranking") or {}
    return {"slack": cfg.get("slack"), "default_slack": overall_ranking.DEFAULT_SLACK}


@router.put("/overall/config")
def overall_config_update(body: dict, session: Session = Depends(get_session)) -> dict:
    """Pin the pooled slack (``{slack: number}``) or return it to measured (``{slack: null}``)."""
    if "slack" not in (body or {}):
        raise HTTPException(status_code=400, detail="slack is required (a number, or null for measured)")
    raw = body["slack"]
    if raw is None:
        value = None
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="slack must be a number or null") from None
        if not 0.0 <= value <= 50.0:
            raise HTTPException(status_code=400, detail="slack must be between 0 and 50 Overall points")
    save_config(session, {"overall_ranking": {"slack": value}})
    log.info("Overall ranking: pooled slack set to %s", "measured" if value is None else value)
    try:
        from .. import crown_follower

        crown_follower.poke()
    except Exception:  # noqa: BLE001
        log.debug("Overall ranking: could not poke the crown follower", exc_info=True)
    return {"slack": value, "default_slack": overall_ranking.DEFAULT_SLACK}
