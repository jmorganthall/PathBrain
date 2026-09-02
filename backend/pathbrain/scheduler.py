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

import os
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select

from .config_store import get_config
from .database import session_scope
from .logging_config import get_logger
from .models import Run, RunStatus
from .runner import create_run, execute_run, fail_stale_runs

log = get_logger("scheduler")

_TICK_SECONDS = 15

_state: dict = {"last_run_at": None, "thread": None, "stop": None, "baseline_last_date": None}

#: Held for the lifetime of the process by whichever process won leadership. Kept in module
#: state rather than closed, because releasing the fd releases the lock.
_leader_fd = None
_leader: bool | None = None


def _lock_path() -> str:
    """Where the leadership lock lives.

    ``/tmp`` deliberately, not the data volume: the scope that needs coordinating is
    *processes sharing this container* (uvicorn workers), and a lock on a shared/NFS data
    volume would additionally — and wrongly — make two separate deployments fight over one
    leadership. Keyed on the database URL so two PathBrains in one container (dev + test)
    do not silence each other.
    """
    import hashlib
    import tempfile

    from .config import get_settings

    key = hashlib.blake2b(get_settings().database_url.encode(), digest_size=8).hexdigest()
    return os.path.join(tempfile.gettempdir(), f"pathbrain-scheduler-{key}.lock")


def is_leader() -> bool:
    """True in the one process allowed to run the scheduler.

    Everything the scheduler drives — monitoring runs, the duel ladder, the nightly
    baseline test, the crown follower's firewall writes — assumes it is the only one
    doing it. The coordinator lock enforces that *within* a process; across processes it
    enforces nothing, because it is a ``threading.Lock``. So running uvicorn with
    ``--workers 2`` would give two schedulers racing to apply profiles and benchmark
    them, each measuring through the other's firewall writes. This container's CMD starts
    a single worker, which is why that has never happened — but it is a one-flag mistake
    away, and a duplicated scheduler is invisible except as data that quietly stops
    meaning anything.

    An advisory ``flock`` settles it: the first process in wins and holds the lock for as
    long as it lives; the kernel releases it if that process dies, so a crash promotes a
    survivor with no cleanup and no stale-lock file to reason about. Platforms without
    ``fcntl`` (Windows dev boxes) assume leadership, since the multi-worker deployment
    this guards is the container.
    """
    global _leader_fd, _leader
    if _leader is not None:
        return _leader
    try:
        import fcntl
    except ImportError:  # pragma: no cover — non-POSIX dev box
        _leader = True
        return _leader
    path = _lock_path()
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        _leader = False
        log.warning(
            "Another PathBrain process holds the scheduler lock (%s: %s) — this process "
            "will serve the API but run no scheduled work. This is expected with multiple "
            "uvicorn workers and is what stops two schedulers benchmarking through each "
            "other's firewall writes.",
            path, exc,
        )
        return _leader
    _leader_fd = fd
    _leader = True
    return _leader


def _reset_leadership_for_tests() -> None:
    global _leader_fd, _leader
    if _leader_fd is not None:
        try:
            os.close(_leader_fd)
        except OSError:
            pass
    _leader_fd, _leader = None, None


def _baseline_config() -> dict:
    with session_scope() as session:
        return get_config(session).get("baseline_test", {}) or {}


def _maybe_run_duel() -> bool:
    """Kick the duel ladder — nightly at its scheduled minute, or perpetually in
    ``continuous`` mode.

    Continuous mode is the "ongoing race" reading of a duel: rather than one window a
    night, the ladder keeps running so the standings keep accruing evidence and a better
    profile can surface at any hour. Each session still restores the pre-duel baseline and
    still runs under the coordinator lock, so other work is deferred rather than trampled,
    and ``continuous_gap_minutes`` leaves the pipeline free between sessions for monitoring
    and manual runs.
    """
    from . import coordinator, duel
    from .timezones import schedule_zone

    if duel.active():
        return False
    with session_scope() as session:
        cfg = get_config(session).get("duel", {}) or {}
    if not cfg.get("enabled"):
        return False

    if cfg.get("continuous"):
        if coordinator.busy():  # someone else is using the pipeline; try again next tick
            return False
        gap = max(float(cfg.get("continuous_gap_minutes", 5) or 0), 0.0) * 60.0
        last = _state.get("duel_last_finished") or 0.0
        if last and (time.monotonic() - last) < gap:
            return False
        try:
            duel.start(int(cfg.get("duration_minutes", 120) or 120), trigger="continuous")
            log.info("Scheduler kicked a continuous duel session")
            return True
        except Exception:  # noqa: BLE001 — never let a scheduling hiccup kill the loop
            log.exception("Scheduler: could not start continuous duel")
            _state["duel_last_finished"] = time.monotonic()  # back off before retrying
            return False

    now = datetime.now(schedule_zone(cfg))
    today = now.date().isoformat()
    if _state.get("duel_last_date") == today:
        return False
    try:
        hour = int(cfg.get("hour", 3))
        minute = int(cfg.get("minute", 0))
    except (TypeError, ValueError):
        return False
    if now.hour != hour or now.minute != minute:
        return False
    try:
        duel.start(int(cfg.get("duration_minutes", 120) or 120), trigger="scheduled")
        _state["duel_last_date"] = today
        log.info("Scheduler kicked nightly duel ladder")
        return True
    except Exception:  # noqa: BLE001 — never let a scheduling hiccup kill the loop
        log.exception("Scheduler: could not start nightly duel")
        _state["duel_last_date"] = today
        return False


def _maybe_run_baseline() -> bool:
    """Kick the nightly baseline (SQM off) test if it's armed and due this scheduled minute.

    Returns True if a baseline test was started this tick (so the caller can yield the tick).
    The engine holds the coordination lock itself and queues behind any in-progress session;
    we only guard against double-firing within the same day and while one is already active.
    The hour/minute are evaluated in the **schedule's own zone** (``baseline_test.timezone``,
    the browser zone captured when the user saved it) — so "run at 02:00" means the user's
    02:00, not the container's. Container-local fallback when no zone is stored (the old
    behavior, which fired at UTC wall-clock unless TZ was wired into the container — the
    "why did my baseline run at 8pm?" bug).
    """
    from . import baseline_test
    from .timezones import schedule_zone

    if baseline_test.active():
        return False
    cfg = _baseline_config()
    if not cfg.get("enabled"):
        return False
    now = datetime.now(schedule_zone(cfg))  # wall-clock in the schedule's own zone
    today = now.date().isoformat()
    if _state.get("baseline_last_date") == today:
        return False  # already fired today
    try:
        hour = int(cfg.get("hour", 1))
        minute = int(cfg.get("minute", 0))
    except (TypeError, ValueError):
        return False
    if now.hour != hour or now.minute != minute:
        return False
    try:
        iterations = max(1, int(cfg.get("iterations", 10) or 10))
        settle = max(0, int(cfg.get("settle_seconds", 30) or 0))
        baseline_test.start(iterations, settle, trigger="scheduled")
        _state["baseline_last_date"] = today
        log.info("Scheduler kicked nightly baseline (SQM off) test")
        return True
    except Exception:  # noqa: BLE001 — never let a scheduling hiccup kill the loop
        log.exception("Scheduler: could not start nightly baseline test")
        # Stamp the date anyway so we don't retry every tick this minute.
        _state["baseline_last_date"] = today
        return False


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
            # Watchdog: fail hung/orphaned runs before doing anything else.
            with session_scope() as session:
                timeout_min = float(
                    (get_config(session).get("monitoring", {}) or {}).get("run_timeout_minutes", 30) or 30
                )
            fail_stale_runs(timeout_min)

            # …and the other half of that watchdog. Failing the *row* of a hung run was
            # only ever bookkeeping: the thread that hung is still holding the pipeline,
            # so every session queued behind it waits on a thread that is never coming
            # back (observed: a duel wedged at 23:00 still owned the lock at 07:30, with
            # this watchdog marking its run failed every 15 seconds and nothing changing).
            # Evicting a holder that has stopped showing progress is what turns the
            # observation into a recovery — the wedged thread is disowned rather than
            # killed, which Python cannot do (see ``coordinator``).
            from . import coordinator as _coordinator

            _coordinator.evict_if_stalled()

            # Another firewall/benchmark session (sweep, profile test, manual run)
            # owns the pipeline while it holds the coordination lock. Yield so
            # monitoring and experiments never overlap its measurements.
            from . import coordinator

            if coordinator.busy():
                stop.wait(_TICK_SECONDS)
                continue

            # Experiments take priority; if one did work this tick, skip monitoring
            # so the two never overlap or pollute each other's measurements.
            from . import experiment

            if experiment.step():
                stop.wait(_TICK_SECONDS)
                continue

            # Nightly baseline (SQM off) test, if armed + due. Its engine holds the
            # coordination lock and queues, so yield the tick once it's kicked.
            if _maybe_run_baseline():
                stop.wait(_TICK_SECONDS)
                continue

            if _maybe_run_duel():
                stop.wait(_TICK_SECONDS)
                continue

            # Crown follower: event-driven — completed runs queue a cheap "did the crown
            # move?" filter (quiet ticks are a pure memory test), with a slow backstop
            # full check. Records crown changes (the churn ledger) and — when enabled —
            # keeps the firewall on the crowned best profile. Returns True only when it
            # wrote the firewall; yield that tick so monitoring doesn't measure
            # mid-transition.
            from . import crown_follower

            if crown_follower.step():
                stop.wait(_TICK_SECONDS)
                continue

            enabled, interval_min = _monitoring_config()
            interval_s = max(interval_min * 60.0, 30.0)
            last = _state["last_run_at"]
            due = enabled and (last is None or (time.time() - last) >= interval_s)
            if due and not _active_run_exists():
                # Non-blocking: a periodic run should defer (try next tick) rather
                # than queue behind a long session and stall the watchdog.
                try:
                    with coordinator.try_hold("monitoring"):
                        run_id = create_run(label="scheduled")
                        _state["last_run_at"] = time.time()
                        log.info("Scheduler triggering run %s", run_id)
                        execute_run(run_id)  # blocking; runs sequentially in this thread
                except coordinator.CoordinatorBusy:
                    pass  # someone grabbed the lock between busy() and here; next tick
        except Exception:  # noqa: BLE001 — never let the scheduler die
            log.exception("Scheduler tick failed")
        stop.wait(_TICK_SECONDS)
    log.info("Scheduler thread stopped")


def start_scheduler() -> None:
    if _state["thread"] and _state["thread"].is_alive():
        return
    if not is_leader():
        return  # another process owns the scheduled work (see :func:`is_leader`)
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
        "leader": is_leader(),
        "active": _active_run_exists(),
        "last_run_at": iso(last),
        "next_run_at": iso(next_at),
    }
