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
    # IANA zone the hour/minute are expressed in (e.g. "America/Chicago"). The UI sends the
    # browser's zone when saving, so "Run at 02:00" fires at the user's 02:00 regardless of
    # the container's TZ. Empty/None = container-local (the legacy behavior).
    timezone: str | None = None


class DuelScheduleUpdate(BaseModel):
    """Update the nightly duel-ladder schedule + stopping rule (all fields optional)."""

    enabled: bool | None = None
    hour: int | None = None
    minute: int | None = None
    # IANA zone the hour/minute are expressed in (browser zone sent by the UI on save).
    timezone: str | None = None
    duration_minutes: int | None = None
    # The window's end wall-clock time (same zone as hour/minute). When given, it REPLACES
    # duration_minutes — the schedule is edited as "from 03:00 until 05:00", and the
    # duration the engine runs on is derived from the pair (wrapping past midnight).
    end_hour: int | None = None
    end_minute: int | None = None
    # Hours a decided matchup rests before it can be fought again.
    rematch_hours: float | None = None
    # How stale the duel champion may be before the crowning policy falls back to pooled.
    # A separate question from the cooldown, which it used to share a field with.
    champion_freshness_days: float | None = None
    # Standard errors subtracted from the rating when ordering the standings (0 = rank on
    # the rating itself), and the virtual pairs added to every record before fitting.
    rank_sigma: float | None = None
    rating_prior_pairs: float | None = None
    # Benchmark iterations per leg of a round — the ring's resolving power.
    iterations_per_round: int | None = None
    # Which rule names the champion: "lineal" (you take the belt by beating its holder,
    # provided your whole shared record then favours you on both matches and rounds) or
    # "rating_floor" (the ring's #1 by proven rating). The standings rank on the floor
    # either way — this only decides who wears the belt.
    crown_rule: str | None = None
    # Seconds to let the link settle after writing a profile before measuring it — each
    # leg is preceded by a setPipe + reconfigure that rebuilds the queues. 0 = measure
    # immediately (the old behaviour).
    settle_seconds: int | None = None
    # Sequential stopping rule: verdicts never fire before `min_pairs`, futility cap at
    # `max_pairs`, and a statistical winner under `min_margin` Overall points is a draw.
    min_pairs: int | None = None
    max_pairs: int | None = None
    min_margin: float | None = None
    # The evidence bar: the edge worth detecting (p1) and the false-positive rate (alpha).
    p1: float | None = None
    alpha: float | None = None
    # How a bout is judged: "margins" (paired signed-rank, magnitude-aware) or the legacy
    # "pair_wins" sign test.
    method: str | None = None
    # "snap" / "quick" / "balanced" / "strict" — sets the statistical fields in one move.
    preset: str | None = None
    # Explicit "N wins in a row ends it" (0 = derive it from the threshold).
    streak_wins: int | None = None
    # Run the ladder perpetually rather than once a night, and the gap between sessions.
    continuous: bool | None = None
    continuous_gap_minutes: float | None = None
    # Who the champion fights: "leaders" (closest to the crown) or "heirs" (explore).
    contenders: str | None = None
    contender_top_n: int | None = None


class DuelStart(BaseModel):
    """Start an on-demand duel-ladder session (duration defaults to the configured window)."""

    duration_minutes: int | None = None


class TestSettings(BaseModel):
    """Apply an arbitrary set of shaper settings (e.g. an AI suggestion) and test to minimum.

    ``settings`` is a list of per-pipe overrides (each with a ``label`` matching a live pipe)
    or a single flat dict of writable fields applied to every pipe. Only *writable* fields are
    applied — the result is always reachable from the live environment.

    ``iterations`` runs exactly that many iterations; omitted, the test tops the profile up to
    the confidence minimum. A short run answers "did this go anywhere?"; the top-up answers
    "is this profile confidently better?" — different questions, and the caller picks."""

    settings: Any
    label: str | None = None
    iterations: int | None = None


class ExploreTest(BaseModel):
    """Measure one Explore recommendation — and write the claim down before doing it.

    Carries the candidate exactly as the landscape proposed it: the levers moved, what the
    model predicted (``predicted`` ± ``uncertainty``, the ``upside`` it was ranked on), the
    ``best_overall`` it claimed to beat, and **what the prediction rested on** (``evidence``).
    Those are stored as an ``ExploreRecommendation`` so the proposal can be graded against
    the measurement later, rather than evaporating the moment the benchmark starts.

    ``parent_fingerprint`` matters more than it looks: the candidate is "*that* profile with
    a lever moved", so the levers are overlaid on the parent's stored settings, not on
    whatever the firewall happens to be set to right now. Without it, a recommendation
    tested while the firewall sits on some third profile measures something nobody proposed.
    """

    settings: Any
    label: str | None = None
    # None → top up to the confidence minimum; an integer → run exactly that many.
    iterations: int | None = None
    parent_fingerprint: str | None = None
    parent_overall: float | None = None
    changes: Any = None
    evidence: list[str] | None = None
    multi_lever: bool = False
    predicted: float | None = None
    uncertainty: float | None = None
    upside: float | None = None
    best_overall: float | None = None
    summary: str | None = None


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
    # "Where's the pause?" diagnostic (one entry per browser URL): the single longest void in the
    # load, which phase it falls in (pre_fcp / fcp_lcp / lcp_load / post_load), and its network-vs-
    # render attribution — so the felt pause is locatable without guessing at a crown metric.
    pause_diagnostics: list[dict[str, Any]] | None = None


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
