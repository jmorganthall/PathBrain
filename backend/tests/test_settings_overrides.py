"""S22a: ``/settings/backfill`` records corrections in ``settings_overrides`` beside the
run instead of rewriting the run row, and the ranking/profile readers see the effective
value (the newest override if present, else the run's own)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.methodology import ensure_current_methodology
from pathbrain.models import Run, RunStatus, Score, SettingsOverride
from pathbrain.profile_aggregates import _recompute, stamps
from pathbrain.ranking import _completed_runs_with_scores
from pathbrain.settings_overrides import effective_fingerprint, effective_settings, overlay, record


def _seed_unstamped(n: int) -> list[int]:
    t0 = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=400)
    ids = []
    with session_scope() as s:
        version = ensure_current_methodology(s, get_config(s)).version
        for i in range(n):
            run = Run(status=RunStatus.COMPLETE, created_at=t0 + timedelta(minutes=i), iterations=3)
            s.add(run)
            s.flush()
            s.add(Score(run_id=run.id, methodology_version=version, is_at_measure=True,
                        comparability="exact", axis_scores={"overall": 60.0 + i},
                        subscores={"s22_leg": 60.0 + i}, weights_used={}, metric_values={}))
            ids.append(run.id)
    return ids


def _view(s, version, fp, ids):
    """What the routed readers say about these runs and their profile."""
    stamp = stamps(s, version, [fp]).get(fp)
    rollup = _recompute(s, version, [fp])[fp]
    ranked = {run.id: (run.settings_fingerprint, run.settings)
              for run, _, _ in _completed_runs_with_scores(s) if run.id in ids}
    return stamp, rollup["run_count"], rollup["iterations"], rollup["metrics"].get("s22_leg"), ranked


def test_backfill_writes_overrides_not_the_run_row_and_ranks_as_before(client):
    ids = _seed_unstamped(3)
    resp = client.post("/api/settings/backfill")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"updated", "fingerprint"} and body["updated"] >= 3
    fp = body["fingerprint"]

    with session_scope() as s:
        version = ensure_current_methodology(s, get_config(s)).version
        # The run rows are untouched...
        for run in s.scalars(select(Run).where(Run.id.in_(ids))):
            assert run.settings is None and run.settings_fingerprint is None
        # ...and each run has its two append-only override rows.
        rows = s.scalars(select(SettingsOverride).where(SettingsOverride.run_id.in_(ids))).all()
        assert sorted((r.run_id, r.field) for r in rows) == sorted(
            (i, f) for i in ids for f in ("settings", "settings_fingerprint")
        )
        assert {r.source for r in rows} == {"backfill"}
        normalized = next(r.value for r in rows if r.field == "settings")
        # The accessor's two forms agree on the effective values.
        got = dict(s.execute(
            select(Run.id, effective_fingerprint()).where(Run.id.in_(ids))
        ).all())
        assert got == {i: fp for i in ids}
        assert s.scalars(select(effective_settings()).where(Run.id.in_(ids))).all() == [normalized] * 3
        runs = s.scalars(select(Run).where(Run.id.in_(ids))).all()
        overlay(s, runs)
        assert all(r.settings_fingerprint == fp and r.settings == normalized for r in runs)
        assert not any(s.is_modified(r) for r in runs)  # read, never written back
        via_overrides = _view(s, version, fp, set(ids))

    # A second backfill finds nothing new for these runs (they are effectively stamped).
    assert client.post("/api/settings/backfill").json()["updated"] == 0
    with session_scope() as s:
        assert len(s.scalars(select(SettingsOverride).where(SettingsOverride.run_id.in_(ids))).all()) == 6

    # The same answer as the old backfill: stamp the rows directly (what it used to do),
    # drop the overrides, and every routed reader agrees with what it said above.
    with session_scope() as s:
        for run in s.scalars(select(Run).where(Run.id.in_(ids))):
            run.settings = normalized
            run.settings_fingerprint = fp
        s.execute(delete(SettingsOverride).where(SettingsOverride.run_id.in_(ids)))
    with session_scope() as s:
        version = ensure_current_methodology(s, get_config(s)).version
        assert _view(s, version, fp, set(ids)) == via_overrides
        # Drop every backfill override left behind (other tests delete runs and SQLite reuses
        # ids, so a stray override would attach to an unrelated new run).
        s.execute(delete(SettingsOverride))
    assert via_overrides[4] == {i: (fp, normalized) for i in ids}


def test_the_newest_override_wins_and_the_run_row_is_the_fallback():
    ids = _seed_unstamped(1)
    rid = ids[0]
    with session_scope() as s:
        run = s.get(Run, rid)
        run.settings_fingerprint = "own-fp"
        run.settings = [{"own": True}]
    with session_scope() as s:
        assert s.scalar(select(effective_fingerprint()).where(Run.id == rid)) == "own-fp"
        assert s.scalar(select(effective_settings()).where(Run.id == rid)) == [{"own": True}]
        record(s, rid, "settings_fingerprint", "first-fix", source="test")
        record(s, rid, "settings_fingerprint", "second-fix", source="test")
    with session_scope() as s:
        assert s.scalar(select(effective_fingerprint()).where(Run.id == rid)) == "second-fix"
        # No settings override: the run's own settings still show through.
        assert s.scalar(select(effective_settings()).where(Run.id == rid)) == [{"own": True}]
        assert s.get(Run, rid).settings_fingerprint == "own-fp"  # the row itself is unchanged
        s.execute(delete(SettingsOverride).where(SettingsOverride.run_id == rid))
        s.execute(delete(Score).where(Score.run_id == rid))
        s.execute(delete(Run).where(Run.id == rid))
