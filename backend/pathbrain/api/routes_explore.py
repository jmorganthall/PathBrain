"""The exploration landscape API — what the parameter space looks like and what to try next.

One endpoint, deliberately: the page needs the axes, the response curves, the interactions,
the gaps and the candidates *together* (a candidate is only meaningful beside the gap it
fills), and they all come from one pass over the profile field. It costs a
``compute_profiles`` pass, so the page fetches it on demand rather than on load — the same
bargain the duel's fight card makes.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import explore as explore_mod
from ..database import get_session

router = APIRouter()


@router.get("/explore/landscape")
def landscape(
    suggestions: int = Query(3, ge=1, le=12),
    confident_only: bool = Query(True),
    reference: str | None = Query(None, description="Fingerprint the conditioned curves are built around (default: the best measured profile)."),
    session: Session = Depends(get_session),
) -> dict:
    """Map the shaper's parameter space and propose the next profiles worth measuring.

    Returns the levers (per pipe) with what's been tested on each, the median-Overall
    response curve per lever, the strongest lever *interactions*, the holes in coverage, and
    a ranked list of untested profiles with a predicted score and an upside — read-only, so
    nothing is applied or run. It also returns the **de-confounded** views: matched-pair
    contrasts (profiles differing in exactly one lever, so the comparison is controlled),
    curves conditioned on the neighbourhood of ``reference``, an imbalance diagnostic naming
    which curve points are measuring two levers at once, and the local maxima (basins) in
    the measured surface. ``confident_only`` (default) models only profiles that have
    reached the iteration minimum; a lucky Overall on two runs is noise, and noise in the
    model comes back out as a confident-sounding prediction.
    """
    return explore_mod.landscape(
        session,
        suggestions=suggestions,
        confident_only=confident_only,
        reference=reference,
    )
