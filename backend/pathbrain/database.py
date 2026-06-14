"""Database engine and session management.

SQLite today; the storage layer is intentionally thin so PostgreSQL/InfluxDB can
slot in later by changing the engine URL (and, for InfluxDB, adding a
time-series sink alongside the relational store).
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
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
    return create_engine(database_url, connect_args=connect_args, future=True)


settings = get_settings()
engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create all tables. Safe to call repeatedly."""
    # Import models so they register with Base.metadata.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


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
