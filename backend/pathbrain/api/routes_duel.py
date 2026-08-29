"""Duel ladder endpoints — the head-to-head adjudication engine + its schedule.

Mirrors the baseline-test surface: a nightly schedule (armed/off, time in the schedule's
own timezone, duration) plus on-demand start / status / cancel, and the head-to-head
ledger. The duel never writes a winner to the firewall — the crowning policy
(``crown_follow.policy``) + crown follower own that.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import duel
from ..config_store import get_config, save_config
from ..database import get_session, session_scope
from ..logging_config import get_logger
from ..schemas import DuelScheduleUpdate, DuelStart
from ..timezones import validate_timezone

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
        "rematch_days": int(d.get("rematch_days", 7) or 7),
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
    if payload.rematch_days is not None:
        if int(payload.rematch_days) < 0:
            raise HTTPException(status_code=422, detail="rematch_days cannot be negative")
        updates["rematch_days"] = int(payload.rematch_days)
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
    """Start a duel-ladder session now. 409 if one is already running."""
    try:
        duel_id = duel.start(payload.duration_minutes, trigger="manual")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    log.info("Duel %s requested", duel_id)
    return duel.current() or {"id": duel_id, "status": "pending"}


@router.get("/duel/status")
def duel_status() -> dict:
    """The most recent duel session (for status polling), or an empty payload."""
    return duel.current() or {"status": None}


@router.post("/duel/cancel")
def cancel_duel() -> dict:
    """Ask the running duel to stop after its current pair (baseline still restored)."""
    cancelled = duel.cancel()
    return {"cancelled": cancelled, "status": (duel.current() or {}).get("status")}


@router.get("/duel/card")
def duel_card(limit: int = 12, session: Session = Depends(get_session)) -> dict:
    """Who would fight whom if a duel started right now, in order.

    On demand rather than on page load: it costs a full profile-ranking pass.
    """
    return duel.fight_card(session, limit=max(1, min(limit, 50)))


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
