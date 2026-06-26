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
