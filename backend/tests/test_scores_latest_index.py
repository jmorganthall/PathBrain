"""``ix_scores_run_methodology_id``: the index that serves ``latest_score()``'s probe.

``latest_score()`` (S21a) is a correlated NOT EXISTS on ``(run_id, methodology_version,
id > ?)``. Without an index ending in ``id``, SQLite serves it from
``ix_scores_methodology_version`` as ``(methodology_version=? AND rowid>?)``: a scan of
every newer row under the version, per row read. The field read went quadratic (about 26s
at 5k runs and 107s at 10k on a synthetic DB) and the profiles endpoint passed 60s in
production. These tests pin that the index reaches existing databases, that the planner
uses it, and that it changes no answer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

import pathbrain.api.routes_settings as rs
from pathbrain.database import _make_engine, _migrate, engine, session_scope
from pathbrain.methodology import latest_score
from pathbrain.models import Run, RunStatus, Score

from .test_scores_migration import _old_schema_db
from .test_settings import _seed_run

INDEX = "ix_scores_run_methodology_id"


def _indexes(conn) -> set[str]:
    return {r[1] for r in conn.execute(text("PRAGMA index_list(scores)"))}


def _index_columns(conn) -> list[str]:
    return [r[2] for r in conn.execute(text(f"PRAGMA index_info({INDEX})"))]


def test_migrate_adds_the_index_to_an_existing_database(tmp_path):
    """A database already past S21b (the state production is in) gets the index."""
    eng = _old_schema_db(tmp_path / "old.db", rows=40, foreign_keys=False)
    _migrate(eng)  # the S21b rebuild, as production ran it last week
    with eng.begin() as conn:
        conn.execute(text(f"DROP INDEX IF EXISTS {INDEX}"))  # ...before this index existed
        before = [tuple(r) for r in conn.execute(text("SELECT * FROM scores ORDER BY id"))]
        assert INDEX not in _indexes(conn)

    _migrate(eng)

    with eng.connect() as conn:
        assert INDEX in _indexes(conn)
        assert _index_columns(conn) == ["run_id", "methodology_version", "id"]
        assert [tuple(r) for r in conn.execute(text("SELECT * FROM scores ORDER BY id"))] == before
    _migrate(eng)  # idempotent
    eng.dispose()


def test_a_pre_s21b_database_ends_with_the_index_too(tmp_path):
    eng = _old_schema_db(tmp_path / "older.db", rows=40, foreign_keys=False)
    _migrate(eng)
    with eng.connect() as conn:
        assert INDEX in _indexes(conn)
    eng.dispose()


def test_a_fresh_database_has_the_index(tmp_path):
    from pathbrain.database import Base

    eng = _make_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(eng)
    with eng.connect() as conn:
        assert INDEX in _indexes(conn)
    eng.dispose()


def _plan(sql_stmt) -> list[str]:
    compiled = sql_stmt.compile(engine, compile_kwargs={"literal_binds": True})
    with engine.connect() as conn:
        return [r[3] for r in conn.execute(text(f"EXPLAIN QUERY PLAN {compiled}"))]


def test_the_latest_score_probe_seeks_the_index():
    """The regression guard: the NOT EXISTS probe is a seek on all three columns, never a
    range scan of the version's rows."""
    stmt = (
        select(Run.id, Score.id)
        .join(Score, Score.run_id == Run.id)
        .where(Run.status == RunStatus.COMPLETE, Score.methodology_version == "v", latest_score())
    )
    plan = _plan(stmt)
    probe = [step for step in plan if "scores_1" in step]
    assert probe, plan
    assert all(INDEX in step and "id>?" in step for step in probe), plan
    assert not any("rowid>?" in step for step in plan), plan


def test_the_field_is_identical_with_and_without_the_index():
    """Same rows, same order, same field: the index changes the plan, nothing else."""
    t0 = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=3)
    fps = [f"ixl{i:09d}" for i in range(3)]
    for i in range(18):
        _seed_run(fps[i % 3], 60.0 + i, t0 + timedelta(hours=i), overall=55.0 + i, iterations=3)

    def field():
        with session_scope() as s:
            rows = [
                (run.id, score.id) for run, score, _ in rs._completed_runs_with_scores(s)
            ]
            return rows, rs._compute_profiles_uncached(s, True, -360, None, True)

    try:
        with_index = field()
        with engine.begin() as conn:
            conn.execute(text(f"DROP INDEX {INDEX}"))
        without_index = field()
        assert with_index == without_index
        assert {p["fingerprint"] for p in with_index[1]["profiles"]} >= set(fps)
    finally:
        _migrate()
        with session_scope() as s:
            for run in s.query(Run).filter(Run.settings_fingerprint.like("ixl%")).all():
                for score in s.query(Score).filter(Score.run_id == run.id).all():
                    s.delete(score)
                s.delete(run)
        rs.invalidate_profiles_cache()
