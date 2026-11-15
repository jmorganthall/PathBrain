"""``_migrate`` adds every listed column to an existing database.

The Nov 15 outage: ``new_columns`` named ``"duels"`` twice, so the second entry replaced the
first and S25's ``duels.judged_under`` was never added to a deployed database. The backfill
then read the missing column and startup failed. Fresh test databases get the column from
``create_all``, so nothing else caught it.
"""
from __future__ import annotations

import ast
import inspect
import json
import textwrap

from sqlalchemy import text

from pathbrain import database as db
from pathbrain.database import Base, _make_engine, _migrate


def _new_columns_literal() -> ast.Dict:
    tree = ast.parse(textwrap.dedent(inspect.getsource(db._migrate)))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "new_columns":
            return node.value
    raise AssertionError("new_columns not found in _migrate")


def test_no_table_is_listed_twice():
    keys = [k.value for k in _new_columns_literal().keys]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    assert not dupes, f"listed twice in new_columns (the later entry silently wins): {dupes}"


def test_an_existing_duels_table_gains_judged_under_and_is_backfilled(tmp_path):
    eng = _make_engine(f"sqlite:///{tmp_path / 'old.db'}")
    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE duels DROP COLUMN judged_under"))  # a pre-S25 table
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(duels)"))}
        assert "judged_under" not in cols
        conn.execute(
            text("INSERT INTO duels (id, status, \"trigger\", duration_s, iterations_run, matchups, created_at) "
                 "VALUES (1, 'COMPLETED', 'manual', 60, 1, :m, CURRENT_TIMESTAMP)"),
            {"m": json.dumps([{"verdict": "challenger", "methodology": "v17"}])},
        )

    _migrate(eng)  # failed with "no such column: judged_under" before the fix
    _migrate(eng)  # and stays idempotent

    with eng.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(duels)"))}
        assert "judged_under" in cols
        assert conn.execute(text("SELECT judged_under FROM duels WHERE id = 1")).scalar_one() == "v17"
