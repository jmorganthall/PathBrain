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
    # Requests over runner.CHUNK_ITERATIONS execute as a series of smaller runs.
    iterations: int | None = None


class CurrentTestStart(BaseModel):
    """Start a "test the current settings for X minutes" session."""

    minutes: float


class BaselineTestStart(BaseModel):
    """Start an on-demand baseline (SQM off) test. Omitted values fall back to the configured
    ``baseline_test`` defaults."""

    iterations: int | None = None
    settle_seconds: int | None = None


class BaselineScheduleUpdate(BaseModel):
    """Update the nightly baseline-test schedule + defaults (all fields optional)."""

    enabled: bool | None = None
    hour: int | None = None
    minute: int | None = None
    iterations: int | None = None
    settle_seconds: int | None = None


class TestSettings(BaseModel):
    """Apply an arbitrary set of shaper settings (e.g. an AI suggestion) and test to minimum.

    ``settings`` is a list of per-pipe overrides (each with a ``label`` matching a live pipe)
    or a single flat dict of writable fields applied to every pipe. Only *writable* fields are
    applied — the result is always reachable from the live environment."""

    settings: Any
    label: str | None = None


class ApplySettings(BaseModel):
    """Apply an arbitrary set of shaper settings (e.g. an AI suggestion) to the firewall
    **permanently** (one-way write, no baseline restore). ``settings`` is a per-pipe override
    list or a flat writable dict, like ``TestSettings``. ``preview`` returns the planned writes
    without touching the firewall; ``run_benchmark`` kicks a 1-iteration benchmark after applying."""

    settings: Any
    label: str | None = None
    preview: bool = False
    run_benchmark: bool = True


class AiConfigUpdate(BaseModel):
    """Partial AI settings; only provided fields are saved. A blank ``api_key`` is ignored."""

    api_key: str | None = None
    model: str | None = None
    prompt: str | None = None


class AiSuggest(BaseModel):
    """Ask the configured model to propose new profiles from the optimizer export."""

    model: str | None = None       # override the saved model for this call
    prompt: str | None = None      # override the saved prompt for this call
    runs_per_profile: int = 50
    profile_limit: int | None = 25  # top-N profiles by Overall (bounds the payload)


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
    # Headline axis scores under the current methodology (null until scored/comparable).
    # ``overall`` is the first-class, versioned corner roll-up persisted on the Score
    # (``axis_scores['overall']``) — the headline figure, replacing the legacy SOPS.
    overall: float | None = None
    responsiveness: float | None = None
    speed: float | None = None
    smoothness: float | None = None
    # True when the run has a score but isn't comparable under the current
    # methodology. False for runs with no score yet (running/failed).
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
    # The run's first-class Overall under the current methodology
    # (``Score.axis_scores['overall']``) — the headline figure shown in the gauge.
    # None when the run isn't comparable / not yet scored under the current methodology.
    overall: float | None = None


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
