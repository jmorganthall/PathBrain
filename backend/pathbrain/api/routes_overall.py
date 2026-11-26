"""The Overall page: one ranking fitted over the pooled record and the ring together."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import overall_ranking, profile_aggregates
from ..database import get_session
from ..logging_config import get_logger

log = get_logger(__name__)
router = APIRouter()


@router.get("/overall")
def overall(
    session: Session = Depends(get_session),
    backtest: bool = Query(True, description="Include the leave-one-session-out predictive check."),
    read: str | None = Query(
        None,
        pattern="^(all|epochs)$",
        description=(
            "Preview the ranking under another pooled read (`all` = one median over the whole "
            "record, `epochs` = per instrument era, v19's read). A preview only: the crown, "
            "Auto-apply and the ring follow the published methodology."
        ),
    ),
    budget: bool | None = Query(
        None,
        description=(
            "Preview the ranking with the crown kept within the speed budget (`true`, v20's "
            "rule) or not (`false`). A preview only, like `read`."
        ),
    ),
) -> dict:
    """The fused ranking: every profile's Overall fitted from its pooled median AND every
    head-to-head round it fought, with an error bar from both; the crown it names; the
    three corners (pooled alone, ring alone, together); what the ring changed; and how
    well each verdict predicted past nights. Read-only, and there is nothing to set: the
    pooled slack is measured from the ledger, never chosen."""
    with profile_aggregates.request_memo():
        return overall_ranking.ranking(session, backtest=backtest, read=read, budget=budget)


@router.get("/overall/ring-target")
def overall_ring_target(session: Session = Depends(get_session)) -> dict:
    """What the ring will fight next under the fused policy: the fused #1 and the profiles
    the fit cannot yet separate from it, most ambiguous first (`overall_ranking.ring_target`)."""
    return {"target": overall_ranking.ring_target(session)}


@router.get("/beliefs")
def beliefs_read(
    session: Session = Depends(get_session),
    backtest: bool = Query(True, description="Include the leave-one-session-out comparison with today's fit."),
    refresh: bool = Query(False, description="Refit now instead of reusing the last read (15 minutes)."),
) -> dict:
    """The belief layer (`beliefs`): every profile's θ fitted from the ring's margins and
    its pooled median per instrument epoch, with each epoch's offset and the quantum
    surface. Read-only. The crown reads today's fit (``fuse``); under a methodology whose
    ``pooled_read`` is ``epochs`` (v19) it reads that fit on per-era anchors, which the
    backtest scores as ``fuse_eras``."""
    from .. import beliefs

    return beliefs.beliefs(session, with_backtest=backtest, refresh=refresh)
