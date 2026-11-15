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
