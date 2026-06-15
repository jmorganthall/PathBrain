"""Tests for multi-iteration aggregation and the run-estimate endpoint."""
from __future__ import annotations

from pathbrain.plugins.base import PluginResult
from pathbrain.runner import _aggregate


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


def test_estimate_endpoint_shape(client):
    resp = client.get("/api/runs/estimate")
    assert resp.status_code == 200
    body = resp.json()
    assert "per_iteration_ms" in body
    assert "based_on_runs" in body
    assert body["max_iterations"] >= 1
