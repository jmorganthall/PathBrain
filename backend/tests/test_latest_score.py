"""S21a: every reader of a run's score under a version goes through
``methodology.latest_score`` — the newest row per ``(run, methodology)`` pair.

Today a pair holds one row (the unique constraint), so the selector filters nothing and
the rest of the suite pins that it changed no reader's answer. These tests pin the other
half: once a pair holds two rows (S21b keeps the at-measure row beside a re-grade), the
join/group readers count that run once and read its newer row.

Two rows per pair can't be inserted into the shared test DB (the unique constraint), so
each test builds a private in-memory DB whose ``scores`` table is created without it.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import pathbrain.duel as duel_mod
from pathbrain.config_store import get_config
from pathbrain.database import Base
from pathbrain.methodology import ensure_current_methodology, latest_score
from pathbrain.models import Run, RunStatus, Score
from pathbrain.profile_aggregates import _recompute, stamps
from pathbrain.ranking import _completed_runs_with_scores

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _private_db(*, unique: bool):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    if not unique:
        with engine.begin() as conn:
            ddl = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type='table' AND name='scores'")
            ).scalar_one()
            ddl = re.sub(r",\s*CONSTRAINT uq_score_run_methodology\w* UNIQUE \([^)]*\)", "", ddl)
            assert "uq_score_run_methodology" not in ddl
            conn.execute(text("DROP TABLE scores"))
            conn.execute(text(ddl))
    return sessionmaker(bind=engine, expire_on_commit=False)


def _score(run_id, version, overall, at, *, at_measure):
    return Score(
        run_id=run_id, methodology_version=version, is_at_measure=at_measure,
        comparability="exact", axis_scores={"overall": overall},
        subscores={"overall_leg": overall}, metric_values={}, computed_at=at,
    )


def _seed(Session, *, second_row: bool):
    """Two runs of profile ``fpA`` (2 iterations each). Run 1 has an at-measure row at 50
    and, with ``second_row``, a later re-grade at 80; run 2 has one row at 60."""
    with Session() as s:
        version = ensure_current_methodology(s, get_config(s)).version
        r1 = Run(status=RunStatus.COMPLETE, settings_fingerprint="fpA", iterations=2,
                 created_at=T0, settings={})
        r2 = Run(status=RunStatus.COMPLETE, settings_fingerprint="fpA", iterations=2,
                 created_at=T0 + timedelta(hours=1), settings={})
        s.add_all([r1, r2])
        s.flush()
        s.add(_score(r1.id, version, 50.0, T0, at_measure=True))
        s.add(_score(r2.id, version, 60.0, T0 + timedelta(hours=1), at_measure=True))
        s.flush()
        if second_row:
            s.add(_score(r1.id, version, 80.0, T0 + timedelta(days=1), at_measure=False))
        s.commit()
        return version, r1.id, r2.id


@pytest.mark.parametrize("second_row", [False, True])
def test_join_and_group_readers_count_each_run_once(second_row, monkeypatch):
    Session = _private_db(unique=second_row is False)
    version, r1, r2 = _seed(Session, second_row=second_row)
    r1_overall = 80.0 if second_row else 50.0
    r1_scored = T0 + timedelta(days=1) if second_row else T0 + timedelta(hours=1)

    with Session() as s:
        # The fixture really holds the rows it claims (so the next checks mean something).
        raw = s.scalar(select(func.count()).select_from(Score).where(Score.run_id == r1))
        assert raw == (2 if second_row else 1)
        latest = s.scalars(select(Score).where(Score.run_id == r1, latest_score())).all()
        assert [x.axis_scores["overall"] for x in latest] == [r1_overall]

        # profile_aggregates: the stamp and the rollup count two runs, four iterations.
        assert stamps(s, version)["fpA"][:3] == (r2, 2, 4)
        rollup = _recompute(s, version, ["fpA"])["fpA"]
        assert rollup["run_count"] == 2 and rollup["iterations"] == 4
        assert rollup["metrics"]["overall_leg"]["n"] == 2
        assert sorted([rollup["metrics"]["overall_leg"]["median"]]) == [(r1_overall + 60.0) / 2]
        assert rollup["stamp"][3].replace(tzinfo=timezone.utc) == max(
            r1_scored, T0 + timedelta(hours=1)
        )

        # ranking: one (run, score) per run, carrying the newest row.
        got = [(run.id, score.axis_scores["overall"]) for run, score, _ in _completed_runs_with_scores(s)]
        assert got == [(r1, r1_overall), (r2, 60.0)]

    # duel: the single-run reader reads the newest row.
    @contextmanager
    def scope():
        with Session() as s:
            yield s

    monkeypatch.setattr(duel_mod, "session_scope", scope)
    assert duel_mod._run_overall(r1, version) == r1_overall
    assert duel_mod._run_overall(r2, version) == 60.0
