"""Continuous monitoring scheduler.

When monitoring is enabled (config ``monitoring.enabled``), a background thread
runs the benchmark suite every ``monitoring.interval_minutes`` minutes. This
builds the run history over time so a stable, windowed "rolling" SOPS can be
computed (see ``/api/score/rolling``).

Design notes:
* One daemon thread ticks every ~15s and reads config live, so enabling/disabling
  or changing the interval takes effect without a restart.
* It never overlaps runs: a scheduled run is skipped if any run is already
  PENDING/RUNNING.
* ``last_run_at`` is seeded from the latest existing run on startup so a restart
  doesn't immediately fire a burst.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from .config_store import get_config
from .database import session_scope
from .logging_config import get_logger
from .models import Run, RunStatus
from .runner import create_run, execute_run

log = get_logger("scheduler")

_TICK_SECONDS = 15

_state: dict = {"last_run_at": None, "thread": None, "stop": None}


def _active_run_exists() -> bool:
    with session_scope() as session:
        return (
            session.scalar(
                select(Run.id)
                .where(Run.status.in_([RunStatus.RUNNING, RunStatus.PENDING]))
                .limit(1)
            )
            is not None
        )


def _monitoring_config() -> tuple[bool, float]:
    with session_scope() as session:
        cfg = get_config(session).get("monitoring", {}) or {}
    enabled = bool(cfg.get("enabled", False))
    interval_min = float(cfg.get("interval_minutes", 15) or 15)
    return enabled, interval_min


def _seed_last_run() -> None:
    """Seed last_run_at from the most recent run so restarts don't double-fire."""
    try:
        with session_scope() as session:
            latest = session.scalars(
                select(Run).order_by(Run.created_at.desc()).limit(1)
            ).first()
            if latest is not None:
                _state["last_run_at"] = latest.created_at.timestamp()
    except Exception:  # noqa: BLE001 — best-effort
        log.debug("Could not seed scheduler last_run_at", exc_info=True)


def _loop(stop: threading.Event) -> None:
    log.info("Scheduler thread started")
    while not stop.is_set():
        try:
            enabled, interval_min = _monitoring_config()
            interval_s = max(interval_min * 60.0, 30.0)
            last = _state["last_run_at"]
            due = enabled and (last is None or (time.time() - last) >= interval_s)
            if due and not _active_run_exists():
                run_id = create_run(label="scheduled")
                _state["last_run_at"] = time.time()
                log.info("Scheduler triggering run %s", run_id)
                execute_run(run_id)  # blocking; runs sequentially in this thread
        except Exception:  # noqa: BLE001 — never let the scheduler die
            log.exception("Scheduler tick failed")
        stop.wait(_TICK_SECONDS)
    log.info("Scheduler thread stopped")


def start_scheduler() -> None:
    if _state["thread"] and _state["thread"].is_alive():
        return
    _seed_last_run()
    stop = threading.Event()
    thread = threading.Thread(target=_loop, args=(stop,), name="pathbrain-scheduler", daemon=True)
    _state["stop"] = stop
    _state["thread"] = thread
    thread.start()


def stop_scheduler() -> None:
    if _state["stop"]:
        _state["stop"].set()


def scheduler_status() -> dict:
    enabled, interval_min = _monitoring_config()
    last = _state["last_run_at"]
    next_at = None
    if enabled:
        next_at = (last + interval_min * 60.0) if last else time.time()

    def iso(ts: float | None) -> str | None:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None

    return {
        "enabled": enabled,
        "interval_minutes": interval_min,
        "active": _active_run_exists(),
        "last_run_at": iso(last),
        "next_run_at": iso(next_at),
    }
