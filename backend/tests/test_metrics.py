"""Tests for the metric registry (single source of truth) and its catalog API."""
from __future__ import annotations

from pathbrain import metrics
from pathbrain.config_store import (
    DEFAULT_COMPLETION_THRESHOLDS,
    DEFAULT_COMPLETION_WEIGHTS,
    DEFAULT_THRESHOLDS,
    DEFAULT_WEIGHTS,
)
from pathbrain.scoring import COMPLETION_METRIC_SOURCES, METRIC_SOURCES


def test_registry_derives_scoring_sources():
    assert METRIC_SOURCES == metrics.metric_sources(metrics.SOPS)
    assert COMPLETION_METRIC_SOURCES == metrics.metric_sources(metrics.COMPLETION)
    # SOPS is perception-led; Completion is pure infra.
    assert set(METRIC_SOURCES) == {"fcp", "lcp", "inp", "ttfb", "render"}
    assert set(COMPLETION_METRIC_SOURCES) == {"dns", "tcp", "tls", "jitter", "packet_loss"}


def test_registry_derives_config_defaults():
    # The previously-hardcoded calibration is reproduced from the registry.
    assert DEFAULT_WEIGHTS == {"fcp": 20, "lcp": 25, "inp": 15, "ttfb": 15, "render": 25}
    assert DEFAULT_THRESHOLDS["fcp"] == {"best": 150.0, "worst": 4000.0}
    assert DEFAULT_THRESHOLDS["ttfb"] == {"best": 30.0, "worst": 1000.0}
    assert DEFAULT_COMPLETION_WEIGHTS["tls"] == 20
    assert DEFAULT_COMPLETION_THRESHOLDS["packet_loss"] == {"best": 0.0, "worst": 2.5}


def test_latest_metric_keys():
    assert set(metrics.latest_metric_keys()) == {"fcp", "lcp"}


def test_catalog_covers_every_metric_with_metadata():
    cat = metrics.catalog()
    keys = {c["key"] for c in cat}
    # Scored + display-only metrics all present.
    assert {"fcp", "lcp", "inp", "ttfb", "render"} <= keys
    assert {"dns", "tcp", "tls", "jitter", "packet_loss"} <= keys
    assert {"latency", "download", "transfer", "dom_content_loaded", "load_event"} <= keys
    for c in cat:
        assert c["label"] and c["description"]  # every metric is documented
        assert "source_key" in c and "unit" in c
    transfer = next(c for c in cat if c["key"] == "transfer")
    assert transfer["higher_is_better"] is True  # the one inverted metric
    assert transfer["axis"] is None  # display-only, not scored


def test_metrics_endpoint(client):
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert "metrics" in body and len(body["metrics"]) == len(metrics.METRICS)
    fcp = next(m for m in body["metrics"] if m["key"] == "fcp")
    assert fcp["source_key"] == "fcp_ms" and fcp["axis"] == "sops"
