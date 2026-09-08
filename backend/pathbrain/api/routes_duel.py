"""Duel ladder endpoints — the head-to-head adjudication engine + its schedule.

Mirrors the baseline-test surface: a nightly schedule (armed/off, time in the schedule's
own timezone, duration) plus on-demand start / status / cancel, and the head-to-head
ledger. The duel never writes a winner to the firewall — the crowning policy
(``crown_follow.policy``) + crown follower own that.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from .. import duel
from ..config_store import get_config, save_config
from ..database import get_session, session_scope
from .. import job_queue
from ..logging_config import get_logger
from ..models import Run, RunStatus
from .. import levers as levers_mod
from ..schemas import DuelScheduleUpdate, DuelStart, LeverCampaignCreate
from ..timezones import validate_timezone
from .routes_jobs import _per_iteration_estimate, _run_entry

router = APIRouter()
log = get_logger("api.duel")


MINUTES_PER_DAY = 24 * 60


def _window_minutes(start_h: int, start_m: int, end_h: int, end_m: int) -> int:
    """Minutes from a start wall-clock time to an end one, wrapping past midnight.

    A duel window is naturally expressed as "from 3:00 until 5:30", not as "150 minutes",
    so the API takes the end time and derives the duration the engine actually runs on.
    An end equal to the start is rejected by the caller (a zero-length window).
    """
    return ((end_h * 60 + end_m) - (start_h * 60 + start_m)) % MINUTES_PER_DAY


def _end_clock(start_h: int, start_m: int, duration_minutes: int) -> tuple[int, int]:
    """The wall-clock time a window starting at start_h:start_m ends (mod 24h)."""
    end = (start_h * 60 + start_m + max(0, duration_minutes)) % MINUTES_PER_DAY
    return end // 60, end % 60


def _schedule_payload(cfg: dict) -> dict:
    d = cfg.get("duel", {}) or {}
    method = str(d.get("method", "margins") or "margins").lower()
    if method not in ("margins", "pair_wins"):
        method = "margins"
    hour = int(d.get("hour", 3) or 0)
    minute = int(d.get("minute", 0) or 0)
    duration = int(d.get("duration_minutes", 120) or 120)
    end_hour, end_minute = _end_clock(hour, minute, duration)
    return {
        "enabled": bool(d.get("enabled", False)),
        "hour": hour,
        "minute": minute,
        # The end of the window, derived from start + duration so the UI can show (and
        # edit) the schedule as a start/end pair. `duration_minutes` stays canonical —
        # it's what the engine counts down, and it survives a window over 24h.
        "end_hour": end_hour,
        "end_minute": end_minute,
        "timezone": (d.get("timezone") or "").strip(),
        "duration_minutes": duration,
        "min_pairs": int(d.get("min_pairs", 10) or 10),
        "max_pairs": int(d.get("max_pairs", 40) or 40),
        "min_margin": float(d.get("min_margin", 1.0) or 0.0),
        # Hours, and a separate field from the champion-freshness window they used to share.
        "rematch_hours": duel.rematch_hours(d),
        "champion_freshness_days": duel.champion_freshness_days(d),
        "rank_sigma": duel.rank_sigma(d),
        "tie_sigma": duel.tie_sigma(d),
        "rating_prior_pairs": duel.rating_prior(d),
        "iterations_per_round": duel.iterations_per_round(d),
        # The ring's shape — see `duel._run_ring`.
        "belt_every": duel.belt_every(d),
        "seats": duel.seats(d),
        "browser_only": duel.browser_only(d),
        # Post-apply settle: each leg writes the profile to the firewall and reconfigures
        # the queues before it measures anything, so this is how long to let the link
        # settle first. Symmetric across both sides — it never biased a verdict, it just
        # put reconfiguration noise into every pair.
        "settle_seconds": int(d.get("settle_seconds", 3) or 0),
        # The evidence bar itself: how big an edge to look for (p1) and how often we're
        # willing to call a coin-flip a winner (alpha).
        "p1": float(d.get("p1", 0.70) or 0.70),
        "streak_wins": int(d.get("streak_wins", 0) or 0),
        "continuous": bool(d.get("continuous", False)),
        "continuous_gap_minutes": float(d.get("continuous_gap_minutes", 5) or 0),
        # Which rule names the champion. The STANDINGS always rank on the proven rating
        # floor; this only decides who wears the belt.
        "crown_rule": duel.crown_rule(d),
        "crown_rules": list(duel.CROWN_RULES),
        "contenders": str(d.get("contenders", "ring") or "ring"),
        "contender_modes": ["ring", "leaders", "heirs"],
        "contender_top_n": int(d.get("contender_top_n", 8) or 8),
        "alpha": float(d.get("alpha", 0.05) or 0.05),
        # How a bout is judged: "margins" (default — Wilcoxon signed-rank on the paired
        # Overall differences, which uses HOW MUCH each pair was won by) or "pair_wins"
        # (the legacy sign test, which only counts who won each pair).
        "method": method,
        "methods": ["margins", "pair_wins"],
        # The one dial that answers "how sure before calling a winner" — the statistical
        # fields are derived from it. Hand-editing them reads back as "custom".
        "preset": duel.preset_for(d),
        "presets": [
            {"key": key, **{k: v for k, v in preset.items()}} for key, preset in duel.PRESETS.items()
        ],
        # What the active rule actually demands of a bout — surfaced because the pair-win
        # rule's cap can make a verdict unreachable, which is otherwise invisible.
        "decision": (
            duel.paired_requirements(
                d.get("alpha", 0.05),
                int(d.get("min_pairs", 10) or 10),
                int(d.get("max_pairs", 40) or 40),
                int(d.get("streak_wins", 0) or 0),
            )
            if method == "margins"
            else duel.sprt_requirements(
                d.get("p1", 0.70),
                d.get("alpha", 0.05),
                int(d.get("min_pairs", 10) or 10),
                int(d.get("max_pairs", 40) or 40),
            )
        ),
    }


@router.get("/duel/config")
def get_duel_config(session: Session = Depends(get_session)) -> dict:
    """The nightly duel schedule + stopping-rule parameters."""
    return _schedule_payload(get_config(session))


@router.put("/duel/config")
def update_duel_config(payload: DuelScheduleUpdate) -> dict:
    """Update the duel schedule / stopping rule. All fields optional."""
    updates: dict = {}
    if payload.enabled is not None:
        updates["enabled"] = bool(payload.enabled)
    if payload.hour is not None:
        if not 0 <= int(payload.hour) <= 23:
            raise HTTPException(status_code=422, detail="hour must be between 0 and 23")
        updates["hour"] = int(payload.hour)
    if payload.minute is not None:
        if not 0 <= int(payload.minute) <= 59:
            raise HTTPException(status_code=422, detail="minute must be between 0 and 59")
        updates["minute"] = int(payload.minute)
    if payload.duration_minutes is not None:
        if int(payload.duration_minutes) <= 0:
            raise HTTPException(status_code=422, detail="duration_minutes must be positive")
        updates["duration_minutes"] = int(payload.duration_minutes)
    if payload.timezone is not None:
        try:  # "" clears the zone back to container-local
            updates["timezone"] = validate_timezone(payload.timezone)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # End time → duration. Sent alongside hour/minute by the UI (which edits the window as
    # a start/end pair), so the duration is always derived from the times being saved, not
    # from a stale stored start.
    if payload.end_hour is not None or payload.end_minute is not None:
        with session_scope() as session:
            current = _schedule_payload(get_config(session))
        end_h = int(payload.end_hour) if payload.end_hour is not None else current["end_hour"]
        end_m = int(payload.end_minute) if payload.end_minute is not None else current["end_minute"]
        if not 0 <= end_h <= 23:
            raise HTTPException(status_code=422, detail="end_hour must be between 0 and 23")
        if not 0 <= end_m <= 59:
            raise HTTPException(status_code=422, detail="end_minute must be between 0 and 59")
        start_h = updates.get("hour", current["hour"])
        start_m = updates.get("minute", current["minute"])
        minutes = _window_minutes(start_h, start_m, end_h, end_m)
        if minutes <= 0:
            raise HTTPException(
                status_code=422, detail="the end time must differ from the start time"
            )
        updates["duration_minutes"] = minutes
    if payload.rematch_hours is not None:
        if float(payload.rematch_hours) < 0:
            raise HTTPException(status_code=422, detail="rematch_hours cannot be negative")
        updates["rematch_hours"] = float(payload.rematch_hours)
    if payload.rank_sigma is not None:
        if not 0 <= float(payload.rank_sigma) <= 3:
            raise HTTPException(status_code=422, detail="rank_sigma must be between 0 and 3")
        updates["rank_sigma"] = float(payload.rank_sigma)
    if payload.tie_sigma is not None:
        if not 0 <= float(payload.tie_sigma) <= 5:
            raise HTTPException(status_code=422, detail="tie_sigma must be between 0 and 5")
        updates["tie_sigma"] = float(payload.tie_sigma)
    if payload.iterations_per_round is not None:
        if not 1 <= int(payload.iterations_per_round) <= 25:
            raise HTTPException(
                status_code=422, detail="iterations_per_round must be between 1 and 25"
            )
        updates["iterations_per_round"] = int(payload.iterations_per_round)
    if payload.belt_every is not None:
        if not 2 <= int(payload.belt_every) <= 6:
            raise HTTPException(status_code=422, detail="belt_every must be between 2 and 6")
        updates["belt_every"] = int(payload.belt_every)
    if payload.seats is not None:
        if not 1 <= int(payload.seats) <= 6:
            raise HTTPException(status_code=422, detail="seats must be between 1 and 6")
        updates["seats"] = int(payload.seats)
    if payload.browser_only is not None:
        updates["browser_only"] = bool(payload.browser_only)
    if payload.rating_prior_pairs is not None:
        if float(payload.rating_prior_pairs) <= 0:
            raise HTTPException(
                status_code=422, detail="rating_prior_pairs must be positive"
            )
        updates["rating_prior_pairs"] = float(payload.rating_prior_pairs)
    if payload.champion_freshness_days is not None:
        if float(payload.champion_freshness_days) <= 0:
            raise HTTPException(
                status_code=422, detail="champion_freshness_days must be positive"
            )
        updates["champion_freshness_days"] = float(payload.champion_freshness_days)
    if payload.settle_seconds is not None:
        if not 0 <= int(payload.settle_seconds) <= 120:
            raise HTTPException(
                status_code=422, detail="settle_seconds must be between 0 and 120"
            )
        updates["settle_seconds"] = int(payload.settle_seconds)

    # A preset writes the statistical fields; explicit fields below still win, so a PUT
    # carrying both applies the preset and then the override.
    if payload.preset is not None:
        try:
            updates.update(duel.preset_config(payload.preset))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.streak_wins is not None:
        if int(payload.streak_wins) < 0:
            raise HTTPException(status_code=422, detail="streak_wins cannot be negative")
        if 0 < int(payload.streak_wins) < 2:
            raise HTTPException(
                status_code=422, detail="a streak rule needs at least 2 wins in a row"
            )
        updates["streak_wins"] = int(payload.streak_wins)
    if payload.continuous is not None:
        updates["continuous"] = bool(payload.continuous)
    if payload.continuous_gap_minutes is not None:
        if float(payload.continuous_gap_minutes) < 0:
            raise HTTPException(status_code=422, detail="the gap cannot be negative")
        updates["continuous_gap_minutes"] = float(payload.continuous_gap_minutes)
    if payload.crown_rule is not None:
        if payload.crown_rule not in duel.CROWN_RULES:
            raise HTTPException(
                status_code=422,
                detail=f"crown_rule must be one of {', '.join(duel.CROWN_RULES)}",
            )
        updates["crown_rule"] = payload.crown_rule
    if payload.contenders is not None:
        if payload.contenders == "levers":
            raise HTTPException(
                status_code=422,
                detail="A lever session is started from the Levers page for one session at a "
                       "time; it is not a standing matchmaking mode.",
            )
        if payload.contenders not in ("ring", "leaders", "heirs"):
            raise HTTPException(
                status_code=422, detail="contenders must be 'ring', 'leaders' or 'heirs'"
            )
        updates["contenders"] = payload.contenders
    if payload.contender_top_n is not None:
        if int(payload.contender_top_n) < 1:
            raise HTTPException(status_code=422, detail="contender_top_n must be at least 1")
        updates["contender_top_n"] = int(payload.contender_top_n)
    if payload.method is not None:
        if payload.method not in ("margins", "pair_wins"):
            raise HTTPException(
                status_code=422, detail="method must be 'margins' or 'pair_wins'"
            )
        updates["method"] = payload.method
    if payload.p1 is not None:
        if not 0.5 < float(payload.p1) < 1.0:
            raise HTTPException(status_code=422, detail="p1 must be between 0.5 and 1.0")
        updates["p1"] = float(payload.p1)
    if payload.alpha is not None:
        if not 0.0 < float(payload.alpha) < 0.5:
            raise HTTPException(status_code=422, detail="alpha must be between 0 and 0.5")
        updates["alpha"] = float(payload.alpha)
    if payload.min_margin is not None:
        if float(payload.min_margin) < 0:
            raise HTTPException(status_code=422, detail="min_margin cannot be negative")
        updates["min_margin"] = float(payload.min_margin)

    # The pair bounds are validated against each other on the *merged* config, so changing
    # one at a time can never leave min_pairs > max_pairs (a matchup that can never decide).
    if payload.min_pairs is not None or payload.max_pairs is not None:
        with session_scope() as session:
            current = _schedule_payload(get_config(session))
        lo = int(payload.min_pairs) if payload.min_pairs is not None else current["min_pairs"]
        hi = int(payload.max_pairs) if payload.max_pairs is not None else current["max_pairs"]
        if lo < 2:
            raise HTTPException(status_code=422, detail="min_pairs must be at least 2")
        if hi < lo:
            raise HTTPException(status_code=422, detail="max_pairs cannot be below min_pairs")
        if payload.min_pairs is not None:
            updates["min_pairs"] = lo
        if payload.max_pairs is not None:
            updates["max_pairs"] = hi

    with session_scope() as session:
        cfg = save_config(session, {"duel": updates}) if updates else get_config(session)
    log.info("Duel schedule updated: %s", updates)
    return _schedule_payload(cfg)


@router.post("/duel/start", status_code=202)
def start_duel(payload: DuelStart) -> dict:
    """Start a duel session now, or queue it behind whatever holds the pipeline.

    ``contenders`` picks this session's kind: a lever session (``"levers"``, from the
    Levers page) or the ladder's configured matchmaking (omitted)."""
    if payload.contenders is not None and payload.contenders not in duel.SESSION_MODES:
        raise HTTPException(
            status_code=422, detail=f"contenders must be one of {', '.join(duel.SESSION_MODES)}"
        )
    if (payload.campaign_id is not None or payload.base_fingerprint) and payload.contenders != "levers":
        raise HTTPException(status_code=422, detail="campaign_id / base_fingerprint apply to a lever session only")
    label = "Lever duel session" if payload.contenders == "levers" else "Duel ladder session"
    spec = {"duration_minutes": payload.duration_minutes, "trigger": "manual"}
    if payload.contenders:
        spec["contenders"] = payload.contenders
    if payload.campaign_id is not None:
        spec["campaign_id"] = int(payload.campaign_id)
    if payload.base_fingerprint:
        spec["base_fingerprint"] = payload.base_fingerprint
    try:
        submission = job_queue.submit("duel", label, spec=spec)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not submission.started:
        return {"id": None, "status": "queued", **submission.placement()}
    log.info("Duel %s requested", submission.result)
    return {
        **(duel.current() or {"id": submission.result, "status": "pending"}),
        **submission.placement(),
    }


def _leg_progress(session: Session, live: dict | None) -> None:
    """Attach the in-flight leg's run progress to the live payload, in place.

    The ring publishes WHICH profile it is measuring (`live["leg"]`, with the run id once
    the run exists); how far that run has got is read here, at poll time, off the run row
    — the same adapter the jobs feed uses for a chunk (`_run_entry`), so the per-profile
    bar on the Duels page and the chunk line in the jobs dropdown are one estimate: same
    counter, same measured per-iteration cost, same countdown. Best-effort: a leg whose
    run can't be read simply carries no progress."""
    leg = (live or {}).get("leg") if isinstance(live, dict) else None
    if not leg:
        return
    leg["run"] = None
    run_id = leg.get("run_id")
    if not run_id:
        return
    try:
        run = session.get(Run, int(run_id))
    except Exception:  # noqa: BLE001 — status must never fail on a progress read
        log.debug("Duel status: could not read leg run %r", run_id, exc_info=True)
        return
    if run is None:
        return
    if run.status in (RunStatus.RUNNING, RunStatus.PENDING):
        leg["run"] = _run_entry(run, _per_iteration_estimate(session), holder=None, parent_id=None)
    else:
        # The run has landed; the ring is scoring it and choosing the next leg.
        leg["run"] = {
            "id": f"run-{run.id}",
            "status": "finished" if run.status == RunStatus.COMPLETE else "failed",
            "current": int(run.iterations_completed or 0),
            "total": int(run.iterations or 1),
        }


@router.get("/duel/status")
def duel_status(session: Session = Depends(get_session)) -> dict:
    """The most recent duel session (for status polling), or an empty payload.

    A running session's ``live.leg`` is enriched with its run's progress (see
    ``_leg_progress``) so the page can show which profile is being measured and how far
    along that measurement is."""
    payload = duel.current() or {"status": None}
    if payload.get("status") in ("running", "pending"):
        _leg_progress(session, payload.get("live"))
    return payload


@router.post("/duel/cancel")
def cancel_duel() -> dict:
    """Ask the running duel to stop after its current pair (baseline still restored)."""
    cancelled = duel.cancel()
    return {"cancelled": cancelled, "status": (duel.current() or {}).get("status")}


@router.get("/duel/card")
def duel_card(
    limit: int = 12,
    contenders: str | None = Query(None, description="Preview a session of this kind (e.g. 'levers') instead of the configured one."),
    base: str | None = Query(None, description="Lever preview only: pin the defender to this campaign base."),
    session: Session = Depends(get_session),
) -> dict:
    """Who would fight whom if a duel started right now, in order.

    On demand rather than on page load: it costs a full profile-ranking pass.
    """
    if contenders is not None and contenders not in duel.SESSION_MODES:
        raise HTTPException(
            status_code=422, detail=f"contenders must be one of {', '.join(duel.SESSION_MODES)}"
        )
    return duel.fight_card(session, limit=max(1, min(limit, 50)), contenders=contenders,
                           base_fingerprint=base if contenders == "levers" else None)


# ── Lever campaigns: one base, measured until its levers are settled ──────────────


@router.get("/levers/campaigns")
def lever_campaigns(session: Session = Depends(get_session)) -> dict:
    """Every campaign, newest activity first, with the open ones flagged."""
    rows = levers_mod.list_campaigns(session)
    return {
        "campaigns": [levers_mod.serialize_campaign(c) for c in rows],
        "open_ids": [c.id for c in rows if c.status == "open"],
    }


@router.post("/levers/campaigns", status_code=201)
def lever_campaign_create(payload: LeverCampaignCreate) -> dict:
    """Open a campaign on a base profile — or return the open one already on it."""
    with session_scope() as session:
        try:
            row = levers_mod.resolve_campaign(session, None, payload.base_fingerprint)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return levers_mod.serialize_campaign(row)


@router.get("/levers/campaigns/{campaign_id}")
def lever_campaign_status(campaign_id: int, session: Session = Depends(get_session)) -> dict:
    """What the campaign has settled at its base, what is open, what is untested."""
    from ..models import LeverCampaign

    row = session.get(LeverCampaign, campaign_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No lever campaign #{campaign_id}")
    return levers_mod.campaign_status(session, row)


@router.post("/levers/campaigns/{campaign_id}/close")
def lever_campaign_close(campaign_id: int) -> dict:
    """Close a campaign (its record stays; a new one can be opened on the same base)."""
    with session_scope() as session:
        try:
            row = levers_mod.close_campaign(session, campaign_id, "closed from the Levers page")
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return levers_mod.serialize_campaign(row)


@router.get("/duel/standings")
def duel_standings(sessions: int = 50) -> dict:
    """The head-to-head **league table** — every profile's record earned in the ring.

    Pure ledger: decided matchups only, ranked by match points (win 3 / draw 1) with
    decisive-win rate / pair-win rate tie-breaks. Nothing pooled, nothing averaged over
    history — the view unique to the dueling-champions approach.
    """
    return duel.standings(limit_sessions=max(1, min(sessions, 200)))


@router.get("/duel/profile/{fingerprint}")
def duel_profile(fingerprint: str, sessions: int = 50) -> dict:
    """One profile's head-to-head record — its standings row, opponents and bout tape.

    The per-profile slice of the ladder, for the Profile Detail page: the ring's verdict on
    a profile, beside the pooled measurements that page already shows. Signed throughout
    from that profile's own side, and ranked by the same fit the league table uses.
    """
    return duel.profile_ledger(fingerprint, limit_sessions=max(1, min(sessions, 200)))


@router.get("/duel/health")
def duel_health(sessions: int = 50) -> dict:
    """Is the ladder measuring anything? Aborted matches, discarded rounds, and why."""
    return duel.round_health(limit_sessions=max(1, min(sessions, 200)))


@router.get("/duel/weather-distance")
def duel_weather_distance(sessions: int = 10, legs: int = 400) -> dict:
    """How much the measured weather shifts between legs 1–4 apart, from recent duel
    sessions' own runs — the number that prices raising `belt_every`. On demand: it stamps
    every leg against the severity yardstick."""
    return duel.weather_by_distance(
        limit_sessions=max(1, min(sessions, 50)), max_legs=max(20, min(legs, 2000))
    )


@router.get("/duel/history")
def duel_history(limit: int = 10, matchups: int = 25) -> dict:
    """Recent duel sessions, newest first — the head-to-head ledger.

    ``matchups`` caps how many of each session's matches come back (the most recent ones);
    each session reports its true ``matchups_total``. Without a cap a continuous ladder's
    twenty most recent sessions is thousands of matches, which is a payload the page
    cannot load rather than a list nobody reads.
    """
    return {
        "duels": duel.history(
            limit=max(1, min(limit, 50)), matchup_limit=max(1, min(matchups, 500))
        )
    }
