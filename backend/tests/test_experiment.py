"""Tests for the autonomous experiment engine (window logic + sweep/decision).

Benchmark execution is stubbed so trials are fast and deterministic; the mock
provider's apply() actually mutates its in-memory state, so we can assert the
baseline-restore / promote behavior end to end.
"""
from __future__ import annotations

from datetime import datetime

import pathbrain.experiment as eng
from pathbrain.database import session_scope
from pathbrain.config_store import get_config
from pathbrain.methodology import ensure_current_methodology
from pathbrain.models import Experiment, ExperimentStatus, ExperimentTrial, Run, RunStatus, Score, ScoreResult
from pathbrain.providers import mock as mockmod


def test_start_rejects_non_writable_param():
    # apply() can't write the scheduler, so an experiment on it would silently no-op every
    # trial. _start must refuse it (validated against the registry, not config trust).
    with session_scope() as session:
        exp = eng._start(session, {"param": "scheduler", "candidates": ["fq_codel", "fq_pie"]})
        assert exp is None
        assert session.query(Experiment).count() == 0


def test_in_window():
    # Tuesday 03:00 (weekday()==1)
    t = datetime(2026, 6, 16, 3, 0)
    assert eng.in_window({"days": [1], "start_hour": 2, "end_hour": 5}, t) is True
    assert eng.in_window({"days": [0], "start_hour": 2, "end_hour": 5}, t) is False  # wrong day
    assert eng.in_window({"days": [], "start_hour": 2, "end_hour": 5}, t) is True  # any day
    assert eng.in_window({"days": [], "start_hour": 4, "end_hour": 6}, t) is False  # before window
    # Overnight window 22:00–04:00 includes 03:00
    assert eng.in_window({"days": [], "start_hour": 22, "end_hour": 4}, t) is True


def _stub_benchmarks(monkeypatch, *, versioned=True):
    """Replace create_run/execute_run so a 'run' is instant and its score depends
    on the currently-applied quantum (3000 is the good one). Like a real run, it writes
    both the legacy ScoreResult and the versioned Score under the current methodology
    (``versioned=False`` leaves the versioned row out)."""

    def fake_create_run(label=None, notes=None, iterations=None):
        q = int(mockmod._OVERRIDES.get("quantum", 1514))
        sops = 90.0 if q == 3000 else 70.0
        with session_scope() as s:
            run = Run(status=RunStatus.COMPLETE)
            s.add(run)
            s.flush()
            s.add(ScoreResult(run_id=run.id, sops=sops, subscores={}, weights_used={}, metric_values={}))
            if versioned:
                version = ensure_current_methodology(s, get_config(s)).version
                s.add(Score(run_id=run.id, methodology_version=version, is_at_measure=True,
                            axis_scores={"overall": sops}))
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


def _latest_experiment(s):
    return s.scalars(
        __import__("sqlalchemy").select(Experiment).order_by(Experiment.id.desc())
    ).first()


def test_trials_ignore_a_regrade_of_run_score(client, monkeypatch):
    # S18: a re-grade overwrites the legacy ``run.score`` in place. The experiment judges
    # on the versioned Score, so flipping every ScoreResult (good value now looks worst)
    # must not move the outcome while the versioned rows stay put.
    _reset()
    _stub_benchmarks(monkeypatch)
    real_execute = eng.execute_run

    def regrade(run_id):
        real_execute(run_id)
        with session_scope() as s:
            sr = s.scalars(
                __import__("sqlalchemy").select(ScoreResult).where(ScoreResult.run_id == run_id)
            ).one()
            sr.sops = 100.0 - sr.sops  # 90 -> 10, 70 -> 30

    monkeypatch.setattr(eng, "execute_run", regrade)
    _run_cycle(client, auto_promote=False)

    with session_scope() as s:
        exp = _latest_experiment(s)
        assert exp.status == ExperimentStatus.COMPLETED
        assert exp.result["winner"] == "3000"
        trials = s.scalars(
            __import__("sqlalchemy").select(ExperimentTrial).where(ExperimentTrial.experiment_id == exp.id)
        ).all()
        assert trials and {t.sops for t in trials} <= {70.0, 90.0}
    client.post("/api/config/reset")
    _reset()


def test_trials_without_a_versioned_score_read_as_missing(client, monkeypatch):
    # S18: no Score row under the current methodology -> the trial's score is None (no
    # fallback to run.score.sops), so no value has data and nothing can win.
    _reset()
    _stub_benchmarks(monkeypatch, versioned=False)
    _run_cycle(client, auto_promote=True)

    with session_scope() as s:
        exp = _latest_experiment(s)
        assert exp.status == ExperimentStatus.COMPLETED
        trials = s.scalars(
            __import__("sqlalchemy").select(ExperimentTrial).where(ExperimentTrial.experiment_id == exp.id)
        ).all()
        assert trials and all(t.sops is None for t in trials)
        assert exp.result["winner"] is None
        assert exp.result["final_value"] == "1514"
    assert int(mockmod._OVERRIDES.get("quantum", 1514)) == 1514
    client.post("/api/config/reset")
    _reset()
