"""The field's rows are read once and shared until the history they came from moves.

The background rebuilds the field after every run, so the Profiles page usually finds the
newest run's rows already loaded and skips the read that was most of its first load. These
tests pin that the shared rows are never stale: a completed run, a new score, an override
or an explicit invalidation each make the next reader load afresh."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete

from pathbrain import ranking
from pathbrain.api import routes_settings
from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.methodology import ensure_current_methodology
from pathbrain.models import BenchmarkResult, Run, RunStatus, Score, SettingsOverride
from pathbrain.phase_timing import recording

FP = "rowscache-a"
T0 = datetime(2026, 10, 1, 12)


def _add_run(s, version, i, fp=FP):
    run = Run(status=RunStatus.COMPLETE, settings_fingerprint=fp, settings={"p": [i]},
              created_at=T0 + timedelta(minutes=i), iterations=2)
    s.add(run)
    s.flush()
    s.add(Score(run_id=run.id, methodology_version=version, is_at_measure=True,
                comparability="exact", axis_scores={"overall": 60.0 + i},
                subscores={}, weights_used={}, metric_values={}))
    s.add(BenchmarkResult(run_id=run.id, plugin="browser", metrics={"lcp": 100.0 + i}))
    return run.id


@pytest.fixture()
def seeded():
    with session_scope() as s:
        version = ensure_current_methodology(s, get_config(s)).version
        ids = [_add_run(s, version, i) for i in range(3)]
    ranking.invalidate_rows_cache()
    yield version, ids
    with session_scope() as s:
        all_ids = [r for (r,) in s.query(Run.id).filter(Run.settings_fingerprint.like("rowscache-%"))]
        s.execute(delete(SettingsOverride).where(SettingsOverride.run_id.in_(all_ids)))
        s.execute(delete(BenchmarkResult).where(BenchmarkResult.run_id.in_(all_ids)))
        s.execute(delete(Score).where(Score.run_id.in_(all_ids)))
        s.query(Run).filter(Run.id.in_(all_ids)).delete(synchronize_session=False)
    ranking.invalidate_rows_cache()


def _mine(rows):
    return {
        run.id: (run.settings_fingerprint, run.iterations, score.axis_scores["overall"], res)
        for run, score, res in rows
        if run.settings_fingerprint.startswith("rowscache-")
    }


def _read(how="field_rows-cached"):
    """(rows for our runs as plain tuples, whether the read went the ``how`` way)."""
    with session_scope() as s, recording() as rec:
        rows = ranking._completed_runs_with_scores(s)
    return _mine(rows), how in rec


def _full_read():
    """The same read with nothing shared: what an appended read has to equal, in order."""
    ranking.invalidate_rows_cache()
    with session_scope() as s:
        rows = ranking._completed_runs_with_scores(s)
    return [(run.id, score.id, res) for run, score, res in rows]


def test_a_repeat_read_is_served_from_the_shared_rows(seeded):
    _, ids = seeded
    first, cached = _read()
    assert not cached and set(first) == set(ids)
    assert first[ids[0]] == (FP, 2, 60.0, {"browser": {"lcp": 100.0}})
    again, cached = _read()
    assert cached and again == first


def test_a_completed_run_is_appended_not_reread(seeded):
    version, ids = seeded
    _read()
    with session_scope() as s:
        new_id = _add_run(s, version, 3)
        # Completed later but stamped earlier than every other run: it lands in time order.
        early_id = _add_run(s, version, -5)
    with session_scope() as s, recording() as rec:
        rows = ranking._completed_runs_with_scores(s)
    assert "field_rows-appended" in rec and "field_rows-runs_scores" in rec
    assert set(_mine(rows)) == set(ids) | {new_id, early_id}
    appended = [(run.id, score.id, res) for run, score, res in rows]
    assert appended == _full_read()
    assert [r[0] for r in appended if r[0] in set(ids) | {new_id, early_id}][0] == early_id


def test_a_run_that_completes_out_of_order_rereads_everything(seeded):
    version, ids = seeded
    with session_scope() as s:
        late = Run(status=RunStatus.RUNNING, settings_fingerprint=FP, settings={"p": []},
                   created_at=T0, iterations=2)
        s.add(late)
        s.flush()
        late_id = late.id
        _add_run(s, version, 4)  # a newer run completes first
    _read()
    with session_scope() as s:
        s.get(Run, late_id).status = RunStatus.COMPLETE
        s.add(Score(run_id=late_id, methodology_version=version, is_at_measure=True,
                    comparability="exact", axis_scores={"overall": 1.0},
                    subscores={}, weights_used={}, metric_values={}))
    got, appended = _read("field_rows-appended")
    assert not appended and got[late_id][2] == 1.0


def test_a_new_score_row_is_read_fresh(seeded):
    version, ids = seeded
    _read()
    with session_scope() as s:
        # A re-grade writes the at-present row beside the at-measure one; it is the newest.
        s.add(Score(run_id=ids[0], methodology_version=version, is_at_measure=False,
                    comparability="exact", axis_scores={"overall": 99.0},
                    subscores={}, weights_used={}, metric_values={}))
    got, cached = _read()
    assert not cached and got[ids[0]][2] == 99.0
    _, appended = _read("field_rows-appended")
    assert not appended


def test_an_override_is_read_fresh(seeded):
    _, ids = seeded
    _read()
    with session_scope() as s:
        s.add(SettingsOverride(run_id=ids[1], field="settings_fingerprint",
                               value="rowscache-b", source="test"))
    got, cached = _read()
    assert not cached and got[ids[1]][0] == "rowscache-b" and got[ids[0]][0] == FP


def test_invalidating_the_profiles_cache_drops_the_rows(seeded):
    _read()
    routes_settings.invalidate_profiles_cache()
    _, cached = _read()
    assert not cached


def test_callers_get_their_own_list(seeded):
    with session_scope() as s:
        rows = ranking._completed_runs_with_scores(s)
        rows.clear()
        assert ranking._completed_runs_with_scores(s)
