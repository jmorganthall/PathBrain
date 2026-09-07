"""Instrument drift: telling "the run got bigger" from "the measurement got slower", and
saying whether a graded number moved."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain.database import session_scope
from pathbrain.instrument_drift import (
    DERIVATION_VERSION,
    MIN_SAMPLES,
    RunSample,
    assess,
    instrument_drift,
    load_samples,
    run_kind,
    trend,
)
from pathbrain.models import BenchmarkResult, Run, RunStatus, ScoreResult

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
DAYS = 12


def _samples(**series) -> list[RunSample]:
    """One run every 6 hours for DAYS days; each keyword is a function of the run's index
    ``i`` (0..n-1) giving that quantity, so a test states exactly what moves."""
    n = DAYS * 4
    base = NOW - timedelta(days=DAYS)
    out = []
    for i in range(n):
        f = {k: fn(i) for k, fn in series.items()}
        pages = f.get("pages", 6)
        page_wall = f.get("page_wall_ms", 3000.0)
        wall = f.get("browser_wall_ms", pages * page_wall + f.get("overhead_ms", 4000.0))
        out.append(
            RunSample(
                id=i + 1,
                at=base + timedelta(hours=6 * i),
                kind="duel",
                iterations=5,
                methodology_only=True,
                per_iteration_ms=f.get("per_iteration_ms", wall * f.get("browser_share", 1.0)),
                browser_wall_ms=wall,
                browser_samples=int(round(5 * f.get("browser_share", 1.0))),
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
            )
        )
    return out


def _wobble(i: int, base: float, amp: float) -> float:
    return base + (amp if i % 2 else -amp)


def test_trend_needs_both_a_significant_rank_correlation_and_a_material_shift():
    # A monotone climb of 50% over 30 points: ρ=1, material → drifts.
    pts = [(float(i), 100.0 + 2.0 * i) for i in range(30)]
    t = trend(pts, floor=10.0)
    assert t["rho"] == 1.0 and t["drifts"] and t["direction"] == "up" and t["shift_pct"] > 30
    # The same monotone climb but only 2 ms end to end: significant, immaterial → not a drift.
    tiny = trend([(float(i), 100.0 + 0.05 * i) for i in range(30)], floor=10.0)
    assert tiny["rho"] == 1.0 and tiny["drifts"] is False
    # Noise around a constant: no drift, and the direction reads flat-ish.
    flat = trend([(float(i), _wobble(i, 100.0, 5.0)) for i in range(30)], floor=1.0)
    assert flat["drifts"] is False
    # Too few points → None.
    assert trend([(0.0, 1.0)] * (MIN_SAMPLES - 1)) is None


def test_a_rising_client_role_metric_means_the_instrument_drifted_and_grading_is_at_risk():
    # Render-to-first-paint climbs 60% over the window while the network phases stay flat:
    # the machine got slower; FCP/LCP carry it. This is the verdict that matters.
    s = _samples(nav_render_ms=lambda i: 120.0 + 1.5 * i, fcp_ms=lambda i: 300.0 + 1.5 * i)
    out = assess(s, days=DAYS, now=NOW)
    assert out["verdict"] == "instrument" and out["grading_at_risk"] is True
    keys = [f["key"] for f in out["findings"]]
    assert keys[0] == "instrument" and out["findings"][0]["severity"] == "bad"
    assert "shaping-immune" in out["findings"][0]["text"]
    assert out["trends"]["nav_render_ms"]["drifts"] and out["trends"]["nav_network_ms"]["drifts"] is False


def test_the_methodology_only_scope_lengthens_the_iteration_without_moving_a_graded_number():
    # First half of the window: the old 2-of-5 browser cap (0.4 browser iterations per suite
    # iteration). Second half: every iteration measures the crown. The tile's number climbs
    # 2.5×; a browser iteration, the page's own clock and every client reading are flat.
    half = DAYS * 2
    s = _samples(browser_share=lambda i: 0.4 if i < half else 1.0)
    out = assess(s, days=DAYS, now=NOW)
    assert out["verdict"] == "mix" and out["grading_at_risk"] is False
    mix = next(f for f in out["findings"] if f["key"] == "mix")
    assert mix["severity"] == "ok" and "browser iterations per suite iteration" in mix["text"]
    assert out["trends"]["per_iteration_ms"]["drifts"] and not out["trends"]["browser_wall_ms"]["drifts"]
    assert out["trends"]["browser_share"]["drifts"]


def test_overhead_outside_page_loads_is_named_when_the_page_time_is_flat():
    # The browser iteration grows 20 s while every page still opens in 3 s: context setup,
    # timing reads and the close got slower. Not graded — but it is the same machine.
    s = _samples(overhead_ms=lambda i: 4000.0 + 450.0 * i)
    out = assess(s, days=DAYS, now=NOW)
    assert out["verdict"] == "overhead" and out["grading_at_risk"] is False
    assert any(f["key"] == "overhead" and f["severity"] == "warn" for f in out["findings"])


def test_a_slower_link_moves_the_page_clock_and_the_network_phases_together():
    # The page's own clock climbs with the network phases, render flat → the link changed.
    s = _samples(page_clock_ms=lambda i: 1200.0 + 15.0 * i, nav_network_ms=lambda i: 200.0 + 8.0 * i,
                 lcp_ms=lambda i: 350.0 + 8.0 * i)
    out = assess(s, days=DAYS, now=NOW)
    assert out["verdict"] == "network" and out["grading_at_risk"] is False
    net = next(f for f in out["findings"] if f["key"] == "network")
    assert net["severity"] == "info" and "instrument is fine" in net["text"]


def test_a_growing_idle_wait_and_a_page_clock_nobody_explains_each_get_their_own_finding():
    # Idle wait: page_wall grows while the page clock is flat.
    s = _samples(page_wall_ms=lambda i: 3000.0 + 80.0 * i)
    out = assess(s, days=DAYS, now=NOW)
    assert out["verdict"] == "idle"
    assert any(f["key"] == "idle" for f in out["findings"])
    # Page clock up, render and network flat → unattributed (pages themselves changed).
    s = _samples(page_clock_ms=lambda i: 1200.0 + 15.0 * i)
    out = assess(s, days=DAYS, now=NOW)
    assert out["verdict"] == "unattributed" and out["findings"][0]["severity"] == "warn"


def test_stable_window_and_the_insufficient_case():
    s = _samples(nav_render_ms=lambda i: _wobble(i, 120.0, 4.0), per_iteration_ms=lambda i: _wobble(i, 22000.0, 500.0))
    out = assess(s, days=DAYS, now=NOW)
    assert out["verdict"] == "stable" and out["findings"][0]["key"] == "stable"
    thin = assess(s[: MIN_SAMPLES - 1], days=DAYS, now=NOW)
    assert thin["verdict"] == "insufficient" and thin["findings"] == [] and "widen" in thin["headline"]


def test_leaked_processes_and_stale_derivations_are_reported_beside_the_trends():
    s = _samples()
    procs = {"available": True, "drivers": 3, "chrome": 40, "zombies": 2, "stray_chrome": 1}
    out = assess(s, days=DAYS, processes=procs, stale_derivations=7, now=NOW)
    assert out["leaked_processes"] == 2 + 1 + 2
    keys = {f["key"] for f in out["findings"]}
    assert "processes" in keys and "derivation" in keys
    # One live driver and nothing else is the healthy state: no process finding.
    clean = assess(s, days=DAYS, processes={"available": True, "drivers": 1, "chrome": 12, "zombies": 0, "stray_chrome": 0}, now=NOW)
    assert clean["leaked_processes"] == 0 and "processes" not in {f["key"] for f in clean["findings"]}


def test_cohorts_bucket_by_day_and_carry_the_run_mix():
    s = _samples(browser_share=lambda i: 0.4 if i < DAYS * 2 else 1.0)
    out = assess(s, days=DAYS, now=NOW)
    assert out["window"]["bucket"] == "day"
    assert len(out["cohorts"]) in (DAYS, DAYS + 1)
    first, last = out["cohorts"][0], out["cohorts"][-1]
    assert first["medians"]["browser_share"] == 0.4 and last["medians"]["browser_share"] == 1.0
    assert first["kinds"] == {"duel": first["runs"]} and first["methodology_only_share"] == 1.0
    # A short window buckets by hour.
    assert assess(s, days=2, now=NOW)["window"]["bucket"] == "hour"


def test_run_kind_reads_the_engine_off_label_or_job_group():
    assert run_kind("duel · Speedy Sloth", "duel-4") == "duel"
    assert run_kind("test · q1514", "profile_test-9") == "test"
    assert run_kind("race · Tall Garland", None) == "race"
    assert run_kind("scheduled", None) == "monitoring"
    assert run_kind("Explore: Tall Garland → q7000", "profile_test-12") == "test"
    assert run_kind(None, None) == "manual"


def _seed_db(n: int = 20) -> list[int]:
    """Completed runs over the last ``n`` days with a browser result each (scalars only)."""
    ids = []
    with session_scope() as s:
        for i in range(n):
            at = NOW - timedelta(days=n - i, hours=1)
            run = Run(
                status=RunStatus.COMPLETE, created_at=at.replace(tzinfo=None), finished_at=at.replace(tzinfo=None),
                label="duel · Fast Fox", job_group="duel-1", iterations=3, iterations_completed=3,
                per_iteration_ms=20000.0 + 1000.0 * i, methodology_version="speed-smoothness-v16",
                config_used={
                    "browser": {"urls": ["https://a/", "https://b/", "https://c/"], "iterations": None, "networkidle_timeout_s": 5.0},
                    "measurement": {"applied": {"methodology_only": True}},
                },
            )
            s.add(run)
            s.flush()
            s.add(
                BenchmarkResult(
                    run_id=run.id, plugin="browser", success=True, duration_ms=18000.0 + 1000.0 * i,
                    metrics={
                        "total_render_ms": 3000.0, "load_event_ms": 1200.0, "fcp_ms": 300.0, "lcp_ms": 350.0,
                        "network_stall_all_ms": 40.0, "nav_render_ms": 100.0 + 5.0 * i, "inp_ms": 30.0, "cls": 0.01,
                        "nav_dns_ms": 2.0, "nav_tcp_ms": 10.0, "nav_tls_ms": 20.0, "nav_request_ms": 50.0, "nav_response_ms": 30.0,
                    },
                    details={"samples": 3}, raw={},
                )
            )
            # Half the runs carry a stale derivation stamp.
            s.add(ScoreResult(run_id=run.id, sops=50.0, derivation_version=DERIVATION_VERSION if i % 2 else "derive-v1"))
            ids.append(run.id)
    return ids


def test_load_samples_reads_scalars_through_json_paths_and_the_audit_runs_end_to_end(client):
    ids = _seed_db(20)
    with session_scope() as s:
        samples = {x.id: x for x in load_samples(s, days=30, now=NOW)}
        for rid in ids:
            assert rid in samples
        x = samples[ids[-1]]
        assert x.kind == "duel" and x.iterations == 3 and x.pages == 3 and x.methodology_only is True
        assert x.browser_samples == 3 and x.browser_share == 1.0 and x.idle_cap_s == 5.0
        assert x.page_wall_ms == 3000.0 and x.page_clock_ms == 1200.0 and x.idle_wait_ms == 1800.0
        assert x.nav_network_ms == 112.0 and x.overhead_ms == x.browser_wall_ms - 3 * 3000.0
        # An even stride across the window keeps the window's first run (index 0 always
        # survives ``ids[::step]``) and thins the rest, whatever else the shared table holds.
        few = load_samples(s, days=30, limit=5, now=NOW)
        assert 5 <= len(few) < len(samples) and few[0].id == min(samples)
        out = instrument_drift(s, days=30, now=NOW)
    assert out["window"]["runs"] >= 20 and out["stale_derivations"] >= 10
    assert out["verdict"] == "instrument" and out["grading_at_risk"] is True
    assert out["processes"] is None or "available" in out["processes"]
    body = client.get("/api/methodologies/instrument-drift?days=30&limit=500").json()
    assert body["verdict"] in {"instrument", "unattributed", "network", "overhead", "idle", "mix", "stable", "insufficient"}
    assert "cohorts" in body and "trends" in body and body["derivation_version"] == DERIVATION_VERSION
