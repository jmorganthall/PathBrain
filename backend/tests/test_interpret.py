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
