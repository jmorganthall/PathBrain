"""Tests for the autonomous experiment engine (window logic + sweep/decision).

Benchmark execution is stubbed so trials are fast and deterministic; the mock
provider's apply() actually mutates its in-memory state, so we can assert the
baseline-restore / promote behavior end to end.
"""
from __future__ import annotations

from datetime import datetime

import pathbrain.experiment as eng
from pathbrain.database import session_scope
from pathbrain.models import Experiment, ExperimentStatus, Run, RunStatus, ScoreResult
from pathbrain.providers import mock as mockmod


def test_in_window():
    # Tuesday 03:00 (weekday()==1)
    t = datetime(2026, 6, 16, 3, 0)
    assert eng.in_window({"days": [1], "start_hour": 2, "end_hour": 5}, t) is True
    assert eng.in_window({"days": [0], "start_hour": 2, "end_hour": 5}, t) is False  # wrong day
    assert eng.in_window({"days": [], "start_hour": 2, "end_hour": 5}, t) is True  # any day
    assert eng.in_window({"days": [], "start_hour": 4, "end_hour": 6}, t) is False  # before window
    # Overnight window 22:00–04:00 includes 03:00
    assert eng.in_window({"days": [], "start_hour": 22, "end_hour": 4}, t) is True


def _stub_benchmarks(monkeypatch):
    """Replace create_run/execute_run so a 'run' is instant and its SOPS depends
    on the currently-applied quantum (3000 is the good one)."""

    def fake_create_run(label=None, notes=None, iterations=None):
        q = int(mockmod._OVERRIDES.get("quantum", 1514))
        sops = 90.0 if q == 3000 else 70.0
        with session_scope() as s:
            run = Run(status=RunStatus.COMPLETE)
            s.add(run)
            s.flush()
            s.add(ScoreResult(run_id=run.id, sops=sops, subscores={}, weights_used={}, metric_values={}))
            return run.id

    monkeypatch.setattr(eng, "create_run", fake_create_run)
    monkeypatch.setattr(eng, "execute_run", lambda run_id: None)


def _reset():
    mockmod._OVERRIDES.clear()
    eng._state["current_value"] = None
    eng._state["applied_at"] = 0.0


def _run_cycle(client, *, auto_promote: bool):
    # Arm an always-open window, sweep quantum over [2000, 3000], dwell 0.
    client.put(
        "/api/config",
        json={
            "experiment": {
                "enabled": True,
                "dry_run": False,  # let the mock provider actually apply
                "auto_promote": auto_promote,
                "window": {"days": [], "start_hour": 0, "end_hour": 24},
                "param": "quantum",
                "candidates": [2000, 3000],
                "dwell_minutes": 0,
                "min_trials_per_value": 1,
                "improve_pct": 5,
            }
        },
    )
    # Several ticks: start + benchmark/advance through 1514, 2000, 3000.
    for _ in range(6):
        eng.step()

    # Close the window -> finalize.
    client.put("/api/config", json={"experiment": {"window": {"days": [], "start_hour": 0, "end_hour": 0}}})
    eng.step()


def test_sweep_restores_baseline_by_default(client, monkeypatch):
    _reset()
    _stub_benchmarks(monkeypatch)
    _run_cycle(client, auto_promote=False)

    with session_scope() as s:
        exp = s.scalars(
            __import__("sqlalchemy").select(Experiment).order_by(Experiment.id.desc())
        ).first()
        assert exp.status == ExperimentStatus.COMPLETED
        assert exp.result["winner"] == "3000"  # 90 vs 70 baseline
        assert exp.result["action"] == "restored_baseline"
        assert exp.result["final_value"] == "1514"
    # Mock firewall is back to baseline quantum.
    assert int(mockmod._OVERRIDES.get("quantum", 1514)) == 1514
    # cleanup config
    client.post("/api/config/reset")


def test_sweep_auto_promotes_winner(client, monkeypatch):
    _reset()
    _stub_benchmarks(monkeypatch)
    _run_cycle(client, auto_promote=True)

    with session_scope() as s:
        exp = s.scalars(
            __import__("sqlalchemy").select(Experiment).order_by(Experiment.id.desc())
        ).first()
        assert exp.result["action"] == "promoted"
        assert exp.result["final_value"] == "3000"
    assert int(mockmod._OVERRIDES.get("quantum", 1514)) == 3000
    client.post("/api/config/reset")
    _reset()


def test_disarm_aborts_and_restores(client, monkeypatch):
    _reset()
    _stub_benchmarks(monkeypatch)
    client.put(
        "/api/config",
        json={
            "experiment": {
                "enabled": True,
                "dry_run": False,
                "window": {"days": [], "start_hour": 0, "end_hour": 24},
                "param": "quantum",
                "candidates": [3000],
                "dwell_minutes": 0,
                "min_trials_per_value": 1,
            }
        },
    )
    eng.step()  # start + apply baseline
    eng.step()  # benchmark, advance to 3000 (applied)
    assert int(mockmod._OVERRIDES.get("quantum", 1514)) == 3000  # changed mid-run
    # Disarm -> abort + restore baseline.
    client.put("/api/config", json={"experiment": {"enabled": False}})
    eng.step()
    assert int(mockmod._OVERRIDES.get("quantum", 1514)) == 1514
    client.post("/api/config/reset")
    _reset()
