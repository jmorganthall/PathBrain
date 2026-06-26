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
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
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


class AppConfig(Base):
    """Singleton-ish key/value store for persisted runtime configuration."""

    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
