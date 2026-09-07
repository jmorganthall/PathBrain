"""What is scheduled to run next, across every source.

The Dashboard could say when the next *monitoring* run was due and nothing else — the one
piece of scheduled work that is small, frequent and unsurprising — while a duel window
opening at 03:00, a nightly unshaped baseline test or an armed experiment announced
themselves only by taking the pipeline. "Nothing is running" and "a duel opens in twenty
minutes" are different states that read identically.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain import schedule

CHICAGO = {"timezone": "America/Chicago"}


def _armed() -> dict:
    return {
        "duel": {"enabled": True, "hour": 3, "minute": 0, "timezone": "America/Chicago",
                 "duration_minutes": 120},
        "baseline_test": {"enabled": True, "hour": 4, "minute": 30,
                          "timezone": "America/Chicago", "iterations": 10},
        "experiment": {"enabled": True, "dry_run": False, "window": {"days": [1, 3], "start_hour": 2}},
        "crown_follow": {"enabled": True, "interval_minutes": 360},
    }


def test_every_scheduled_source_is_listed_not_just_monitoring():
    """The whole point: monitoring was the only thing the Dashboard could see."""
    out = schedule.status(_armed(), {"enabled": True, "interval_minutes": 15,
                                     "next_run_at": "2099-01-01T00:00:00+00:00"})
    kinds = {e["kind"] for e in out["upcoming"]}
    assert kinds == {"monitoring", "duel", "baseline_test", "experiment", "crown_follow"}


def test_a_daily_schedule_is_converted_from_its_own_zone_exactly_once():
    """A schedule is stored in the zone the user saved it from and rendered in the viewer's,
    so the conversion has to happen once and be labelled — a naive string gets double-shifted."""
    at = schedule.next_daily(3, 0, CHICAGO)
    assert at is not None
    assert at.hour == 3 and at.minute == 0          # 03:00 in Chicago...
    assert at.tzinfo is not None                     # ...as a real instant, not a naive time
    assert at > datetime.now(timezone.utc)           # and always in the future


def test_the_soonest_armed_event_is_the_next_one_and_monitoring_never_is():
    """The Monitoring tile already says when the next monitoring run is due; "Next
    scheduled" naming it too was the same fact twice, hiding the overnight work the tile
    exists to announce. So a monitoring run five minutes out is listed but never ``next``."""
    now = datetime.now(timezone.utc)
    soon = (now + timedelta(minutes=5)).isoformat()
    out = schedule.status(_armed(), {"enabled": True, "interval_minutes": 15, "next_run_at": soon})
    assert out["next"]["kind"] in {"duel", "baseline_test", "experiment"}
    monitoring = next(e for e in out["upcoming"] if e["kind"] == "monitoring")
    assert monitoring["enabled"] is True and monitoring["at"] == soon  # still listed, still timed

    # With monitoring off the answer is the same: whichever event clock comes first.
    out = schedule.status(_armed(), {"enabled": False, "interval_minutes": 15, "next_run_at": None})
    assert out["next"] is not None and out["next"]["kind"] in {"duel", "baseline_test", "experiment"}
    assert out["next"]["at"] is not None

    # Only routine work armed → no next event, but the cadence is still in the list.
    out = schedule.status({}, {"enabled": True, "interval_minutes": 15, "next_run_at": soon})
    assert out["next"] is None
    assert any(e["kind"] == "monitoring" and e["enabled"] for e in out["upcoming"])


def test_disarmed_schedules_are_listed_and_marked_never_dropped():
    """"The duel is off" and "the duel is six hours away" are different answers, and a list
    that omits the first cannot tell them apart."""
    out = schedule.status({}, {"enabled": False, "interval_minutes": 15, "next_run_at": None})
    assert out["next"] is None
    duel = next(e for e in out["upcoming"] if e["kind"] == "duel")
    assert duel["enabled"] is False and duel["at"] is None
    # Everything unscheduled still appears, so the card can say what is armed and what isn't.
    assert len(out["upcoming"]) >= 4


def test_continuous_duel_reports_a_cadence_rather_than_inventing_a_time():
    """Continuous mode starts whenever the pipeline is free and the gap has elapsed. There
    is no clock, so naming a time would be making one up."""
    cfg = {"duel": {"enabled": True, "continuous": True, "continuous_gap_minutes": 5}}
    out = schedule.status(cfg, {})
    duel = next(e for e in out["upcoming"] if e["kind"] == "duel")
    assert duel["at"] is None
    assert "continuous" in duel["detail"]


def test_a_window_with_no_allowed_days_has_no_next_occurrence():
    """Rather than a date a fortnight out, found by scanning until the search gave up."""
    assert schedule.next_window([], 2, {}) is None


def test_a_weekday_window_lands_on_an_allowed_day():
    at = schedule.next_window([1, 3], 2, {})  # Tuesday / Thursday
    assert at is not None
    assert at.weekday() in {1, 3} and at.hour == 2
    assert at > datetime.now(at.tzinfo)


def test_the_endpoint_serves_it(client):
    body = client.get("/api/schedule").json()
    assert "next" in body and "upcoming" in body
    assert {e["kind"] for e in body["upcoming"]} >= {"monitoring", "duel", "baseline_test"}
