"""`slow_profiles` is cached between reads, and the cache lets go the moment its inputs
move: a run completes, a health verdict is written, or the asked-for profiles change."""
from __future__ import annotations

from datetime import datetime
from unittest import mock

import pytest
from sqlalchemy import delete

from pathbrain import instrument_health as ih
from pathbrain.database import session_scope
from pathbrain.models import InstrumentHealthVerdict, Run, RunStatus

FPS = ["cachefield", "cacheslow", "cachefair"]


@pytest.fixture()
def field():
    with session_scope() as s:
        s.query(Run).filter(Run.settings_fingerprint.in_(FPS)).delete(synchronize_session=False)
        for i in range(40):
            s.add(Run(status=RunStatus.COMPLETE, settings_fingerprint="cachefield",
                      created_at=datetime(2026, 10, 1, i % 24),
                      instrument_health={"verdict": "healthy", "ratio": 1.0 + i / 100}))
        for _ in range(8):
            s.add(Run(status=RunStatus.COMPLETE, settings_fingerprint="cacheslow",
                      created_at=datetime(2026, 10, 30, 10),
                      instrument_health={"verdict": "healthy", "ratio": 1.6}))
        for i in range(8):
            s.add(Run(status=RunStatus.COMPLETE, settings_fingerprint="cachefair",
                      created_at=datetime(2026, 10, 30, 15),
                      instrument_health={"verdict": "healthy", "ratio": 1.05 + i / 50}))
    ih._SLOW_CACHE.update({"key": None, "value": None})
    yield
    with session_scope() as s:
        ids = [r.id for r in s.query(Run.id).filter(Run.settings_fingerprint.in_(FPS))]
        s.execute(delete(InstrumentHealthVerdict).where(InstrumentHealthVerdict.run_id.in_(ids)))
        s.query(Run).filter(Run.id.in_(ids)).delete(synchronize_session=False)
    ih._SLOW_CACHE.update({"key": None, "value": None})


def _scan_count(fps):
    with mock.patch.object(ih, "_slow_profiles", side_effect=ih._slow_profiles) as spy:
        with session_scope() as s:
            got = ih.slow_profiles(s, fps)
    return got, spy.call_count


def test_a_repeat_read_reuses_the_answer(field):
    assert _scan_count(["cacheslow", "cachefair"]) == ({"cacheslow"}, 1)
    assert _scan_count(["cachefair", "cacheslow"]) == ({"cacheslow"}, 0)  # order doesn't matter


def test_other_profiles_are_a_fresh_read(field):
    _scan_count(["cacheslow", "cachefair"])
    assert _scan_count(["cachefair"]) == (set(), 1)


def test_a_completed_run_lets_the_cache_go(field):
    _scan_count(["cacheslow", "cachefair"])
    with session_scope() as s:
        for _ in range(12):  # the fair profile now mostly measured on a slow machine
            s.add(Run(status=RunStatus.COMPLETE, settings_fingerprint="cachefair",
                      created_at=datetime(2026, 10, 31, 10),
                      instrument_health={"verdict": "healthy", "ratio": 1.7}))
    got, scans = _scan_count(["cacheslow", "cachefair"])
    assert scans == 1 and "cachefair" in got  # (the slow runs also lift the field's cutoff)


def test_a_new_verdict_lets_the_cache_go(field):
    _scan_count(["cacheslow", "cachefair"])
    with session_scope() as s:
        ids = [r.id for r in s.query(Run.id).filter(Run.settings_fingerprint == "cacheslow")]
        for rid in ids:  # re-assessed: the slow profile's runs were fine after all
            s.add(InstrumentHealthVerdict(run_id=rid, version=ih.HEALTH_VERSION, source="assess",
                                          health={"verdict": "healthy", "ratio": 1.0}))
    assert _scan_count(["cacheslow", "cachefair"]) == (set(), 1)


def test_a_cached_answer_is_a_copy(field):
    with session_scope() as s:
        first = ih.slow_profiles(s, ["cacheslow", "cachefair"])
        first.add("tampered")
        assert ih.slow_profiles(s, ["cacheslow", "cachefair"]) == {"cacheslow"}


def _ratio_loads():
    """(answer, the ``after`` of each ratio load the read made) for a fresh slow read."""
    ih._SLOW_CACHE.update({"key": None, "value": None})
    with mock.patch.object(ih, "_load_ratios", side_effect=ih._load_ratios) as spy:
        with session_scope() as s:
            got = ih.slow_profiles(s, ["cacheslow", "cachefair"])
    return got, [c.kwargs.get("after") for c in spy.call_args_list]


def test_a_new_run_reads_only_that_run(field):
    _, loads = _ratio_loads()
    with session_scope() as s:
        for _ in range(12):
            s.add(Run(status=RunStatus.COMPLETE, settings_fingerprint="cachefair",
                      created_at=datetime(2026, 10, 31, 10),
                      instrument_health={"verdict": "healthy", "ratio": 1.7}))
    got, loads = _ratio_loads()
    assert len(loads) == 1 and loads[0] is not None  # appended, not re-read
    ih.invalidate_ratio_cache()
    assert _ratio_loads() == (got, [None])  # a full re-read agrees


def test_a_verdict_under_the_loaded_runs_rereads_them(field):
    _ratio_loads()
    with session_scope() as s:
        rid = s.query(Run.id).filter(Run.settings_fingerprint == "cacheslow").first()[0]
        s.add(InstrumentHealthVerdict(run_id=rid, version=ih.HEALTH_VERSION, source="assess",
                                      health={"verdict": "healthy", "ratio": 1.0}))
    _, loads = _ratio_loads()
    assert loads == [None]


def test_a_reused_run_id_rereads(field):
    _ratio_loads()
    with session_scope() as s:
        top = s.query(Run).order_by(Run.id.desc()).first()
        top_id, top_fp = top.id, top.settings_fingerprint
        s.delete(top)
    with session_scope() as s:  # SQLite hands the freed top id out again
        s.add(Run(status=RunStatus.COMPLETE, settings_fingerprint=top_fp,
                  created_at=datetime(2026, 11, 2, 9),
                  instrument_health={"verdict": "healthy", "ratio": 1.1}))
        s.flush()
        assert s.query(Run.id).order_by(Run.id.desc()).first()[0] == top_id
    _, loads = _ratio_loads()
    assert loads == [None]
