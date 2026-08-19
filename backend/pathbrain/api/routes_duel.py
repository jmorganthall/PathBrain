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


@router.get("/duel/standings")
def duel_standings(sessions: int = 50) -> dict:
    """The head-to-head **league table** — every profile's record earned in the ring.

    Pure ledger: decided matchups only, ranked by match points (win 3 / draw 1) with
    decisive-win rate / pair-win rate tie-breaks. Nothing pooled, nothing averaged over
    history — the view unique to the dueling-champions approach.
    """
    return duel.standings(limit_sessions=max(1, min(sessions, 200)))


@router.get("/duel/history")
def duel_history(limit: int = 10) -> dict:
    """Recent duel sessions, newest first — the head-to-head ledger."""
    return {"duels": duel.history(limit=max(1, min(limit, 50)))}
