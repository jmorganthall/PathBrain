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

    # Flat, plugin-defined metrics, e.g. {"latency_ms": 12.3, "jitter_ms": 1.1}.
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    # Per-target / detailed breakdown.
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    run: Mapped["Run"] = relationship(back_populates="results")


class ScoreResult(Base):
    __tablename__ = "score_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), unique=True)
    sops: Mapped[float] = mapped_column(Float)  # 0..100

    # Per-metric subscores and the (possibly redistributed) weights used.
    subscores: Mapped[dict] = mapped_column(JSON, default=dict)
    weights_used: Mapped[dict] = mapped_column(JSON, default=dict)
    metric_values: Mapped[dict] = mapped_column(JSON, default=dict)

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


class AppConfig(Base):
    """Singleton-ish key/value store for persisted runtime configuration."""

    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
