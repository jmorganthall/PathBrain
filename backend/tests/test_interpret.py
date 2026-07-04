"""Tests for the interpretation layer: raw observations -> derived metrics."""
from __future__ import annotations

from statistics import mean, pstdev

from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.interpret.derive import (
    cadence_from_progress,
    derive,
    speed_index_from_progress,
)
from pathbrain.models import BenchmarkResult, Run, RunStatus, ScoreResult
from pathbrain.runner import rederive_run


# ── derivation parity: the formulas that used to live in the probes ──────────


def test_derive_icmp_jitter_is_stddev_of_rtts():
    rtts = [10.0, 12.0, 14.0, 16.0]
    raw = {"targets": {"1.1.1.1": {"rtts_ms": rtts, "sent": 4, "received": 4}}}
    m = derive("icmp", raw)
    assert m["latency_ms"] == round(mean(rtts), 3)
    assert m["jitter_ms"] == round(pstdev(rtts), 3)
    assert m["packet_loss_pct"] == 0.0


def test_derive_icmp_loss_and_dead_target():
    raw = {
        "targets": {
            "a": {"rtts_ms": [10.0, 10.0], "sent": 4, "received": 2},  # 50% loss
            "b": {"rtts_ms": [], "sent": 4, "received": 0},  # dead -> 100%
        }
    }
    m = derive("icmp", raw)
    assert m["packet_loss_pct"] == round((50.0 + 100.0) / 2, 3)
    assert m["latency_ms"] == 10.0  # only the alive target contributes


def test_derive_http_transfer_from_bytes_and_time():
    raw = {"urls": {"u": {"ttfb_ms": 50.0, "download_ms": 1000.0, "bytes": 1_000_000, "total_ms": 1050.0}}}
    m = derive("http", raw)
    assert m["transfer_mbps"] == 8.0  # 1e6 bytes * 8 / 1s / 1e6
    assert m["ttfb_ms"] == 50.0


def test_derive_dns_tcp_tls_means():
    assert derive("dns", {"providers": [{"lookups_ms": [10, 20]}, {"lookups_ms": [30]}]})["lookup_ms"] == 20.0
    assert derive("tcp", {"targets": {"a": {"connect_ms": 10}, "b": {"connect_ms": 20}}})["connect_ms"] == 15.0
    assert derive("tls", {"targets": {"a": {"handshake_ms": 40}}})["handshake_ms"] == 40.0


# ── trajectory metrics: the core perception fix ──────────────────────────────


def test_speed_index_rewards_early_progressive_paint():
    # Both finish at 2000ms. 'steady' is mostly visible by 400ms; 'stall' stays
    # blank then dumps everything at the end.
    steady = [(0, 0.0), (200, 0.5), (400, 0.8), (2000, 1.0)]
    stall = [(0, 0.0), (1800, 0.0), (1900, 0.1), (2000, 1.0)]
    assert speed_index_from_progress(steady) < speed_index_from_progress(stall)


def test_cadence_penalizes_stall_then_jump():
    steady = [(0, 0.0), (100, 0.4), (200, 0.7), (300, 1.0)]
    stall = [(0, 0.0), (100, 0.0), (200, 0.0), (300, 1.0)]
    assert cadence_from_progress(steady) < cadence_from_progress(stall)


def test_derive_browser_paint_without_filmstrip():
    raw = {
        "urls": {
            "u": {
                "nav": {},
                "paint": {"fcp": 200.0, "lcp": 400.0, "cls_entries": []},
                "total_render_ms": 1500.0,
                "filmstrip": [],
            }
        }
    }
    m = derive("browser", raw)
    assert m["fcp_ms"] == 200.0 and m["lcp_ms"] == 400.0
    assert m["total_render_ms"] == 1500.0
    assert m["cls"] == 0.0
    assert "speed_index_ms" not in m  # no filmstrip -> not derivable
    # No resource series captured -> no smoothness metrics (legacy runs stay thin).
    assert "longest_stall_ms" not in m


def test_derive_browser_emits_smoothness_from_resource_series():
    raw = {
        "urls": {
            "u": {
                "nav": {"responseStart": 50.0, "responseEnd": 80.0, "loadEventEnd": 900.0},
                "paint": {"fcp": 120.0, "lcp": 400.0, "cls_entries": []},
                "total_render_ms": 1500.0,
                "filmstrip": [],
                # A long blank plateau (chunky delivery) with no long task -> network.
                "resources": [
                    {"responseEnd": t, "transferSize": 1000, "nextHopProtocol": "h2"}
                    for t in (60, 70, 80, 760, 800)
                ],
                "loaf": {"entries": [], "source": "loaf"},
            }
        }
    }
    m = derive("browser", raw)
    assert m["longest_stall_ms"] >= 600  # the 80->760 plateau
    assert m["network_stall_ms"] > 0     # no long task overlapped it
    assert "perceived_time_ms" in m and "byte_earliness_ms" in m


# ── rederive: re-interpret stored raw end-to-end ─────────────────────────────


def test_rederive_recomputes_metrics_from_raw():
    with session_scope() as s:
        run = Run(status=RunStatus.COMPLETE)
        s.add(run)
        s.flush()
        s.add(
            BenchmarkResult(
                run_id=run.id, plugin="browser", success=True, metrics={},
                raw={"iterations": [{"urls": {"u": {
                    "nav": {}, "paint": {"fcp": 200.0, "lcp": 400.0, "cls_entries": []},
                    "total_render_ms": 1500.0, "filmstrip": [],
                }}}]},
            )
        )
        s.add(
            BenchmarkResult(
                run_id=run.id, plugin="http", success=True, metrics={},
                raw={"iterations": [{"urls": {"u": {
                    "ttfb_ms": 50.0, "download_ms": 1000.0, "bytes": 1_000_000, "total_ms": 1050.0,
                }}}]},
            )
        )
        s.add(ScoreResult(run_id=run.id, sops=0.0, subscores={}, weights_used={}, metric_values={}))
        s.flush()

        cfg = get_config(s)
        ok = rederive_run(
            run, cfg["weights"], cfg["thresholds"], cfg["rubric_version"],
            cfg["completion_weights"], cfg["completion_thresholds"], artifact_base="/nonexistent",
        )
        s.commit()
        assert ok
        # Metric values were re-derived from raw (not present in the seeded cache).
        assert run.score.metric_values.get("fcp") == 200.0
        assert run.score.metric_values.get("ttfb") == 50.0
        assert run.score.derivation_version
        assert run.score.sops > 0


def test_navigation_phases_additive_and_telescoping():
    """The waterfall phases tile navigationStart→load and the network prefix telescopes."""
    from pathbrain.interpret.waterfall import SEGMENT_KEYS, navigation_phases

    nav = {
        "startTime": 0, "domainLookupStart": 5, "domainLookupEnd": 12,
        "connectStart": 12, "secureConnectionStart": 20, "connectEnd": 40,
        "requestStart": 41, "responseStart": 95, "responseEnd": 130, "loadEventEnd": 900,
    }
    p = navigation_phases(nav, {"fcp": 200.0, "lcp": 400.0})

    # Segments are non-overlapping and tile [0, loadEventEnd].
    assert round(sum(p[k] for k in SEGMENT_KEYS), 3) == 900.0
    # TCP excludes TLS (they used to overlap); TLS is its own phase.
    assert p["nav_tcp_ms"] == 8.0 and p["nav_tls_ms"] == 20.0
    # The network prefix telescopes exactly to responseStart (cumulative TTFB).
    prefix = sum(
        p[k] for k in ("nav_stall_ms", "nav_dns_ms", "nav_tcp_ms", "nav_tls_ms", "nav_request_ms")
    )
    assert round(prefix, 3) == p["nav_ttfb_cumulative_ms"] == 95.0
    # Render residual + network-independent roll-ups.
    assert p["nav_render_ms"] == 70.0  # responseEnd(130) -> FCP(200)
    assert p["nav_fcp_after_ttfb_ms"] == 105.0  # FCP(200) - responseStart(95)
    assert p["nav_lcp_after_ttfb_ms"] == 305.0  # LCP(400) - responseStart(95)


def test_navigation_phases_no_tls_and_empty():
    from pathbrain.interpret.waterfall import navigation_phases

    # Non-HTTPS: secureConnectionStart == 0 -> all connect time is TCP, TLS is 0.
    nav = {"domainLookupStart": 5, "domainLookupEnd": 12, "connectStart": 12,
           "secureConnectionStart": 0, "connectEnd": 40,
           "responseStart": 95, "responseEnd": 130}
    p = navigation_phases(nav, {})
    assert p["nav_tcp_ms"] == 28.0 and p["nav_tls_ms"] == 0.0
    # An empty nav yields no phases (rather than fabricated zeros).
    assert navigation_phases({}, {"fcp": 200.0}) == {}


def test_derive_browser_emits_navigation_waterfall():
    raw = {"urls": {"u": {
        "nav": {"domainLookupStart": 5, "domainLookupEnd": 12, "connectStart": 12,
                "secureConnectionStart": 20, "connectEnd": 40, "responseStart": 95,
                "responseEnd": 130, "loadEventEnd": 900},
        "paint": {"fcp": 200.0, "lcp": 400.0, "cls_entries": []},
        "total_render_ms": 1500.0, "filmstrip": [],
    }}}
    m = derive("browser", raw)
    assert m["nav_dns_ms"] == 7.0
    assert m["nav_ttfb_cumulative_ms"] == 95.0
    assert m["nav_lcp_after_ttfb_ms"] == 305.0


def test_jank_fraction_ratio_cancels_bulk_pace():
    """jank_fraction is stall as a fraction of the delivery window — so a uniformly slower
    load (bulk pace ×2, stall membership unchanged) leaves it flat while absolute stall doubles."""
    from pathbrain.interpret.smoothness import jank_fraction, stall_time

    series = [0.0, 50.0, 850.0, 900.0]          # one 800ms stall; two 50ms (sub-threshold) gaps
    jank = jank_fraction(series, 0.0, 900.0)     # window responseStart→LCP = [0, 900]
    assert jank == round(800.0 / 900.0, 4)       # 0.8889 — 800ms of a 900ms window frozen
    assert stall_time(series) == 800.0

    slow = [t * 2 for t in series]               # same load, uniformly 2× slower
    assert jank_fraction(slow, 0.0, 1800.0) == jank   # ratio unchanged — weather cancels
    assert stall_time(slow) == 1600.0                 # absolute drifts (doubles)


def test_jank_fraction_bounds_and_guards():
    from pathbrain.interpret.smoothness import jank_fraction

    # Bounded to [0,1] even if a stall overruns a short window.
    assert jank_fraction([0.0, 5000.0], 0.0, 1000.0) == 1.0
    # No perceptible stall → 0.0 (a real, comparable reading).
    assert jank_fraction([0.0, 100.0, 200.0], 0.0, 1000.0) == 0.0
    # Missing / too-short window → None (can't host a perceptible stall).
    assert jank_fraction([0.0, 900.0], None, 900.0) is None
    assert jank_fraction([0.0, 900.0], 0.0, 100.0) is None


def test_derive_browser_emits_jank_fraction():
    raw = {"urls": {"u": {
        "nav": {"responseStart": 100.0, "responseEnd": 200.0, "loadEventEnd": 1100.0},
        "paint": {"fcp": 150.0, "lcp": 1100.0, "cls_entries": []},
        "resources": [
            {"responseEnd": 200.0, "transferSize": 100},
            {"responseEnd": 1000.0, "transferSize": 100},  # ~800ms stall inside the window
        ],
        "total_render_ms": 1200.0, "filmstrip": [],
    }}}
    m = derive("browser", raw)
    assert "jank_fraction" in m and 0.0 <= m["jank_fraction"] <= 1.0
