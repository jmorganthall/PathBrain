"""The repeat-visit load: warm metrics beside the cold ones, never in their place — and the
one question that decides whether the crown should read them."""
from __future__ import annotations

from datetime import datetime, timezone

from pathbrain import warm_agreement
from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.interpret.derive import DERIVATION_VERSION, derive
from pathbrain.methodology import ensure_current_methodology
from pathbrain.metrics import METRIC_ROLES, METRICS, all_metric_sources
from pathbrain.models import BenchmarkResult, Run, RunStatus

NAV = {"startTime": 0, "fetchStart": 5, "domainLookupStart": 5, "domainLookupEnd": 7, "connectStart": 7,
       "secureConnectionStart": 20, "connectEnd": 40, "requestStart": 41, "responseStart": 90, "responseEnd": 120,
       "domContentLoadedEventEnd": 300, "loadEventEnd": 500}
WARM_NAV = {**NAV, "domainLookupStart": 5, "domainLookupEnd": 5, "connectStart": 5, "secureConnectionStart": 0,
            "connectEnd": 5, "requestStart": 6, "responseStart": 40, "responseEnd": 60, "domContentLoadedEventEnd": 200,
            "loadEventEnd": 320}


def _obs(nav, fcp, lcp, warm=None):
    return {"nav": nav, "paint": {"fcp": fcp, "lcp": lcp, "inp": 20, "cls_entries": []}, "total_render_ms": 900.0,
            "resources": [], "loaf": None, **({"warm": warm} if warm is not None else {})}


def test_warm_block_derives_beside_the_cold_reading_and_is_omitted_when_absent():
    warm = {"nav": WARM_NAV, "paint": {"fcp": 150, "lcp": 170, "inp": 18, "cls_entries": []}, "total_render_ms": 600.0,
            "resources": [], "loaf": None}
    m = derive("browser", {"urls": {"https://a/": _obs(NAV, 320, 340, warm)}})
    assert m["fcp_ms"] == 320.0 and m["lcp_ms"] == 340.0 and m["load_event_ms"] == 500.0
    assert m["warm_fcp_ms"] == 150.0 and m["warm_lcp_ms"] == 170.0 and m["warm_load_event_ms"] == 320.0
    assert m["warm_total_render_ms"] == 600.0 and m["warm_nav_render_ms"] is not None
    # The cold reading is untouched by the warm one: nothing graded moves.
    cold_only = derive("browser", {"urls": {"https://a/": _obs(NAV, 320, 340)}})
    assert {k: v for k, v in m.items() if not k.startswith("warm_")} == cold_only
    assert not any(k.startswith("warm_") for k in cold_only)
    # A failed warm load is absent, not zero.
    failed = derive("browser", {"urls": {"https://a/": _obs(NAV, 320, 340, {"error": "TimeoutError: x"})}})
    assert not any(k.startswith("warm_") for k in failed)
    assert DERIVATION_VERSION == "derive-v15"


def test_the_warm_metrics_are_registry_metrics_display_only_with_their_cold_twins_roles():
    by_key = {m.key: m for m in METRICS}
    for key, role in (("warm_fcp", "O"), ("warm_lcp", "O"), ("warm_load_event", "O"), ("warm_network_stall_all", "S"), ("warm_nav_render", "C")):
        assert by_key[key].axis is None and by_key[key].plugin == "browser"
        assert by_key[key].source_key == key + "_ms" and METRIC_ROLES[key] == role
    assert all_metric_sources()["warm_fcp"] == ("browser", "warm_fcp_ms")


def _definition():
    with session_scope() as s:
        return dict(ensure_current_methodology(s, get_config(s)).definition or {})


def _profile(cold, warm, n=5):
    return [{"cold": dict(cold), "warm": dict(warm)} for _ in range(n)]


def test_agreement_when_the_two_instruments_rank_the_field_alike():
    d = _definition()
    sources = warm_agreement.crown_sources(d)
    assert set(sources) == {"fcp", "lcp", "network_stall_all"}
    fast = {"fcp": 250.0, "lcp": 300.0, "network_stall_all": 40.0}
    mid = {"fcp": 400.0, "lcp": 500.0, "network_stall_all": 80.0}
    slow = {"fcp": 900.0, "lcp": 1200.0, "network_stall_all": 200.0}
    warm = lambda c: {k: v * 0.6 for k, v in c.items()}  # noqa: E731 — warm is faster everywhere, same order
    profiles = {"a": _profile(fast, warm(fast)), "b": _profile(mid, warm(mid)), "c": _profile(slow, warm(slow)), "thin": _profile(fast, warm(fast), n=2)}
    out = warm_agreement.assess(profiles, definition=d, sources=sources, min_runs=5, crown_fp="a", names={"a": "Alpha"})
    assert out["verdict"] == "agree" and out["rho"] == 1.0 and out["profiles"] == 3 and out["thin_profiles"] == 1
    assert out["top_cold"]["name"] == "Alpha" and out["agree_top"] and out["crown"] == {"fingerprint": "a", "cold_rank": 1, "warm_rank": 1}
    rows = {r["fingerprint"]: r for r in out["rows"]}
    assert rows["a"]["cold_rank"] == 1 and rows["c"]["warm_rank"] == 3 and rows["a"]["warm_minus_cold"] > 0
    assert "no methodology change" in out["text"]


def test_disagreement_names_both_number_ones_and_the_crowns_two_ranks():
    d = _definition()
    sources = warm_agreement.crown_sources(d)
    a_cold, a_warm = {"fcp": 250.0, "lcp": 300.0, "network_stall_all": 40.0}, {"fcp": 240.0, "lcp": 290.0, "network_stall_all": 120.0}
    b_cold, b_warm = {"fcp": 400.0, "lcp": 500.0, "network_stall_all": 80.0}, {"fcp": 140.0, "lcp": 160.0, "network_stall_all": 20.0}
    c_cold, c_warm = {"fcp": 900.0, "lcp": 1200.0, "network_stall_all": 200.0}, {"fcp": 600.0, "lcp": 800.0, "network_stall_all": 150.0}
    profiles = {"a": _profile(a_cold, a_warm), "b": _profile(b_cold, b_warm), "c": _profile(c_cold, c_warm)}
    out = warm_agreement.assess(profiles, definition=d, sources=sources, min_runs=5, crown_fp="a", names={"a": "Alpha", "b": "Bravo"})
    assert out["verdict"] == "disagree" and out["top_cold"]["name"] == "Alpha" and out["top_warm"]["name"] == "Bravo"
    assert out["crown"]["cold_rank"] == 1 and out["crown"]["warm_rank"] == 2
    assert "adopt the warm legs" in out["text"] and "#1 cold and #2 warm" in out["text"]
    # Fewer than three profiles: no verdict, and the sentence says what would fill it in.
    thin = warm_agreement.assess({"a": profiles["a"], "b": profiles["b"]}, definition=d, sources=sources, min_runs=5)
    assert thin["verdict"] == "insufficient" and "needs three" in thin["text"]


def test_load_profiles_reads_paired_readings_through_json_paths_and_the_endpoint_answers(client):
    d = _definition()
    sources = warm_agreement.crown_sources(d)
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    with session_scope() as s:
        for fp, cold, warm in (("wa-fast", (250.0, 300.0, 40.0), (150.0, 180.0, 20.0)), ("wa-slow", (900.0, 1200.0, 200.0), (500.0, 700.0, 100.0))):
            for i in range(6):
                run = Run(status=RunStatus.COMPLETE, created_at=now.replace(tzinfo=None), settings_fingerprint=fp, iterations=1, iterations_completed=1)
                s.add(run)
                s.flush()
                metrics = {"fcp_ms": cold[0] + i, "lcp_ms": cold[1] + i, "network_stall_all_ms": cold[2],
                           "warm_fcp_ms": warm[0] + i, "warm_lcp_ms": warm[1] + i, "warm_network_stall_all_ms": warm[2]}
                if i == 5:
                    metrics.pop("warm_lcp_ms")  # a run missing one warm leg is not a pair
                s.add(BenchmarkResult(run_id=run.id, plugin="browser", success=True, metrics=metrics, details={}, raw={}))
        # A cold-only run (no warm block at all) is never loaded.
        run = Run(status=RunStatus.COMPLETE, created_at=now.replace(tzinfo=None), settings_fingerprint="wa-fast", iterations=1, iterations_completed=1)
        s.add(run)
        s.flush()
        s.add(BenchmarkResult(run_id=run.id, plugin="browser", success=True, metrics={"fcp_ms": 250.0, "lcp_ms": 300.0, "network_stall_all_ms": 40.0}, details={}, raw={}))
    with session_scope() as s:
        profiles = warm_agreement.load_profiles(s, sources)
        assert len(profiles["wa-fast"]) == 5 and len(profiles["wa-slow"]) == 5
        assert profiles["wa-fast"][0]["warm"]["fcp"] < profiles["wa-fast"][0]["cold"]["fcp"]
        out = warm_agreement.warm_agreement(s, get_config(s), min_runs=5)
    assert out["methodology"] and out["runs_with_warm_reading"] >= 10
    body = client.get("/api/methodologies/warm-agreement?min_runs=5").json()
    assert body["verdict"] in {"insufficient", "agree", "same_top", "disagree"} and "rows" in body and body["crown_metrics"]
