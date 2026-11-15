"""The field's rollup stamps are kept between reads and never go stale.

The grouped query behind `profile_aggregates.stamps` scans every comparable run, and the
fused ranking reads the whole field's stamps on every Profiles load. They are now kept and
re-checked cheaply: a repeat read is served, a new completed run is merged in, and anything
that moves under the runs already read (a re-grade in place, a new score on an old run, an
invalidation) reads them afresh. Every test compares against the uncached query."""
from __future__ import annotations

from sqlalchemy import delete

from pathbrain import profile_aggregates as pa
from pathbrain.database import session_scope
from pathbrain.methodology import upsert_score
from pathbrain.models import ProfileAggregate, Run, RunStatus, Score
from pathbrain.phase_timing import recording

VERSION = "stampcache-v1"


def _add_run(fp: str, fcp: float = 50.0, iterations: int = 2) -> int:
    with session_scope() as s:
        run = Run(status=RunStatus.COMPLETE, iterations=iterations, settings_fingerprint=fp)
        s.add(run)
        s.flush()
        s.add(Score(run_id=run.id, methodology_version=VERSION, is_at_measure=True,
                    comparability="exact", subscores={"fcp": fcp}, axis_scores={},
                    weights_used={}, metric_values={}))
        return run.id


def _cleanup() -> None:
    with session_scope() as s:
        ids = [r for (r,) in s.query(Run.id).filter(Run.settings_fingerprint.like("stampcache-%"))]
        s.execute(delete(Score).where(Score.run_id.in_(ids)))
        s.query(Run).filter(Run.id.in_(ids)).delete(synchronize_session=False)
        s.execute(delete(ProfileAggregate).where(ProfileAggregate.methodology_version == VERSION))
    pa.invalidate_stamps_cache()


def _read():
    """(the kept stamps, the uncached stamps, the laps the kept read took)."""
    with session_scope() as s, recording() as rec:
        kept = pa._field_stamps(s, VERSION)
    with session_scope() as s:
        direct = pa.stamps(s, VERSION)
    return kept, direct, rec


def test_a_repeat_read_is_served_and_equals_the_query():
    try:
        _cleanup()
        _add_run("stampcache-a")
        _add_run("stampcache-b")
        kept, direct, rec = _read()
        assert kept == direct and "stampcache-a" in kept
        assert "aggregates-stamps_cached" not in rec
        kept, direct, rec = _read()
        assert kept == direct
        assert "aggregates-stamps_cached" in rec
    finally:
        _cleanup()


def test_new_completed_runs_are_merged_in_not_rescanned():
    try:
        _cleanup()
        _add_run("stampcache-a", iterations=3)
        _read()
        _add_run("stampcache-a", iterations=5)   # a profile already held
        _add_run("stampcache-c")                 # a profile new to the field
        kept, direct, rec = _read()
        assert "aggregates-stamps_appended" in rec
        assert kept == direct
        assert kept["stampcache-a"][1:3] == (2, 8)
    finally:
        _cleanup()


def test_a_regrade_in_place_reads_afresh():
    """`upsert_score` rewrites the at-present row and moves only its ``computed_at`` — no
    id, no count. The kept stamps must still notice, and the rollup with them."""
    try:
        _cleanup()
        rid = _add_run("stampcache-a", fcp=40.0)
        with session_scope() as s:
            upsert_score(s, rid, VERSION, is_at_measure=False, comparability="exact",
                         subscores={"fcp": 41.0}, axis_scores={}, weights_used={},
                         metric_values={})
        before, _, _ = _read()
        with session_scope() as s:                  # exactly what a re-grade does
            upsert_score(s, rid, VERSION, is_at_measure=False, comparability="exact",
                         subscores={"fcp": 95.0}, axis_scores={}, weights_used={},
                         metric_values={})
        kept, direct, rec = _read()
        assert "aggregates-stamps_cached" not in rec
        assert "aggregates-stamps_appended" not in rec
        assert kept == direct and kept["stampcache-a"] != before["stampcache-a"]
        with session_scope() as s:
            agg = pa.aggregates(s, VERSION, persist=False)
        assert agg["stampcache-a"]["metrics"]["fcp"]["median"] == 95.0
    finally:
        _cleanup()


def test_a_new_score_on_an_old_run_reads_afresh():
    try:
        _cleanup()
        rid = _add_run("stampcache-a")
        _add_run("stampcache-b")
        _read()
        with session_scope() as s:   # a re-grade's first at-present row beside the capture
            s.add(Score(run_id=rid, methodology_version=VERSION, is_at_measure=False,
                        comparability="incomparable", subscores={}, axis_scores={},
                        weights_used={}, metric_values={}))
        kept, direct, rec = _read()
        assert "aggregates-stamps_cached" not in rec
        assert kept == direct and "stampcache-a" not in kept
    finally:
        _cleanup()


def test_invalidation_drops_the_kept_stamps():
    try:
        _cleanup()
        _add_run("stampcache-a")
        _read()
        pa.invalidate(version=VERSION)
        kept, direct, rec = _read()
        assert "aggregates-stamps_cached" not in rec and kept == direct
    finally:
        _cleanup()


def test_a_large_request_reads_through_the_field_and_a_small_one_does_not():
    try:
        _cleanup()
        _add_run("stampcache-a")
        many = ["stampcache-a"] + [f"stampcache-none-{i}" for i in range(pa._FIELD_MIN)]
        with session_scope() as s, recording() as rec:
            pa.aggregates(s, VERSION, ["stampcache-a"], persist=False)
        assert not any(k.startswith("aggregates-") for k in rec)
        with session_scope() as s, recording() as rec:
            out = pa.aggregates(s, VERSION, many, persist=False)
        assert any(k.startswith("aggregates-stamps") for k in rec)
        assert out["stampcache-a"]["run_count"] == 1 and out["stampcache-none-0"]["run_count"] == 0
    finally:
        _cleanup()
