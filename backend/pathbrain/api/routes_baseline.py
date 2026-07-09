"""Baseline (SQM off) test endpoints — the "Test baseline behavior" tab.

Two concerns:

* the **nightly schedule** + its defaults (``config.baseline_test``): armed/off, the local
  time to run, and the default iterations / settle time — read/written here so the tab has a
  dedicated surface (the same values are also visible under the general Config);
* the **on-demand run + live status** — kick a baseline test now (overriding iterations /
  settle), poll its progress, or cancel it.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import baseline_test
from ..config_store import get_config, save_config
from ..database import get_session, session_scope
from ..logging_config import get_logger
from ..schemas import BaselineScheduleUpdate, BaselineTestStart

router = APIRouter()
log = get_logger("api.baseline")


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

    # Next local (container-TZ) fire time, for the UI — informational only.
    next_run_at = None
    if enabled:
        now = datetime.now()
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate = candidate + timedelta(days=1)
        next_run_at = candidate.isoformat()

    return {
        "enabled": enabled,
        "hour": hour,
        "minute": minute,
        "iterations": iterations,
        "settle_seconds": settle,
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
