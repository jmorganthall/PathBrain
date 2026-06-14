"""Tests for the SOPS scoring engine."""
from __future__ import annotations

from pathbrain.config_store import DEFAULT_THRESHOLDS, DEFAULT_WEIGHTS
from pathbrain.scoring import compute_score


def _score(metrics):
    return compute_score(metrics, DEFAULT_WEIGHTS, DEFAULT_THRESHOLDS)


def test_perfect_metrics_score_100():
    metrics = {
        "dns": {"lookup_ms": 1.0},
        "tcp": {"connect_ms": 1.0},
        "tls": {"handshake_ms": 1.0},
        "http": {"ttfb_ms": 1.0},
        "icmp": {"jitter_ms": 0.0, "packet_loss_pct": 0.0},
    }
    result = _score(metrics)
    assert result.sops == 100.0


def test_worst_metrics_score_0():
    metrics = {
        "dns": {"lookup_ms": 9999},
        "tcp": {"connect_ms": 9999},
        "tls": {"handshake_ms": 9999},
        "http": {"ttfb_ms": 9999},
        "icmp": {"jitter_ms": 9999, "packet_loss_pct": 100},
    }
    result = _score(metrics)
    assert result.sops == 0.0


def test_missing_metrics_redistribute_weights():
    # Only DNS present, perfect -> SOPS should be 100 (weight redistributed).
    result = _score({"dns": {"lookup_ms": 1.0}})
    assert result.sops == 100.0
    assert set(result.weights_used) == {"dns"}
    assert abs(sum(result.weights_used.values()) - 1.0) < 1e-9


def test_ping_does_not_dominate():
    # Terrible jitter/packet loss, but everything else perfect.
    good = {
        "dns": {"lookup_ms": 1.0},
        "tcp": {"connect_ms": 1.0},
        "tls": {"handshake_ms": 1.0},
        "http": {"ttfb_ms": 1.0},
        "icmp": {"jitter_ms": 9999, "packet_loss_pct": 100},
    }
    result = _score(good)
    # Jitter+packet_loss are only 10/75 of available weight -> score stays high.
    assert result.sops > 85.0


def test_midpoint_normalization():
    # dns best=5, worst=200 -> midpoint ~102.5 should score ~50.
    thresholds = {"dns": {"best": 5.0, "worst": 205.0}}
    weights = {"dns": 10}
    result = compute_score({"dns": {"lookup_ms": 105.0}}, weights, thresholds)
    assert 49.0 <= result.subscores["dns"] <= 51.0


def test_empty_metrics_score_zero():
    result = _score({})
    assert result.sops == 0.0
    assert result.weights_used == {}
