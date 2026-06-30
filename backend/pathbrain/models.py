"""ORM models for PathBrain.

Core entities:

* ``Run``             — a single execution of a benchmark suite.
* ``BenchmarkResult`` — raw metrics from one plugin within a run.
* ``ScoreResult``     — the computed Seat of Pants Score for a run.
* ``ConfigSnapshot``  — a captured firewall configuration (for safety/rollback).
* ``AppConfig``       — persisted runtime config (targets, weights, thresholds).
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class Run(Base):
    __tablename__ = "runs"
    # Nearly every query filters by status and/or orders by created_at, and the
    # settings views group by fingerprint. Without these, SQLite full-scans + filesorts
    # the whole table on each request — the dominant cost as history grows.
    __table_args__ = (
        Index("ix_runs_status_created_at", "status", "created_at"),
        Index("ix_runs_created_at", "created_at"),
        Index("ix_runs_settings_fingerprint", "settings_fingerprint"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus), default=RunStatus.PENDING)

    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Multi-iteration support: run the whole suite `iterations` times and average.
    iterations: Mapped[int] = mapped_column(Integer, default=1)
    iterations_completed: Mapped[int] = mapped_column(Integer, default=0)
    # Mean wall-clock duration of a single full iteration, for ETA estimates.
    per_iteration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Firewall/SQM settings in effect during this run, for settings-vs-score
    # attribution. ``settings`` is the normalized pipe list; ``settings_fingerprint``
    # is a stable hash so runs can be grouped by configuration profile.
    settings_fingerprint: Mapped[str | None] = mapped_column(String(40), nullable=True)
    settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # The methodology version this run was scored under at capture (its at-measure
    # interpretation). See the Score table + docs/methodology.md.
    methodology_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The benchmark config used for this run (snapshot for reproducibility).
    config_used: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The firewall config snapshot id associated with this run, if any.
    config_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("config_snapshots.id"), nullable=True
    )

    results: Mapped[list["BenchmarkResult"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="BenchmarkResult.id"
    )
    score: Mapped["ScoreResult | None"] = relationship(
        back_populates="run", cascade="all, delete-orphan", uselist=False
    )
    config_snapshot: Mapped["ConfigSnapshot | None"] = relationship()


class BenchmarkResult(Base):
    __tablename__ = "benchmark_results"
    # Results are always fetched by run_id (per-run detail + the eager-load on profile/
    # trend aggregations). SQLite doesn't auto-index foreign keys, so add it explicitly.
    __table_args__ = (Index("ix_benchmark_results_run_id", "run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    plugin: Mapped[str] = mapped_column(String(64))
    success: Mapped[bool] = mapped_column(default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Derived, scoreable metrics — a *materialized cache* of the current
    # interpretation, e.g. {"latency_ms": 12.3, "jitter_ms": 1.1}. Rebuildable from
    # ``raw`` at any time; never the source of truth.
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    # Per-target / detailed breakdown.
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Immutable raw observations the plugin captured, per iteration:
    # ``{"iterations": [<plugin-specific raw payload>, ...]}``. The source of truth —
    # every value in ``metrics`` is derived from here, so a new metric or a changed
    # formula can be re-derived across history without re-collecting.
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    run: Mapped["Run"] = relationship(back_populates="results")


class ScoreResult(Base):
    __tablename__ = "score_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), unique=True)
    sops: Mapped[float] = mapped_column(Float)  # 0..100 (robust central value)

    # Spread of the per-iteration SOPS, for a confidence band on the headline.
    sops_stdev: Mapped[float | None] = mapped_column(Float, nullable=True)
    sops_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    sops_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The scoring rubric (curve/thresholds) version that produced this score.
    rubric_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # The derivation (raw -> metric values) version behind the cached metric_values.
    # Lets us tell when a run's cache predates the current derivation and needs a
    # re-derive (vs. a cheaper rubric-only rescore).
    derivation_version: Mapped[str | None] = mapped_column(String(40), nullable=True)

    # Per-metric subscores and the (possibly redistributed) weights used.
    subscores: Mapped[dict] = mapped_column(JSON, default=dict)
    weights_used: Mapped[dict] = mapped_column(JSON, default=dict)
    metric_values: Mapped[dict] = mapped_column(JSON, default=dict)

    # The Completion score — a *separate* axis from SOPS (pure-infrastructure
    # timing, not human-feel). NULL when none of its metrics were captured.
    # Stored in the legacy ``responsiveness``/``perceptual_*`` columns: SOPS is now
    # the perception-led headline, so this axis was relabeled to "completion" at
    # the attribute/API layer while reusing the existing columns (no migration;
    # a deeper column rename is deferred).
    completion: Mapped[float | None] = mapped_column("responsiveness", Float, nullable=True)
    completion_stdev: Mapped[float | None] = mapped_column(
        "responsiveness_stdev", Float, nullable=True
    )
    completion_min: Mapped[float | None] = mapped_column(
        "responsiveness_min", Float, nullable=True
    )
    completion_max: Mapped[float | None] = mapped_column(
        "responsiveness_max", Float, nullable=True
    )
    completion_subscores: Mapped[dict | None] = mapped_column(
        "perceptual_subscores", JSON, nullable=True
    )
    completion_weights_used: Mapped[dict | None] = mapped_column(
        "perceptual_weights_used", JSON, nullable=True
    )
    completion_metric_values: Mapped[dict | None] = mapped_column(
        "perceptual_metric_values", JSON, nullable=True
    )

    run: Mapped["Run"] = relationship(back_populates="score")


class ConfigSnapshot(Base):
    """A captured firewall configuration for safety and rollback."""

    __tablename__ = "config_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    provider: Mapped[str] = mapped_column(String(64))
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The discovered configuration (e.g. FQ-CoDel parameters, bandwidth).
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ExperimentStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    ABORTED = "aborted"


class Experiment(Base):
    """An autonomous sweep of one shaper parameter across candidate values."""

    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[ExperimentStatus] = mapped_column(
        Enum(ExperimentStatus), default=ExperimentStatus.RUNNING
    )

    param: Mapped[str] = mapped_column(String(64))
    pipe_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidates: Mapped[list] = mapped_column(JSON, default=list)
    dry_run: Mapped[bool] = mapped_column(default=True)

    baseline_value: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Full pre-experiment settings snapshot, restored at window close by default.
    baseline_settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Outcome: per-value medians, winner, and whether we promoted or restored.
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    trials: Mapped[list["ExperimentTrial"]] = relationship(
        back_populates="experiment", cascade="all, delete-orphan", order_by="ExperimentTrial.id"
    )


class ExperimentTrial(Base):
    """One measured sample of a candidate value within an experiment."""

    __tablename__ = "experiment_trials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    value: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    sops: Mapped[float | None] = mapped_column(Float, nullable=True)
    applied: Mapped[bool] = mapped_column(default=False)

    experiment: Mapped["Experiment"] = relationship(back_populates="trials")


class SweepStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


class Sweep(Base):
    """A fast, supervised foreground sweep across shaper parameter values.

    Unlike the autonomous Experiment (window-gated, dry-run-by-default), a sweep is
    kicked off on demand: it applies each variant for real, benchmarks it, and
    restores the original config at the end. The row persists the baseline so a
    crash mid-sweep can still restore the firewall on startup.
    """

    __tablename__ = "sweeps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[SweepStatus] = mapped_column(Enum(SweepStatus), default=SweepStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    dry_run: Mapped[bool] = mapped_column(default=False)
    iterations: Mapped[int] = mapped_column(Integer, default=2)
    dwell_s: Mapped[float] = mapped_column(Float, default=0.0)
    pipe_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The sweep definition: per-param {min,max,step} ranges + the generated variants.
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    # Pre-sweep baseline to restore: {quantum, target, settings:[...]}.
    baseline: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    total_variants: Mapped[int] = mapped_column(Integer, default=0)
    completed_variants: Mapped[int] = mapped_column(Integer, default=0)
    # Per-variant outcomes: [{index, quantum, target, run_id, sops}].
    results: Mapped[list] = mapped_column(JSON, default=list)


class AppConfig(Base):
    """Singleton-ish key/value store for persisted runtime configuration."""

    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class Methodology(Base):
    """An immutable, append-only snapshot of *how raw becomes a score* at a point in time.

    A methodology bundles a **derivation** (raw → metric scalars) with a **rubric**
    (metric scalars → axis scores: the metric set, weights, thresholds, axes). The
    ``definition`` JSON is a self-contained snapshot of that whole interpretation —
    everything needed to read or reproduce a score with no reference to current code,
    so a historical run can always be shown "scored under this methodology" (see
    ``docs/methodology.md``). You never edit a methodology; a new weight, threshold,
    or metric is published as a new version.
    """

    __tablename__ = "methodologies"

    # The bundle id scores reference (the rubric version, e.g. "perceptual-v5").
    version: Mapped[str] = mapped_column(String(64), primary_key=True)
    rubric_version: Mapped[str] = mapped_column(String(64))
    derivation_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Human changelog, e.g. "re-anchored thresholds to CWV good/poor boundaries".
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Exactly one row is the published-now methodology.
    is_current: Mapped[bool] = mapped_column(default=False)
    # The full frozen catalog + rubric: {axes:[...], metrics:[{key, axis, weight,
    # best, worst, unit, label, required, ...}]}. See methodology.build_definition.
    definition: Mapped[dict] = mapped_column(JSON, default=dict)


class Score(Base):
    """A run's score under one methodology — the (run × methodology) record.

    Each pairing of a run with a methodology is its own immutable row, so a run can
    be viewed under any past or present methodology (see ``docs/methodology.md``):

    * **score-at-measure** — the row whose ``methodology_version`` is the one that was
      current when the run was collected (``is_at_measure=True``). Written once at
      capture, never overwritten.
    * **score-at-present** — the row for the *current* methodology; added/refreshed by
      re-grading (Phase 3), which never touches the at-measure row of another version.

    ``comparability`` records whether this run's raw can reproduce the methodology's
    metrics: ``exact`` (all present), ``partial`` (some optional ones missing —
    redistributed, see ``missing_metrics``), or ``incomparable`` (a required metric the
    raw never captured).
    """

    __tablename__ = "scores"
    # The unique constraint indexes (run_id, methodology_version) — good for joining by
    # run_id. The aggregations also filter by methodology_version alone (all scores under
    # the current methodology), which that index can't serve; add a leading-column index.
    __table_args__ = (
        UniqueConstraint("run_id", "methodology_version", name="uq_score_run_methodology"),
        Index("ix_scores_methodology_version", "methodology_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"))
    # Soft reference (no hard FK): pre-foundation versions may not be recorded.
    methodology_version: Mapped[str] = mapped_column(String(64))
    is_at_measure: Mapped[bool] = mapped_column(default=False)
    comparability: Mapped[str] = mapped_column(String(16), default="exact")
    missing_metrics: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Per-axis headline scores, e.g. {"sops": 78.1, "completion": 70.4}.
    axis_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    # Per-metric 0..100 subscores, redistributed weights, and the scalars scored.
    subscores: Mapped[dict] = mapped_column(JSON, default=dict)
    weights_used: Mapped[dict] = mapped_column(JSON, default=dict)
    metric_values: Mapped[dict] = mapped_column(JSON, default=dict)
    # Per-axis confidence bands: {axis: {stdev, min, max, ...}}.
    bands: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ProfileTestStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class ProfileTest(Base):
    """An on-demand "test this profile up to the confidence minimum" session.

    Applies a stored settings profile to the firewall, runs one benchmark with
    exactly the iterations still needed to reach ``correlation.min_iterations``,
    then restores the pre-test settings — always, in a ``finally``. Like
    ``Sweep``, the row persists the baseline so a crash mid-test can still restore
    the firewall on startup (``reconcile_interrupted_profile_tests``).
    """

    __tablename__ = "profile_tests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[ProfileTestStatus] = mapped_column(
        Enum(ProfileTestStatus), default=ProfileTestStatus.PENDING
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The profile being topped up + a short human label, for the status display.
    fingerprint: Mapped[str] = mapped_column(String(40))
    target_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # How many iterations this test runs (the gap to the minimum).
    iterations: Mapped[int] = mapped_column(Integer, default=1)
    # Pre-test live settings to restore: normalized pipe list.
    baseline: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # The benchmark run this test produced (once it starts), for linking.
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"), nullable=True)


class ChallengerRaceStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChallengerRace(Base):
    """An adaptive, time-boxed "race" of limited-data profiles against the confident
    best (the adaptive sibling of ``ProfileTest``).

    Runs ONE iteration at a time on the most promising under-minimum profile, re-ranks,
    and eliminates any challenger whose optimistic best-case can no longer beat the
    best. Like ``ProfileTest``/``Sweep`` it persists the pre-race baseline so a crash
    mid-race can still restore the firewall on startup
    (``reconcile_interrupted_challenges``). When ``auto_promote`` is set and a
    challenger confirms it beats the best, the winner is left applied instead of
    restoring the baseline.
    """

    __tablename__ = "challenger_races"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[ChallengerRaceStatus] = mapped_column(
        Enum(ChallengerRaceStatus), default=ChallengerRaceStatus.PENDING
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Race configuration.
    time_budget_s: Mapped[int] = mapped_column(Integer, default=300)
    auto_promote: Mapped[bool] = mapped_column(Boolean, default=False)

    # Pre-race live settings to restore (normalized pipe list) — drives reconcile.
    baseline: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Live/result progress.
    iterations_run: Mapped[int] = mapped_column(Integer, default=0)
    # Iterations spent re-measuring the crowned incumbent (a stale-bar refresh), so the
    # challengers race a contemporaneous bar. Counted within iterations_run.
    incumbent_refreshes: Mapped[int] = mapped_column(Integer, default=0)
    leader_fingerprint: Mapped[str | None] = mapped_column(String(40), nullable=True)
    leader_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    winner_fingerprint: Mapped[str | None] = mapped_column(String(40), nullable=True)
    promoted: Mapped[bool] = mapped_column(Boolean, default=False)
    # Eliminated challengers: [{fingerprint, label, reason}].
    eliminated: Mapped[list | None] = mapped_column(JSON, nullable=True)


class ProfileRefreshStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProfileRefresh(Base):
    """A "re-run all profiles" session: top every stored profile up to the confidence
    minimum under the current methodology, so profiles whose history can't supply the
    current crown metrics get fresh, comparable data.

    For each profile it applies the stored settings, benchmarks exactly the iterations
    still needed to reach ``correlation.min_iterations`` of comparable runs, then moves
    on — restoring the pre-refresh baseline at the end (always, in a ``finally``). Like
    ``ProfileTest``/``ChallengerRace`` it persists the baseline so a crash mid-refresh
    can still restore the firewall on startup (``reconcile_interrupted_refreshes``).
    """

    __tablename__ = "profile_refreshes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[ProfileRefreshStatus] = mapped_column(
        Enum(ProfileRefreshStatus), default=ProfileRefreshStatus.PENDING
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Pre-refresh live settings to restore (normalized pipe list) — drives reconcile.
    baseline: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Live/result progress.
    profiles_total: Mapped[int] = mapped_column(Integer, default=0)
    profiles_done: Mapped[int] = mapped_column(Integer, default=0)
    iterations_run: Mapped[int] = mapped_column(Integer, default=0)
    current_fingerprint: Mapped[str | None] = mapped_column(String(40), nullable=True)
    current_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
