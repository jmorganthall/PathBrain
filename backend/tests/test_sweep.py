"""Tests for the Shotgun Sweep driver, variant generation, and API."""
from __future__ import annotations

from datetime import datetime, timezone

from pathbrain import sweep as sweep_mod
from pathbrain.database import session_scope
from pathbrain.models import Run, RunStatus, ScoreResult, Sweep, SweepStatus
from pathbrain.providers import get_provider
from pathbrain.providers import mock as mock_mod


# ── pure: variant generation + ETA ───────────────────────────────────────────


def test_generate_variants_cartesian():
    spec = {
        "quantum": {"enabled": True, "min": 1500, "max": 3000, "step": 750},
        "target": {"enabled": True, "min": 3, "max": 5, "step": 1},
    }
    v = sweep_mod.generate_variants(spec)
    assert len(v) == 9  # [1500,2250,3000] × [3ms,4ms,5ms]
    assert {"quantum": 1500, "target": "3ms"} in v
    assert {"quantum": 3000, "target": "5ms"} in v


def test_generate_variants_single_param():
    spec = {"quantum": {"enabled": True, "min": 1000, "max": 1000, "step": 500}, "target": {"enabled": False}}
    assert sweep_mod.generate_variants(spec) == [{"quantum": 1000}]


def test_generate_variants_none_enabled():
    assert sweep_mod.generate_variants({"quantum": {"enabled": False}, "target": {"enabled": False}}) == []


def test_estimate_eta():
    variants = [{"quantum": 1}, {"quantum": 2}]
    est = sweep_mod.estimate(variants, iterations=2, dwell_s=60, per_iteration_ms=1000)
    assert est["total_variants"] == 2
    assert est["eta_ms"] == 124000.0  # 2 × (60×1000 + 2×1000)
    assert sweep_mod.estimate(variants, 2, 0, None)["eta_ms"] is None


# ── driver: applies each variant for real, then restores the baseline ────────


def _fake_execute(run_id: int) -> None:
    """Stand-in for the network-heavy execute_run: mark the run scored."""
    with session_scope() as s:
        run = s.get(Run, run_id)
        run.status = RunStatus.COMPLETE
        run.finished_at = datetime.now(timezone.utc)
        s.add(
            ScoreResult(
                run_id=run_id, sops=80.0, subscores={}, weights_used={},
                metric_values={"speed_index": 1500.0},
            )
        )


def test_sweep_driver_applies_and_restores(monkeypatch):
    mock_mod._OVERRIDES.clear()  # mock baseline quantum=1514, target="5ms"
    monkeypatch.setattr(sweep_mod, "execute_run", _fake_execute)
    monkeypatch.setattr(sweep_mod, "_wait_for_idle", lambda *a, **k: None)

    spec = {
        "quantum": {"enabled": True, "min": 1600, "max": 1700, "step": 100},
        "target": {"enabled": False},
    }
    sweep_id = sweep_mod.start(spec, iterations=2, dwell_s=0.0, dry_run=False, pipe_uuid=None)
    sweep_mod._state["thread"].join(timeout=10)
    assert not sweep_mod.active()

    with session_scope() as s:
        sw = s.get(Sweep, sweep_id)
        assert sw.status == SweepStatus.COMPLETE
        assert sw.total_variants == 2 and sw.completed_variants == 2
        assert [r["sops"] for r in sw.results] == [80.0, 80.0]
        run_ids = [r["run_id"] for r in sw.results]
        labels = [s.get(Run, rid).label for rid in run_ids]
    assert all(lbl.startswith("sweep · q") for lbl in labels)
    # Baseline restored: the firewall is back to the original quantum.
    assert get_provider().discover()[0].quantum == 1514


def test_reconcile_interrupted_sweeps_restores(monkeypatch):
    mock_mod._OVERRIDES.clear()
    mock_mod._OVERRIDES["quantum"] = 9999  # firewall stranded on a test value
    with session_scope() as s:
        sw = Sweep(
            status=SweepStatus.RUNNING, dry_run=False, pipe_uuid=None, spec={},
            baseline={"quantum": 1514, "target": "5ms", "settings": []},
            total_variants=2, completed_variants=1, results=[],
        )
        s.add(sw)
        s.flush()
        sid = sw.id

    assert sweep_mod.reconcile_interrupted_sweeps() >= 1
    assert get_provider().discover()[0].quantum == 1514  # restored
    with session_scope() as s:
        assert s.get(Sweep, sid).status == SweepStatus.FAILED


# ── API smoke ────────────────────────────────────────────────────────────────


def test_sweep_preview_endpoint(client):
    resp = client.post(
        "/api/sweep/preview",
        json={"spec": {"quantum": {"enabled": True, "min": 1500, "max": 3000, "step": 750}}, "iterations": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_variants"] == 3
    assert body["cap"] == 64
    assert len(body["variants"]) == 3


def test_sweep_current_empty_or_shaped(client):
    resp = client.get("/api/sweep/current")
    assert resp.status_code == 200
    assert "sweep" in resp.json()
