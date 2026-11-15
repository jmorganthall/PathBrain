"""S21b: ``_migrate`` widens ``scores``' unique key to include ``is_at_measure``, once.

Each test builds its own file-backed SQLite DB with the **old** ``scores`` schema (the
live DDL with the old one-row-per-pair key put back, plus the index the app adds and a
trigger, so the rebuild's "recreate indexes and triggers" step is exercised), migrates it,
and checks that every row and id survives, the backup holds identical rows, and a second
``_migrate`` changes nothing.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, text

from pathbrain import database as db
from pathbrain.database import Base, SCORES_BACKUP_TABLE, _make_engine, _migrate
from pathbrain.models import Run, RunStatus

NEW_KEY = "CONSTRAINT uq_score_run_methodology_measure UNIQUE (run_id, methodology_version, is_at_measure)"
OLD_KEY = "CONSTRAINT uq_score_run_methodology UNIQUE (run_id, methodology_version)"


def _old_schema_db(path, *, rows: int, foreign_keys: bool):
    eng = _make_engine(f"sqlite:///{path}")
    if foreign_keys:
        @event.listens_for(eng, "connect")
        def _fk(dbapi_conn, _record):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        ddl = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='scores'")
        ).scalar_one()
        assert NEW_KEY in ddl
        conn.execute(text("DROP TABLE scores"))
        conn.execute(text(ddl.replace(NEW_KEY, OLD_KEY)))
        conn.execute(text("CREATE INDEX ix_scores_methodology_version ON scores (methodology_version)"))
        conn.execute(text(
            "CREATE TRIGGER trg_scores_probe AFTER INSERT ON scores BEGIN SELECT 1; END"
        ))
        n_runs = max(rows // 2, 1)
        conn.execute(
            Run.__table__.insert(),
            [{"id": i, "status": RunStatus.COMPLETE, "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
             for i in range(1, n_runs + 1)],
        )
        payload = json.dumps({"overall": 71.5, "speed": 70.0})
        conn.execute(
            text(
                "INSERT INTO scores (id, run_id, methodology_version, is_at_measure, comparability,"
                " axis_scores, subscores, weights_used, metric_values, computed_at) VALUES"
                " (:id, :run, :ver, :am, 'exact', :ax, :ax, '{}', '{}', :at)"
            ),
            [
                # Ids with gaps (3, 6, 9, ...), two versions per run: ids must survive as-is.
                {"id": 3 * (i + 1), "run": i // 2 + 1, "ver": f"v{i % 2}", "am": i % 2 == 0,
                 "ax": payload, "at": "2026-01-02 00:00:00"}
                for i in range(rows)
            ],
        )
    return eng


def _dump(conn, table):
    return [tuple(r) for r in conn.execute(text(f"SELECT * FROM {table} ORDER BY id"))]


def _schema(conn):
    return {
        (r[0], r[1]): (r[2], r[3])
        for r in conn.execute(text("SELECT type, name, sql, rootpage FROM sqlite_master"))
    }


@pytest.mark.parametrize("foreign_keys", [False, True])
def test_migrate_rebuilds_scores_once_and_keeps_every_row(tmp_path, foreign_keys):
    eng = _old_schema_db(tmp_path / "old.db", rows=200, foreign_keys=foreign_keys)
    with eng.connect() as conn:
        before = _dump(conn, "scores")
    assert len(before) == 200

    _migrate(eng)

    with eng.connect() as conn:
        ddl = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='scores'")
        ).scalar_one()
        assert NEW_KEY in ddl and OLD_KEY not in ddl
        assert _dump(conn, "scores") == before              # every row, every id, every column
        assert _dump(conn, SCORES_BACKUP_TABLE) == before   # the backup is a faithful copy
        names = {name for (_, name) in _schema(conn)}
        assert {"ix_scores_methodology_version", "trg_scores_probe"} <= names
        assert "scores_s21b_new" not in names
        fks = list(conn.execute(text("PRAGMA foreign_key_list(scores)")))
        assert [(r[2], r[3], r[4]) for r in fks] == [("runs", "run_id", "id")]
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == int(foreign_keys)
        schema_after_first = _schema(conn)

    # The new key admits the at-present row beside an at-measure row...
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO scores (run_id, methodology_version, is_at_measure, comparability,"
            " axis_scores, subscores, weights_used, metric_values, computed_at)"
            " VALUES (1, 'v0', 0, 'exact', '{}', '{}', '{}', '{}', '2026-01-03 00:00:00')"
        ))
    # ...and still refuses a second at-measure row for the pair.
    with pytest.raises(Exception, match="UNIQUE"):
        with eng.begin() as conn:
            conn.execute(text(
                "INSERT INTO scores (run_id, methodology_version, is_at_measure, comparability,"
                " axis_scores, subscores, weights_used, metric_values, computed_at)"
                " VALUES (1, 'v0', 1, 'exact', '{}', '{}', '{}', '{}', '2026-01-03 00:00:00')"
            ))

    # A second _migrate is a no-op: no rebuild (same root pages), backup untouched.
    with eng.connect() as conn:
        backup_before = _dump(conn, SCORES_BACKUP_TABLE)
    assert db._migrate_scores_unique(eng) is False
    _migrate(eng)
    with eng.connect() as conn:
        assert _schema(conn) == schema_after_first
        assert _dump(conn, SCORES_BACKUP_TABLE) == backup_before
    eng.dispose()


def test_an_existing_backup_table_is_never_overwritten(tmp_path):
    eng = _old_schema_db(tmp_path / "old.db", rows=10, foreign_keys=False)
    with eng.begin() as conn:
        conn.execute(text(f"CREATE TABLE {SCORES_BACKUP_TABLE} (marker TEXT)"))
        conn.execute(text(f"INSERT INTO {SCORES_BACKUP_TABLE} VALUES ('earlier backup')"))
        before = _dump(conn, "scores")
    assert db._migrate_scores_unique(eng) is True
    with eng.connect() as conn:
        assert [tuple(r) for r in conn.execute(text(f"SELECT * FROM {SCORES_BACKUP_TABLE}"))] == [
            ("earlier backup",)
        ]
        assert _dump(conn, "scores") == before
    eng.dispose()


def test_a_fresh_database_needs_no_rebuild(tmp_path):
    eng = _make_engine(f"sqlite:///{tmp_path / 'new.db'}")
    Base.metadata.create_all(eng)
    assert db._migrate_scores_unique(eng) is False
    with eng.connect() as conn:
        assert conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE name=:n"), {"n": SCORES_BACKUP_TABLE}
        ).first() is None
    eng.dispose()
