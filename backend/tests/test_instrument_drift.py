"""Instrument drift: telling "the run got bigger" from "the measurement got slower", a step
from a drift, a publish from a pooled instrument — and saying whether a graded number moved."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain import runner
from pathbrain.database import session_scope
from pathbrain.instrument_drift import (
    DERIVATION_VERSION,
    MIN_SAMPLES,
    UNRECORDED_CLIENT,
    RunSample,
    assess,
    client_label,
    cohorts,
    declared_clients,
    instrument_drift,
    load_samples,
    run_kind,
    steps,
    trend,
)
from pathbrain.methodology import collection_from_lists
from pathbrain.models import BenchmarkResult, Methodology, Run, RunStatus, ScoreResult
from pathbrain.plugins.base import PluginResult

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
DAYS = 12
# Twelve runs a day from midnight, so every day-cohort clears the step detector's floor and a
# regime change at HALF lands exactly on a day boundary.
PER_DAY = 12
BASE = (NOW - timedelta(days=DAYS)).replace(hour=0, minute=0)
OLD_CLIENT = "legacy · default viewport · Chrome 120 · headless UA · automation visible"
NEW_CLIENT = "new · 1920×1080 · Chrome 128 · desktop UA · automation hidden"


def _samples(**series) -> list[RunSample]:
    """One run every 6 hours for DAYS days; each keyword is a function of the run's index
    ``i`` (0..n-1) giving that quantity, so a test states exactly what moves. ``version``,
    ``client`` and ``browser_samples`` may be given the same way."""
    n = DAYS * PER_DAY
    out = []
    for i in range(n):
        f = {k: fn(i) for k, fn in series.items()}
        pages = f.get("pages", 6)
        page_wall = f.get("page_wall_ms", 3000.0)
        wall = f.get("browser_wall_ms", pages * page_wall + f.get("overhead_ms", 4000.0))
        share = f.get("browser_share", 1.0)
        out.append(
            RunSample(
                id=i + 1,
                at=BASE + timedelta(hours=24 / PER_DAY * i),
                kind="duel",
                iterations=5,
                methodology_version=f.get("version", "v1"),
                methodology_only=True,
                per_iteration_ms=f.get("per_iteration_ms", wall * share),
                browser_wall_ms=wall,
                browser_samples=f.get("browser_samples", int(round(5 * share))),
                pages=pages,
                page_wall_ms=page_wall,
                page_clock_ms=f.get("page_clock_ms", 1200.0),
                fcp_ms=f.get("fcp_ms", 300.0),
                lcp_ms=f.get("lcp_ms", 350.0),
                network_stall_all_ms=f.get("network_stall_all_ms", 40.0),
                nav_render_ms=f.get("nav_render_ms", 120.0),
                inp_ms=f.get("inp_ms", 30.0),
                cls=f.get("cls", 0.01),
                nav_network_ms=f.get("nav_network_ms", 200.0),
                client=f.get("client", NEW_CLIENT),
                phases=f.get("phases"),
            )
        )
    return out


def _wobble(i: int, base: float, amp: float) -> float:
    return base + (amp if i % 2 else -amp)


HALF = DAYS * PER_DAY // 2  # the run index at which day 6 begins
STEP_DAY = (BASE + timedelta(days=HALF // PER_DAY)).strftime("%Y-%m-%d")


def test_trend_needs_both_a_significant_rank_correlation_and_a_material_shift():
    pts = [(float(i), 100.0 + 2.0 * i) for i in range(30)]
    t = trend(pts, floor=10.0)
    assert t["rho"] == 1.0 and t["drifts"] and t["direction"] == "up" and t["shift_pct"] > 30
    tiny = trend([(float(i), 100.0 + 0.05 * i) for i in range(30)], floor=10.0)
    assert tiny["rho"] == 1.0 and tiny["drifts"] is False
    flat = trend([(float(i), _wobble(i, 100.0, 5.0)) for i in range(30)], floor=1.0)
    assert flat["drifts"] is False
    assert trend([(0.0, 1.0)] * (MIN_SAMPLES - 1)) is None


def test_a_rising_client_role_metric_means_the_instrument_drifted_and_grading_is_at_risk():
    s = _samples(nav_render_ms=lambda i: 120.0 + 0.5 * i, fcp_ms=lambda i: 300.0 + 0.5 * i)
    out = assess(s, days=DAYS, now=NOW)
    assert out["verdict"] == "instrument" and out["grading_at_risk"] is True
    assert out["findings"][0]["key"] == "instrument" and out["findings"][0]["severity"] == "bad"
    assert "shaping-immune" in out["findings"][0]["text"]
    assert out["trends"]["nav_render_ms"]["drifts"] and out["trends"]["nav_network_ms"]["drifts"] is False


def test_the_methodology_only_scope_lengthens_the_iteration_without_moving_a_graded_number():
    s = _samples(browser_share=lambda i: 0.4 if i < HALF else 1.0)
    out = assess(s, days=DAYS, now=NOW)
    assert out["verdict"] == "mix" and out["grading_at_risk"] is False
    mix = next(f for f in out["findings"] if f["key"] == "mix")
    assert mix["severity"] == "ok" and "browser iterations per suite iteration" in mix["text"]
    assert mix["impact_ms"] > 0
    assert out["trends"]["per_iteration_ms"]["drifts"] and not out["trends"]["browser_wall_ms"]["drifts"]


def test_overhead_outside_page_loads_is_named_when_the_page_time_is_flat():
    s = _samples(overhead_ms=lambda i: 4000.0 + 150.0 * i)
    out = assess(s, days=DAYS, now=NOW)
    assert out["verdict"] == "overhead" and out["grading_at_risk"] is False
    over = next(f for f in out["findings"] if f["key"] == "overhead")
    assert over["severity"] == "warn" and "phase timers" in over["text"]


def test_measured_phases_say_which_part_of_the_overhead_grew():
    # The plugin's own phase totals ride along: the close got slower, nothing else did.
    s = _samples(
        overhead_ms=lambda i: 4000.0 + 150.0 * i,
        phases=lambda i: {"context_ms": 600.0, "goto_ms": 7000.0, "idle_ms": 9000.0, "reads_ms": 1500.0, "close_ms": 300.0 + 150.0 * i},
    )
    out = assess(s, days=DAYS, now=NOW)
    over = next(f for f in out["findings"] if f["key"] == "overhead")
    assert "Measured by phase: context close" in over["text"] and "context setup" not in over["text"]
    assert out["trends"]["phase_close_ms"]["drifts"] and not out["trends"]["phase_context_ms"]["drifts"]


def test_wall_clock_findings_are_ranked_by_seconds_per_browser_iteration_not_check_order():
    # Idle wait grows 0.5 s per page over 6 pages (≈ +3 s per iteration); overhead grows +6 s.
    # The first reading of real data headlined idle because it was checked first; the
    # bigger contributor must lead, and the headline must name both.
    s = _samples(page_wall_ms=lambda i: 3000.0 + 4.0 * i, overhead_ms=lambda i: 4000.0 + 50.0 * i)
    out = assess(s, days=DAYS, now=NOW)
    wall = [f for f in out["findings"] if f.get("impact_ms") is not None]
    assert [f["key"] for f in wall] == ["overhead", "idle"]
    assert out["verdict"] == "overhead"
    assert "outside page loads" in out["headline"] and "idle wait" in out["headline"]


def test_a_slower_link_moves_the_page_clock_and_the_network_phases_together():
    s = _samples(page_clock_ms=lambda i: 1200.0 + 5.0 * i, nav_network_ms=lambda i: 200.0 + 3.0 * i,
                 lcp_ms=lambda i: 350.0 + 3.0 * i)
    out = assess(s, days=DAYS, now=NOW)
    assert out["verdict"] == "network" and out["grading_at_risk"] is False
    net = next(f for f in out["findings"] if f["key"] == "network")
    assert net["severity"] == "info" and "instrument is fine" in net["text"]


def test_a_growing_idle_wait_and_a_page_clock_nobody_explains_each_get_their_own_finding():
    s = _samples(page_wall_ms=lambda i: 3000.0 + 25.0 * i)
    out = assess(s, days=DAYS, now=NOW)
    assert out["verdict"] == "idle"
    idle = next(f for f in out["findings"] if f["key"] == "idle")
    assert "per browser iteration over 6 pages" in idle["text"]
    s = _samples(page_clock_ms=lambda i: 1200.0 + 5.0 * i)
    out = assess(s, days=DAYS, now=NOW)
    assert out["verdict"] == "unattributed" and out["findings"][0]["severity"] == "warn"


def test_stable_window_and_the_insufficient_case():
    s = _samples(nav_render_ms=lambda i: _wobble(i, 120.0, 4.0), per_iteration_ms=lambda i: _wobble(i, 22000.0, 500.0))
    out = assess(s, days=DAYS, now=NOW)
    assert out["verdict"] == "stable" and out["findings"][0]["key"] == "stable" and out["steps"] == []
    thin = assess(s[: MIN_SAMPLES - 1], days=DAYS, now=NOW)
    assert thin["verdict"] == "insufficient" and thin["findings"] == [] and "widen" in thin["headline"]


# ── Steps: the day something changed ─────────────────────────────────────────────────


def _step_field(**extra):
    """Every page metric doubles (or worse) from day 6 on — the real fortnight's shape."""
    late = lambda a, b: (lambda i: a if i < HALF else b)  # noqa: E731
    return _samples(
        page_clock_ms=late(600.0, 3000.0), nav_render_ms=late(93.0, 316.0), fcp_ms=late(301.0, 665.0),
        lcp_ms=late(321.0, 766.0), nav_network_ms=late(131.0, 280.0), **extra,
    )


def test_a_thirds_trend_hides_a_step_but_the_step_detector_names_the_day():
    s = _step_field()
    st = {x["key"]: x for x in steps(cohorts(s, bucket="day"))}
    assert st["nav_render_ms"]["at"] == STEP_DAY
    assert st["nav_render_ms"]["before"] == 93.0 and st["nav_render_ms"]["after"] == 316.0
    assert st["page_clock_ms"]["shift_pct"] == 400.0 and st["fcp_ms"]["direction"] == "up"
    # Both cohorts on either side must be populated: a one-run day cannot make a step.
    thin = cohorts(s, bucket="day")
    thin[HALF // PER_DAY]["runs"] = 1
    assert all(x["at"] != STEP_DAY for x in steps(thin))


def test_a_client_change_under_a_version_that_declares_it_is_a_publish_not_a_drift():
    # Day 6: new version v2 takes effect and declares the client the later runs measured as.
    s = _step_field(version=lambda i: "v1" if i < HALF else "v2", client=lambda i: OLD_CLIENT if i < HALF else NEW_CLIENT)
    out = assess(s, days=DAYS, now=NOW, version_clients={"v1": False, "v2": True})
    assert out["verdict"] == "published" and out["grading_at_risk"] is False
    pub = next(f for f in out["findings"] if f["key"] == "published")
    assert "declares that client" in pub["text"] and pub["at"] == out["steps"][0]["at"]
    # Trends were computed within v2, where nothing moved — so no `instrument` finding.
    assert out["scope"] == {"methodology": "v2", "runs": HALF, "within_version": True}
    assert "instrument" not in {f["key"] for f in out["findings"]}
    assert "by design" in out["headline"]


def test_a_client_change_under_a_version_that_declares_none_pools_two_instruments():
    # The real case: v16 adopted from code the day the real-browser client landed; it carried
    # the prior collection forward with no client, so old-client and new-client runs pool.
    s = _step_field(version=lambda i: "v15" if i < HALF else "v16", client=lambda i: OLD_CLIENT if i < HALF else NEW_CLIENT)
    out = assess(s, days=DAYS, now=NOW, version_clients={"v15": False, "v16": False})
    assert out["verdict"] == "pooled_instruments" and out["grading_at_risk"] is True
    bad = next(f for f in out["findings"] if f["key"] == "pooled_instruments")
    assert bad["severity"] == "bad" and "Publish sites + client" in bad["text"] and "v16" in bad["text"]
    assert "pooled under one version" in out["headline"]
    # An unstamped earlier client (runs from before the client block existed) still counts as
    # a different client — that is exactly how the first real reading presented.
    s = _step_field(version=lambda i: "v16", client=lambda i: UNRECORDED_CLIENT if i < HALF else NEW_CLIENT)
    out = assess(s, days=DAYS, now=NOW, version_clients={"v16": False})
    assert out["verdict"] == "pooled_instruments"


def test_a_step_with_only_a_version_change_or_with_nothing_changed_is_reported_as_such():
    s = _step_field(version=lambda i: "v1" if i < HALF else "v2")
    out = assess(s, days=DAYS, now=NOW, version_clients={"v1": False, "v2": False})
    assert out["verdict"] == "published"
    pub = next(f for f in out["findings"] if f["key"] == "published")
    assert "code-shipped adoption" in pub["text"]
    s = _step_field()
    out = assess(s, days=DAYS, now=NOW)
    assert out["verdict"] == "unexplained_step" and out["findings"][0]["severity"] == "warn"
    assert "same recorded client" in out["findings"][0]["text"]


def test_trends_fall_back_to_the_whole_window_when_the_version_in_force_is_too_thin():
    s = _samples(version=lambda i: "v1" if i < DAYS * PER_DAY - 3 else "v2", nav_render_ms=lambda i: 120.0 + 1.0 * i)
    out = assess(s, days=DAYS, now=NOW)
    assert out["scope"]["within_version"] is False and out["scope"]["runs"] == DAYS * PER_DAY
    assert out["verdict"] == "instrument"


def test_days_on_which_the_browser_failed_on_most_runs_are_named():
    s = _samples(browser_samples=lambda i: 0 if PER_DAY * 2 <= i < PER_DAY * 4 else 5)
    out = assess(s, days=DAYS, now=NOW)
    failed = next(f for f in out["findings"] if f["key"] == "browser_failed")
    assert "no successful browser iteration" in failed["text"]
    coh = {c["key"]: c for c in out["cohorts"]}
    shares = sorted(c["browser_failed_share"] for c in coh.values())
    assert shares[-1] == 1.0 and shares[0] == 0.0


def test_cohorts_carry_version_client_and_the_run_mix():
    s = _samples(browser_share=lambda i: 0.4 if i < HALF else 1.0, client=lambda i: OLD_CLIENT if i < HALF else NEW_CLIENT)
    out = assess(s, days=DAYS, now=NOW)
    assert out["window"]["bucket"] == "day" and len(out["cohorts"]) in (DAYS, DAYS + 1)
    first, last = out["cohorts"][0], out["cohorts"][-1]
    assert first["medians"]["browser_share"] == 0.4 and last["medians"]["browser_share"] == 1.0
    assert first["client"] == OLD_CLIENT and last["client"] == NEW_CLIENT and first["version"] == "v1"
    assert first["kinds"] == {"duel": first["runs"]} and first["methodology_only_share"] == 1.0
    assert assess(s, days=2, now=NOW)["window"]["bucket"] == "hour"


def test_client_label_reads_the_client_block_into_one_line():
    assert client_label(None) == UNRECORDED_CLIENT
    lab = client_label({"headless_mode": "new", "viewport": {"width": 1920, "height": 1080},
                        "chromium_version": "128.0.6613.18", "user_agent": "Mozilla/5.0 (X11) Chrome/128", "hide_automation": True})
    assert lab == NEW_CLIENT
    shell = client_label({"headless": True, "user_agent": "Mozilla/5.0 HeadlessChrome/120.0", "chromium_version": "120.0.1"})
    assert shell.startswith("headless shell · default viewport · Chrome 120 · headless UA")


def test_run_kind_reads_the_engine_off_label_or_job_group():
    assert run_kind("duel · Speedy Sloth", "duel-4") == "duel"
    assert run_kind("test · q1514", "profile_test-9") == "test"
    assert run_kind("race · Tall Garland", None) == "race"
    assert run_kind("scheduled", None) == "monitoring"
    assert run_kind(None, None) == "manual"


def test_aggregate_medians_the_plugins_phase_totals_over_iterations():
    def _r(close: float) -> PluginResult:
        return PluginResult(plugin="browser", success=True, duration_ms=1000.0, raw={}, metrics={"fcp_ms": 300.0},
                            details={"client": {"headless_mode": "new"}, "phases": {"context_ms": 500.0, "goto_ms": 6000.0, "close_ms": close}})
    agg = runner._aggregate([_r(100.0), _r(900.0), _r(300.0)])
    assert agg["details"]["phases"] == {"close_ms": 300.0, "context_ms": 500.0, "goto_ms": 6000.0}
    assert agg["details"]["client"] == {"headless_mode": "new"}
    # No phases anywhere → no key, rather than an empty dict pretending to be a measurement.
    plain = runner._aggregate([PluginResult(plugin="browser", success=True, duration_ms=1.0, raw={}, metrics={}, details={})])
    assert "phases" not in plain["details"]


def _seed_db(n: int = 20) -> list[int]:
    """Completed runs over the last ``n`` days with a browser result each (scalars only): the
    first half as the old client under v-old, the second half as the new client under v-new."""
    ids = []
    with session_scope() as s:
        for i in range(n):
            at = NOW - timedelta(days=n - i, hours=1)
            new = i >= n // 2
            run = Run(
                status=RunStatus.COMPLETE, created_at=at.replace(tzinfo=None), finished_at=at.replace(tzinfo=None),
                label="duel · Fast Fox", job_group="duel-1", iterations=3, iterations_completed=3,
                per_iteration_ms=20000.0 + 1000.0 * i, methodology_version="drift-v-new" if new else "drift-v-old",
                config_used={
                    "browser": {"urls": ["https://a/", "https://b/", "https://c/"], "iterations": None, "networkidle_timeout_s": 5.0},
                    "measurement": {"applied": {"methodology_only": True}},
                },
            )
            s.add(run)
            s.flush()
            client = ({"headless_mode": "new", "viewport": {"width": 1920, "height": 1080}, "chromium_version": "128.0.1",
                       "user_agent": "Mozilla/5.0 Chrome/128", "hide_automation": True} if new else None)
            details = {"samples": 3, "phases": {"context_ms": 400.0, "goto_ms": 5000.0, "idle_ms": 9000.0, "reads_ms": 1200.0, "close_ms": 250.0}}
            if client:
                details["client"] = client
            s.add(
                BenchmarkResult(
                    run_id=run.id, plugin="browser", success=True, duration_ms=18000.0 + 1000.0 * i,
                    metrics={
                        "total_render_ms": 3000.0, "load_event_ms": 1200.0, "fcp_ms": 300.0, "lcp_ms": 350.0,
                        "network_stall_all_ms": 40.0, "nav_render_ms": 100.0 + 5.0 * i, "inp_ms": 30.0, "cls": 0.01,
                        "nav_dns_ms": 2.0, "nav_tcp_ms": 10.0, "nav_tls_ms": 20.0, "nav_request_ms": 50.0, "nav_response_ms": 30.0,
                    },
                    details=details, raw={},
                )
            )
            s.add(ScoreResult(run_id=run.id, sops=50.0, derivation_version=DERIVATION_VERSION if i % 2 else "derive-v1"))
            ids.append(run.id)
        # One version declares a client, the other carries only sites forward.
        for v, client in (("drift-v-old", None), ("drift-v-new", {"headless_mode": "new", "viewport": {"width": 1920, "height": 1080}})):
            if s.get(Methodology, v) is None:
                s.add(Methodology(version=v, rubric_version=v, derivation_version=DERIVATION_VERSION,
                                  definition={"collection": collection_from_lists(["https://a/"], [], client)}, is_current=False))
    return ids


def test_load_samples_reads_scalars_through_json_paths_and_the_audit_runs_end_to_end(client):
    ids = _seed_db(20)
    with session_scope() as s:
        samples = {x.id: x for x in load_samples(s, days=30, now=NOW)}
        for rid in ids:
            assert rid in samples
        old, new = samples[ids[0]], samples[ids[-1]]
        assert new.kind == "duel" and new.iterations == 3 and new.pages == 3 and new.methodology_only is True
        assert new.browser_samples == 3 and new.browser_share == 1.0 and new.idle_cap_s == 5.0
        assert new.page_wall_ms == 3000.0 and new.page_clock_ms == 1200.0 and new.idle_wait_ms == 1800.0
        assert new.nav_network_ms == 112.0 and new.overhead_ms == new.browser_wall_ms - 3 * 3000.0
        assert new.client == NEW_CLIENT and old.client == UNRECORDED_CLIENT
        assert new.value("phase_close_ms") == 250.0 and new.phases["goto_ms"] == 5000.0
        few = load_samples(s, days=30, limit=5, now=NOW)
        assert 5 <= len(few) < len(samples) and few[0].id == min(samples)
        assert declared_clients(s, ["drift-v-old", "drift-v-new", "no-such-version"]) == {
            "drift-v-old": False, "drift-v-new": True, "no-such-version": False,
        }
        out = instrument_drift(s, days=30, now=NOW)
    assert out["window"]["runs"] >= 20 and out["stale_derivations"] >= 10
    assert out["version_clients"]["drift-v-new"] is True
    assert out["processes"] is None or "available" in out["processes"]
    body = client.get("/api/methodologies/instrument-drift?days=30&limit=500").json()
    assert body["verdict"] in {
        "instrument", "pooled_instruments", "unexplained_step", "unattributed", "network", "overhead", "idle",
        "mix", "published", "browser_failed", "stable", "insufficient",
    }
    assert "cohorts" in body and "trends" in body and "steps" in body and body["derivation_version"] == DERIVATION_VERSION
