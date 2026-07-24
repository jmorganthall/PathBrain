"""Tests for the crown follower: churn ledger, follow-the-crown apply, guards, stats."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from pathbrain import coordinator, crown_follower
from pathbrain.config_store import save_config
from pathbrain.database import session_scope
from pathbrain.models import CrownEvent
from pathbrain.providers import get_provider
from pathbrain.providers.mock import _OVERRIDES
from pathbrain.settings_profile import SQM_OFF_FINGERPRINT, fingerprint, normalize


@pytest.fixture(autouse=True)
def _clean():
    """Each test starts with an empty ledger, default config, and a pristine mock firewall."""
    _OVERRIDES.clear()
    crown_follower._state.update({"last_check": 0.0, "last_result": None})
    with session_scope() as session:
        session.query(CrownEvent).delete()
        save_config(session, {"crown_follow": {"enabled": False, "interval_minutes": 30}})
    yield
    _OVERRIDES.clear()
    with session_scope() as session:
        session.query(CrownEvent).delete()
        save_config(session, {"crown_follow": {"enabled": False, "interval_minutes": 30}})


def _live_norm() -> list[dict]:
    return normalize(get_provider().discover())


def _profile(settings: list[dict], overall: float = 90.0) -> dict:
    return {
        "fingerprint": fingerprint(settings),
        "label": "test-profile",
        "confident": True,
        "overall": overall,
        "settings": settings,
    }


def _field_for(*profiles: dict, best: str | None = None) -> dict:
    return {
        "profiles": list(profiles),
        "best_fingerprint": best if best is not None else (profiles[0]["fingerprint"] if profiles else None),
    }


def _off_crown_target(quantum: int = 2000) -> list[dict]:
    """The live mock profile with a different (writable) quantum — reachable, off-crown."""
    target = copy.deepcopy(_live_norm())
    target[0]["quantum"] = quantum
    return target


def _events(kind: str | None = None) -> list[CrownEvent]:
    with session_scope() as session:
        rows = session.query(CrownEvent).order_by(CrownEvent.id.asc()).all()
        session.expunge_all()
    return [r for r in rows if kind is None or r.kind == kind]


def _enable():
    with session_scope() as session:
        save_config(session, {"crown_follow": {"enabled": True}})


# ── Tracking (churn ledger) ──────────────────────────────────────────────────────────


def test_first_observation_marks_tracking_start_not_a_change(monkeypatch):
    live = _live_norm()
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(live)))
    result = crown_follower.check()
    assert result["crown_fingerprint"] == fingerprint(live)
    assert result["crown_changed"] is False  # first observation, not a change
    assert result["on_crown"] is True        # firewall already on the crown
    events = _events("change")
    assert len(events) == 1 and events[0].previous_fingerprint is None
    with session_scope() as session:
        stats = crown_follower.stats(session)
    assert stats["total_changes"] == 0
    assert stats["tracked_since"] is not None
    assert stats["current_crown_fingerprint"] == fingerprint(live)


def test_crown_change_is_recorded_and_counted(monkeypatch):
    live = _live_norm()
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(live)))
    crown_follower.check()

    target = _off_crown_target()
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(target)))
    result = crown_follower.check()
    assert result["crown_changed"] is True
    assert result["applied"] is False  # disabled: track only
    events = _events("change")
    assert len(events) == 2
    assert events[1].previous_fingerprint == fingerprint(live)
    assert events[1].fingerprint == fingerprint(target)
    with session_scope() as session:
        stats = crown_follower.stats(session)
    assert stats["total_changes"] == 1
    assert stats["changes_24h"] == 1
    assert stats["current_crown_fingerprint"] == fingerprint(target)


def test_no_crown_records_nothing(monkeypatch):
    monkeypatch.setattr(
        crown_follower, "_compute_field", lambda s: {"profiles": [], "best_fingerprint": None}
    )
    result = crown_follower.check()
    assert result["crown_fingerprint"] is None
    assert _events() == []


def test_unchanged_crown_records_no_new_event(monkeypatch):
    live = _live_norm()
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(live)))
    crown_follower.check()
    crown_follower.check()
    assert len(_events("change")) == 1


# ── Following (the firewall write) ───────────────────────────────────────────────────


def test_apply_when_enabled_and_off_crown(monkeypatch):
    _enable()
    target = _off_crown_target(quantum=2000)
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(target)))
    result = crown_follower.check()
    assert result["applied"] is True
    assert result["on_crown"] is True
    assert int(_OVERRIDES["quantum"]) == 2000  # the write actually landed on the firewall
    events = _events("change")
    assert len(events) == 1 and events[0].applied is True
    assert "change(s) written" in (events[0].detail or "")


def test_disabled_tracks_but_never_writes(monkeypatch):
    target = _off_crown_target(quantum=3000)
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(target)))
    result = crown_follower.check()
    assert result["applied"] is False
    assert result["on_crown"] is False
    assert "disabled" in (result["apply_skipped"] or "")
    assert "quantum" not in _OVERRIDES  # firewall untouched


def test_apply_without_crown_change_records_apply_event(monkeypatch):
    # Crown already recorded, then the follower is enabled while the firewall sits
    # elsewhere: the write happens with no crown change → a standalone "apply" row.
    target = _off_crown_target(quantum=2500)
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(target)))
    crown_follower.check()  # disabled: records the crown, no write
    _enable()
    result = crown_follower.check()
    assert result["applied"] is True and result["crown_changed"] is False
    assert len(_events("change")) == 1
    applies = _events("apply")
    assert len(applies) == 1 and applies[0].applied is True


def test_sqm_off_crown_is_never_applied(monkeypatch):
    _enable()
    target = copy.deepcopy(_live_norm())
    target[0]["enabled"] = False  # the collapsed "SQM off" profile
    profile = _profile(target)
    assert profile["fingerprint"] == SQM_OFF_FINGERPRINT
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(profile))
    result = crown_follower.check()
    assert result["applied"] is False
    assert "SQM off" in (result["apply_skipped"] or "")
    assert "quantum" not in _OVERRIDES


def test_unreachable_crown_is_skipped(monkeypatch):
    _enable()
    target = copy.deepcopy(_live_norm())
    target[0]["scheduler"] = "fq_pie"  # non-writable field → unreachable environment
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(target)))
    result = crown_follower.check()
    assert result["applied"] is False
    assert "unreachable" in (result["apply_skipped"] or "")


def test_busy_coordinator_defers_the_apply(monkeypatch):
    _enable()
    target = _off_crown_target(quantum=4000)
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(target)))
    with coordinator.hold("test-session"):
        result = crown_follower.check()
    assert result["applied"] is False
    assert (result["apply_skipped"] or "").startswith("deferred")
    assert "quantum" not in _OVERRIDES
    # The change is still recorded — tracking never defers.
    assert len(_events("change")) == 1


# ── step() interval gating ───────────────────────────────────────────────────────────


def test_step_respects_interval(monkeypatch):
    calls = []
    monkeypatch.setattr(crown_follower, "check", lambda: calls.append(1) or {"applied": False})
    crown_follower._state["last_check"] = 0.0
    assert crown_follower.step() is False
    assert len(calls) == 1
    # Immediately again: within the interval → no check.
    assert crown_follower.step() is False
    assert len(calls) == 1


# ── Stats math ───────────────────────────────────────────────────────────────────────


def test_stats_reign_and_window_math():
    now = datetime.now(timezone.utc)
    rows = [
        ("A", None, now - timedelta(days=10)),   # tracking starts
        ("B", "A", now - timedelta(days=8)),     # reign A: 48h
        ("A", "B", now - timedelta(days=2)),     # reign B: 144h
        ("C", "A", now - timedelta(hours=1)),    # reign A: 47h; C reigns now
    ]
    with session_scope() as session:
        for fp, prev, at in rows:
            session.add(
                CrownEvent(kind="change", fingerprint=fp, previous_fingerprint=prev, created_at=at)
            )
            session.flush()
    with session_scope() as session:
        stats = crown_follower.stats(session, now=now)
    assert stats["total_changes"] == 3
    assert stats["changes_24h"] == 1
    assert stats["changes_7d"] == 2
    assert stats["changes_30d"] == 3
    assert stats["distinct_crowns_30d"] == 3  # B, A, C
    assert stats["current_crown_fingerprint"] == "C"
    assert stats["current_reign_hours"] == 1.0
    assert stats["median_reign_hours"] == 48.0
    assert stats["mean_reign_hours"] == pytest.approx(79.7, abs=0.1)
    assert stats["changes_per_day"] == pytest.approx(0.3, abs=0.01)


# ── API ──────────────────────────────────────────────────────────────────────────────


def test_api_status_config_and_toggle(client):
    res = client.get("/api/settings/crown-follow")
    assert res.status_code == 200
    body = res.json()
    assert body["config"] == {"enabled": False, "interval_minutes": 30.0}
    assert "stats" in body and "events" in body and "status" in body

    res = client.post("/api/settings/crown-follow", json={"enabled": True, "interval_minutes": 10})
    assert res.status_code == 200
    assert res.json()["config"] == {"enabled": True, "interval_minutes": 10.0}
    assert client.get("/api/settings/crown-follow").json()["config"]["enabled"] is True

    assert client.post("/api/settings/crown-follow", json={}).status_code == 400
    assert (
        client.post("/api/settings/crown-follow", json={"interval_minutes": 1}).status_code == 400
    )


def test_api_sync_runs_a_check(client, monkeypatch):
    live = _live_norm()
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(live)))
    res = client.post("/api/settings/crown-follow/sync")
    assert res.status_code == 200
    assert res.json()["result"]["crown_fingerprint"] == fingerprint(live)
