"""Tests for the run baseline endpoint (this-run-vs-profile-average comparison)."""
from __future__ import annotations

from pathbrain.database import session_scope
from pathbrain.models import BenchmarkResult, Run, RunStatus


def _seed_run(fp: str | None, lookup_ms: float, success: bool = True) -> int:
    with session_scope() as session:
        run = Run(
            status=RunStatus.COMPLETE,
            settings_fingerprint=fp,
            settings=[{"label": "wan", "quantum": 1514}] if fp else None,
        )
        session.add(run)
        session.flush()
        session.add(
            BenchmarkResult(
                run_id=run.id,
                plugin="dns",
                success=success,
                metrics={"lookup_ms": lookup_ms},
            )
        )
        return run.id


def test_baseline_averages_same_profile(client):
    fp = "baselinefp01"
    # Three prior runs of the same profile average 20ms…
    for v in (10.0, 20.0, 30.0):
        _seed_run(fp, v)
    # …and the run we're viewing was faster (5ms).
    target = _seed_run(fp, 5.0)

    resp = client.get(f"/api/results/{target}/baseline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "profile"
    assert body["profile_fingerprint"] == fp
    assert body["run_count"] == 3  # excludes the run being viewed
    assert body["metrics"]["dns"]["lookup_ms"] == 20.0


def test_baseline_falls_back_to_recent_when_no_profile(client):
    # A run with no settings fingerprint still gets a baseline from recent runs.
    _seed_run(None, 40.0)
    target = _seed_run(None, 12.0)
    resp = client.get(f"/api/results/{target}/baseline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "all"
    assert body["run_count"] >= 1
    assert "dns" in body["metrics"]


def test_baseline_404_for_missing_run(client):
    assert client.get("/api/results/99999999/baseline").status_code == 404
