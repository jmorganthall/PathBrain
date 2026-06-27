"""REST API routers, mounted under ``/api`` by the application."""
from __future__ import annotations

from fastapi import APIRouter

from . import (
    routes_config,
    routes_experiments,
    routes_history,
    routes_jobs,
    routes_methodology,
    routes_metrics,
    routes_monitoring,
    routes_plugins,
    routes_results,
    routes_run,
    routes_score,
    routes_settings,
    routes_smoothness,
    routes_sweep,
    routes_trends,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(routes_run.router, tags=["run"])
api_router.include_router(routes_results.router, tags=["results"])
api_router.include_router(routes_history.router, tags=["history"])
api_router.include_router(routes_score.router, tags=["score"])
api_router.include_router(routes_config.router, tags=["config"])
api_router.include_router(routes_settings.router, tags=["settings"])
api_router.include_router(routes_smoothness.router, tags=["smoothness"])
api_router.include_router(routes_monitoring.router, tags=["monitoring"])
api_router.include_router(routes_plugins.router, tags=["plugins"])
api_router.include_router(routes_metrics.router, tags=["metrics"])
api_router.include_router(routes_methodology.router, tags=["methodology"])
api_router.include_router(routes_experiments.router, tags=["experiments"])
api_router.include_router(routes_trends.router, tags=["trends"])
api_router.include_router(routes_sweep.router, tags=["sweep"])
api_router.include_router(routes_jobs.router, tags=["jobs"])
