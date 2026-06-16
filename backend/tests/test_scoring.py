"""Tests for the scoring engine — SOPS (perception-led) and Completion axes."""
from __future__ import annotations

from pathbrain.config_store import (
    DEFAULT_COMPLETION_THRESHOLDS,
    DEFAULT_COMPLETION_WEIGHTS,
    DEFAULT_THRESHOLDS,
    DEFAULT_WEIGHTS,
)
from pathbrain.scoring import compute_completion, compute_score

# All SOPS (perception-led) metrics at their "best" threshold.
PERFECT_SOPS = {
    "browser": {"fcp_ms": 1.0, "lcp_ms": 1.0, "inp_ms": 1.0, "total_render_ms": 1.0},
    "http": {"ttfb_ms": 1.0},
}
# All Completion (infra) metrics perfect.
PERFECT_COMPLETION = {
    "dns": {"lookup_ms": 1.0},
    "tcp": {"connect_ms": 1.0},
    "tls": {"handshake_ms": 1.0},
    "icmp": {"jitter_ms": 0.0, "packet_loss_pct": 0.0},
}


def _score(metrics):
    return compute_score(metrics, DEFAULT_WEIGHTS, DEFAULT_THRESHOLDS)


def _completion(metrics):
    return compute_completion(metrics, DEFAULT_COMPLETION_WEIGHTS, DEFAULT_COMPLETION_THRESHOLDS)


def test_ideal_metrics_score_100():
    # 100 is reachable — but only by hitting the near-ideal `best` thresholds.
    assert _score(PERFECT_SOPS).sops == 100.0


def test_good_but_ordinary_setup_is_well_below_100():
    # The flagged run: good, not perfect. The scale (not a cap) keeps it down, and
    # no individual metric pins at 100.
    good = {
        "browser": {"fcp_ms": 489.0, "lcp_ms": 561.0, "inp_ms": 98.7, "total_render_ms": 1540.0},
        "http": {"ttfb_ms": 209.0},
    }
    result = _score(good)
    assert result.sops < 85.0
    assert all(v < 100.0 for v in result.subscores.values())


def test_only_near_floor_values_reach_100():
    # A value worse than `best` scores below 100; at/under `best` it's 100.
    thr = {"ttfb": {"best": 30.0, "worst": 1000.0}}
    w = {"ttfb": 10}

    def sub(v: float) -> float:
        return compute_score({"http": {"ttfb_ms": v}}, w, thr).subscores["ttfb"]

    assert sub(200) < sub(60) < 100.0  # ordinary values leave headroom
    assert sub(30) == 100.0            # hitting the near-ideal floor scores 100
    assert sub(200) < sub(60)          # monotonic: faster is better


def test_worst_metrics_score_0():
    metrics = {
        "browser": {"fcp_ms": 9999, "lcp_ms": 9999, "inp_ms": 9999, "total_render_ms": 9999},
        "http": {"ttfb_ms": 9999},
    }
    assert _score(metrics).sops == 0.0


def test_sops_is_perception_led_not_infra():
    # Pure infra metrics don't move SOPS at all (they're the Completion axis).
    assert _score(PERFECT_COMPLETION).subscores == {}
    assert _score(PERFECT_COMPLETION).sops == 0.0
    # Paint metrics don't move Completion.
    assert _completion(PERFECT_SOPS).subscores == {}


def test_completion_axis_scores_infra():
    assert _completion(PERFECT_COMPLETION).sops == 100.0
    assert set(_completion(PERFECT_COMPLETION).subscores) == {
        "dns",
        "tcp",
        "tls",
        "jitter",
        "packet_loss",
    }


def test_missing_metrics_redistribute_weights():
    # Only TTFB present (a SOPS metric), at the ideal floor -> 100, weight to it.
    result = _score({"http": {"ttfb_ms": 1.0}})
    assert result.sops == 100.0
    assert set(result.weights_used) == {"ttfb"}
    assert abs(sum(result.weights_used.values()) - 1.0) < 1e-9


def test_sops_survives_without_browser():
    # No browser engine -> SOPS falls back to TTFB only (never blank when http ran).
    result = _score({"http": {"ttfb_ms": 80.0}})  # ordinary-good TTFB
    assert 0.0 < result.sops < 100.0
    assert set(result.subscores) == {"ttfb"}


def test_log_curve_geometric_midpoint():
    # Perceptual (log) curve: the 50-point is the *geometric* mean of best/worst.
    thresholds = {"ttfb": {"best": 10.0, "worst": 1000.0}}  # geo mean = 100
    weights = {"ttfb": 10}
    mid = compute_score({"http": {"ttfb_ms": 100.0}}, weights, thresholds)
    assert 48.0 <= mid.subscores["ttfb"] <= 52.0
    arith = compute_score({"http": {"ttfb_ms": 505.0}}, weights, thresholds)
    assert arith.subscores["ttfb"] < 30


def test_equal_ratios_equal_score_drops():
    # Weber–Fechner: 20->40ms costs the same as 200->400ms.
    thr = {"ttfb": {"best": 10.0, "worst": 1000.0}}
    w = {"ttfb": 10}

    def sub(v: float) -> float:
        return compute_score({"http": {"ttfb_ms": v}}, w, thr).subscores["ttfb"]

    drop_low = sub(20) - sub(40)
    drop_high = sub(200) - sub(400)
    assert abs(drop_low - drop_high) < 0.5


def test_empty_metrics_score_zero():
    result = _score({})
    assert result.sops == 0.0
    assert result.weights_used == {}
