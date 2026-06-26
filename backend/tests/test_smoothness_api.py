"""Tests for the smoothness API: per-run records + two-config comparison."""
from __future__ import annotations

from pathbrain.database import session_scope
from pathbrain.models import BenchmarkResult, Run, RunStatus, ScoreResult


def _chunky_resources():
    # A long blank plateau (80->760) with no long task -> network-attributed stall.
    return [
        {"responseEnd": t, "transferSize": 1000, "nextHopProtocol": "h2"}
        for t in (60, 70, 80, 760, 800)
    ]


def _seed_browser_run(fp: str, *, longest_stall: float, load_event: float) -> int:
    """A completed run with a browser result whose raw carries a resource series."""
    with session_scope() as s:
        run = Run(status=RunStatus.COMPLETE, settings_fingerprint=fp,
                  settings=[{"label": "wan", "quantum": 1514}])
        s.add(run)
        s.flush()
        raw = {
            "iterations": [
                {
                    "urls": {
                        "https://baywest.co": {
                            "nav": {"responseStart": 50.0, "responseEnd": 80.0,
                                    "loadEventEnd": load_event},
                            "paint": {"fcp": 120.0, "lcp": 400.0, "cls_entries": []},
                            "total_render_ms": 1500.0,
                            "filmstrip": [],
                            "resources": _chunky_resources(),
                            "loaf": {"entries": [], "source": "loaf"},
                        }
                    }
                }
            ]
        }
        s.add(BenchmarkResult(
            run_id=run.id, plugin="browser", success=True,
            metrics={
                "longest_stall_ms": longest_stall,
                "cadence_cov": 1.2,
                "byte_earliness_ms": 2000.0,
                "delivery_gini": 0.6,
                "perceived_time_ms": 1800.0,
                "network_stall_ms": longest_stall,
                "render_stall_ms": 0.0,
                "load_event_ms": load_event,
                "lcp_ms": 400.0,
            },
            raw=raw,
        ))
        # latest-rubric marker so complete_only keeps it.
        s.add(ScoreResult(run_id=run.id, sops=70.0, subscores={}, weights_used={},
                          metric_values={"speed_index": 1500.0}))
        s.flush()
        return run.id


def test_smoothness_run_returns_full_record(client):
    rid = _seed_browser_run("fp-run", longest_stall=680.0, load_event=900.0)
    resp = client.get(f"/api/smoothness/run/{rid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["config_tag"] == "fp-run"
    assert body["count"] == 1
    rec = body["records"][0]
    # Smoothness + speed-side travel together; attribution + protocol mix present.
    # FCP (120ms) is injected as a boundary, so the plateau stall is 120->760 = 640.
    assert rec["longest_stall_ms"] == 640.0
    assert rec["load_event_ms"] == 900.0 and rec["lcp_ms"] == 400.0
    assert rec["longest_stall_attribution"] == "network"
    assert rec["protocol_mix"] == {"h2": 5}


def test_smoothness_run_perceived_weight_override(client):
    rid = _seed_browser_run("fp-weights", longest_stall=680.0, load_event=900.0)
    flat = client.get(f"/api/smoothness/run/{rid}", params={"w_unoccupied": 1.0}).json()
    steep = client.get(f"/api/smoothness/run/{rid}", params={"w_unoccupied": 5.0}).json()
    # A higher stall weight raises perceived time (the stall slices cost more).
    assert steep["records"][0]["perceived_time_ms"] > flat["records"][0]["perceived_time_ms"]
    assert steep["perceived_time_params"]["w_unoccupied"] == 5.0


def test_smoothness_run_404_without_browser(client):
    with session_scope() as s:
        run = Run(status=RunStatus.COMPLETE)
        s.add(run)
        s.flush()
        rid = run.id
    assert client.get(f"/api/smoothness/run/{rid}").status_code == 404


def test_smoothness_compare_surfaces_distributions(client):
    _seed_browser_run("cfg-a", longest_stall=120.0, load_event=850.0)
    _seed_browser_run("cfg-a", longest_stall=160.0, load_event=870.0)
    _seed_browser_run("cfg-b", longest_stall=680.0, load_event=820.0)
    _seed_browser_run("cfg-b", longest_stall=700.0, load_event=810.0)

    resp = client.get("/api/smoothness/compare", params={"a": "cfg-a", "b": "cfg-b"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["a"]["runs"] == 2 and body["b"]["runs"] == 2
    # The tradeoff: cfg-b finishes a touch sooner but stalls far more.
    assert body["a"]["smoothness"]["longest_stall_ms"]["p50"] < \
        body["b"]["smoothness"]["longest_stall_ms"]["p50"]
    assert body["b"]["speed"]["load_event_ms"]["p50"] < \
        body["a"]["speed"]["load_event_ms"]["p50"]
    # Percentile keys present and attribution aggregated from raw.
    dist = body["a"]["smoothness"]["perceived_time_ms"]
    assert {"p50", "p75", "p95", "count"} <= set(dist)
    assert body["b"]["attribution"].get("network", 0) >= 1
