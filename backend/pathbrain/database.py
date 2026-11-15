"""Database engine and session management.

SQLite today; the storage layer is intentionally thin so PostgreSQL/InfluxDB can
slot in later by changing the engine URL (and, for InfluxDB, adding a
time-series sink alongside the relational store).
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings
from .logging_config import get_logger


class Base(DeclarativeBase):
    pass


#: Connections kept warm for SQLite, and the burst allowed past them.
#:
#: SQLAlchemy's default pool — 5 connections plus 10 overflow, and a **30-second** wait
#: before the 16th caller gives up — is sized for sessions on a networked database, where
#: connections are a scarce server-side resource worth rationing. A SQLite connection is a
#: file handle: WAL lets any number of readers work at once and there is no server to
#: protect. Against a process that runs every sync endpoint on Starlette's 40-thread pool,
#: *plus* a scheduler, a duel, a probe worker and whichever engine is mid-session, 15 is
#: not a safety margin. It is a queue, and a page that fans out a handful of requests while
#: a duel runs walks straight into it: the 16th caller waits half a minute and then fails
#: with ``QueuePool limit of size 5 overflow 10 reached``. That is the "the Duels page takes
#: minutes and then times out" report — a request whose own query takes a second or two,
#: spending thirty seconds waiting for permission to make it.
#:
#: So capacity is set past the number of threads that can possibly ask, and the wait is
#: cut: a caller still queuing after ``_SQLITE_POOL_TIMEOUT`` is not busy, it is leaking a
#: session somewhere, and a loud failure names that in seconds where a 30-second stall
#: reads as "the server is broken". Raising the ceiling does not raise write pressure —
#: the same threads do the same work, they simply stop waiting in line for a file handle —
#: so ``busy_timeout`` is unchanged.
_SQLITE_POOL_SIZE = 30
_SQLITE_MAX_OVERFLOW = 70
_SQLITE_POOL_TIMEOUT = 10.0


def _make_engine(database_url: str):
    connect_args = {}
    kwargs: dict = {}
    if database_url.startswith("sqlite"):
        # Allow use across FastAPI's threadpool / background tasks.
        connect_args["check_same_thread"] = False
        kwargs.update(
            pool_size=_SQLITE_POOL_SIZE,
            max_overflow=_SQLITE_MAX_OVERFLOW,
            pool_timeout=_SQLITE_POOL_TIMEOUT,
        )
        # Ensure the parent directory exists for file-based SQLite.
        path = database_url.split("sqlite:///", 1)[-1]
        if path and path not in (":memory:",):
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    eng = create_engine(database_url, connect_args=connect_args, future=True, **kwargs)
    if database_url.startswith("sqlite"):
        _install_sqlite_pragmas(eng)
    return eng


def pool_status() -> dict:
    """What the connection pool is doing right now — for ``/api/health/pipeline``.

    Exhaustion is invisible from the outside: every request simply takes thirty seconds
    and then fails, which reads as "the server is broken" rather than "the sixteenth
    caller is queuing for a file handle". ``checked_out`` against ``capacity`` says it
    outright, and is the one number that distinguishes a slow query from a starved one.
    """
    pool = engine.pool
    try:
        size = int(pool.size())                      # type: ignore[attr-defined]
        overflow = int(pool.overflow())              # type: ignore[attr-defined]
        checked_out = int(pool.checkedout())         # type: ignore[attr-defined]
        capacity = size + int(getattr(pool, "_max_overflow", 0) or 0)
    except Exception:  # noqa: BLE001 — a pool without these (NullPool) still reports a shape
        return {"kind": type(pool).__name__, "capacity": None, "checked_out": None}
    return {
        "kind": type(pool).__name__,
        "capacity": capacity,
        "checked_out": checked_out,
        # Connections handed out beyond the kept-warm set: a steady non-zero overflow is
        # the early warning that capacity is being approached.
        "overflow": overflow,
        "size": size,
        "timeout_s": getattr(pool, "_timeout", None),
    }


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


log = get_logger("database")
settings = get_settings()
engine = _make_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create all tables. Safe to call repeatedly."""
    # Import models so they register with Base.metadata.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate()


#: S21b's scores rebuild: the old one-row-per-pair key clause, and the key that replaces it.
_SCORES_OLD_UNIQUE = re.compile(
    r'(?:CONSTRAINT\s+"?\w+"?\s+)?UNIQUE\s*\(\s*"?run_id"?\s*,\s*"?methodology_version"?\s*\)',
    re.IGNORECASE,
)
_SCORES_NEW_UNIQUE = (
    "CONSTRAINT uq_score_run_methodology_measure UNIQUE (run_id, methodology_version, is_at_measure)"
)
SCORES_BACKUP_TABLE = "scores_backup_s21b"


def _scores_has_old_key(cur) -> bool:
    """Whether the live ``scores`` table still has a unique index on exactly
    (run_id, methodology_version) — read from the schema itself, not a flag."""
    for row in cur.execute("PRAGMA index_list(scores)").fetchall():
        name, unique = row[1], row[2]
        if unique:
            cols = [r[2] for r in cur.execute(f'PRAGMA index_info("{name}")').fetchall()]
            if cols == ["run_id", "methodology_version"]:
                return True
    return False


def _migrate_scores_unique(eng) -> bool:
    """Widen ``scores``' unique key to include ``is_at_measure`` (S21b), once.

    SQLite can't alter a table constraint, so the table is rebuilt the documented way: a new
    table from the live table's own DDL with only the key clause swapped (every column, type
    and the run FK kept as they are), every row copied with its id, the old table dropped,
    the new one renamed, and the old table's indexes and triggers recreated — all in one
    transaction, with foreign-key enforcement off around it and checked before commit.

    Before anything changes, ``scores`` is copied to :data:`SCORES_BACKUP_TABLE`, which is
    created only if absent and never overwritten. "Already migrated" is read from the live
    schema (no unique index on exactly the old pair), not from a flag. Returns True when it
    rebuilt the table."""
    raw = eng.raw_connection()
    dbapi = raw.driver_connection
    saved_isolation = dbapi.isolation_level
    dbapi.isolation_level = None  # manual transaction control for the rebuild
    cur = dbapi.cursor()
    fk_on = False
    try:
        if not cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scores'"
        ).fetchone() or not _scores_has_old_key(cur):
            return False  # no table yet (create_all builds the new key) or already migrated
        fk_on = bool(cur.execute("PRAGMA foreign_keys").fetchone()[0])
        if fk_on:
            cur.execute("PRAGMA foreign_keys=OFF")  # a no-op inside a transaction, so first
        cur.execute("BEGIN IMMEDIATE")
        try:
            # Re-check under the write lock: another process may have migrated meanwhile.
            if not _scores_has_old_key(cur):
                cur.execute("ROLLBACK")
                return False
            ddl = cur.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='scores'"
            ).fetchone()[0]
            new_ddl, n = _SCORES_OLD_UNIQUE.subn(_SCORES_NEW_UNIQUE, ddl)
            if n != 1:
                raise RuntimeError(f"scores rebuild: expected one old unique clause, found {n}")
            new_ddl = re.sub(r'^CREATE TABLE\s+"?scores"?', "CREATE TABLE scores_s21b_new", new_ddl, count=1)
            if not cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (SCORES_BACKUP_TABLE,)
            ).fetchone():
                cur.execute(f"CREATE TABLE {SCORES_BACKUP_TABLE} AS SELECT * FROM scores")
            extras = [
                sql for (sql,) in cur.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name='scores' "
                    "AND type IN ('index', 'trigger') AND sql IS NOT NULL"
                ).fetchall()
            ]
            cols = ", ".join(f'"{r[1]}"' for r in cur.execute("PRAGMA table_info(scores)").fetchall())
            before = cur.execute("SELECT count(*) FROM scores").fetchone()[0]
            cur.execute(new_ddl)
            cur.execute(f"INSERT INTO scores_s21b_new ({cols}) SELECT {cols} FROM scores")
            cur.execute("DROP TABLE scores")
            cur.execute("ALTER TABLE scores_s21b_new RENAME TO scores")
            for sql in extras:
                cur.execute(sql)
            after = cur.execute("SELECT count(*) FROM scores").fetchone()[0]
            if after != before:
                raise RuntimeError(f"scores rebuild: {before} rows before, {after} after")
            if cur.execute("PRAGMA foreign_key_check(scores)").fetchone():
                raise RuntimeError("scores rebuild: foreign_key_check failed")
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
    finally:
        if fk_on:
            cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
        dbapi.isolation_level = saved_isolation
        raw.close()
    log.info("scores: unique key now includes is_at_measure (%d rows; backup in %s)",
             after, SCORES_BACKUP_TABLE)
    return True


def _migrate_instrument_health(eng) -> int:
    """S23: copy every instrument-health stamp already on a run row into the derived
    ``instrument_health_verdicts`` table, under the current assessment version, source
    ``run_row``. Additive and idempotent: a run that already has a row for that version is
    skipped, so a restart copies nothing twice. The run columns are left as they are.

    Also keeps the derived table from outliving its run: a trigger removes a run's verdicts
    when the run is deleted (nothing in the app deletes runs, but a cleanup or a test may,
    and SQLite re-issues a deleted max id to the next run). Returns rows copied."""
    from sqlalchemy import text

    from .instrument_health import HEALTH_VERSION

    with eng.begin() as conn:
        if not list(conn.execute(text("PRAGMA table_info(instrument_health_verdicts)"))):
            return 0  # create_all has not made it (non-model engine); nothing to do
        conn.execute(text(
            "CREATE TRIGGER IF NOT EXISTS trg_runs_delete_instrument_health "
            "AFTER DELETE ON runs BEGIN "
            "DELETE FROM instrument_health_verdicts WHERE run_id = OLD.id; END"
        ))
        copied = conn.execute(text(
            "INSERT INTO instrument_health_verdicts (run_id, version, health, source, created_at) "
            "SELECT r.id, :v, r.instrument_health, 'run_row', CURRENT_TIMESTAMP FROM runs r "
            "WHERE json_extract(r.instrument_health, '$.verdict') IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM instrument_health_verdicts h "
            "WHERE h.run_id = r.id AND h.version = :v)"
        ), {"v": HEALTH_VERSION}).rowcount
    if copied:
        log.info("Instrument health: copied %d run-row stamps into the derived table", copied)
    return copied or 0


def _duel_judged_under(matchups) -> str | None:
    """What an old duel row already says it was judged under: its last decided match's
    ``verdict_methodology`` (set when a ledger re-grade re-read it) or ``methodology``."""
    for m in reversed([x for x in (matchups or []) if isinstance(x, dict)]):
        if m.get("verdict"):
            v = m.get("verdict_methodology") or m.get("methodology")
            if v:
                return str(v)
    return None


def _backfill_duel_judged_under(conn) -> int:
    """S25: fill ``duels.judged_under`` on old rows from what their matches record. Only
    NULL rows are read and only a found version is written, so it is idempotent; a row whose
    matches name no version stays NULL (read as the default rules)."""
    import json

    from sqlalchemy import text

    if not list(conn.execute(text("PRAGMA table_info(duels)"))):
        return 0
    done = 0
    for duel_id, raw in conn.execute(text(
        "SELECT id, matchups FROM duels WHERE judged_under IS NULL AND matchups IS NOT NULL"
    )).all():
        try:
            version = _duel_judged_under(json.loads(raw) if isinstance(raw, str) else raw)
        except (TypeError, ValueError):
            version = None
        if version:
            conn.execute(text("UPDATE duels SET judged_under = :v WHERE id = :i"), {"v": version, "i": duel_id})
            done += 1
    if done:
        log.info("Duels: recorded judged_under on %d old session(s) from their matches", done)
    return done


def _migrate_field_records(eng) -> bool:
    """S32: make ``field_records`` append-only. The table itself is new and comes from
    ``create_all``; this adds triggers that refuse UPDATE and DELETE, so a raw record is never
    rewritten or dropped after it arrives. Idempotent. Returns whether the table was there."""
    from sqlalchemy import text

    with eng.begin() as conn:
        if not list(conn.execute(text("PRAGMA table_info(field_records)"))):
            return False  # create_all has not made it (non-model engine); nothing to do
        for when in ("UPDATE", "DELETE"):
            conn.execute(text(
                f"CREATE TRIGGER IF NOT EXISTS trg_field_records_no_{when.lower()} "
                f"BEFORE {when} ON field_records BEGIN "
                "SELECT RAISE(ABORT, 'field_records is append-only'); END"
            ))
    return True


def _migrate(eng=None) -> None:
    """Lightweight migrations for SQLite (additive, plus S21b's one ``scores`` rebuild).

    ``create_all`` never alters existing tables, so columns added after a
    database already exists (e.g. on a deployed volume) must be added by hand.
    This adds any missing columns; it is idempotent and data-preserving.
    """
    eng = eng if eng is not None else engine
    if eng.dialect.name != "sqlite":
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
            "job_group": "VARCHAR(64)",
            "job_group_total": "INTEGER",
            "instrument_health": "JSON",
            "host_pressure": "JSON",
        },
        "benchmark_results": {
            "raw": "JSON",
        },
        "portable_runs": {
            "home_detection": "VARCHAR(16)",
            "egress_ip": "VARCHAR(96)",
            "home_ip": "VARCHAR(96)",
            "source_run_id": "INTEGER",
            "network": "JSON",
        },
        "challenger_races": {
            "incumbent_refreshes": "INTEGER DEFAULT 0",
        },
        "profile_aggregates": {
            "raw": "JSON",
        },
        "duels": {
            "live": "JSON",
            "open_matches": "JSON",
            "mode": "VARCHAR(16)",
            "campaign_id": "INTEGER",
            "explore": "JSON",
            "judged_under": "VARCHAR(64)",  # S25
        },
        "current_tests": {
            "target_fingerprint": "VARCHAR(40)",
        },
        "write_probes": {
            "mode": "VARCHAR(16)",
        },
        "profile_tests": {
            "stage": "VARCHAR(255)",
            "reached_fingerprint": "VARCHAR(40)",
            "target": "JSON",
        },
        "firewall_writes": {
            # ``waited_ms`` (the guard's pacing wait) was here. Nothing paces a write now
            # and the model does not map it, so it is not added to new databases; existing
            # ones keep the column and its rows.
            "gap_ms": "FLOAT",
            "box_gap_ms": "FLOAT",
            "through_gap_ms": "FLOAT",
            "watch": "JSON",
        },
        "explore_recommendations": {
            "unreachable": "BOOLEAN DEFAULT 0",
            "anchor": "FLOAT",
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
        ("ix_runs_job_group", "runs", "job_group"),
        ("ix_benchmark_results_run_id", "benchmark_results", "run_id"),
        ("ix_scores_methodology_version", "scores", "methodology_version"),
        ("ix_link_gaps_at", "link_gaps", "at"),
    ]
    _migrate_scores_unique(eng)
    _migrate_instrument_health(eng)
    _migrate_field_records(eng)
    with eng.begin() as conn:
        for table, columns in new_columns.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if not existing:
                continue  # table doesn't exist yet; create_all handles it
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        _backfill_duel_judged_under(conn)
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
