"""The nightly schedules must survive a busy pipeline.

Reported as "our overnight duels didn't run at all". Two things combined to make a whole
night contingent on one 60-second window being clear:

  1. The scheduler abandoned the rest of its tick whenever the pipeline was busy, so it
     never reached the duel check at all.
  2. The nightly gate fired only on an exact hour:minute match, with no catch-up.

So any session running across 03:00 — a monitoring run, or a queue of profile tests —
meant the ladder silently did not run, with nothing logged and nothing to notice.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain import scheduler


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 7, hour, minute, tzinfo=timezone.utc)


def test_a_schedule_is_due_from_its_minute_onward_not_only_during_it():
    """The exact-minute gate is what made a busy 03:00 cost the entire night."""
    assert scheduler._schedule_due(_at(3, 0), 3, 0) is True
    # Still due a few minutes late — the tick that would have caught it was busy.
    assert scheduler._schedule_due(_at(3, 5), 3, 0) is True
    assert scheduler._schedule_due(_at(3, 45), 3, 0) is True


def test_it_is_not_due_before_its_time():
    assert scheduler._schedule_due(_at(2, 59), 3, 0) is False
    assert scheduler._schedule_due(_at(0, 0), 3, 0) is False


def test_the_catch_up_is_bounded_so_a_restart_does_not_kick_a_duel_at_lunchtime():
    """"Past the scheduled time and not yet run today" alone would fire on any restart,
    hours late, which is not the night's run in any meaningful sense."""
    edge = _at(3, 0) + timedelta(minutes=scheduler.SCHEDULE_CATCHUP_MINUTES)
    assert scheduler._schedule_due(edge, 3, 0) is False
    assert scheduler._schedule_due(_at(14, 0), 3, 0) is False


def test_the_nightly_checks_run_before_the_busy_gate():
    """Both engines queue on the coordinator themselves, so a busy pipeline must not stop
    them being *started* — only when they measure. Ordering is the fix; this reads the
    loop source because the ordering is the behaviour."""
    import inspect

    src = inspect.getsource(scheduler)
    body = src[src.index("def _loop"):]
    baseline_at = body.index("_maybe_run_baseline()")
    duel_at = body.index("_maybe_run_duel()")
    busy_at = body.index("if coordinator.busy():")
    assert baseline_at < busy_at, "the baseline check must not sit behind the busy gate"
    assert duel_at < busy_at, "the duel check must not sit behind the busy gate"


def test_a_busy_pipeline_no_longer_skips_the_nightly_duel(monkeypatch):
    """End to end through the real gate: the pipeline is held, and the duel is still
    kicked (its own thread then queues for the lock)."""
    from pathbrain import coordinator, duel
    from pathbrain.config_store import save_config
    from pathbrain.database import session_scope

    started: list = []
    monkeypatch.setattr(duel, "start", lambda minutes, trigger=None: started.append(trigger) or 1)
    monkeypatch.setattr(duel, "active", lambda: False)

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        save_config(s, {"duel": {
            "enabled": True, "continuous": False, "timezone": "UTC",
            "hour": now.hour, "minute": now.minute, "duration_minutes": 120,
        }})
    scheduler._state.pop("duel_last_date", None)

    with coordinator.hold("profile-test#1"):   # the pipeline is busy, as it was at 03:00
        assert scheduler._maybe_run_duel() is True
    assert started == ["scheduled"]

    # And only once for the day, however many ticks land inside the catch-up window.
    assert scheduler._maybe_run_duel() is False
    scheduler._state.pop("duel_last_date", None)


def test_a_window_that_opens_on_a_running_duel_is_said_once_not_swallowed(monkeypatch, caplog):
    """A nightly window that opens while a session already holds the ring (a resumed
    remainder, a lever session) used to return silently — which reads, the next morning, as
    "our overnight duel didn't fire at all". It is now logged once per window."""
    import logging

    from pathbrain import duel
    from pathbrain.config_store import save_config
    from pathbrain.database import session_scope

    monkeypatch.setattr(duel, "active", lambda: True)
    monkeypatch.setattr(duel, "current", lambda: {"id": 77})
    monkeypatch.setattr(duel, "start", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not start")))
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        save_config(s, {"duel": {
            "enabled": True, "continuous": False, "timezone": "UTC",
            "hour": now.hour, "minute": now.minute, "duration_minutes": 120,
        }})
    scheduler._state.pop("duel_last_date", None)
    scheduler._state.pop("duel_skip_logged", None)
    try:
        with caplog.at_level(logging.WARNING, logger="pathbrain.scheduler"):
            assert scheduler._maybe_run_duel() is False
            assert scheduler._maybe_run_duel() is False
        said = [r for r in caplog.records if "already running (duel #77)" in r.getMessage()]
        assert len(said) == 1, "said once per window, not every tick"
        # Outside the window an active duel is simply an active duel — nothing to say.
        caplog.clear()
        with session_scope() as s:
            save_config(s, {"duel": {"hour": (now.hour + 12) % 24, "minute": now.minute}})
        scheduler._state.pop("duel_skip_logged", None)
        with caplog.at_level(logging.WARNING, logger="pathbrain.scheduler"):
            assert scheduler._maybe_run_duel() is False
        assert not [r for r in caplog.records if "already running" in r.getMessage()]
    finally:
        scheduler._state.pop("duel_skip_logged", None)
        scheduler._state.pop("duel_last_date", None)
