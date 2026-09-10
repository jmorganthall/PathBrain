"""A run measured on a sick machine is not a measurement of the link.

Reported after a stretch of NAS trouble as *"profiles showing bad results that weren't
actually bad"* — and they were on the record and counting, because comparability asked
whether a run measured the same *thing* (sites, client, coverage, metrics) and never
whether the thing doing the measuring was working. A degraded host makes the browser
slower, which lands inside FCP and LCP through the render phase.
"""
from __future__ import annotations

from types import SimpleNamespace

from pathbrain import instrument_health as ih
from pathbrain.methodology import INSTRUMENT_MARKER, comparability


def _definition(**overall) -> dict:
    """A minimal methodology that scores one metric and crowns on it."""
    return {
        "metrics": [
            {"key": "fcp", "axis": "speed", "plugin": "browser", "source_key": "fcp_ms",
             "weight": 1.0, "best": 100.0, "worst": 2000.0, "required": True},
        ],
        "axes": [{"key": "speed", "label": "Speed"}],
        "overall": {"metrics": ["fcp"], **overall},
    }


# ── The baseline: robust to the very stretch it is measured against ────────────


def test_the_baseline_is_the_healthy_quarter_not_the_average_of_a_bad_stretch():
    """The whole problem is that recent history IS the contaminated stretch, so ranking a
    bad run against mostly-bad runs calls it normal. A p25 holds until more than three
    quarters of history is bad, where a median gives up at half."""
    # 60% of history is degraded: context setup at 4000 ms against a true healthy 400 ms.
    healthy = [{"context_ms": 400.0, "close_ms": 500.0} for _ in range(40)]
    sick = [{"context_ms": 4000.0, "close_ms": 5000.0} for _ in range(60)]
    base = ih.build_baseline(healthy + sick)

    assert base.values["context_ms"] == 400.0, "the healthy quarter still sets the reference"
    # A median baseline would have been 4000 and called the sick runs perfectly normal.
    assert ih.build_baseline(healthy + sick).ratios({"context_ms": 4000.0})["context_ms"] == 10.0


def test_a_quantity_without_enough_readings_is_dropped_rather_than_guessed():
    thin = [{"context_ms": 400.0} for _ in range(ih.MIN_BASELINE_READINGS - 1)]
    assert "context_ms" not in ih.build_baseline(thin).values
    enough = [{"context_ms": 400.0} for _ in range(ih.MIN_BASELINE_READINGS)]
    assert ih.build_baseline(enough).values["context_ms"] == 400.0


def test_a_baseline_at_the_timing_floor_is_not_a_reference():
    """A 3 ms close against a 1 ms baseline is a 3× ratio and means nothing about the host."""
    at_floor = [{"close_ms": 1.0} for _ in range(50)]
    base = ih.build_baseline(at_floor)
    assert "close_ms" not in base.values
    assert base.ratios({"close_ms": 3.0}) == {}


# ── The verdict ───────────────────────────────────────────────────────────────


def _baseline() -> ih.Baseline:
    return ih.build_baseline(
        [{"context_ms": 400.0, "close_ms": 2400.0, "reads_ms": 300.0, "nav_render_ms": 200.0}]
        * 40
    )


def _assess(readings, **kw):
    opts = {"strained_ratio": 1.8, "degraded_ratio": 3.0, "min_quantities": 2, **kw}
    return ih.assess(readings, _baseline(), **opts)


def test_the_observed_degradation_reads_as_degraded():
    """The real event: context setup 421 ms → 3.7 s, close 2.4 → 5.2 s, reads ×4, and the
    render that lands inside FCP/LCP dragged with them."""
    out = _assess({"context_ms": 3700.0, "close_ms": 5200.0, "reads_ms": 1200.0, "nav_render_ms": 480.0})
    assert out["verdict"] == ih.DEGRADED
    assert out["ratio"] >= 3.0 and out["quantities"] == 4
    assert out["ratios"]["context_ms"] == 9.25
    # The sentence leads with the run's verdict-driving MEDIAN ratio and names the worst
    # quantity in its own units — "3.2× slower, and context setup took 3.7 s against 400 ms"
    # is checkable; the median alone is not, and the worst alone would overstate.
    why = ih.explain(out)
    assert "3.2× slower than healthy" in why
    assert "context setup took 3700 ms against 400 ms" in why
    assert "Quarantined" in why



def test_a_healthy_machine_is_healthy_and_a_middling_one_only_strained():
    assert _assess({"context_ms": 430.0, "close_ms": 2500.0})["verdict"] == ih.HEALTHY
    mid = _assess({"context_ms": 900.0, "close_ms": 5000.0})
    assert mid["verdict"] == ih.STRAINED
    assert "still counts" in ih.explain(mid)


def test_one_noisy_phase_cannot_condemn_a_run():
    """The reading is the MEDIAN ratio, so a single slow phase beside three normal ones is
    a wobble, not a sick machine — and one flattering phase cannot rescue a sick one."""
    out = _assess({"context_ms": 8000.0, "close_ms": 2400.0, "reads_ms": 300.0, "nav_render_ms": 200.0})
    assert out["verdict"] == ih.HEALTHY, "three healthy quantities outvote one outlier"
    sick = _assess({"context_ms": 400.0, "close_ms": 9600.0, "reads_ms": 1500.0, "nav_render_ms": 900.0})
    assert sick["verdict"] == ih.DEGRADED


def test_too_few_comparable_readings_is_no_opinion_never_a_quarantine():
    assert _assess({"context_ms": 9000.0}) is None, "one reading is not a verdict"
    assert _assess({}) is None
    # …and "no opinion" is exactly what the gate treats as innocent.
    tag, missing = comparability(_definition(), {"fcp": 300.0}, instrument=None)
    assert tag == "exact" and missing == []


# ── The gate ──────────────────────────────────────────────────────────────────


def test_a_degraded_run_is_quarantined_under_its_own_token():
    tag, missing = comparability(_definition(), {"fcp": 300.0}, instrument=ih.DEGRADED)
    assert tag == "incomparable" and missing == [INSTRUMENT_MARKER]


def test_a_strained_run_still_counts():
    """Flag-and-steer, the same discipline the weather stamp follows: a run that was a
    little slow is evidence with a caveat, not a broken measurement."""
    for verdict in (ih.HEALTHY, ih.STRAINED):
        tag, missing = comparability(_definition(), {"fcp": 300.0}, instrument=verdict)
        assert (tag, missing) == ("exact", []), verdict


def test_the_instrument_token_joins_the_others_rather_than_replacing_them():
    tag, missing = comparability(
        _definition(), {"fcp": 300.0}, pages_missing=["https://example.com"], instrument=ih.DEGRADED
    )
    assert tag == "incomparable"
    assert set(missing) == {"site_coverage", INSTRUMENT_MARKER}


def test_a_missing_crown_metric_still_wins_over_the_machine_verdict():
    """A run that cannot produce the crown is quarantined for that, and the reader is told
    which metric — the machine's health is a second question that never masks the first."""
    tag, missing = comparability(_definition(), {}, instrument=ih.DEGRADED)
    assert tag == "incomparable" and missing == ["fcp"]


# ── Reading a run, and healing the record ─────────────────────────────────────


def _run(*, phases=None, metrics=None, stored=None):
    result = SimpleNamespace(plugin="browser", details={"phases": phases} if phases else {}, metrics=metrics or {})
    return SimpleNamespace(id=7, results=[result], instrument_health=stored)


def test_the_readings_come_from_the_browsers_own_phases_and_its_render_metric():
    run = _run(
        phases={"context_ms": 3700.0, "close_ms": 5200.0, "goto_ms": 900.0},
        metrics={"nav_render_ms": 480.0, "fcp_ms": 1200.0},
    )
    readings = ih.readings_from_results(run.results)
    # Only the shaping-immune host quantities — `goto_ms` is a page load and `fcp_ms` is
    # the thing being protected, so neither may vote on whether the machine was well.
    assert readings == {"context_ms": 3700.0, "close_ms": 5200.0, "nav_render_ms": 480.0}
    assert "goto_ms" not in readings and "fcp_ms" not in readings


def test_grading_a_run_that_was_never_assessed_stamps_it_so_a_regrade_heals_history(monkeypatch):
    """The seam that answers "those runs are already on the record": the readings were
    always written, so a run measured during the bad stretch can be judged now, and the
    first grading persists the verdict. One re-grade, no re-measurement."""
    monkeypatch.setattr(ih, "baseline", lambda session, limit=None: _baseline())
    run = _run(
        phases={"context_ms": 3700.0, "close_ms": 5200.0, "reads_ms": 1200.0},
        metrics={"nav_render_ms": 480.0},
    )
    health = ih.assess_run(None, run, {})
    assert health["verdict"] == ih.DEGRADED
    assert run.instrument_health == health, "stamped back onto the run"
    assert ih.is_degraded(run.instrument_health)


def test_an_existing_stamp_is_never_recomputed(monkeypatch):
    """A verdict is a fact about the moment the run was measured, so it is read back, not
    re-derived against a baseline the field has moved since."""
    def boom(*a, **k):
        raise AssertionError("must not rebuild the baseline for an assessed run")

    monkeypatch.setattr(ih, "baseline", boom)
    run = _run(stored={"verdict": ih.STRAINED, "ratio": 2.0})
    assert ih.assess_run(None, run, {})["verdict"] == ih.STRAINED


def test_the_gate_can_be_switched_off_and_a_run_without_readings_is_never_judged(monkeypatch):
    monkeypatch.setattr(ih, "baseline", lambda session, limit=None: _baseline())
    loaded = _run(phases={"context_ms": 3700.0}, metrics={"nav_render_ms": 480.0})
    assert ih.assess_run(None, loaded, {"instrument": {"enabled": False}}) is None
    assert ih.assess_run(None, _run(), {}) is None, "no browser readings — no opinion"


def test_an_assessment_that_raises_costs_the_verdict_and_never_the_run(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no database")

    monkeypatch.setattr(ih, "baseline", boom)
    assert ih.assess_run(None, _run(phases={"context_ms": 9.0e3}), {}) is None


def test_the_config_block_carries_its_defaults():
    cfg = ih.config_block({})
    assert cfg == {
        "enabled": True, "strained_ratio": 1.8, "degraded_ratio": 3.0,
        "min_quantities": 2, "baseline_runs": ih.DEFAULT_BASELINE_RUNS,
    }
    assert ih.config_block({"instrument": {"degraded_ratio": 5}})["degraded_ratio"] == 5.0


# ── The audit: the threshold is an evidence question ──────────────────────────


def test_the_audit_prices_every_candidate_threshold(client):
    """A gate nobody can price is a gate nobody should arm, so the endpoint reports how
    many runs each candidate line would quarantine over the same history."""
    body = client.get("/api/methodologies/instrument-health").json()
    assert body["enabled"] is True
    assert body["thresholds"] == {"strained": 1.8, "degraded": 3.0}
    assert body["baseline"]["percentile"] == ih.BASELINE_PERCENTILE
    priced = body["would_quarantine"]
    assert [row["ratio"] for row in priced] == sorted(row["ratio"] for row in priced)
    assert sum(1 for row in priced if row["live"]) == 1, "the live threshold is marked"
    # Monotone by construction: a higher bar can never quarantine more runs.
    counts = [row["runs"] for row in priced]
    assert counts == sorted(counts, reverse=True)
