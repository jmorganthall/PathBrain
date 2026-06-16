"""Tests for the SOPS scoring engine."""
from __future__ import annotations

from pathbrain.config_store import (
    DEFAULT_PERCEPTUAL_THRESHOLDS,
    DEFAULT_PERCEPTUAL_WEIGHTS,
    DEFAULT_THRESHOLDS,
    DEFAULT_WEIGHTS,
)
from pathbrain.scoring import compute_responsiveness, compute_score


def _score(metrics):
    return compute_score(metrics, DEFAULT_WEIGHTS, DEFAULT_THRESHOLDS)


def _resp(metrics):
    return compute_responsiveness(
        metrics, DEFAULT_PERCEPTUAL_WEIGHTS, DEFAULT_PERCEPTUAL_THRESHOLDS
    )


def test_responsiveness_is_separate_from_sops():
    # Paint metrics drive responsiveness but NOT sops; latency drives sops but not
    # responsiveness — the two axes are independent.
    metrics = {"browser": {"fcp_ms": 1000.0, "lcp_ms": 1500.0, "inp_ms": 100.0}}
    resp = _resp(metrics)
    assert resp.sops == 100.0  # all at "best" thresholds
    assert set(resp.subscores) == {"fcp", "lcp", "inp"}
    # The same metrics produce no SOPS (no completion metrics present).
    assert compute_score(metrics, DEFAULT_WEIGHTS, DEFAULT_THRESHOLDS).subscores == {}


def test_responsiveness_empty_without_paint_metrics():
    # No browser paint metrics -> no responsiveness subscores (caller stores NULL).
    resp = _resp({"dns": {"lookup_ms": 1.0}})
    assert resp.subscores == {}


def test_responsiveness_partial_metrics_redistribute():
    # Only FCP present, at worst -> responsiveness 0 with weight redistributed.
    resp = _resp({"browser": {"fcp_ms": 9999.0}})
    assert set(resp.weights_used) == {"fcp"}
    assert resp.sops == 0.0


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


def test_log_curve_geometric_midpoint():
    # Perceptual (log) curve: the 50-point is the *geometric* mean of best/worst.
    thresholds = {"dns": {"best": 10.0, "worst": 1000.0}}  # geo mean = 100
    weights = {"dns": 10}
    mid = compute_score({"dns": {"lookup_ms": 100.0}}, weights, thresholds)
    assert 48.0 <= mid.subscores["dns"] <= 52.0
    # The arithmetic midpoint (505ms) scores well below 50 — early latency hurts more.
    arith = compute_score({"dns": {"lookup_ms": 505.0}}, weights, thresholds)
    assert arith.subscores["dns"] < 30


def test_equal_ratios_equal_score_drops():
    # Weber–Fechner: 20->40ms costs the same as 200->400ms.
    thr = {"dns": {"best": 10.0, "worst": 1000.0}}
    w = {"dns": 10}

    def sub(v: float) -> float:
        return compute_score({"dns": {"lookup_ms": v}}, w, thr).subscores["dns"]

    drop_low = sub(20) - sub(40)
    drop_high = sub(200) - sub(400)
    assert abs(drop_low - drop_high) < 0.5


def test_empty_metrics_score_zero():
    result = _score({})
    assert result.sops == 0.0
    assert result.weights_used == {}
