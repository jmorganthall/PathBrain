"""Baseline (SQM off) test endpoints — the "Test baseline behavior" tab.

Two concerns:

* the **nightly schedule** + its defaults (``config.baseline_test``): armed/off, the local
  time to run, and the default iterations / settle time — read/written here so the tab has a
  dedicated surface (the same values are also visible under the general Config);
* the **on-demand run + live status** — kick a baseline test now (overriding iterations /
  settle), poll its progress, or cancel it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import baseline_test
from ..config_store import get_config, save_config
from ..database import get_session, session_scope
from ..logging_config import get_logger
from ..schemas import BaselineScheduleUpdate, BaselineTestStart

router = APIRouter()
log = get_logger("api.baseline")


def schedule_zone(bt: dict):
    """The tzinfo the schedule's hour/minute are expressed in.

    ``baseline_test.timezone`` holds the IANA zone the user saved the schedule from (the
    browser's zone, sent by the UI) — so "Run at 02:00" means the *user's* 02:00 no matter
    what TZ the container happens to run. Empty/invalid → the container's local zone (the
    legacy behavior, correct only when TZ is wired through to the container).
    """
    from zoneinfo import ZoneInfo

    tz_name = (bt.get("timezone") or "").strip()
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 — bad stored zone → fall back, never crash the tick
            log.warning("Invalid baseline_test.timezone %r; falling back to container-local", tz_name)
    return datetime.now().astimezone().tzinfo


def _schedule_payload(cfg: dict) -> dict:
    bt = cfg.get("baseline_test", {}) or {}
    enabled = bool(bt.get("enabled", False))
    try:
        hour = int(bt.get("hour", 1))
        minute = int(bt.get("minute", 0))
    except (TypeError, ValueError):
        hour, minute = 1, 0
    iterations = int(bt.get("iterations", 10) or 10)
    settle = int(bt.get("settle_seconds", 30) or 0)
    tz_name = (bt.get("timezone") or "").strip()

    # Next fire time, for the UI — informational only. The scheduler fires when the
    # schedule's OWN zone (the zone the user saved it from; container-local fallback)
    # reaches hour:minute — so compute the next occurrence in that zone, then emit a real
    # UTC **instant** (``+00:00`` suffix) consistent with ``started_at``/``finished_at``:
    # the frontend's ``parseApiDate`` sees the offset and renders it in the viewer's local
    # zone instead of mis-tagging a naive string as UTC and double-shifting it.
    next_run_at = None
    if enabled:
        zone = schedule_zone(bt)
        now_z = datetime.now(zone)
        candidate = now_z.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now_z:
            candidate = candidate + timedelta(days=1)
        next_run_at = candidate.astimezone(timezone.utc).isoformat()

    return {
        "enabled": enabled,
        "hour": hour,
        "minute": minute,
        "iterations": iterations,
        "settle_seconds": settle,
        # The IANA zone the hour/minute are interpreted in ("" = container-local fallback).
        "timezone": tz_name,
        "next_run_at": next_run_at,
    }


@router.get("/baseline/config")
def get_baseline_config(session: Session = Depends(get_session)) -> dict:
    """The nightly baseline-test schedule + defaults (and the next scheduled fire time)."""
    return _schedule_payload(get_config(session))


@router.put("/baseline/config")
def update_baseline_config(payload: BaselineScheduleUpdate) -> dict:
    """Update the nightly schedule / defaults. All fields optional; only provided ones change."""
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
    if payload.iterations is not None:
        if int(payload.iterations) <= 0:
            raise HTTPException(status_code=422, detail="iterations must be a positive whole number")
        updates["iterations"] = int(payload.iterations)
    if payload.settle_seconds is not None:
        if int(payload.settle_seconds) < 0:
            raise HTTPException(status_code=422, detail="settle_seconds cannot be negative")
        updates["settle_seconds"] = int(payload.settle_seconds)
    if payload.timezone is not None:
        tz_name = payload.timezone.strip()
        if tz_name:  # "" clears the zone back to container-local
            from zoneinfo import ZoneInfo

            try:
                ZoneInfo(tz_name)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=422, detail=f"unknown timezone {tz_name!r} (use an IANA name)"
                ) from exc
        updates["timezone"] = tz_name

    with session_scope() as session:
        cfg = save_config(session, {"baseline_test": updates}) if updates else get_config(session)
    log.info("Baseline schedule updated: %s", updates)
    return _schedule_payload(cfg)


@router.post("/baseline/test", status_code=202)
def start_baseline_test(payload: BaselineTestStart, session: Session = Depends(get_session)) -> dict:
    """Start an on-demand baseline (SQM off) test now: disable SQM on every pipe, settle,
    benchmark, then restore. Iterations / settle default to the configured values. Returns the
    session status. 409 if one is already running."""
    bt = (get_config(session).get("baseline_test", {}) or {})
    iterations = payload.iterations if payload.iterations is not None else int(bt.get("iterations", 10) or 10)
    settle = payload.settle_seconds if payload.settle_seconds is not None else int(bt.get("settle_seconds", 30) or 0)
    try:
        bt_id = baseline_test.start(iterations, settle, trigger="manual")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    log.info("Baseline test %s requested (%s iterations, %ss settle)", bt_id, iterations, settle)
    return baseline_test.current() or {"id": bt_id, "status": "pending"}


@router.get("/baseline/test")
def get_baseline_test() -> dict:
    """The most recent baseline test (for status polling), or an empty payload."""
    return baseline_test.current() or {"status": None}


@router.post("/baseline/test/cancel")
def cancel_baseline_test() -> dict:
    """Ask the running baseline test to stop after its current chunk (SQM is still restored)."""
    cancelled = baseline_test.cancel()
    return {"cancelled": cancelled, "status": (baseline_test.current() or {}).get("status")}
