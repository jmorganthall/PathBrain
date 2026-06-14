"""REST API routers, mounted under ``/api`` by the application."""
from __future__ import annotations

from fastapi import APIRouter

from . import (
    routes_config,
    routes_experiments,
    routes_history,
    routes_plugins,
    routes_results,
    routes_run,
    routes_score,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(routes_run.router, tags=["run"])
api_router.include_router(routes_results.router, tags=["results"])
api_router.include_router(routes_history.router, tags=["history"])
api_router.include_router(routes_score.router, tags=["score"])
api_router.include_router(routes_config.router, tags=["config"])
api_router.include_router(routes_plugins.router, tags=["plugins"])
api_router.include_router(routes_experiments.router, tags=["experiments"])
