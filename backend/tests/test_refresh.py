"""Tests for the profile-refresh batch: driver lifecycle, reconcile, and the estimate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain import refresh
from pathbrain.database import session_scope
from pathbrain.models import (
    ProfileRefresh,
    ProfileRefreshStatus,
    Run,
    RunStatus,
)


class _FakeProvider:
    name = "fake"

    def discover(self):
        return []


def _run_drive(monkeypatch, plan, *, cancel_after=None):
    """Run _drive synchronously over a scripted profile plan, primitives stubbed."""
    spy = {"apply_profile": [], "restore": 0, "runs": 0, "iterations": []}

    def fake_apply_profile(provider, settings, fp):
        spy["apply_profile"].append(fp)
        if cancel_after is not None and len(spy["apply_profile"]) >= cancel_after:
            refresh._state["cancel"] = True

    def fake_create_run(**kwargs):
        spy["runs"] += 1
        spy["iterations"].append(kwargs.get("iterations"))
        return spy["runs"]

    monkeypatch.setattr(refresh, "get_provider", lambda: _FakeProvider())
    monkeypatch.setattr(refresh, "normalize", lambda x: [])
    monkeypatch.setattr(refresh, "plan_apply", lambda target, live: ([], []))
    monkeypatch.setattr(refresh, "_apply_all", lambda provider, changes: spy.__setitem__("restore", spy["restore"] + 1))
    monkeypatch.setattr(refresh, "_apply_profile", fake_apply_profile)
    monkeypatch.setattr(refresh, "create_run", fake_create_run)
    monkeypatch.setattr(refresh, "execute_run", lambda rid: None)

    with session_scope() as s:
        row = ProfileRefresh(status=ProfileRefreshStatus.PENDING, profiles_total=len(plan))
        s.add(row)
        s.flush()
        rid = row.id

    refresh._state.update({"active": True, "id": rid, "cancel": False, "plan": plan})
    refresh._drive(rid)
    with session_scope() as s:
        return s.get(ProfileRefresh, rid), spy


def test_drive_refreshes_each_profile_and_restores(monkeypatch):
    plan = [
        {"fingerprint": "aaaa", "settings": [{"label": "A"}], "label": "A", "needed": 3},
        {"fingerprint": "bbbb", "settings": [{"label": "B"}], "label": "B", "needed": 3},
    ]
    row, spy = _run_drive(monkeypatch, plan)
    assert row.status == ProfileRefreshStatus.COMPLETE
    # Applied both profiles, ran a benchmark per profile with the requested iterations.
    assert spy["apply_profile"] == ["aaaa", "bbbb"]
    assert spy["runs"] == 2 and spy["iterations"] == [3, 3]
    assert row.profiles_done == 2 and row.iterations_run == 6
    # Baseline restored exactly once, at the end (not between profiles).
    assert spy["restore"] == 1
    assert refresh.active() is False


def test_drive_cancel_stops_after_current_and_restores(monkeypatch):
    plan = [
        {"fingerprint": "aaaa", "settings": [{"label": "A"}], "label": "A", "needed": 2},
        {"fingerprint": "bbbb", "settings": [{"label": "B"}], "label": "B", "needed": 2},
        {"fingerprint": "cccc", "settings": [{"label": "C"}], "label": "C", "needed": 2},
    ]
    # Cancel trips during the first profile → only the first runs, baseline still restored.
    row, spy = _run_drive(monkeypatch, plan, cancel_after=1)
    assert row.status == ProfileRefreshStatus.CANCELLED
    assert spy["apply_profile"] == ["aaaa"] and spy["runs"] == 1
    assert spy["restore"] == 1


def test_reconcile_restores_stranded_baseline(monkeypatch):
    monkeypatch.setattr(refresh, "get_provider", lambda: _FakeProvider())
    monkeypatch.setattr(refresh, "plan_apply", lambda target, live: ([{"x": 1}], []))
    applied = {"n": 0}
    monkeypatch.setattr(refresh, "_apply_all", lambda provider, changes: applied.__setitem__("n", applied["n"] + 1))

    with session_scope() as s:
        row = ProfileRefresh(
            status=ProfileRefreshStatus.RUNNING, baseline=[{"label": "wan"}], profiles_total=2
        )
        s.add(row)
        s.flush()
        rid = row.id

    restored = refresh.reconcile_interrupted_refreshes()
    assert restored >= 1 and applied["n"] >= 1
    with session_scope() as s:
        assert s.get(ProfileRefresh, rid).status == ProfileRefreshStatus.FAILED


def test_preview_estimates_duration():
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # Two distinct profiles with stored settings + a per-iteration timing to estimate from.
    with session_scope() as s:
        for i, fp in enumerate(["fpaaaaaaaaaa", "fpbbbbbbbbbb"]):
            s.add(Run(
                status=RunStatus.COMPLETE, created_at=t0 - timedelta(minutes=i),
                settings_fingerprint=fp, settings=[{"label": "wan", "quantum": 1514}],
                iterations=1, per_iteration_ms=2000.0,
            ))

    with session_scope() as s:
        prev = refresh.preview(s, 4)
    # The module DB accumulates other tests' profiles, so assert relationships, not exacts.
    assert prev["profiles"] >= 2
    assert prev["iterations"] == 4
    assert prev["total_iterations"] == prev["profiles"] * 4
    assert prev["per_iteration_ms"] is not None
    # total_iterations × per-iteration time + per-profile overhead → a positive estimate.
    assert prev["estimated_seconds"] is not None and prev["estimated_seconds"] > 0
