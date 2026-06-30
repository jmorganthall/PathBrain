"""Database engine and session management.

SQLite today; the storage layer is intentionally thin so PostgreSQL/InfluxDB can
slot in later by changing the engine URL (and, for InfluxDB, adding a
time-series sink alongside the relational store).
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def _make_engine(database_url: str):
    connect_args = {}
    if database_url.startswith("sqlite"):
        # Allow use across FastAPI's threadpool / background tasks.
        connect_args["check_same_thread"] = False
        # Ensure the parent directory exists for file-based SQLite.
        path = database_url.split("sqlite:///", 1)[-1]
        if path and path not in (":memory:",):
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    eng = create_engine(database_url, connect_args=connect_args, future=True)
    if database_url.startswith("sqlite"):
        _install_sqlite_pragmas(eng)
    return eng


def _install_sqlite_pragmas(eng) -> None:
    """Per-connection SQLite tuning for a read-heavy, single-writer workload.

    WAL lets the API keep reading while the scheduler writes (the default rollback
    journal serializes them, so a monitoring write blocks every dashboard query);
    ``synchronous=NORMAL`` is the safe companion to WAL; ``busy_timeout`` makes a
    contended write wait briefly instead of raising ``database is locked``.
    """

    @event.listens_for(eng, "connect")
    def _set_pragmas(dbapi_conn, _record):  # noqa: ANN001 — DBAPI handle
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
        finally:
            cur.close()


settings = get_settings()
engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create all tables. Safe to call repeatedly."""
    # Import models so they register with Base.metadata.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate()


def _migrate() -> None:
    """Lightweight additive migrations for SQLite.

    ``create_all`` never alters existing tables, so columns added after a
    database already exists (e.g. on a deployed volume) must be added by hand.
    This adds any missing columns; it is idempotent and data-preserving.
    """
    if engine.dialect.name != "sqlite":
        return

    from sqlalchemy import text

    new_columns: dict[str, dict[str, str]] = {
        "runs": {
            "iterations": "INTEGER DEFAULT 1",
            "iterations_completed": "INTEGER DEFAULT 0",
            "per_iteration_ms": "FLOAT",
            "settings_fingerprint": "VARCHAR(40)",
            "settings": "JSON",
            "methodology_version": "VARCHAR(64)",
        },
        "benchmark_results": {
            "raw": "JSON",
        },
        "challenger_races": {
            "incumbent_refreshes": "INTEGER DEFAULT 0",
        },
        "score_results": {
            "sops_stdev": "FLOAT",
            "sops_min": "FLOAT",
            "sops_max": "FLOAT",
            "rubric_version": "VARCHAR(40)",
            "derivation_version": "VARCHAR(40)",
            "responsiveness": "FLOAT",
            "responsiveness_stdev": "FLOAT",
            "responsiveness_min": "FLOAT",
            "responsiveness_max": "FLOAT",
            "perceptual_subscores": "JSON",
            "perceptual_weights_used": "JSON",
            "perceptual_metric_values": "JSON",
        },
    }
    # Performance indexes. ``create_all`` adds these on a fresh DB (they're declared on
    # the models), but it skips already-existing tables — so existing deployments need
    # them created by hand. ``IF NOT EXISTS`` makes this idempotent and data-preserving.
    new_indexes: list[tuple[str, str, str]] = [
        ("ix_runs_status_created_at", "runs", "status, created_at"),
        ("ix_runs_created_at", "runs", "created_at"),
        ("ix_runs_settings_fingerprint", "runs", "settings_fingerprint"),
        ("ix_benchmark_results_run_id", "benchmark_results", "run_id"),
        ("ix_scores_methodology_version", "scores", "methodology_version"),
    ]
    with engine.begin() as conn:
        for table, columns in new_columns.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if not existing:
                continue  # table doesn't exist yet; create_all handles it
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        for name, table, cols in new_indexes:
            # Skip if the table isn't there yet (create_all will build it + its indexes).
            if not list(conn.execute(text(f"PRAGMA table_info({table})"))):
                continue
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})"))


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-managed session for background tasks / scripts."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
