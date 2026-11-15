"""S8: blackout hours for the duel schedule (and the Explore share that rides it)."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from pathbrain import coordinator, duel as duel_mod, scheduler
from pathbrain.config_store import DEFAULT_CONFIG, save_config
from pathbrain.database import session_scope
from pathbrain.schedule import blackout_ranges, in_blackout, in_blackout_now

CHI = ZoneInfo("America/Chicago")


def _at(h, m=0):
    return datetime(2026, 11, 15, h, m, tzinfo=CHI)


def test_blackout_ships_empty_and_is_never_in_force():
    assert DEFAULT_CONFIG["duel"]["blackout"] == []
    assert in_blackout_now(DEFAULT_CONFIG["duel"]) is False


def test_a_daytime_range_is_start_inclusive_end_exclusive():
    r = [(7, 12)]
    assert in_blackout(_at(7), r) and in_blackout(_at(11, 59), r)
    assert not in_blackout(_at(6, 59), r) and not in_blackout(_at(12), r)


def test_a_range_that_straddles_midnight():
    r = [(23, 2)]
    assert in_blackout(_at(23), r) and in_blackout(_at(23, 59), r)
    assert in_blackout(_at(0), r) and in_blackout(_at(1, 59), r)
    assert not in_blackout(_at(2), r) and not in_blackout(_at(22, 59), r)
    assert not in_blackout(_at(12), r)


def test_entries_parse_from_both_shapes_and_bad_ones_are_skipped():
    section = {"blackout": [{"start_hour": 23, "end_hour": 2}, [7, 9], {"start_hour": 5},
                            [3, 3], ["x", 1], [0, 24]]}
    assert blackout_ranges(section) == [(23, 2), (7, 9), (0, 24)]
    assert in_blackout(_at(15), [(0, 24)])


def test_now_is_read_in_the_sections_zone():
    section = {"timezone": "America/Chicago", "blackout": [[23, 2]]}
    # 05:30 UTC is 23:30 in Chicago (CST, UTC-6).
    assert in_blackout_now(section, datetime(2026, 11, 15, 5, 30, tzinfo=timezone.utc))
    assert not in_blackout_now(section, datetime(2026, 11, 15, 9, 0, tzinfo=timezone.utc))


@pytest.fixture()
def _duel_cfg():
    yield
    with session_scope() as s:
        save_config(s, {"duel": {"enabled": False, "continuous": False, "blackout": []}})


def test_the_scheduler_starts_no_session_inside_a_blackout(monkeypatch, _duel_cfg):
    started = []
    monkeypatch.setattr(duel_mod, "start", lambda *a, **k: started.append(k) or 1)
    monkeypatch.setattr(duel_mod, "active", lambda: False)
    monkeypatch.setattr(coordinator, "busy", lambda: False)
    scheduler._state.pop("duel_last_finished", None)
    with session_scope() as s:
        save_config(s, {"duel": {"enabled": True, "continuous": True, "timezone": "UTC",
                                 "blackout": [[0, 24]]}})
    assert scheduler._maybe_run_duel() is False and started == []
    with session_scope() as s:
        save_config(s, {"duel": {"blackout": []}})
    assert scheduler._maybe_run_duel() is True and started[0]["trigger"] == "continuous"


def test_a_scheduled_session_closes_at_the_blackout_and_a_manual_one_does_not(monkeypatch, _duel_cfg):
    from .test_duel_resilience import A, B, _mock_ring, _wait

    _mock_ring(monkeypatch, {A: 70.0, B: 62.0}, lambda fp, n: None)
    with session_scope() as s:
        save_config(s, {"duel": {"timezone": "UTC", "blackout": [[0, 24]]}})
    scheduled = _wait(duel_mod.start(duration_minutes=10, trigger="scheduled"))
    assert scheduled.error is None and not scheduled.matchups

    _mock_ring(monkeypatch, {A: 70.0, B: 62.0}, lambda fp, n: None)
    with session_scope() as s:
        save_config(s, {"duel": {"timezone": "UTC", "blackout": [[0, 24]]}})
    manual = _wait(duel_mod.start(duration_minutes=10))
    assert manual.error is None and manual.matchups
