"""The Overall page: one ranking fitted over the pooled record and the ring together."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import overall_ranking
from ..database import get_session
from ..logging_config import get_logger

log = get_logger(__name__)
router = APIRouter()


@router.get("/overall")
def overall(
    session: Session = Depends(get_session),
    backtest: bool = Query(True, description="Include the leave-one-session-out predictive check."),
) -> dict:
    """The fused ranking: every profile's Overall fitted from its pooled median AND every
    head-to-head round it fought, with an error bar from both; the crown it names; the
    three corners (pooled alone, ring alone, together); what the ring changed; and how
    well each verdict predicted past nights. Read-only, and there is nothing to set: the
    pooled slack is measured from the ledger, never chosen."""
    return overall_ranking.ranking(session, backtest=backtest)


@router.get("/overall/ring-target")
def overall_ring_target(session: Session = Depends(get_session)) -> dict:
    """What the ring will fight next under the fused policy: the fused #1 and the profiles
    the fit cannot yet separate from it, most ambiguous first (`overall_ranking.ring_target`)."""
    return {"target": overall_ranking.ring_target(session)}
