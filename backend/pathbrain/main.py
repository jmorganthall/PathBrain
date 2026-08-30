"""PathBrain FastAPI application.

Serves the REST API under ``/api`` and, in production, the built React frontend
as static files. In development the frontend runs on Vite (:5173) and proxies to
this server, so CORS is permitted for localhost.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import api_router
from .config import get_settings
from .database import init_db
from .logging_config import configure_logging, get_logger

settings = get_settings()
configure_logging(settings.log_level)
log = get_logger("main")

# Directory containing the built frontend (set in Docker). Optional in dev.
FRONTEND_DIST = os.environ.get(
    "PATHBRAIN_FRONTEND_DIST",
    os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("PathBrain %s starting up", __version__)
    init_db()
    log.info("Database initialized (%s)", settings.database_url)
    from .runner import reconcile_interrupted_runs

    reconcile_interrupted_runs()  # fail any runs orphaned by a previous restart
    from .methodology import seed_current_methodology

    seed_current_methodology()  # record the interpretation methodology in play
    from .sweep import reconcile_interrupted_sweeps

    reconcile_interrupted_sweeps()  # restore the firewall if a sweep was interrupted
    from .profile_test import reconcile_interrupted_profile_tests

    reconcile_interrupted_profile_tests()  # restore the firewall if a profile test was interrupted
    from .challenger import reconcile_interrupted_challenges

    reconcile_interrupted_challenges()  # restore the firewall if a challenger race was interrupted
    from .refresh import reconcile_interrupted_refreshes

    reconcile_interrupted_refreshes()  # restore the firewall if a profile refresh was interrupted
    from .current_test import reconcile_interrupted_current_tests

    reconcile_interrupted_current_tests()  # close out any interrupted test-current session
    from .baseline_test import reconcile_interrupted_baseline_tests
    from .duel import reconcile_interrupted_duels

    reconcile_interrupted_baseline_tests()  # re-enable SQM if a baseline test was interrupted
    reconcile_interrupted_duels()  # restore the pre-duel baseline if a duel was interrupted
    from .updates import verify_pending_updates

    # A self-update recreates this container, so startup is the moment of truth: compare the
    # build that was running when "Update now" was pressed against the one running now.
    verify_pending_updates()
    from .scheduler import start_scheduler, stop_scheduler

    start_scheduler()
    yield
    stop_scheduler()
    log.info("PathBrain shutting down")


app = FastAPI(
    title="PathBrain",
    version=__version__,
    description="Empirical tuner for OPNsense SQM / FQ-CoDel traffic shaping, "
    "scored by human-perceived responsiveness (Seat of Pants Score).",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.get("/api/health/pipeline")
def pipeline_health() -> dict:
    """What the benchmark pipeline is doing, and — if it is doing nothing — where it stopped.

    "It seems stuck" is unfalsifiable from the outside: the jobs feed shows a running job
    because a row says RUNNING, and a wedged thread looks exactly like a busy one. This is
    the read that settles it, in one request and without touching the database: who holds
    the coordination lock, how long since they last showed progress, how many sessions are
    queued behind them, the probe worker's health (including any thread abandoned mid-call)
    — and the **stack of every thread**, which is the only thing that says *what call* is
    not returning.

    Read-only and cheap: ``sys._current_frames()`` is a snapshot, not an interruption.

    ``database`` is here because a starved connection pool is invisible from every other
    angle: each request simply takes ``pool_timeout`` seconds and then fails, which reads
    as "the server is broken" rather than "the sixteenth caller is queuing for a file
    handle". ``checked_out`` against ``capacity`` is the one number that tells a slow query
    from a starved one.
    """
    import sys
    import threading
    import traceback

    from . import coordinator, probes
    from .database import pool_status

    by_id = {t.ident: t for t in threading.enumerate()}
    stacks = []
    for ident, frame in sys._current_frames().items():
        thread = by_id.get(ident)
        name = thread.name if thread is not None else f"thread-{ident}"
        # Only the frames that matter: a full stack is mostly framework, and the tail is
        # where the blocked call is.
        stacks.append({
            "thread": name,
            "daemon": bool(thread.daemon) if thread is not None else None,
            "stack": [line.rstrip() for line in traceback.format_stack(frame)[-12:]],
        })
    return {
        "coordinator": coordinator.status(),
        "probes": probes.stats(),
        "database": pool_status(),
        "threads": sorted(stacks, key=lambda s: s["thread"]),
    }


@app.get("/api/version")
def version() -> dict:
    """Build identity + a cached, best-effort check for a newer build to pull."""
    from .updates import version_info

    return version_info()


@app.post("/api/version/refresh")
def version_refresh() -> dict:
    """Re-check upstream *now*, bypassing the 1-hour cache — the "Check now" button. Returns the
    same shape as /api/version so the UI can swap the result in directly."""
    from .updates import version_info

    return version_info(force=True)


@app.get("/api/update/config")
def update_config() -> dict:
    """Watchtower integration config state (no network call) — for the Plugins integration card."""
    from .updates import self_update_config

    return self_update_config()


@app.post("/api/update/test")
def test_update_connection() -> dict:
    """Test the Watchtower integration without triggering an update: probe reachability + report
    config. Always 200 with a ``status`` field (ok | unreachable | not_configured)."""
    from .updates import test_update_connection as _test

    return _test()


@app.get("/api/update/log")
def update_log(limit: int = 10) -> dict:
    """Recent self-update attempts and what became of each.

    The container log can't answer "did the update work?" — a *successful* update replaces the
    container along with its log. This reads the persisted attempt ledger instead, resolving any
    still-pending verdict by comparing the build that was running when the button was pressed
    against the one running now."""
    from .updates import update_log as _log

    return _log(limit)


@app.post("/api/update/trigger")
def trigger_update():
    """One-click self-update: ask Watchtower to pull the newer image and recreate this container.

    Returns ``{"triggered": true, "detail": ...}`` on success (the container will restart shortly),
    or ``409`` when Watchtower isn't configured / a ``502`` when it's unreachable or rejects the
    token. A successful update may sever this very response as the container is recreated — the
    frontend treats a dropped connection on this call as "update in progress"."""
    from fastapi import HTTPException

    from .updates import trigger_update as _trigger

    result = _trigger()
    if result.get("triggered"):
        return result
    error = result.get("error") or "Update could not be triggered."
    # Not configured → 409 (nothing to call); reachable-but-failed → 502 (upstream problem).
    status = 409 if "not configured" in error else 502
    raise HTTPException(status_code=status, detail=error)


# -- Browser-engine artifacts (screenshots, HAR) --------------------------
_artifact_dir = os.path.abspath(settings.artifact_dir)
os.makedirs(_artifact_dir, exist_ok=True)
app.mount("/artifacts", StaticFiles(directory=_artifact_dir), name="artifacts")


# -- Static frontend (production) -----------------------------------------
def _mount_frontend() -> None:
    dist = os.path.abspath(FRONTEND_DIST)
    if not os.path.isdir(dist):
        log.info("Frontend dist not found at %s; serving API only", dist)
        return

    assets = os.path.join(dist, "assets")
    if os.path.isdir(assets):
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index_file = os.path.join(dist, "index.html")

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):  # pragma: no cover - thin static handler
        candidate = os.path.join(dist, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(index_file)

    log.info("Serving frontend from %s", dist)


_mount_frontend()
