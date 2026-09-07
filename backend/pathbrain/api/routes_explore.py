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

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import explore as explore_mod
from .. import explore_tracker
from ..config_store import get_config
from ..database import get_session
from ..logging_config import get_logger
from ..methodology import ensure_current_methodology
from ..schemas import ExploreBatchTest, ExploreTest

router = APIRouter()
log = get_logger("api.explore")

#: How many candidates a batch generates before picking its top N. Generating more than
#: we take is the point — re-ranking five candidates and taking five ranks nothing — and
#: the cost is the same ``compute_profiles`` pass either way.
BATCH_CANDIDATE_POOL = 12


def _allowed_values() -> dict | None:
    """The firewall's own select option lists, so every proposed value is one it can hold.

    Best-effort: the landscape must render against an unreachable firewall, and no options
    simply means no snapping.
    """
    try:
        from ..providers import get_provider

        provider = get_provider()
        allowed = provider.field_options() or None
        if allowed is None:
            provider.discover()
            allowed = provider.field_options() or None
        return allowed
    except Exception:  # noqa: BLE001
        log.debug("Explore: could not read provider field options", exc_info=True)
        return None


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
    return explore_mod.landscape(
        session,
        suggestions=suggestions,
        confident_only=confident_only,
        allowed_values=_allowed_values(),
        reference=reference,
    )


def _start_candidate(session: Session, payload: ExploreTest) -> dict:
    """Measure one candidate and write its claim down first. The one path both buttons use.

    Factored out so "Test now" on a single row and "run the top N bets" cannot drift into
    two different notions of what materializing a candidate means — which is exactly how
    the ledger ends up grading claims against profiles nobody proposed.
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
    return _start_candidate(session, payload)


@router.post("/explore/test-batch")
def test_batch(payload: ExploreBatchTest, session: Session = Depends(get_session)) -> dict:
    """Queue the top **N** recommendations at **M** iterations each — "run the smartest bets".

    Explore routinely produces ten proposals worth measuring and, until now, exactly one
    button per row to measure them with: a person had to press, wait, come back and press
    again. Since profile tests queue, a batch is simply N presses the server makes on your
    behalf, and the pipeline drains them one at a time in order.

    Which N is the whole question, and it is not the page's default order. The landscape
    ranks candidates by an **upper** confidence bound because exploring should be drawn to
    what we don't know; deciding what to spend a night running is the opposite question, so
    ``rank="confidence"`` (the default) scores each candidate at the **pessimistic** end of
    a band widened by what its evidence class has actually missed by in the recommendation
    ledger — the measured track record of which bets win, on this link. ``rank="upside"``
    keeps the page's exploring order for callers that want it.

    One bad candidate is skipped with its reason, never fatal to the batch — the same
    discipline ``refresh`` applies to a bad profile. Nothing is applied to the firewall
    here: each test snapshots, applies, benchmarks and restores when its own turn comes.
    """
    landscape = explore_mod.landscape(
        session,
        suggestions=BATCH_CANDIDATE_POOL,
        confident_only=payload.confident_only,
        allowed_values=_allowed_values(),
    )
    pool = landscape.get("bets" if payload.rank == "confidence" else "candidates") or []
    if not pool:
        raise HTTPException(
            status_code=400,
            detail=(
                landscape.get("reason")
                or "No candidates to test — the landscape has nothing to propose right now."
            ),
        )

    best_overall = landscape.get("best_overall")
    queued: list[dict] = []
    skipped: list[dict] = []
    for candidate in pool[: payload.count]:
        label = ", ".join(
            f"{ch['pipe']} {ch['field_label']} {ch['to']}" for ch in candidate.get("changes") or []
        )
        one = ExploreTest(
            settings=candidate.get("settings"),
            label=f"Explore: {label}",
            iterations=payload.iterations,
            parent_fingerprint=(candidate.get("parent") or {}).get("fingerprint"),
            parent_overall=(candidate.get("parent") or {}).get("overall"),
            changes=candidate.get("changes"),
            evidence=candidate.get("evidence"),
            multi_lever=bool(candidate.get("multi_lever")),
            predicted=candidate.get("predicted"),
            uncertainty=candidate.get("uncertainty"),
            upside=candidate.get("upside"),
            best_overall=best_overall,
            summary=candidate.get("summary"),
        )
        try:
            started = _start_candidate(session, one)
        except HTTPException as exc:
            # A no-op or an unreachable change. Worth reporting per row — "3 of 5 queued,
            # and here is why the other two weren't" is an answer; a failed batch is not.
            skipped.append({"label": label, "reason": exc.detail})
            continue
        except Exception as exc:  # noqa: BLE001
            log.exception("Batch test: could not queue a candidate")
            skipped.append({"label": label, "reason": f"{type(exc).__name__}: {exc}"})
            continue
        queued.append({
            **started,
            "summary": candidate.get("summary"),
            "predicted": candidate.get("predicted"),
            "uncertainty": candidate.get("uncertainty"),
            "confidence_score": candidate.get("confidence_score"),
            "confidence": candidate.get("confidence"),
            "clears_bar": candidate.get("clears_bar"),
        })

    log.info(
        "Batch test: queued %s of %s candidate(s) at %s iteration(s), ranked by %s",
        len(queued), min(payload.count, len(pool)), payload.iterations, payload.rank,
    )
    return {
        "queued": queued,
        "skipped": skipped,
        "requested": payload.count,
        "iterations": payload.iterations,
        "rank": payload.rank,
        "best_overall": best_overall,
        # What the ranking rested on, so an uncalibrated batch never reads like a
        # track-record-backed one.
        "calibration": landscape.get("calibration") or {},
    }


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
