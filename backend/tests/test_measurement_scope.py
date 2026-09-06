"""Runs measure only what the methodology requires (plus the always-on portable reference),
and the idle-wait audit reads what the post-load settle buys off stored raw."""
from __future__ import annotations

from pathbrain.database import session_scope
from pathbrain.idle_audit import audit_observations
from pathbrain.methodology import METHODOLOGY_REGISTRY, build_definition_from_spec, required_plugins
from pathbrain.models import Run
from pathbrain.plugins import iter_plugins
from pathbrain.runner import create_run, measurement_scope

PLUGINS = [p.name for p in iter_plugins()]
V16 = build_definition_from_spec(METHODOLOGY_REGISTRY["speed-smoothness-v16"])
V15 = build_definition_from_spec(METHODOLOGY_REGISTRY["speed-smoothness-v15"])


def test_required_plugins_is_the_browser_under_the_current_crown():
    # The crown is FCP × LCP × network_stall_all (+ the flagged longest_stall): browser-only.
    assert required_plugins(V16) == {"browser"}
    assert required_plugins(V15) == {"browser"}
    # A pre-v5 style definition with no overall spec and no flags requires nothing.
    assert required_plugins({"metrics": [{"key": "dns", "axis": "completion"}]}) == set()


def test_scope_skips_the_probes_keeps_the_browser_and_the_portable_reference():
    scope = measurement_scope({"measurement": {"methodology_only": True, "always": ["portable"]}}, V16, PLUGINS)
    assert set(scope["skipped"]) == {"icmp", "dns", "tcp", "tls", "http"}
    assert scope["kept"] == ["browser", "portable"] and scope["uncapped"] == ["browser"]
    # The default config section means the same thing.
    assert measurement_scope({}, V16, PLUGINS)["skipped"] == scope["skipped"] or measurement_scope({}, V16, PLUGINS)["kept"] == ["browser"]
    # A plugin the metric registry doesn't know supplies nothing the methodology grades, so
    # the scope leaves it alone (a test double, an experiment).
    with_fake = measurement_scope({}, V16, PLUGINS + ["threadbound"])
    assert "threadbound" in with_fake["kept"] and "threadbound" not in with_fake["skipped"]
    # Off → the full suite, nothing skipped, caps untouched.
    off = measurement_scope({"measurement": {"methodology_only": False}}, V16, PLUGINS)
    assert off["skipped"] == [] and off["uncapped"] == [] and off["kept"] == PLUGINS


def test_create_run_bakes_the_scope_into_config_used():
    run_id = create_run(iterations=3)
    with session_scope() as s:
        cfg = s.get(Run, run_id).config_used
        for name in ("icmp", "dns", "tcp", "tls", "http"):
            assert cfg[name]["skip"] is True, name
        # The browser's per-plugin cap is lifted: every iteration measures the crown.
        assert cfg["browser"].get("iterations") is None
        # The portable reference keeps its own cap and is not skipped.
        assert cfg["portable"].get("skip") is not True and cfg["portable"]["iterations"] == 2
        # The decision is on the record.
        assert cfg["measurement"]["applied"]["required"] == ["browser"]
        assert set(cfg["measurement"]["applied"]["skipped"]) == {"icmp", "dns", "tcp", "tls", "http"}
        # The HTTP URL list is untouched, so the run's site-set stamp is unchanged by the scope.
        assert cfg["http"]["urls"]


def test_a_cap_an_engine_set_on_purpose_is_kept():
    # The profile test lifts the browser cap to exactly N; the scope must not replace it.
    run_id = create_run(iterations=5, config_overrides={"browser": {"iterations": 5}})
    with session_scope() as s:
        assert s.get(Run, run_id).config_used["browser"]["iterations"] == 5


def test_an_engines_own_skip_survives_and_full_suite_is_one_switch():
    # The duel's browser-only leg skips the portable plugin too; the scope must not un-skip it.
    run_id = create_run(iterations=1, config_overrides={"portable": {"skip": True}})
    with session_scope() as s:
        cfg = s.get(Run, run_id).config_used
        assert cfg["portable"]["skip"] is True
    run_id = create_run(iterations=1, config_overrides={"measurement": {"methodology_only": False}})
    with session_scope() as s:
        cfg = s.get(Run, run_id).config_used
        assert cfg["icmp"].get("skip") is not True and cfg["browser"]["iterations"] == 2
        assert cfg["measurement"]["applied"]["methodology_only"] is False


def _obs(lcp, load_end, render):
    return {"nav": {"loadEventEnd": load_end}, "paint": {"lcp": lcp}, "total_render_ms": render}


def test_idle_audit_reads_late_lcp_and_idle_wait_off_raw():
    obs = [
        ("https://a/", _obs(300, 600, 1400)),   # LCP before load; waited 0.8 s after load
        ("https://a/", _obs(320, 610, 5610)),   # never went idle: the 5 s cap
        ("https://b/", _obs(900, 700, 1300)),   # LCP 200 ms AFTER load
        ("https://b/", _obs(None, 700, 1200)),  # no LCP captured
    ]
    out = audit_observations(obs, current_cap_s=5.0)
    assert out["loads"] == 4
    a = next(s for s in out["sites"] if s["url"] == "https://a/")
    b = next(s for s in out["sites"] if s["url"] == "https://b/")
    assert a["lcp_after_load"] == 0 and a["median_idle_wait_ms"] == 2900.0
    assert b["lcp_after_load"] == 1 and b["max_lag_ms"] == 200.0 and b["with_lcp"] == 1
    assert out["worst_lcp_after_load_ms"] == 200.0
    # Worst lag 200 ms + 500 ms margin → 1 s is the smallest safe cap (never above the current one).
    assert out["recommended_networkidle_timeout_s"] == 1.0
    assert "keep the cap" in out["verdict"]
    # With no late LCP at all the verdict says the wait never moved LCP.
    clean = audit_observations([("https://a/", _obs(300, 600, 1400))], current_cap_s=5.0)
    assert clean["worst_lcp_after_load_ms"] == 0.0 and "never moved LCP" in clean["verdict"]
    assert audit_observations([])["recommended_networkidle_timeout_s"] is None


def test_idle_audit_endpoint(client):
    body = client.get("/api/methodologies/idle-audit?limit=50").json()
    assert "sites" in body and "runs" in body and body["current_networkidle_timeout_s"] == 5.0
