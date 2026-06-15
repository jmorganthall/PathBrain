"""Pydantic schemas for API requests and responses."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


# -- Requests -------------------------------------------------------------
class RunCreate(BaseModel):
    label: str | None = None
    notes: str | None = None
    # Number of full-suite iterations to run and average. None -> config default.
    iterations: int | None = None


class ConfigUpdate(BaseModel):
    """Partial benchmark config; merged over the stored config."""

    model_config = ConfigDict(extra="allow")


# -- Responses ------------------------------------------------------------
class BenchmarkResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    plugin: str
    success: bool
    error: str | None = None
    duration_ms: float | None = None
    metrics: dict[str, Any]
    details: dict[str, Any] | None = None


class ScoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sops: float
    sops_stdev: float | None = None
    sops_min: float | None = None
    sops_max: float | None = None
    subscores: dict[str, float]
    weights_used: dict[str, float]
    metric_values: dict[str, float]


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str
    label: str | None = None
    sops: float | None = None
    iterations: int = 1
    iterations_completed: int = 0
    per_iteration_ms: float | None = None


class RunDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str
    label: str | None = None
    notes: str | None = None
    error: str | None = None
    iterations: int = 1
    iterations_completed: int = 0
    per_iteration_ms: float | None = None
    settings_fingerprint: str | None = None
    settings: list[dict[str, Any]] | None = None
    config_used: dict[str, Any] | None = None
    results: list[BenchmarkResultOut] = []
    score: ScoreOut | None = None


class RunBaselineOut(BaseModel):
    """Average plugin metrics across the run's settings profile, for comparison.

    ``metrics`` maps plugin name -> {metric_key: mean_value} over the other
    completed runs that share this run's settings fingerprint (or, when none are
    available, the most recent completed runs). The frontend uses it to render
    improved/worse arrows next to each measurement.
    """

    run_id: int
    scope: str  # "profile" (same settings fingerprint) or "all" (recent runs)
    profile_fingerprint: str | None = None
    profile_label: str | None = None
    run_count: int
    metrics: dict[str, dict[str, float]] = {}


class PluginInfo(BaseModel):
    name: str
    description: str


class ConfigSnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    provider: str
    label: str | None = None
    data: dict[str, Any]


class DiscoverOut(BaseModel):
    provider: str
    pipes: list[dict[str, Any]]
    snapshot_id: int | None = None
