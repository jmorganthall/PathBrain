"""The exploration landscape API — what the parameter space looks like, what to try next,
and whether the last few "try next"s were any good.

Two endpoints beside the landscape, and they're the same loop closed: ``POST /explore/test``
measures a candidate *and writes down the claim it made first*; ``GET
/explore/recommendations`` grades every stored claim against what the link actually did.
Without the second one the landscape is a horoscope — it costs a night of benchmarking
either way, and nobody could say whether its numbers mean anything.

The landscape itself needs the axes, the response curves, the interactions, the gaps and
the candidates *together* (a candidate is only meaningful beside the gap it fills), and
they all come from one pass over the profile field. It costs a ``compute_profiles`` pass,
so the page fetches it on demand rather than on load — the same bargain the duel's fight
card makes. The ledger is deliberately much cheaper (two indexed queries), so the page can
show it immediately.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import explore as explore_mod
from .. import explore_tracker
from ..config_store import get_config
from ..database import get_session
from ..logging_config import get_logger
from ..methodology import ensure_current_methodology
from ..schemas import ExploreTest

router = APIRouter()
log = get_logger("api.explore")


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
    the measured surface. Every measured profile is in the model — a five-iteration reading
    is thin but it is the only reading anyone has of that point, and excluding it means a
    quick test teaches the model nothing until it crosses the confidence bar.
    ``confident_only`` (default) instead **weights** a profile by how much measurement stands
    behind it, so a lucky Overall on two runs informs a curve without carrying it; passing
    false counts every profile equally.
    """
    # The firewall's own select option lists, so every proposed target/interval is a value
    # the firewall can hold. Best-effort: the landscape must render with an unreachable
    # firewall, and no options simply means no snapping.
    allowed: dict | None = None
    try:
        from ..providers import get_provider

        provider = get_provider()
        allowed = provider.field_options() or None
        if allowed is None:
            provider.discover()
            allowed = provider.field_options() or None
    except Exception:  # noqa: BLE001
        log.debug("Explore landscape: could not read provider field options", exc_info=True)

    return explore_mod.landscape(
        session,
        suggestions=suggestions,
        confident_only=confident_only,
        allowed_values=allowed,
        reference=reference,
    )


@router.post("/explore/test")
def test_candidate(payload: ExploreTest, session: Session = Depends(get_session)) -> dict:
    """Measure one recommendation — and record the claim it made, before measuring it.

    Two questions, one path. ``iterations`` (default 5, "Test now") runs a short block:
    enough to see whether the recommendation went anywhere at all, cheap enough to try
    several in an evening. Omitting it tops the profile up to the confidence minimum — the
    long answer, for a candidate worth settling. Either way it's the same supervised
    apply → benchmark → restore session as any profile test, under the coordinator lock.

    The candidate is materialized on the **parent's** stored settings rather than on
    whatever the firewall is currently set to (see ``explore.full_overrides``), because
    "Speedy Sloth, with the download quantum nobody has tried" is only that profile if it
    starts from Speedy Sloth. When the parent's settings can't be found the levers fall
    back to the live profile and the recommendation is stamped with a ``note`` saying so —
    a caveat on the record beats a silent substitution.
    """
    from .routes_settings import _profile_settings, start_settings_test

    settings = payload.settings
    note: str | None = None
    if payload.parent_fingerprint:
        parent_settings = _profile_settings(session, payload.parent_fingerprint)
        if parent_settings:
            settings = explore_mod.full_overrides(parent_settings, payload.settings)
        else:
            note = (
                "The parent profile's stored settings were unavailable, so the levers were "
                "applied to the live profile instead — this may not be the profile that was "
                "proposed."
            )

    started = start_settings_test(session, settings, payload.label, payload.iterations)

    # Fields the firewall cannot write were reverted so the fingerprint names the profile
    # that will really be measured — which means the profile measured is not quite the one
    # proposed. That belongs on the record, not in a log line.
    dropped = started.get("warnings") or []
    if dropped:
        detail = (
            "The firewall cannot write "
            + "; ".join(dropped)
            + " — those fields were left as they are, so this measures the closest reachable "
            "profile rather than exactly what was proposed."
        )
        note = f"{note} {detail}" if note else detail

    methodology = ensure_current_methodology(session, get_config(session))
    rec_id = None
    try:
        rec_id = explore_tracker.record(
            fingerprint=started["fingerprint"],
            parent_fingerprint=payload.parent_fingerprint,
            parent_overall=payload.parent_overall,
            label=payload.label,
            summary=payload.summary,
            changes=payload.changes,
            evidence=payload.evidence,
            multi_lever=payload.multi_lever,
            predicted=payload.predicted,
            uncertainty=payload.uncertainty,
            upside=payload.upside,
            best_overall=payload.best_overall,
            methodology_version=methodology.version,
            iterations_requested=started["iterations"],
            baseline_iterations=started.get("existing_iterations", 0),
            # Fields were dropped/reverted, so the benchmark will measure the closest
            # reachable profile rather than this claim — its ledger row must say so
            # instead of being graded as a modelling miss.
            unreachable=bool(dropped),
            profile_test_id=started["id"],
            note=note,
        )
    except Exception:  # noqa: BLE001 — the benchmark is already running; losing the
        # bookkeeping is a shame, not a reason to report the test as failed.
        log.exception("Could not record the explore recommendation for test %s", started["id"])

    return {**started, "recommendation_id": rec_id, "note": note}


@router.get("/explore/recommendations")
def recommendations(
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict:
    """The recommendation ledger: every claim Explore made, graded against the measurement.

    Per row: what was predicted, what the profile actually scores now, whether that landed
    inside the stated band, and a sentence on **why** — which joins the miss to the kind of
    evidence it was priced from (a controlled matched pair, the parent's own neighbourhood,
    or a marginal curve already flagged confounded). The ``summary`` aggregates the same
    thing across the ledger and splits it by evidence class, which is the measured answer
    to "how much should I believe this page?".

    Verdicts are **derived on every read**, never stored — a re-grade or fresh runs move
    them, exactly like every other score here. A claim made under an older methodology is
    reported ``incomparable`` rather than scored against a yardstick it never claimed.
    """
    return explore_tracker.recommendations(session, limit=limit)
