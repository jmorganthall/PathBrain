"""Tests for multi-iteration aggregation and the run-estimate endpoint."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain.database import session_scope
from pathbrain.models import Run, RunStatus
from pathbrain.plugins.base import PluginResult
from pathbrain.runner import (
    _aggregate,
    _plugin_metrics_from_values,
    fail_stale_runs,
    reconcile_interrupted_runs,
)


def _make_run(status: RunStatus, started_at=None) -> int:
    with session_scope() as s:
        run = Run(status=status, started_at=started_at)
        s.add(run)
        s.flush()
        return run.id


def test_reconcile_interrupted_runs():
    rid = _make_run(RunStatus.RUNNING)
    reconcile_interrupted_runs()
    with session_scope() as s:
        run = s.get(Run, rid)
        assert run.status == RunStatus.FAILED
        assert "Interrupted" in run.error


def test_watchdog_fails_stale_runs():
    old = datetime.now(timezone.utc) - timedelta(minutes=45)
    rid = _make_run(RunStatus.RUNNING, started_at=old)
    fresh = _make_run(RunStatus.RUNNING, started_at=datetime.now(timezone.utc))
    fail_stale_runs(30)
    with session_scope() as s:
        assert s.get(Run, rid).status == RunStatus.FAILED
        assert s.get(Run, fresh).status == RunStatus.RUNNING  # within limit


def test_cancel_run_endpoint(client):
    rid = _make_run(RunStatus.RUNNING)
    resp = client.post(f"/api/runs/{rid}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"
    assert "Cancelled" in (resp.json()["error"] or "")


def test_aggregate_averages_metrics_with_stats():
    results = [
        PluginResult("dns", success=True, duration_ms=10, metrics={"lookup_ms": 10.0}),
        PluginResult("dns", success=True, duration_ms=20, metrics={"lookup_ms": 20.0}),
        PluginResult("dns", success=True, duration_ms=30, metrics={"lookup_ms": 30.0}),
    ]
    agg = _aggregate(results)
    assert agg["success"] is True
    assert agg["metrics"]["lookup_ms"] == 20.0  # mean of 10/20/30
    stats = agg["details"]["metric_stats"]["lookup_ms"]
    assert stats["n"] == 3
    assert stats["min"] == 10.0
    assert stats["max"] == 30.0
    assert stats["stdev"] > 0
    assert agg["details"]["iterations"] == 3
    assert agg["details"]["samples"] == 3
    assert agg["duration_ms"] == 20.0


def test_aggregate_skips_failed_iterations():
    results = [
        PluginResult("tcp", success=True, duration_ms=5, metrics={"connect_ms": 4.0}),
        PluginResult("tcp", success=False, error="boom"),
        PluginResult("tcp", success=True, duration_ms=7, metrics={"connect_ms": 6.0}),
    ]
    agg = _aggregate(results)
    assert agg["success"] is True
    assert agg["metrics"]["connect_ms"] == 5.0  # mean of the two successes
    assert agg["details"]["samples"] == 2
    assert agg["details"]["iterations"] == 3


def test_aggregate_all_failed():
    results = [
        PluginResult("icmp", success=False, error="no route"),
        PluginResult("icmp", success=False, error="no route"),
    ]
    agg = _aggregate(results)
    assert agg["success"] is False
    assert agg["metrics"] == {}
    assert agg["error"] == "no route"
    assert agg["details"]["samples"] == 0


def test_aggregate_handles_missing_metric_key():
    # One iteration reports jitter, another doesn't (e.g. host went down).
    results = [
        PluginResult("icmp", success=True, metrics={"latency_ms": 10.0, "jitter_ms": 2.0}),
        PluginResult("icmp", success=True, metrics={"latency_ms": 12.0, "jitter_ms": None}),
    ]
    agg = _aggregate(results)
    assert agg["metrics"]["latency_ms"] == 11.0
    assert agg["metrics"]["jitter_ms"] == 2.0  # averaged over the one sample


def test_aggregate_uses_robust_median_central_value():
    # A single huge outlier iteration must not drag the central value up.
    results = [
        PluginResult("http", success=True, metrics={"ttfb_ms": 100.0}),
        PluginResult("http", success=True, metrics={"ttfb_ms": 100.0}),
        PluginResult("http", success=True, metrics={"ttfb_ms": 1000.0}),
    ]
    agg = _aggregate(results)
    assert agg["metrics"]["ttfb_ms"] == 100.0  # median, not the 400 mean
    stats = agg["details"]["metric_stats"]["ttfb_ms"]
    assert stats["median"] == 100.0
    assert stats["mean"] == 400.0  # still reported for reference


def test_plugin_metrics_from_values_reverse_maps():
    pm = _plugin_metrics_from_values({"dns": 12.0, "ttfb": 80.0, "jitter": 1.5})
    assert pm["dns"]["lookup_ms"] == 12.0
    assert pm["http"]["ttfb_ms"] == 80.0
    assert pm["icmp"]["jitter_ms"] == 1.5


def test_estimate_endpoint_shape(client):
    resp = client.get("/api/runs/estimate")
    assert resp.status_code == 200
    body = resp.json()
    assert "per_iteration_ms" in body
    assert "based_on_runs" in body
    assert body["max_iterations"] >= 1
