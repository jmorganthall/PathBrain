"""Tests for the perceived load-smoothness metrics (pure functions over a series).

Uses synthetic series throughout: a deliberately "chunky" load (long plateau then
a dump) and a "smooth" linear load, asserting the instrument clearly separates
them. Attribution tests build a synthetic main-thread block vs a synthetic
delivery gap and assert they tag ``render`` vs ``network``.
"""
from __future__ import annotations

from pathbrain.interpret.smoothness import (
    attribute_stall,
    byte_earliness,
    cadence_cov,
    completion_series,
    delivery_gini,
    longest_stall,
    longest_stall_window,
    perceived_time,
    protocol_mix,
    smoothness_metrics,
    smoothness_record,
    stall_attribution_times,
    stall_time,
    total_stall,
    worst_void_fraction,
)


def _res(end, size=1000, proto="h2"):
    return {"responseEnd": end, "transferSize": size, "nextHopProtocol": proto}


# A steady trickle (smooth) vs a long blank then everything-at-once (chunky).
SMOOTH = [_res(t) for t in (100, 200, 300, 400, 500, 600, 700, 800)]
CHUNKY = [_res(t) for t in (50, 60, 70, 80, 760, 770, 780, 800)]


# ── R2: completion series ────────────────────────────────────────────────────


def test_completion_series_filters_zero_and_sorts():
    res = [_res(300), _res(0), {"responseEnd": -1}, _res(100)]
    assert completion_series(res) == [100.0, 300.0]


def test_completion_series_injects_boundaries():
    series = completion_series([_res(300)], fcp=50, doc_response_end=80, load_event_end=400)
    assert series == [50.0, 80.0, 300.0, 400.0]


# ── R3 / R4: the core discriminators (acceptance #2) ─────────────────────────


def test_longest_stall_and_cadence_separate_chunky_from_smooth():
    smooth_series = completion_series(SMOOTH)
    chunky_series = completion_series(CHUNKY)
    # The chunky load has a long blank stretch; the smooth one never stalls.
    assert longest_stall(chunky_series) > longest_stall(smooth_series)
    assert longest_stall(chunky_series) >= 600  # the 80→760 plateau
    # And its delivery is far less metronomic.
    assert cadence_cov(chunky_series) > cadence_cov(smooth_series)


def test_total_stall_counts_dead_air_beyond_rhythm():
    smooth_series = completion_series(SMOOTH)
    chunky_series = completion_series(CHUNKY)
    # Steady delivery never falls behind its own median pace → ~no cumulative stall.
    assert total_stall(smooth_series) == 0.0
    # The chunky load's 80→760 freeze is dead air far beyond its rhythm.
    assert total_stall(chunky_series) >= 670.0
    assert total_stall(chunky_series) > total_stall(smooth_series)
    # A steady fast trickle (uniform gaps) also has no excess over its own pace.
    assert total_stall([0.0, 10.0, 20.0, 30.0]) == 0.0
    # Need a rhythm to compare against → None with fewer than two gaps.
    assert total_stall([]) is None
    assert total_stall([100.0, 200.0]) is None


def test_stall_time_is_absolute_dead_air_against_a_fixed_threshold():
    smooth_series = completion_series(SMOOTH)   # uniform 100ms gaps
    chunky_series = completion_series(CHUNKY)   # a single 680ms freeze
    # Absolute measure (fixed 200ms threshold): the smooth 100ms-gap trickle has no
    # perceptible stall → 0; the chunky load's 80→760 freeze counts its whole 680ms.
    assert stall_time(smooth_series) == 0.0
    assert stall_time(chunky_series) == 680.0
    # Unlike total_stall, it does NOT subtract the run's own median — it's a fixed yardstick.
    # A run with two big gaps counts BOTH in full (700 + 500 = 1200), where total_stall would
    # net one against the median and under-count the cumulative dead air.
    two_freezes = [0.0, 700.0, 800.0, 1300.0]  # gaps: 700, 100, 500
    assert stall_time(two_freezes) == 1200.0    # 700 + 500 (100 is below the threshold)
    assert total_stall(two_freezes) < stall_time(two_freezes)
    # A gap exactly at the threshold counts; just below does not (custom threshold too).
    assert stall_time([0.0, 200.0]) == 200.0
    assert stall_time([0.0, 199.0]) == 0.0
    assert stall_time([0.0, 100.0], threshold_ms=50.0) == 100.0
    # 0.0 is a real measurement (no stall); None only when there's no gap to measure.
    assert stall_time([5.0]) is None
    assert stall_time([]) is None


def test_worst_void_fraction_flags_the_pregnant_pause_in_the_window():
    # The pure windowing math: worst void as a fraction of the FCP→end window (end = loadEventEnd
    # since v12). Two loads with IDENTICAL endpoints (100 → 700) that timing alone can't separate;
    # the felt difference is the *shape* of the fill between them.
    fcp, end = 100.0, 700.0
    # Steady, consistent progress: completions every ~120ms → no perceptible pause → ~0.
    smooth = [100.0, 220.0, 340.0, 460.0, 580.0, 700.0]
    assert worst_void_fraction(smooth, fcp, end) == 0.0
    # Fast start, then a "pregnant pause" (150→600 = 450ms of nothing), then a lurch to the end.
    # 450 / 600 = 0.75 of the journey was one dead void — scale-free, so it captures the
    # *evenness* independent of the (identical) endpoints.
    lurch = [100.0, 150.0, 600.0, 650.0, 700.0]
    assert worst_void_fraction(lurch, fcp, end) == 0.75
    # An empty middle (nothing completes in the window) is the worst case → the whole span.
    assert worst_void_fraction([100.0, 700.0], fcp, end) == 1.0
    # Scale-free: a gap under the 200ms perceptible threshold isn't a felt pause → 0.0, and this
    # also cleanly handles a near-instant load (the whole tiny span is sub-threshold).
    assert worst_void_fraction([100.0, 250.0, 400.0, 550.0, 700.0], fcp, end) == 0.0
    assert worst_void_fraction([100.0, 120.0], 100.0, 120.0) == 0.0
    # None when the window is undefined (missing/degenerate load event) — no journey to measure.
    assert worst_void_fraction(lurch, fcp, None) is None
    assert worst_void_fraction(lurch, 700.0, 100.0) is None


def test_smoothness_metrics_emits_the_crown_leg_over_the_load_window():
    # worst_void_fraction rides in the numeric record so the derive layer / crown can read it, and
    # (since v12) its window is FCP→loadEventEnd. FCP=30, loadEventEnd=850; CHUNKY's 80→760 freeze
    # falls inside → a dominant void.
    m = smoothness_metrics({"responseStart": 40.0, "loadEventEnd": 850.0}, CHUNKY,
                           {"fcp": 30.0, "lcp": 90.0}, None)
    assert "worst_void_fraction" in m and m["worst_void_fraction"] > 0.5
    # A near-instant FCP→LCP no longer zeroes it: the pause lives in the post-LCP settle (LCP 90,
    # load 850), which the widened window now captures where the old FCP→LCP window read 0.
    assert m["worst_void_fraction"] > 0.5
    # Absent (not fabricated) when the load event is missing — no window to measure.
    m2 = smoothness_metrics({"responseStart": 40.0}, CHUNKY, {"fcp": 30.0, "lcp": 90.0}, None)
    assert "worst_void_fraction" not in m2


def test_smoothness_metrics_emits_both_relative_and_absolute_stall():
    m = smoothness_metrics({"responseStart": 40.0}, CHUNKY, {"fcp": 30.0}, None)
    # Both stall dimensions ride in the record: the relative total_stall (display-only since
    # v8) and the absolute stall_time (the v8 crown dimension). They measure different things —
    # stall_time counts the full 680ms freeze against a fixed 200ms threshold (ignoring sub-
    # threshold gaps), total_stall nets every gap against the run's own median — so neither
    # dominates the other in general; here they land near each other on this single-freeze load.
    assert "total_stall_ms" in m and "stall_time_ms" in m
    assert m["stall_time_ms"] == 680.0            # the whole freeze, sub-200ms gaps ignored
    assert m["total_stall_ms"] > 0.0


def test_longest_void_diagnostic_locates_and_attributes_the_pause():
    from pathbrain.interpret.smoothness import longest_void_diagnostic
    # FCP 307, LCP 348, load 572; the biggest void (410→504) sits in the post-LCP settle.
    nav = {"responseStart": 165.0, "responseEnd": 300.0, "loadEventEnd": 572.0}
    paint = {"fcp": 307.0, "lcp": 348.0}
    resources = [{"responseEnd": t} for t in (320.0, 410.0, 504.0, 560.0)]
    # A long task covering the void → render-bound (shaping can't move it).
    render = longest_void_diagnostic(nav, resources, paint,
                                     {"source": "longtask", "entries": [{"startTime": 410.0, "duration": 100.0}]})
    assert render["duration_ms"] == 94.0
    assert render["phase"] == "lcp_load"          # post-LCP settle, not FCP→LCP
    assert render["attribution"] == "render"
    # Same void, no long task → a network (byte-delivery) gap.
    net = longest_void_diagnostic(nav, resources, paint, {"source": "longtask", "entries": []})
    assert net["attribution"] == "network"
    # No LoAF/longtask support at all → can't attribute (don't guess).
    assert longest_void_diagnostic(nav, resources, paint, None)["attribution"] == "unknown"
    # A void before first paint is phased pre_fcp: the 50→300 gap dominates while the post-FCP
    # completions stay dense up to the load event (so no larger later gap steals the "longest").
    early = longest_void_diagnostic(
        {"loadEventEnd": 390.0},
        [{"responseEnd": t} for t in (50.0, 300.0, 340.0, 360.0, 380.0)],
        {"fcp": 320.0, "lcp": 340.0}, None,
    )
    assert early["phase"] == "pre_fcp"
    # Nothing to measure → None.
    assert longest_void_diagnostic({}, [], {}, None) is None


def test_pause_diagnostics_unwraps_the_stored_iterations_raw():
    # Regression: BenchmarkResult.raw is {"iterations": [<per-iter {"urls": ...}>, ...]}, NOT a bare
    # {"urls": ...}. _pause_diagnostics must unwrap "iterations" or the card is empty for every run.
    from types import SimpleNamespace
    from pathbrain.api.routes_results import _pause_diagnostics

    nav = {"responseStart": 165.0, "responseEnd": 300.0, "loadEventEnd": 597.0}
    paint = {"fcp": 250.0, "lcp": 260.0}
    resources = [{"responseEnd": t} for t in (180.0, 210.0, 300.0, 560.0)]
    loaf = {"source": "longtask", "entries": [{"startTime": 300.0, "duration": 200.0}]}
    per_iter = {"urls": {"https://a/": {"nav": nav, "paint": paint, "resources": resources, "loaf": loaf}}}
    browser = SimpleNamespace(plugin="browser", raw={"iterations": [per_iter, per_iter]})
    run = SimpleNamespace(results=[browser])

    diags = _pause_diagnostics(run)
    assert diags is not None and len(diags) == 1
    d = diags[0]
    assert d["url"] == "https://a/"
    assert d["phase"] == "lcp_load"          # the felt pause is the post-LCP settle
    assert d["attribution"] == "render"      # main-thread, not byte delivery
    assert d["duration_ms"] == 260.0

    # A list-shaped loaf (some browsers) must NOT crash the run-detail page — degrade to "unknown".
    per_iter_bad = {"urls": {"x": {"nav": nav, "paint": paint, "resources": resources, "loaf": [1, 2]}}}
    run_bad = SimpleNamespace(results=[SimpleNamespace(plugin="browser", raw={"iterations": [per_iter_bad]})])
    bad = _pause_diagnostics(run_bad)
    assert bad and bad[0]["attribution"] == "unknown"

    # No browser raw at all → None (card hidden, not an error).
    assert _pause_diagnostics(SimpleNamespace(results=[])) is None


def test_longest_stall_window_points_at_the_plateau():
    window = longest_stall_window(completion_series(CHUNKY))
    assert window is not None
    start, end, dur = window
    assert (start, end) == (80.0, 760.0)
    assert dur == 680.0


def test_cadence_needs_two_gaps():
    assert cadence_cov([100.0]) is None
    assert cadence_cov([100.0, 200.0]) is None  # one gap → undefined CoV


# ── R5: byte-weighted earliness ──────────────────────────────────────────────


def test_byte_earliness_rewards_early_bytes():
    # Same total bytes, same finish; one front-loads delivery, one back-loads it.
    early = [_res(100, 900), _res(900, 100)]
    late = [_res(100, 100), _res(900, 900)]
    assert byte_earliness(early, start=0) < byte_earliness(late, start=0)


def test_byte_earliness_none_without_bytes():
    assert byte_earliness([_res(100, 0)], start=0) is None


# ── R6: delivery evenness ────────────────────────────────────────────────────


def test_delivery_gini_smooth_is_lower_than_chunky():
    g_smooth = delivery_gini(SMOOTH, start=0, end=900)
    g_chunky = delivery_gini(CHUNKY, start=0, end=900)
    assert 0.0 <= g_smooth <= 1.0 and 0.0 <= g_chunky <= 1.0
    assert g_smooth < g_chunky


# ── R8: perceived time ───────────────────────────────────────────────────────


def test_perceived_time_penalizes_unoccupied_stalls():
    # Both span the same real window; chunky has a long unoccupied stretch.
    smooth_events = completion_series(SMOOTH)
    chunky_events = completion_series(CHUNKY)
    pt_smooth = perceived_time(smooth_events, start=0, end=900)
    pt_chunky = perceived_time(chunky_events, start=0, end=900)
    assert pt_chunky > pt_smooth


def test_recalibrated_perceived_time_keeps_stall_loads_out_of_green():
    # 5c recalibration (w_unoccupied=4 default + the v2 400/8000 threshold): a load
    # that is mostly one dead stall must not score green, and must score below a
    # steadily-delivered load of the same span.
    from pathbrain.scoring.engine import _normalize

    smooth = completion_series([_res(t) for t in range(100, 1000, 100)])
    chunky = completion_series([_res(t) for t in (50, 60, 70, 760, 800)])
    pt_smooth = perceived_time(smooth, 0, 900)  # uses the new w_unoccupied=4 default
    pt_chunky = perceived_time(chunky, 0, 900)
    best, worst = 400.0, 8000.0  # speed-smoothness-v2 perceived_time threshold
    assert _normalize(pt_chunky, best, worst) < 80.0  # mostly-stall → not green
    assert _normalize(pt_chunky, best, worst) < _normalize(pt_smooth, best, worst)


def test_perceived_time_weight_ratio_controls_penalty():
    events = completion_series(CHUNKY)
    flat = perceived_time(events, 0, 900, w_occupied=1.0, w_unoccupied=1.0)
    steep = perceived_time(events, 0, 900, w_occupied=1.0, w_unoccupied=5.0)
    # With equal weights perceived time == real time; a higher unoccupied weight
    # only ever raises it (the stall slices cost more).
    assert steep > flat
    assert abs(flat - 900.0) < 1e-6


# ── R7: attribution (acceptance #3) ──────────────────────────────────────────


def test_delivery_gap_with_no_long_task_is_network():
    # A long gap, LoAF supported but no long task overlapping → tunable layer.
    window = (100.0, 800.0, 700.0)
    assert attribute_stall(window, loaf=[], loaf_source="loaf") == "network"


def test_stall_overlapping_long_task_is_render():
    # A long task spans the whole stall → render-bound, shaping won't fix it.
    window = (100.0, 800.0, 700.0)
    loaf = [{"startTime": 90.0, "duration": 800.0}]
    assert attribute_stall(window, loaf, loaf_source="loaf") == "render"


def test_partial_overlap_is_mixed():
    window = (100.0, 800.0, 700.0)
    loaf = [{"startTime": 100.0, "duration": 200.0}]  # covers ~29%
    assert attribute_stall(window, loaf, loaf_source="loaf") == "mixed"


def test_no_loaf_support_is_unknown():
    window = (100.0, 800.0, 700.0)
    assert attribute_stall(window, loaf=[], loaf_source=None) == "unknown"


def test_attribution_times_split_network_and_render():
    series = completion_series(CHUNKY)  # one big 680ms stall (80→760)
    # A long task covering the first 180ms of that stall → mixed split.
    loaf = [{"startTime": 80.0, "duration": 180.0}]
    times = stall_attribution_times(series, loaf, loaf_source="loaf")
    assert times["render_ms"] == 180.0
    assert times["network_ms"] == 500.0  # 680 - 180
    assert times["unknown_ms"] == 0.0


def test_attribution_times_unknown_without_loaf():
    series = completion_series(CHUNKY)
    times = stall_attribution_times(series, loaf=[], loaf_source=None)
    assert times["unknown_ms"] >= 600.0
    assert times["network_ms"] == 0.0 and times["render_ms"] == 0.0


# ── protocol mix + assemblers ────────────────────────────────────────────────


def test_protocol_mix_counts_live_resources():
    res = [_res(100, proto="h2"), _res(200, proto="h3"), _res(300, proto="h2"), _res(0, proto="h3")]
    assert protocol_mix(res) == {"h2": 2, "h3": 1}


def test_smoothness_metrics_returns_numeric_subset():
    nav = {"responseStart": 50.0, "responseEnd": 80.0, "loadEventEnd": 900.0}
    paint = {"fcp": 120.0, "lcp": 400.0}
    loaf = {"entries": [], "source": "loaf"}
    m = smoothness_metrics(nav, CHUNKY, paint, loaf)
    assert m["longest_stall_ms"] > 0
    assert m["network_stall_ms"] > 0  # the plateau, no long task → network
    assert "perceived_time_ms" in m and "byte_earliness_ms" in m
    # Numeric only — categorical fields must not leak into the scoreable subset.
    assert all(isinstance(v, (int, float)) for v in m.values())


def test_smoothness_record_carries_speed_side_and_attribution():
    nav = {"responseStart": 50.0, "responseEnd": 80.0, "loadEventEnd": 900.0,
           "domContentLoadedEventEnd": 600.0}
    paint = {"fcp": 120.0, "lcp": 400.0}
    rec = smoothness_record(nav, CHUNKY, paint, {"entries": [], "source": "loaf"})
    # Speed-side context travels with the smoothness metrics (acceptance #1).
    assert rec["load_event_ms"] == 900.0 and rec["lcp_ms"] == 400.0
    assert rec["longest_stall_attribution"] == "network"
    assert rec["protocol_mix"] == {"h2": 8}
    assert rec["perceived_time_params"]["w_unoccupied"] == 4.0


def test_smoothness_handles_empty_and_cross_origin_zeroed_input():
    # Cross-origin without TAO: phase timings zeroed, but responseEnd/transferSize
    # present → R3/R4/R5 still computable, nothing crashes.
    res = [{"responseEnd": 100.0, "transferSize": 0, "nextHopProtocol": ""},
           {"responseEnd": 500.0, "transferSize": 0, "nextHopProtocol": ""}]
    rec = smoothness_record({}, res, {}, {})
    assert rec["longest_stall_ms"] == 400.0
    assert rec["loaf_source"] is None
    assert rec["longest_stall_attribution"] in ("unknown", None)
    # Totally empty input is graceful too.
    assert smoothness_metrics({}, [], {}, {}) == {}
