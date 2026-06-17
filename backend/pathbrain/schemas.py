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


class ManualApplyIn(BaseModel):
    """Manual firewall shaper edit for one pipe.

    ``changes`` maps normalized param names (bandwidth/quantum/limit/flows/
    target/interval/ecn) to their new values. ``pipe_uuid`` blank → first pipe.
    """

    pipe_uuid: str | None = None
    changes: dict[str, Any]


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
    # True when this score predates the current rubric's metrics (no paint data),
    # so its SOPS isn't comparable — the UI quarantines it as "legacy".
    legacy: bool = False

    # Completion axis (pure-infra timing) — separate from SOPS. None when the run
    # captured none of its metrics.
    completion: float | None = None
    completion_stdev: float | None = None
    completion_min: float | None = None
    completion_max: float | None = None
    completion_subscores: dict[str, float] | None = None
    completion_weights_used: dict[str, float] | None = None
    completion_metric_values: dict[str, float] | None = None


class RunSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str
    label: str | None = None
    sops: float | None = None
    # True when this run's score predates the current rubric's metrics (legacy,
    # not comparable). False for runs with no score yet (running/failed).
    legacy: bool = False
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
    """Average plugin metrics for the best-scoring settings profile, for comparison.

    ``metrics`` maps plugin name -> {metric_key: mean_value} across the runs of the
    profile with the highest median SOPS (or, when no profile is usable, the most
    recent completed runs). The frontend uses it to render improved/worse arrows
    showing how far this run is from the best-known configuration.
    """

    run_id: int
    scope: str  # "best_profile" (highest-median-SOPS profile) or "all" (recent runs)
    profile_fingerprint: str | None = None
    profile_label: str | None = None
    profile_median_sops: float | None = None
    # True when the viewed run already belongs to the best profile (so the
    # comparison is against that profile's own average rather than a better one).
    is_best_profile: bool = False
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


class ManualApplyResult(BaseModel):
    param: str
    value: Any
    ok: bool
    detail: str | None = None


class ManualApplyOut(BaseModel):
    provider: str
    snapshot_id: int | None = None
    applied: int
    results: list[ManualApplyResult]
