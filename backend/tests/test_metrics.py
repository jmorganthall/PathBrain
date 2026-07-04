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
    # SOPS is perception-led; the byte-arrival smoothness metrics now carry the
    # delivery signal (the pixel Speed Index / paint cadence are display-only).
    assert set(METRIC_SOURCES) == {
        "byte_earliness", "fcp", "longest_stall", "perceived_time",
        "cls", "lcp", "inp", "ttfb", "render",
    }
    assert set(COMPLETION_METRIC_SOURCES) == {"dns", "tcp", "tls", "jitter", "packet_loss"}


def test_registry_derives_config_defaults():
    # Byte-arrival rubric: byte_earliness + FCP lead; completion (LCP/render) trails.
    assert DEFAULT_WEIGHTS == {
        "byte_earliness": 25, "fcp": 20, "longest_stall": 10, "perceived_time": 5,
        "cls": 5, "lcp": 10, "inp": 10, "ttfb": 10, "render": 5,
    }
    # perceptual-v5: thresholds anchored to CWV "good"/"poor" boundaries.
    assert DEFAULT_THRESHOLDS["fcp"] == {"best": 1800.0, "worst": 3000.0}
    assert DEFAULT_THRESHOLDS["ttfb"] == {"best": 800.0, "worst": 1800.0}
    assert DEFAULT_THRESHOLDS["inp"] == {"best": 200.0, "worst": 500.0}
    assert DEFAULT_THRESHOLDS["byte_earliness"] == {"best": 300.0, "worst": 5000.0}
    assert DEFAULT_COMPLETION_WEIGHTS["tls"] == 20
    assert DEFAULT_COMPLETION_THRESHOLDS["packet_loss"] == {"best": 0.0, "worst": 2.5}


def test_latest_metric_keys():
    # Longest stall (byte-arrival, always captured) marks the current rubric.
    assert set(metrics.latest_metric_keys()) == {"longest_stall"}


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


def test_ledger_covers_every_metric_with_a_valid_role():
    """Every metric is assigned exactly one ledger bucket (import-invariant, re-checked)."""
    assert set(metrics.METRIC_ROLES) == {m.key for m in metrics.METRICS}
    assert set(metrics.METRIC_ROLES.values()) <= metrics.VALID_ROLES


def test_ledger_classification_anchors():
    """Lock the classification so a mis-tag (or the probe/nav name confusion) is caught."""
    r = metrics.role_of
    # W — the scored dns/tcp/tls/ttfb are independent *probe* instruments, not nav phases.
    for k in ("ttfb", "dns", "tcp", "tls", "latency", "jitter", "packet_loss", "download", "transfer"):
        assert r(k) == metrics.ROLE_WEATHER, k
    # N — navigation phases; body delivery is the crown-eligible one.
    assert r("nav_request") == metrics.ROLE_NETWORK   # the *nav* TTFB phase (≠ probe ttfb)
    assert r("nav_response") == metrics.ROLE_NETWORK   # body delivery (SQM-facing)
    # C — client CPU: the render residual and interaction/layout, shaping-immune.
    for k in ("nav_render", "inp", "cls"):
        assert r(k) == metrics.ROLE_CLIENT, k
    # S — byte-arrival shape.
    for k in ("stall_time", "longest_stall", "delivery_gini", "cadence_cov", "byte_earliness"):
        assert r(k) == metrics.ROLE_SHAPE, k
    # O — opaque milestone sums (kept for display, never ranked).
    for k in ("fcp", "lcp", "render", "load_event"):
        assert r(k) == metrics.ROLE_COMPOSITE, k


def test_rank_eligibility_gate():
    assert metrics.rank_eligible("nav_response") is True   # delivery — network, rankable
    assert metrics.rank_eligible("delivery_gini") is True  # ratio shape, rankable
    assert metrics.rank_eligible("ttfb") is False          # probe instrument (weather)
    assert metrics.rank_eligible("fcp") is False           # opaque milestone
    assert metrics.rank_eligible("cls") is False           # client health-check


def test_completion_axis_is_exactly_the_weather_bucket():
    """The Completion axis is the explicit infra/weather axis — every member is a W probe."""
    for k in metrics.metric_sources(metrics.COMPLETION):
        assert metrics.role_of(k) == metrics.ROLE_WEATHER, k


def test_headline_ledger_violations_are_the_phase3_backlog():
    """The SOPS headline currently scores metrics the ledger says can't be ranked. This
    pins that KNOWN set (the crown rework's to-do) so no *new* violation slips in — and
    empties to {} exactly when Phase 3 removes them, at which point the check can become a
    hard import assert. `ineligible_scored` is the same guard that flags scored-TTFB."""
    sops_keys = list(metrics.metric_sources(metrics.SOPS))
    violations = metrics.ineligible_scored(sops_keys)
    assert violations == {
        "ttfb": "W",    # a probe instrument scored in Responsiveness — the headline confound
        "fcp": "O", "lcp": "O", "render": "O",  # opaque milestone sums
        "cls": "C", "inp": "C",                  # client health-checks
    }
    # Nothing outside the known set — a brand-new ineligible scored metric fails here.
    assert set(violations) == {"ttfb", "fcp", "lcp", "render", "cls", "inp"}
