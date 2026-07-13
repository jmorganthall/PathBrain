"""Methodology snapshots — how raw becomes a score, frozen and versioned.

A methodology is the immutable bundle of *derivation* (raw → metric scalars) and
*rubric* (metric scalars → axis scores). This module builds a self-contained
``definition`` from the live registry + effective config and persists it once per
version, so every score can be read/reproduced under the exact interpretation that
was in play (see ``docs/methodology.md``). Methodologies are append-only: a weight,
threshold, or metric change is published as a new version, never an edit.
"""
from __future__ import annotations

from math import sqrt

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import metrics as metrics_mod
from .config_store import get_config, save_config
from .database import session_scope
from .interpret import DERIVATION_VERSION
from .logging_config import get_logger
from .metrics import COMPLETION, SOPS, has_latest_metrics, latest_metric_keys
from .models import Methodology, Run, Score, ScoreResult

log = get_logger("methodology")

# Display metadata for each score axis (display-only metrics carry axis=None).
AXIS_META: dict[str, dict] = {
    SOPS: {"label": "SOPS", "role": "headline"},
    COMPLETION: {"label": "Completion", "role": "secondary"},
}

# ── Methodology registry ─────────────────────────────────────────────────────
# Published methodology versions, declared explicitly (append-only). A version
# names its axes and assigns each metric an axis + weight + thresholds; metric
# *metadata* (plugin/source_key/unit/label/description) comes from metrics.py, so a
# version is just the rubric, not a re-statement of the catalog. Publishing a new
# weight/threshold/metric = a new entry here. Weights are relative within an axis
# (the engine normalizes them), so each axis can sum to whatever is readable.

SPEED, SMOOTHNESS, STABILITY = "speed", "smoothness", "stability"
# v4 splits the old blended "Speed" into Responsiveness (time-to-first) and a
# redefined Speed (time-to-last + interactive).
RESPONSIVENESS = "responsiveness"

# The version new runs are scored under (the "published now" methodology).
CURRENT_METHODOLOGY = "speed-smoothness-v15"


def corner_score(values: list[float]) -> float | None:
    """0–100 'closeness to the perfect corner' over the present 0–100 values: 100 at
    all-100, 0 at all-0. Normalized by √k so the scale is independent of how many
    dimensions were present (a 2-corner and a 3-corner are comparable). This is an
    *intersection* — one weak dimension pulls the result down and can't be averaged
    away. Returns None for an empty list. The single corner primitive shared by the
    methodology's first-class Overall and the settings/crown layer."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    dist = sqrt(sum((100.0 - v) ** 2 for v in vals))
    return round(100.0 - dist / sqrt(len(vals)), 1)


def weighted_score(pairs: list[tuple[float, float]]) -> float | None:
    """0–100 **weighted average** over ``(value, weight)`` pairs (present values only).

    The magnitude-aware alternative to ``corner_score``: unlike the corner (an intersection where
    one weak dimension can't be averaged away), this lets a strong dimension compensate a weaker
    one — "good on balance", weighted by importance. Returns None when nothing is present or all
    weights are 0."""
    present = [(v, w) for v, w in pairs if v is not None and w]
    den = sum(w for _, w in present)
    if not present or den <= 0:
        return None
    return round(sum(v * w for v, w in present) / den, 1)

# Shared axis layout + per-metric rubric for the speed/smoothness family.
_SS_AXES = [
    {"key": SPEED, "label": "Speed", "role": "headline"},
    {"key": SMOOTHNESS, "label": "Smoothness", "role": "headline"},
    {"key": STABILITY, "label": "Stability & Interactivity", "role": "secondary"},
    {"key": COMPLETION, "label": "Completion", "role": "secondary"},
]


def _ss_assignments(perceived_threshold: dict) -> dict:
    return {
        # Speed — when content arrives.
        "ttfb": {"axis": SPEED, "weight": 15, "best": 800.0, "worst": 1800.0},
        "fcp": {"axis": SPEED, "weight": 25, "best": 1800.0, "worst": 3000.0},
        "lcp": {"axis": SPEED, "weight": 20, "best": 2500.0, "worst": 4000.0},
        "byte_earliness": {"axis": SPEED, "weight": 30, "best": 300.0, "worst": 5000.0},
        "render": {"axis": SPEED, "weight": 10, "best": 1000.0, "worst": 8000.0},
        # Smoothness — how steady delivery was (the reason this project exists).
        "longest_stall": {"axis": SMOOTHNESS, "weight": 40, "best": 50.0, "worst": 2000.0,
                          "required": True},
        "perceived_time": {"axis": SMOOTHNESS, "weight": 30, **perceived_threshold},
        "cadence_cov": {"axis": SMOOTHNESS, "weight": 15, "best": 0.5, "worst": 2.5},
        "delivery_gini": {"axis": SMOOTHNESS, "weight": 15, "best": 0.2, "worst": 0.7},
        # Stability & interactivity — kept off the speed/smoothness axes.
        "cls": {"axis": STABILITY, "weight": 50, "best": 0.1, "worst": 0.25},
        "inp": {"axis": STABILITY, "weight": 50, "best": 200.0, "worst": 500.0},
        # Completion — pure infrastructure timing.
        "dns": {"axis": COMPLETION, "weight": 10, "best": 10.0, "worst": 150.0},
        "tcp": {"axis": COMPLETION, "weight": 15, "best": 10.0, "worst": 250.0},
        "tls": {"axis": COMPLETION, "weight": 20, "best": 30.0, "worst": 500.0},
        "jitter": {"axis": COMPLETION, "weight": 5, "best": 1.0, "worst": 30.0},
        "packet_loss": {"axis": COMPLETION, "weight": 5, "best": 0.0, "worst": 2.5},
    }


# speed-smoothness-v3: same axis layout as v2, with thresholds re-anchored so a
# value "comfortably inside good" reads green on home-connection scales — tighter
# "best" anchors on the infra/connection metrics (DNS 1ms, TCP/TLS 5ms, jitter
# 0.5ms), the paint metrics (FCP best 300ms, LCP 800ms, TTFB 50ms, render 500ms),
# and the smoothness metrics (longest-stall best 25ms, perceived-time 300ms,
# cadence 0.20, evenness 0.10), plus CLS best at a pristine 0. Weights unchanged
# from v2. Derivation is unchanged (no formula change), so history re-grades
# straight from raw.
def _ss_v3_assignments() -> dict:
    return {
        # Completion — pure infrastructure timing.
        "dns": {"axis": COMPLETION, "weight": 10, "best": 1.0, "worst": 150.0},
        "tcp": {"axis": COMPLETION, "weight": 15, "best": 5.0, "worst": 250.0},
        "tls": {"axis": COMPLETION, "weight": 20, "best": 5.0, "worst": 500.0},
        "jitter": {"axis": COMPLETION, "weight": 5, "best": 0.5, "worst": 30.0},
        "packet_loss": {"axis": COMPLETION, "weight": 5, "best": 0.0, "worst": 2.5},
        # Speed — when content arrives.
        "ttfb": {"axis": SPEED, "weight": 15, "best": 50.0, "worst": 1800.0},
        "fcp": {"axis": SPEED, "weight": 25, "best": 300.0, "worst": 3000.0},
        "lcp": {"axis": SPEED, "weight": 20, "best": 800.0, "worst": 4000.0},
        "render": {"axis": SPEED, "weight": 10, "best": 500.0, "worst": 8000.0},
        "byte_earliness": {"axis": SPEED, "weight": 30, "best": 200.0, "worst": 5000.0},
        # Stability & interactivity.
        "cls": {"axis": STABILITY, "weight": 50, "best": 0.0, "worst": 0.25},
        "inp": {"axis": STABILITY, "weight": 50, "best": 50.0, "worst": 500.0},
        # Smoothness — how steady delivery was (the reason this project exists).
        "perceived_time": {"axis": SMOOTHNESS, "weight": 30, "best": 300.0, "worst": 8000.0},
        "longest_stall": {"axis": SMOOTHNESS, "weight": 40, "best": 25.0, "worst": 2000.0,
                          "required": True},
        "cadence_cov": {"axis": SMOOTHNESS, "weight": 15, "best": 0.2, "worst": 2.5},
        "delivery_gini": {"axis": SMOOTHNESS, "weight": 15, "best": 0.1, "worst": 0.7},
    }


# speed-smoothness-v4: reframe the headline axes around the three temporal phases
# of a page load. The old "Speed" axis blended time-to-first (TTFB/FCP/byte-
# earliness) with time-to-last (LCP/render); v4 splits those into **Responsiveness**
# (how fast the first content appears) and a redefined **Speed** (overall time to the
# last paint + interaction-ready, so INP moves here from Stability). Smoothness is
# unchanged; Stability becomes CLS-only; Completion is unchanged. Each metric still
# maps to exactly one axis (a clean re-partition — no engine change). Thresholds and
# derivation are carried over from v3 unchanged, so history re-grades straight from
# raw. Weights within an axis are relative (the engine normalizes them).
_SS_V4_AXES = [
    {"key": RESPONSIVENESS, "label": "Responsiveness", "role": "headline"},
    {"key": SMOOTHNESS, "label": "Smoothness", "role": "headline"},
    {"key": SPEED, "label": "Speed", "role": "headline"},
    {"key": STABILITY, "label": "Stability", "role": "secondary"},
    {"key": COMPLETION, "label": "Completion", "role": "secondary"},
]


def _ss_v4_assignments() -> dict:
    return {
        # Completion — pure infrastructure timing (unchanged from v3).
        "dns": {"axis": COMPLETION, "weight": 10, "best": 1.0, "worst": 150.0},
        "tcp": {"axis": COMPLETION, "weight": 15, "best": 5.0, "worst": 250.0},
        "tls": {"axis": COMPLETION, "weight": 20, "best": 5.0, "worst": 500.0},
        "jitter": {"axis": COMPLETION, "weight": 5, "best": 0.5, "worst": 30.0},
        "packet_loss": {"axis": COMPLETION, "weight": 5, "best": 0.0, "worst": 2.5},
        # Responsiveness — how fast the *first* content appears (time-to-first).
        "ttfb": {"axis": RESPONSIVENESS, "weight": 15, "best": 50.0, "worst": 1800.0},
        "fcp": {"axis": RESPONSIVENESS, "weight": 25, "best": 300.0, "worst": 3000.0},
        "byte_earliness": {"axis": RESPONSIVENESS, "weight": 30, "best": 200.0, "worst": 5000.0},
        # Speed — overall time to the *last* paint + interaction-ready.
        "lcp": {"axis": SPEED, "weight": 40, "best": 800.0, "worst": 4000.0},
        "render": {"axis": SPEED, "weight": 20, "best": 500.0, "worst": 8000.0},
        "inp": {"axis": SPEED, "weight": 40, "best": 50.0, "worst": 500.0},
        # Stability — layout stability (CLS only; INP moved to Speed).
        "cls": {"axis": STABILITY, "weight": 50, "best": 0.0, "worst": 0.25},
        # Smoothness — how steady delivery was (the reason this project exists).
        "perceived_time": {"axis": SMOOTHNESS, "weight": 30, "best": 300.0, "worst": 8000.0},
        "longest_stall": {"axis": SMOOTHNESS, "weight": 40, "best": 25.0, "worst": 2000.0,
                          "required": True},
        "cadence_cov": {"axis": SMOOTHNESS, "weight": 15, "best": 0.2, "worst": 2.5},
        "delivery_gini": {"axis": SMOOTHNESS, "weight": 15, "best": 0.1, "worst": 0.7},
    }


def _ss_v5_assignments() -> dict:
    """v4 rubric with the **time-to-content** ``best`` anchors re-anchored to an
    *aspirational floor* rather than "typical good", so a fast connection no longer pins
    FCP/LCP/byte-earliness at 99–100 and there's headroom to *show* a tuning improvement.
    Only the paint/timing ``best`` values move; weights, worsts, and every other metric are
    unchanged from v4. (DNS/CLS/packet-loss are left as-is: 0% loss / 0 CLS / ~1ms DNS are
    genuine physical floors — no threshold can manufacture headroom there, and they're
    secondary axes anyway. ``render`` is left as-is: at ~50 it already shows change.)"""
    a = _ss_v4_assignments()
    a["ttfb"] = {**a["ttfb"], "best": 30.0}            # 50 → 30ms
    a["fcp"] = {**a["fcp"], "best": 150.0}             # 300 → 150ms (a crown metric)
    a["byte_earliness"] = {**a["byte_earliness"], "best": 150.0}  # 200 → 150
    a["lcp"] = {**a["lcp"], "best": 150.0}             # 800 → 150ms (40% of Speed)
    return a


def _ss_v6_assignments() -> dict:
    """v5 rubric, with the crown decomposed into independent metrics. Drops the conflated
    ``perceived_time`` from scoring (kept as a display-only diagnostic), adds ``total_stall``
    (cumulative dead air) to Smoothness, and promotes the built-in ``load_event``
    (loadEventEnd page-load time) to a scored Speed metric. The crown then corners over
    FCP × total_stall × load_event — two built-in standards plus the one bespoke stall
    signal — so stalls pull the Overall down via the corner geometry, not a hidden weight.
    The new thresholds are reasoned defaults (calibratable)."""
    a = _ss_v5_assignments()
    del a["perceived_time"]  # no longer scored — display-only diagnostic
    # Smoothness: cumulative dead air takes the slot perceived_time vacated.
    a["total_stall"] = {"axis": SMOOTHNESS, "weight": 30, "best": 0.0, "worst": 3000.0}
    # Speed: the recognized "page load time" (loadEventEnd) as the honest time-to-done.
    a["load_event"] = {"axis": SPEED, "weight": 20, "best": 800.0, "worst": 8000.0}
    return a


def _ss_v10_assignments() -> dict:
    """v8 rubric with the Smoothness scored-stall metric = ``stall_energy`` (√Σgap², the L2
    magnitude of the in-load fill gaps — the worst hang *and* the accumulation of stalls in one,
    threshold-free). It replaces ``stall_time`` (→ display-only) exactly as ``stall_time`` replaced
    ``total_stall``. Crown corners over FCP × LCP × stall_energy: first content painted (native
    FCP) × main content loaded (native LCP) × how smoothly the fill happened between them. This
    reverts v9's crown-chasing swaps (delivery / jank_fraction) back to display-only."""
    a = _ss_v8_assignments()
    del a["stall_time"]  # absolute-thresholded sum → display-only diagnostic
    a["stall_energy"] = {"axis": SMOOTHNESS, "weight": 30, "best": 0.0, "worst": 2000.0}
    return a


def _ss_v11_assignments() -> dict:
    """v10 rubric with the Smoothness scored-stall metric = ``worst_void_fraction`` (the
    "pregnant pause" index: the longest void within the FCP→LCP window as a fraction of that
    window). It replaces ``stall_energy`` (→ display-only) exactly as ``stall_energy`` replaced
    ``stall_time``. Crown corners over FCP × LCP × worst_void_fraction: first content painted
    (native FCP) × main content loaded (native LCP) × how *evenly* the fill progressed between
    them. Where ``stall_energy`` was absolute ms — and so correlated with LCP, double-counting a
    slow load's freeze on both the LCP and smoothness legs — ``worst_void_fraction`` is scale-free
    (a fraction of the FCP→LCP span), so it measures *only* evenness, decoupled from how long the
    journey took. A fast-but-lurching load (good FCP, good LCP, dead middle) now scores badly on
    the smoothness leg even though its endpoints are good; a load that dawdles evenly scores well
    here and is caught only by its slow LCP. Best 0 / worst 0.6 (a gap taking ≥60% of the FCP→LCP
    span is a dominant pause), calibratable. derive-v11 adds worst_void_fraction and is purely
    additive, so history re-grades straight from raw."""
    a = _ss_v10_assignments()
    del a["stall_energy"]  # absolute L2 magnitude → display-only diagnostic
    a["worst_void_fraction"] = {"axis": SMOOTHNESS, "weight": 30, "best": 0.0, "worst": 0.6}
    return a


def _ss_v12_assignments() -> dict:
    """v11 rubric, re-anchoring two saturated ``best`` thresholds to the fastest values actually
    measured (so a fast link no longer pins them at 99–100 with no headroom to show a tuning gain):
    DNS ``best`` 1.0 → 0.8ms (a Completion diagnostic, 91% saturated) and page-load (``load_event``)
    ``best`` 800 → 556.2ms (a scored Speed metric, 100% saturated). Crown metrics/thresholds are
    unchanged — these two are secondary axis metrics, so the re-anchor sharpens the Completion/Speed
    axis subscores + clears the saturation warnings without touching the Overall (which corners over
    field-percentile-normalized *raw* crown measurements, immune to grading). Pairs with derive-v12,
    which widens the crown's ``worst_void_fraction`` smoothness leg from the FCP→LCP window to
    FCP→loadEventEnd (same metric key, wider window — the felt pause on a fast link is in the
    post-LCP settle, which the LCP-bounded window missed and read 0)."""
    a = _ss_v11_assignments()
    a["dns"] = {**a["dns"], "best": 0.8}                   # 1.0 → 0.8ms (fastest measured)
    a["load_event"] = {**a["load_event"], "best": 556.2}  # 800 → 556.2ms (fastest measured)
    return a


def _ss_v13_assignments() -> dict:
    """v12 rubric with the Smoothness scored-stall metric = ``network_stall_all`` (floor-free
    network-attributed dead-air) in place of ``worst_void_fraction`` (→ display-only). On a fast
    link the felt/measured "pauses" are RTT-gated resource handoffs, and ``worst_void_fraction``
    read 0 for every profile because its 200ms perceptible floor discards those sub-perceptible
    gaps — so it couldn't rank anything. ``network_stall_all`` drops the floor entirely and isolates
    the network-attributed share (render excluded via LoAF overlap), so it registers the handoff
    gaps fq_codel actually moves and discriminates profiles. The crown corners over FCP × LCP ×
    network_stall_all. Deliberately sub-perceptible: the objective is to rank the *best* profile,
    not to gate on human-noticeable stalls."""
    a = _ss_v12_assignments()
    del a["worst_void_fraction"]  # 200ms-floored scale-free void → display-only (inert on fast links)
    a["network_stall_all"] = {"axis": SMOOTHNESS, "weight": 30, "best": 0.0, "worst": 2000.0}
    return a


def _ss_v9_assignments() -> dict:
    """v8 rubric reworked so the crown ranks only *rank-eligible* measurements (ledger roles
    N/S), not opaque milestone sums. Two scored-metric swaps feed the new crown:

      * Smoothness: the absolute ``stall_time`` → the ratio ``jank_fraction`` (fraction of the
        responseStart→loadEventEnd window spent in perceptible ≥200ms stalls) — weather-immune by
        normalization. ``stall_time`` returns to a display-only diagnostic (as ``total_stall`` did
        when ``stall_time`` superseded it).
      * Speed: newly score ``nav_response`` — body delivery (responseStart→responseEnd), the one
        SQM-facing network phase, the part shaping actually moves. Absolute ms (weather handled by
        interleaved measurement, not a rank-time lens).

    ``byte_earliness`` stays scored on Responsiveness. FCP/LCP/TTFB stay scored on their axes as
    display decompositions — they just leave the crown. Thresholds are reasoned, calibratable
    defaults (Methodology re-anchor)."""
    a = _ss_v8_assignments()
    del a["stall_time"]  # absolute → display-only; the ratio jank_fraction takes the scored slot
    a["jank_fraction"] = {"axis": SMOOTHNESS, "weight": 30, "best": 0.0, "worst": 0.5}
    a["nav_response"] = {"axis": SPEED, "weight": 25, "best": 100.0, "worst": 2000.0}
    return a


def _ss_v8_assignments() -> dict:
    """v6/v7 rubric with the Smoothness cumulative-stall metric swapped from the *relative*
    ``total_stall`` (excess of each gap over the run's own median pace — an average baked into
    the number) to the *absolute* ``stall_time`` (summed duration of every completion gap over a
    fixed 200ms perceptible-stall threshold). ``stall_time`` is an **actual per-run measurement**
    against a fixed yardstick — like FCP/LCP measure a real timestamp — so profiles compare on
    measured dead-air, not on each run's deviation from its own baseline (the "averages of
    averages" the median version introduced). It takes total_stall's Smoothness slot + weight;
    ``total_stall`` returns to a display-only diagnostic. Thresholds carry over (best 0 / worst
    3000ms, calibratable). Derivation bumps to derive-v5 (adds stall_time_ms), which is purely
    additive, so history re-grades straight from raw."""
    a = _ss_v6_assignments()
    del a["total_stall"]  # relative shape stat → back to display-only diagnostic
    a["stall_time"] = {"axis": SMOOTHNESS, "weight": 30, "best": 0.0, "worst": 3000.0}
    return a


METHODOLOGY_REGISTRY: dict[str, dict] = {
    "speed-smoothness-v1": {
        "derivation_version": "derive-v2",
        "notes": (
            "Split the single SOPS headline into Speed (when content arrives) and "
            "Smoothness (how steady delivery was); CLS+INP become Stability & "
            "Interactivity. Thresholds anchored to CWV/Nielsen (perceptual-v5)."
        ),
        "axes": _SS_AXES,
        "assignments": _ss_assignments({"axis": SMOOTHNESS, "best": 500.0, "worst": 8000.0}),
    },
    "speed-smoothness-v2": {
        "derivation_version": "derive-v3",  # frozen: published under derive-v3 (perceived-time w_unoccupied 3→4)
        "notes": (
            "Recalibrate perceived-time so a mostly-stall load can't score green: the "
            "unoccupied/stall weight rose 3→4 (derive-v3) and the perceived-time "
            "threshold tightened to 400/8000ms. A reasoned default — the calibration "
            "harness fits the exact ratio to subjective ratings."
        ),
        "axes": _SS_AXES,
        "assignments": _ss_assignments({"axis": SMOOTHNESS, "best": 400.0, "worst": 8000.0}),
    },
    "speed-smoothness-v3": {
        "derivation_version": "derive-v3",  # frozen: published under derive-v3
        "notes": (
            "Re-anchor thresholds to home-connection 'good' scales while keeping v2's "
            "axes and weights: tighter best anchors on the connection metrics (DNS 1ms, "
            "TCP/TLS 5ms, jitter 0.5ms), the paint/speed metrics (TTFB best 50ms, FCP "
            "300ms, LCP 800ms, render 500ms, byte-earliness 200ms), and the smoothness "
            "metrics (longest-stall 25ms, perceived-time 300ms, cadence 0.20, evenness "
            "0.10), plus CLS best at a pristine 0. Derivation unchanged, so history "
            "re-grades straight from raw."
        ),
        "axes": _SS_AXES,
        "assignments": _ss_v3_assignments(),
    },
    "speed-smoothness-v4": {
        "derivation_version": "derive-v3",  # frozen: published under derive-v3
        "notes": (
            "Reframe the headline axes around the three phases of a load: split the "
            "old blended Speed into Responsiveness (time-to-first: TTFB/FCP/byte-"
            "earliness) and a redefined Speed (time-to-last + interactive: LCP/render/"
            "INP, so INP moves out of Stability). Smoothness unchanged; Stability "
            "becomes CLS-only. Thresholds/weights and derivation carried over from v3, "
            "so history re-grades straight from raw."
        ),
        "axes": _SS_V4_AXES,
        "assignments": _ss_v4_assignments(),
    },
    "speed-smoothness-v5": {
        "derivation_version": "derive-v3",  # frozen: published under derive-v3
        "notes": (
            "Two changes. (1) Promote the seat-of-pants Overall to a first-class, versioned "
            "quantity so grading and crowning can never drift: Overall is defined here (not "
            "in the settings layer) as the corner over the 'feel trinity' metric subscores — "
            "fcp (quickest first response) + perceived_time (lowest perceived time) + inp "
            "(quickest to interactive) — an intersection, not a mean (one weak metric can't "
            "be averaged away); FCP + perceived-time required, INP folds in when captured. "
            "(2) Re-anchor the time-to-content 'best' thresholds to an aspirational floor "
            "(TTFB 50→30, FCP 300→150, byte-earliness 200→150, LCP 800→150ms) so a fast "
            "connection no longer pins FCP/LCP at 99–100 and tuning gains are visible. "
            "Axes/weights/worsts and derivation are otherwise unchanged from v4, so history "
            "re-grades straight from raw."
        ),
        "axes": _SS_V4_AXES,
        "assignments": _ss_v5_assignments(),
        # First-class Overall: the methodology owns *which* metrics define the headline
        # roll-up and *how* they combine. The settings/crown layer reads this, never
        # redefines it. Crowning is then trivial — the confident profile with the highest
        # Overall wins; finding *challengers* to it (the optimistic-ceiling hunt) is separate.
        "overall": {
            "method": "corner",
            "metrics": ["fcp", "perceived_time", "inp"],
            "required": ["fcp", "perceived_time"],
        },
    },
    "speed-smoothness-v6": {
        "derivation_version": "derive-v4",  # frozen: published under derive-v4 (adds total_stall_ms)
        "notes": (
            "Decompose the crown into independent, mostly-built-in metrics. The conflated "
            "perceived_time (which baked an uncalibrated 4× stall penalty into a duration) is "
            "dropped from scoring and kept as a display-only diagnostic. The Overall now corners "
            "over FCP × total_stall × load_event — quickest first response (FCP, a Core Web "
            "Vital), total dead-air across the load (total_stall, the one bespoke smoothness "
            "signal), and the recognized page-load time (load_event = loadEventEnd, a built-in "
            "Navigation-Timing value, newly scored on Speed). Stalls still pull the Overall down "
            "— via the corner geometry, not a hidden weight. total_stall joins Smoothness (best "
            "0 / worst 3000ms) and load_event joins Speed (best 800 / worst 8000ms); both are "
            "reasoned, calibratable defaults. v5's re-anchored time-to-content thresholds carry "
            "over. derive-v4 adds total_stall_ms, so history re-grades straight from raw."
        ),
        "axes": _SS_V4_AXES,
        "assignments": _ss_v6_assignments(),
        # First-class Overall, now decomposed: FCP × total_stall × load_event (all three
        # required — each reliably present on a complete browser run). This single spec is
        # the source of truth the settings/crown layer + challenger race read.
        "overall": {
            "method": "corner",
            "metrics": ["fcp", "total_stall", "load_event"],
            "required": ["fcp", "total_stall", "load_event"],
        },
    },
    "speed-smoothness-v7": {
        "derivation_version": "derive-v4",  # frozen: published under derive-v4 (no new metric; lcp already derived)
        "notes": (
            "Swap the crown's completion leg from load_event (the *technical* page-load: all "
            "resources fetched) to LCP (Largest Contentful Paint — the *perceptual* 'main "
            "content is visible / usefully loaded' milestone). The Overall now corners over "
            "FCP × LCP × total_stall — how fast it starts (FCP), how fast the main content is "
            "there (LCP), and how steadily it filled in between (total_stall, cumulative dead-"
            "air). Three genuinely independent dimensions of the felt experience, so the corner "
            "geometry does real work (paint milestones alone are correlated and co-saturate). "
            "load_event stays a scored Speed metric — just no longer a crown metric. Thresholds "
            "and derivation are unchanged from v6, so history re-grades straight from raw. NB: "
            "LCP's 'best' carries over from v5's aspirational anchor (150ms), so as a corner "
            "term it tends to dominate; re-anchor it from the Methodology page if the Overall "
            "reads too LCP-limited on your link."
        ),
        "axes": _SS_V4_AXES,
        "assignments": _ss_v6_assignments(),  # unchanged from v6 (lcp already scored at best 150)
        "overall": {
            "method": "corner",
            "metrics": ["fcp", "lcp", "total_stall"],
            "required": ["fcp", "lcp", "total_stall"],
        },
    },
    "speed-smoothness-v8": {
        "derivation_version": "derive-v5",  # frozen: published under derive-v5 (adds stall_time_ms)
        "notes": (
            "Swap the crown's cumulative-stall leg from the *relative* total_stall (excess of "
            "each completion gap over the run's OWN median pace — an average baked into the "
            "metric, so a profile's stall standing is a comparison of deviations-from-own-"
            "baseline, not of measured values) to the *absolute* stall_time (summed duration of "
            "every gap over a fixed 200ms perceptible-stall threshold). Like FCP and LCP, "
            "stall_time is an actual per-run measurement against a fixed yardstick — the same for "
            "every run — so Settings-Impact compares profiles on real measured dead-air instead "
            "of averages-of-averages. The Overall now corners over FCP × LCP × stall_time (start "
            "× main-content-visible × measured dead-air). stall_time takes total_stall's "
            "Smoothness slot (best 0 / worst 3000ms); total_stall stays a display-only "
            "diagnostic. derive-v5 adds stall_time_ms and is purely additive, so history "
            "re-grades straight from raw — every run with resource-timing raw gains an actual "
            "stall_time; only pre-resource-timing legacy runs stay quarantined."
        ),
        "axes": _SS_V4_AXES,
        "assignments": _ss_v8_assignments(),
        "overall": {
            "method": "corner",
            "metrics": ["fcp", "lcp", "stall_time"],
            "required": ["fcp", "lcp", "stall_time"],
        },
    },
    "speed-smoothness-v9": {
        "derivation_version": "derive-v8",  # frozen: v9 introduced at derive-v7; derive-v8 fixed jank's window
        "notes": (
            "Rework the crown to rank on *rank-eligible measurements* instead of opaque paint sums. "
            "FCP and LCP are what a human means by 'fast', but each silently bundles DNS+TCP+TLS+"
            "TTFB (network weather) with client render, so ranking on them credits a profile for "
            "network conditions its shaper didn't cause (run-level FCP and probe-TTFB are ~"
            "uncorrelated because the probes ride separate sockets — the confound is real). The "
            "Overall now corners over three independent, attributable dimensions of the felt load: "
            "nav_response (body delivery, responseStart→responseEnd — the SQM-facing phase shaping "
            "actually moves) × byte_earliness (did content arrive early and progressively) × "
            "jank_fraction (fraction of the delivery window responseStart→loadEventEnd spent frozen — the weather-immune "
            "ratio form of stall). Delivery is absolute ms, its weather handled by interleaved "
            "measurement (the challenger race + stale-refresh keep profiles contemporaneous), NOT a "
            "rank-time lens; jank is a ratio, weather-immune by construction. In the scored axes "
            "jank_fraction takes Smoothness's stall slot (stall_time → display-only, as total_stall "
            "was) and nav_response is newly scored on Speed; FCP/LCP/TTFB stay scored as display "
            "decompositions but leave the crown. Thresholds are calibratable defaults. derive-v7 "
            "adds jank_fraction + the nav waterfall and is purely additive, so history re-grades "
            "straight from raw — re-derive first to backfill the new measurements, then re-grade."
        ),
        "axes": _SS_V4_AXES,
        "assignments": _ss_v9_assignments(),
        "overall": {
            "method": "corner",
            "metrics": ["nav_response", "byte_earliness", "jank_fraction"],
            "required": ["nav_response", "byte_earliness", "jank_fraction"],
        },
    },
    "speed-smoothness-v10": {
        "derivation_version": "derive-v10",  # frozen: adds stall_energy
        "notes": (
            "Return the crown to the three things a human directly experiences, ranked on their "
            "felt outcome rather than on what the shaper can move: FCP (initial content appears) × "
            "LCP (main content loaded) × stall_energy (how smoothly it filled in between). FCP and "
            "LCP are *native* browser paint timestamps, not abstractions. The smoothness leg is "
            "stall_energy = √(Σ gap²) over the in-load gaps between resource completions — the L2 "
            "magnitude: the worst single hang plus the accumulation of stalls in one threshold-free, "
            "absolute number (a load's felt smoothness is set by its worst freeze AND how often it "
            "stuttered; the sum of gaps is just duration, the max ignores frequency, √Σgap² captures "
            "both). It's minimised by a quick, even 'zipper' fill and rises as the wait clumps into "
            "chunky stalls. stall_energy takes the Smoothness scored-stall slot (stall_time → "
            "display-only). This reverts v9's crown-chasing legs (delivery/jank_fraction) — those "
            "were chosen for shaper-movability, which is a measurement concern, not what the crown "
            "should measure. derive-v10 adds stall_energy and is purely additive, so history "
            "re-grades straight from raw."
        ),
        "axes": _SS_V4_AXES,
        "assignments": _ss_v10_assignments(),
        "overall": {
            "method": "corner",
            "metrics": ["fcp", "lcp", "stall_energy"],
            "required": ["fcp", "lcp", "stall_energy"],
        },
    },
    "speed-smoothness-v11": {
        "derivation_version": "derive-v11",  # frozen: adds worst_void_fraction (FCP→LCP window)
        "notes": (
            "Refine the crown's smoothness leg to measure the *journey from FCP to LCP*, not the "
            "whole-load dead-air. The felt difference between two profiles with identical fast FCP "
            "and LCP is the shape of the middle: one fills in with steady, consistent progress; the "
            "other paints, sits through a 'pregnant pause' where nothing arrives, then lurches to "
            "LCP. v10's stall_energy (√Σgap² over the whole in-load fill, absolute ms) missed this "
            "two ways: it spanned past LCP (punishing a post-content tail the user never felt) and, "
            "being absolute, it correlated with LCP — double-counting a slow load's freeze on both "
            "the LCP and smoothness legs. The new leg is worst_void_fraction: the single longest "
            "void within the FCP→LCP window as a *fraction* of that window (the pregnant-pause "
            "index). Scale-free by construction, so it measures ONLY the evenness of the fill, "
            "decoupled from how long the journey took (that stays LCP's job) — which makes the "
            "three crown legs genuinely independent (when it starts × when it's done × how steady "
            "the trip was) and kills the double-count. A fast-but-lurching load now scores badly on "
            "smoothness even with a good LCP; a load that dawdles evenly scores well here and is "
            "caught only by its slow LCP. The Overall corners over FCP × LCP × worst_void_fraction. "
            "worst_void_fraction takes stall_energy's Smoothness slot (best 0 / worst 0.6, "
            "calibratable); stall_energy stays a display-only diagnostic (as stall_time did when "
            "stall_energy superseded it). derive-v11 adds worst_void_fraction and is purely "
            "additive, so history re-grades straight from raw."
        ),
        "axes": _SS_V4_AXES,
        "assignments": _ss_v11_assignments(),
        "overall": {
            "method": "corner",
            "metrics": ["fcp", "lcp", "worst_void_fraction"],
            "required": ["fcp", "lcp", "worst_void_fraction"],
        },
    },
    "speed-smoothness-v12": {
        "derivation_version": "derive-v12",  # frozen: widens worst_void_fraction to FCP→load
        "notes": (
            "Two changes, both driven by fast-link measurements. (1) Widen the crown's smoothness "
            "leg. worst_void_fraction stays the crown metric (FCP × LCP × worst_void_fraction) but "
            "its window widens from FCP→LCP to FCP→loadEventEnd (derive-v12). On a fast link the "
            "largest paint lands almost immediately after the first (FCP→LCP ~tens of ms), so there "
            "is no pre-LCP journey to have a pause in — the felt dead-air is in the *post-LCP "
            "settle*, which the LCP-bounded window missed entirely and read 0 for nearly every "
            "profile (an inert crown leg). Ending at the load event captures where the pause "
            "actually lives, while the resources_within_load bound still excludes the post-load "
            "background trickle. This is NOT a revert to v10's stall_energy: the metric stays "
            "scale-free (a *fraction* of the window), so — unlike absolute √Σgap² ms — it doesn't "
            "correlate with the load duration and doesn't double-count a slow load's freeze on both "
            "the LCP and smoothness legs. Only the window reverts; the form that fixed the "
            "double-count stays. (2) Re-anchor two saturated best thresholds to the fastest measured "
            "value so a fast link no longer pins them at 99–100: DNS best 1.0 → 0.8ms (Completion "
            "diagnostic, 91% saturated) and page-load best 800 → 556.2ms (scored Speed, 100% "
            "saturated). Both are secondary axis metrics, so this sharpens their subscores + clears "
            "the saturation warnings without moving the Overall. derive-v12 changes "
            "worst_void_fraction's value (a formula change), so re-derive from raw first, then "
            "re-grade."
        ),
        "axes": _SS_V4_AXES,
        "assignments": _ss_v12_assignments(),
        "overall": {
            "method": "corner",
            "metrics": ["fcp", "lcp", "worst_void_fraction"],
            "required": ["fcp", "lcp", "worst_void_fraction"],
        },
    },
    "speed-smoothness-v13": {
        "derivation_version": "derive-v13",  # frozen: adds network_stall_all (fabricated 0 pre-v14)
        "notes": (
            "Swap the crown's smoothness leg to network-attributed stall with NO floor. On a fast "
            "link worst_void_fraction read 0 for every profile — its 200ms perceptible-stall floor "
            "discards exactly the sub-perceptible RTT/handoff gaps that a page load on fiber is made "
            "of (the waterfall is gated by round trips — DNS/TCP/TLS/request + ACK pacing — not "
            "bandwidth), so it couldn't rank anything. network_stall_all sums every network-"
            "attributed inter-resource gap with the minimum-gap floor dropped to 0, isolating the "
            "share fq_codel's fairness/AQM actually moves (render-covered time excluded via LoAF "
            "overlap). It is deliberately below human perception: the objective is to CROWN the best "
            "profile by measured network dead-air, not to gate on human-noticeable hitches. The "
            "Overall corners over FCP × LCP × network_stall_all; worst_void_fraction → display-only "
            "(joining stall_energy/stall_time/total_stall). derive-v13 adds network_stall_all and is "
            "purely additive, so history re-grades straight from raw."
        ),
        "axes": _SS_V4_AXES,
        "assignments": _ss_v13_assignments(),
        "overall": {
            "method": "corner",
            "metrics": ["fcp", "lcp", "network_stall_all"],
            "required": ["fcp", "lcp", "network_stall_all"],
        },
    },
    "speed-smoothness-v14": {
        "derivation_version": "derive-v14",  # frozen: omit network_stall_all when unmeasurable
        "notes": (
            "Same rubric as v13 (crown = FCP × LCP × network_stall_all, identical axes/weights/"
            "thresholds) — this version exists to mark a **comparability boundary**, not a rubric "
            "change. Under v13 + derive-v13 the crown leg network_stall_all was fabricated as a "
            "perfect 0 for any run without LoAF/longtask provenance (loaf_source is None → the "
            "network-vs-render split is unmeasurable, so stall_attribution_times routed all gap time "
            "to 'unknown' and returned network_ms=0). Because the metric is lower-is-better, that 0 "
            "was the *best possible* score: pre-instrument history rode it to #1 and out-ranked real "
            "measurements, so a crowned profile slid down the standings over time as fresh, "
            "attributable runs arrived (the 'best drops to 65th' report). derive-v14 stops "
            "synthesizing the attribution metrics (network_stall/render_stall/network_stall_all) when "
            "provenance is absent — smoothness_metrics omits them — so comparability quarantines those "
            "runs as *incomparable* instead of scoring them wrong. Publishing v14 forces every run to "
            "be freshly graded under the corrected derivation (new Score rows), so no stale v13 Score "
            "with a fabricated 0 can linger. Follow-through: re-derive (drop the bogus 0s from raw) → "
            "re-grade (re-quarantine) → Re-run top-N profiles (winner-first refresh) to rebuild fresh "
            "comparable data on the best performers. The crown still pools across ALL times by design "
            "— comparing profiles across every scenario is the point; no recency-window/weather "
            "de-confound."
        ),
        "axes": _SS_V4_AXES,
        "assignments": _ss_v13_assignments(),
        "overall": {
            "method": "corner",
            "metrics": ["fcp", "lcp", "network_stall_all"],
            "required": ["fcp", "lcp", "network_stall_all"],
        },
    },
    "speed-smoothness-v15": {
        "derivation_version": DERIVATION_VERSION,  # derive-v14 — grading change only, re-grade not re-derive
        "notes": (
            "Crown by a magnitude-aware WEIGHTED AVERAGE of the perception-calibrated subscores "
            "instead of a field-percentile CORNER. Measurement (the crown-lead-vs-noise readout) "
            "showed the percentile corner was un-crownable on a fast link: with ~149 profiles packed "
            "into a few ms, a sub-ms per-run wobble crosses dozens of profiles, so the normalized "
            "Overall carried a ±17-point SE and the top ~66 profiles were a statistical tie — no "
            "feasible number of runs could separate them (SE shrinks only as √n). The noise was "
            "manufactured by percentile ranking; it isn't in the raw milliseconds. v15 ranks on "
            "``weighted``: each run's Overall is the weight-averaged calibrated subscore (FCP 1 · LCP 1 "
            "· network_stall_all 0.5 — fastest-to-first and fastest-to-main-content even, smoothness "
            "secondary), a scale on which a profile's median is pinned to ~±1 and the field actually "
            "separates. It's additive, not an intersection: a strong FCP/LCP is no longer vetoed by a "
            "mediocre stall leg (the corner's behavior) — the deliberate trade the corner was hiding. "
            "Same metrics/derivation as v14, so history re-grades straight from cached scalars (no "
            "re-derive). Trade-off named: because it's calibrated, re-anchoring a threshold can move "
            "the crown — but that is exactly the lever that makes the field distinguishable, which the "
            "percentile corner could not. The per-metric percentile standings columns stay for display."
        ),
        "axes": _SS_V4_AXES,
        "assignments": _ss_v13_assignments(),
        "overall": {
            "method": "weighted",
            "metrics": ["fcp", "lcp", "network_stall_all"],
            "required": ["fcp", "lcp", "network_stall_all"],
            # Per-metric weights on the calibrated 0–100 subscore scale. FCP and LCP even;
            # network_stall_all (smoothness) secondary. Read generically by the crown wiring —
            # a new methodology changes the mix here, nothing else.
            "weights": {"fcp": 1.0, "lcp": 1.0, "network_stall_all": 0.5},
        },
    },
}


# Invariant: every Overall/crown metric a version *requires* must be a scored metric in
# that version (have an assignment) — else no run could ever supply it and every run would
# be quarantined as ``incomparable`` (the "valid but unscorable Overall" trap). Asserted at
# import so a new methodology can't ship a crown-required metric it doesn't actually score.
for _ver, _spec in METHODOLOGY_REGISTRY.items():
    _crown_required = set((_spec.get("overall") or {}).get("required") or [])
    _scored_keys = set(_spec.get("assignments") or {})
    _unscored = _crown_required - _scored_keys
    assert not _unscored, (
        f"methodology {_ver}: crown-required metrics not scored (no assignment): "
        f"{sorted(_unscored)}"
    )


def build_definition_from_spec(spec: dict) -> dict:
    """Build a full frozen definition from a registry spec + the metric catalog."""
    assignments = spec.get("assignments", {})
    overall = spec.get("overall") or {}
    # One universal `required` set, materialized onto the metric entries so the frozen
    # snapshot is self-describing: the Overall (a.k.a. *crown*) metrics this version
    # requires (``overall.required``) plus any axis metric explicitly flagged required.
    # Overall == Crown == required — the metrics that compute the headline roll-up are
    # exactly the ones a run must carry. ``comparability``, the Methodology view, and the
    # re-grade all read this one field via ``required_metric_keys``.
    required_keys = set(overall.get("required") or [])
    required_keys |= {k for k, a in assignments.items() if a.get("required")}
    out_metrics: list[dict] = []
    for m in metrics_mod.METRICS:
        a = assignments.get(m.key)
        if a is not None:
            out_metrics.append(
                {
                    "key": m.key, "axis": a["axis"], "plugin": m.plugin,
                    "source_key": m.source_key, "label": m.label,
                    "description": m.description, "unit": m.unit,
                    "weight": a["weight"], "best": a.get("best"), "worst": a.get("worst"),
                    "higher_is_better": m.higher_is_better,
                    "required": m.key in required_keys, "order": metrics_mod._order(m.key),
                }
            )
        else:  # not scored under this version — carried as a display-only diagnostic
            out_metrics.append(
                {
                    "key": m.key, "axis": None, "plugin": m.plugin,
                    "source_key": m.source_key, "label": m.label,
                    "description": m.description, "unit": m.unit, "weight": 0.0,
                    "best": m.best, "worst": m.worst, "higher_is_better": m.higher_is_better,
                    "required": False, "order": metrics_mod._order(m.key),
                }
            )
    out_metrics.sort(key=lambda x: x["order"])
    definition = {"axes": spec["axes"], "metrics": out_metrics}
    if spec.get("overall"):  # first-class Overall spec (v5+), carried into the frozen def
        definition["overall"] = spec["overall"]
    return definition


def overall_from_definition(definition: dict, subscores: dict | None) -> float | None:
    """The methodology's first-class Overall for one run: the corner over its feel-trinity
    metric subscores, per the version's ``overall`` spec. Requires the spec's ``required``
    metrics and folds in the rest when present. Returns None if the version has no overall
    spec (pre-v5) or a required metric is missing — so grading and crowning derive the
    headline number from one versioned definition."""
    spec = (definition or {}).get("overall") or {}
    metrics = spec.get("metrics") or []
    required = spec.get("required") or []
    sub = subscores or {}
    if not metrics or any(sub.get(k) is None for k in required):
        return None
    # The combine method lives in the spec, so the crown formula is a methodology property, not
    # code: ``weighted`` = weight-averaged calibrated subscores (magnitude-aware); anything else
    # (default) = the √k corner (intersection). A new methodology swaps the method here, nothing else.
    if spec.get("method") == "weighted":
        weights = spec.get("weights") or {}
        return weighted_score([(sub.get(k), float(weights.get(k, 1.0))) for k in metrics])
    return corner_score([sub.get(k) for k in metrics if sub.get(k) is not None])


def overall_weights(definition: dict) -> dict:
    """The crown's per-metric weights from the methodology's ``overall`` spec (empty for a
    corner/percentile method, which is unweighted). The single source the crown wiring reads,
    so weights are a methodology property — change them there, not in the ranking code."""
    return dict(((definition or {}).get("overall") or {}).get("weights") or {})


def overall_method(definition: dict) -> str:
    """The crown's combine method (``corner`` default | ``weighted``) — read by the settings/crown
    layer so it ranks with the same rule the persisted per-run Overall was graded under."""
    return str(((definition or {}).get("overall") or {}).get("method") or "corner")


def required_metric_keys(definition: dict) -> list[str]:
    """The single, canonical **required** set for a methodology — the one field referenced
    everywhere (comparability, the Methodology view, the re-grade) instead of each call site
    re-deriving it.

        required = metrics flagged ``required`` in the definition  ∪  the Overall (a.k.a.
        *crown*) metrics the version's ``overall`` spec marks required.

    Overall == Crown == required: the metrics that compute the headline roll-up are exactly
    the metrics a run must carry, or it can't reproduce this methodology's score and is
    quarantined as ``incomparable``. The union (rather than reading only the materialized
    per-metric flag) keeps this correct for definitions snapshotted before the flag was
    materialized onto crown metrics. Order-preserved, de-duplicated, limited to metrics the
    definition actually declares."""
    metrics = (definition or {}).get("metrics", [])
    known = {m["key"] for m in metrics}
    flagged = [m["key"] for m in metrics if m.get("required")]
    _, overall_required = overall_metrics(definition)
    return list(dict.fromkeys(flagged + [k for k in overall_required if k in known]))


def is_comparable(score) -> bool:
    """The single predicate every scored view filters on: a run is comparable under a
    methodology iff its Score isn't ``incomparable`` (its raw supplied every required
    metric). Centralizing it means an incomparable run can't leak a headline number into a
    view that simply forgot the filter. ``None`` (no score) → not comparable."""
    return score is not None and getattr(score, "comparability", None) != "incomparable"


def overall_metrics(definition: dict) -> tuple[list[str], list[str]]:
    """The crown's ``(metrics, required)`` keys from a methodology's ``overall`` spec —
    the single source of truth for which metric subscores the Overall corners over. The
    settings/crown layer reads this so the persisted Overall, the live fallback, the
    challenger's optimistic estimate, and the per-metric spreads never diverge. Empty
    lists for a pre-v5 definition with no overall spec."""
    spec = (definition or {}).get("overall") or {}
    return list(spec.get("metrics") or []), list(spec.get("required") or [])


def _effective(m: metrics_mod.MetricDef, config: dict) -> tuple[float, float | None, float | None]:
    """A metric's weight + best/worst thresholds, with stored config overriding the
    registry defaults (so a snapshot reflects *this instance's* actual rubric)."""
    if m.axis == SOPS:
        weight = (config.get("weights") or {}).get(m.key, m.weight)
        thr = (config.get("thresholds") or {}).get(m.key) or {}
    elif m.axis == COMPLETION:
        weight = (config.get("completion_weights") or {}).get(m.key, m.weight)
        thr = (config.get("completion_thresholds") or {}).get(m.key) or {}
    else:  # display-only — not scored
        weight, thr = 0.0, {}
    return weight, thr.get("best", m.best), thr.get("worst", m.worst)


def build_definition(config: dict) -> dict:
    """The full frozen catalog + rubric for the current registry and config.

    Self-contained: axes plus every metric with its effective weight/thresholds,
    unit, label, and whether it's ``required`` (a run lacking a required metric
    can't be scored *exactly* under this methodology — drives comparability)."""
    axes = [{"key": axis, **meta} for axis, meta in AXIS_META.items()]
    out_metrics: list[dict] = []
    for m in metrics_mod.METRICS:
        weight, best, worst = _effective(m, config)
        out_metrics.append(
            {
                "key": m.key,
                "axis": m.axis,
                "plugin": m.plugin,
                "source_key": m.source_key,
                "label": m.label,
                "description": m.description,
                "unit": m.unit,
                "weight": weight,
                "best": best,
                "worst": worst,
                "higher_is_better": m.higher_is_better,
                # A run missing a `required` metric isn't exactly-scorable here.
                "required": m.marks_latest,
                "order": metrics_mod._order(m.key),
            }
        )
    out_metrics.sort(key=lambda x: x["order"])
    return {"axes": axes, "metrics": out_metrics}


def current_version(config: dict) -> str:
    """The methodology version id new runs are scored under.

    Defaults to ``CURRENT_METHODOLOGY``; an explicit ``methodology_version`` in config
    overrides it (lets an instance pin a version, and keeps tests isolated)."""
    return str((config or {}).get("methodology_version") or CURRENT_METHODOLOGY)


def ensure_current_methodology(session: Session, config: dict, notes: str | None = None) -> Methodology:
    """Record the current methodology if not already stored, and mark it current.

    Snapshots the definition the *first* time a version is seen — from the registry
    spec when one exists, else from the live catalog+config (legacy bootstrap) — and
    never edits it afterward (append-only). Flips ``is_current`` so exactly one row is
    the published-now methodology. Idempotent."""
    version = current_version(config)
    row = session.get(Methodology, version)
    if row is None:
        spec = METHODOLOGY_REGISTRY.get(version)
        definition = build_definition_from_spec(spec) if spec else build_definition(config)
        derivation = spec["derivation_version"] if spec else DERIVATION_VERSION
        row = Methodology(
            version=version,
            rubric_version=version,
            derivation_version=derivation,
            notes=spec.get("notes") if spec else notes,
            definition=definition,
            is_current=True,
        )
        session.add(row)
        log.info("Recorded methodology %s (derivation %s)", version, derivation)
    # Exactly one current: clear the flag on every other version.
    for other in session.scalars(select(Methodology).where(Methodology.version != version)):
        other.is_current = False
    row.is_current = True
    session.commit()
    return row


def supersede_stale_methodology_pin(session: Session, config: dict) -> str | None:
    """Drop a stale re-anchor pin so a newly-shipped code methodology takes over on deploy.

    The GUI re-anchor endpoint (``POST /api/methodologies/reanchor``) pins
    ``config.methodology_version`` to a *fork* of whatever methodology was current at the
    time — e.g. ``speed-smoothness-v6+fcp-best150``. That pin otherwise outlives the deploy
    that bumps ``CURRENT_METHODOLOGY``, so ``current_version`` keeps returning the old fork
    and a freshly-published methodology (say v7) never becomes current — the instance is
    permanently frozen on the old rubric.

    A fork pin whose base (the part before ``+``) is no longer ``CURRENT_METHODOLOGY`` was
    forked from a now-superseded methodology, so we clear it and let the code-published
    version take over. Left untouched: a bare (non-fork) pin — a deliberate operator choice
    to hold a version — and a re-anchor fork of the *current* base (still valid, keep it
    until the next methodology ships). Returns the cleared pin, if any."""
    pin = (config or {}).get("methodology_version")
    if not pin or "+" not in str(pin):
        return None  # unset, or a deliberate bare version pin — respect it
    if str(pin).split("+", 1)[0] == CURRENT_METHODOLOGY:
        return None  # a re-anchor of the still-current methodology — keep it
    save_config(session, {"methodology_version": None})
    log.info(
        "Superseded stale methodology pin %s → %s (code-published on deploy)",
        pin, CURRENT_METHODOLOGY,
    )
    return str(pin)


def seed_current_methodology() -> None:
    """Startup hook: ensure the current methodology is recorded. Best-effort.

    First drops any stale re-anchor pin (see ``supersede_stale_methodology_pin``) so a
    deploy that ships a new ``CURRENT_METHODOLOGY`` actually adopts it, then records +
    marks that version current."""
    try:
        with session_scope() as session:
            supersede_stale_methodology_pin(session, get_config(session))
            ensure_current_methodology(session, get_config(session))
    except Exception:  # noqa: BLE001 — never block startup on this
        log.warning("Could not seed current methodology", exc_info=True)


def _scored(definition: dict) -> list[dict]:
    return [m for m in definition.get("metrics", []) if m.get("axis")]


def summarize(row: Methodology) -> dict:
    """Compact list-view of a methodology (no full metric table)."""
    definition = row.definition or {}
    scored = _scored(definition)
    return {
        "version": row.version,
        "rubric_version": row.rubric_version,
        "derivation_version": row.derivation_version,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "notes": row.notes,
        "is_current": row.is_current,
        "axes": definition.get("axes", []),
        "metric_count": len(definition.get("metrics", [])),
        "scored_metric_count": len(scored),
        # The canonical required set (crown/Overall ∪ flagged), not just the materialized
        # per-metric flag — so old snapshots still report the crown metrics as required.
        "required_metrics": required_metric_keys(definition),
    }


def serialize(row: Methodology) -> dict:
    """Full methodology including the frozen definition.

    The per-metric ``required`` flag is overlaid from the canonical ``required_metric_keys``
    so the Methodology page's chips agree with what comparability enforces, even for a
    definition snapshotted before the flag was materialized onto crown metrics."""
    definition = row.definition or {}
    req = set(required_metric_keys(definition))
    metrics = [{**m, "required": m["key"] in req} for m in definition.get("metrics", [])]
    return {**summarize(row), "definition": {**definition, "metrics": metrics}}


# ── (run × methodology) scores ───────────────────────────────────────────────


def at_measure_comparability(metric_values: dict | None) -> tuple[str, list[str]]:
    """Comparability of a run's at-measure score: ``exact`` once it carries the
    current-rubric markers, else ``incomparable`` (the legacy case — a required
    metric the run never captured). The cross-methodology ``partial`` tier is a
    Phase-3 concern (re-grading onto a *different* methodology)."""
    if has_latest_metrics(metric_values):
        return "exact", []
    missing = [k for k in latest_metric_keys() if (metric_values or {}).get(k) is None]
    return "incomparable", missing


def comparability(definition: dict, metric_values: dict | None) -> tuple[str, list[str]]:
    """Can a run's raw reproduce this methodology's metrics? (the at-present check)

    ``exact`` (every scored metric present), ``partial`` (some optional metrics
    missing → redistributed; ``missing`` lists them), or ``incomparable`` (a
    ``required`` metric the raw never captured — a new instrument added after this
    run). Drives the RTINGS-style "scored under v4; under current v6: N (exact)" vs
    "not comparable — needs metric X".

    Comparability is tied to *crownability*: the methodology's ``overall`` crown
    metrics (``overall_metrics``) count as required, so a run whose raw can't produce
    the headline Overall (e.g. a pre-v6 run with no ``total_stall``) is flagged
    ``incomparable`` and quarantined — never silently scored without the metrics that
    define the score. This auto-adapts to every methodology's crown."""
    metrics = (definition or {}).get("metrics", [])
    # The required set is the single canonical accessor (per-metric `required` flags ∪ the
    # crown/Overall metrics) — one source of truth, never re-unioned ad hoc here.
    required = required_metric_keys(definition)
    scored = [m["key"] for m in metrics if m.get("axis")]
    mv = metric_values or {}
    missing_required = [k for k in required if mv.get(k) is None]
    if missing_required:
        return "incomparable", missing_required
    missing = [k for k in scored if mv.get(k) is None]
    if missing:
        return "partial", missing
    return "exact", []


def rubric_from_definition(definition: dict, axis: str) -> tuple[dict, dict]:
    """Reconstruct an axis's ``(weights, thresholds)`` from a frozen methodology
    definition — so a run can be re-scored under *that* methodology's rubric, not
    whatever the live config happens to be."""
    metrics = [m for m in (definition or {}).get("metrics", []) if m.get("axis") == axis]
    weights = {m["key"]: m["weight"] for m in metrics}
    thresholds = {m["key"]: {"best": m["best"], "worst": m["worst"]} for m in metrics}
    return weights, thresholds


def scored_axes(definition: dict) -> list[dict]:
    """The axes that actually carry scored metrics, in definition order."""
    have = {m["axis"] for m in (definition or {}).get("metrics", []) if m.get("axis")}
    return [a for a in (definition or {}).get("axes", []) if a["key"] in have]


def axis_rubric(definition: dict, axis: str) -> tuple[dict, dict, dict]:
    """An axis's ``(metric_sources, weights, thresholds)`` for generic scoring —
    enough to call ``compute_score`` for *any* axis the methodology defines."""
    metrics = [m for m in (definition or {}).get("metrics", []) if m.get("axis") == axis]
    sources = {m["key"]: (m["plugin"], m["source_key"]) for m in metrics}
    weights = {m["key"]: m["weight"] for m in metrics}
    thresholds = {m["key"]: {"best": m["best"], "worst": m["worst"]} for m in metrics}
    return sources, weights, thresholds


def serialize_score(row: Score) -> dict:
    """A (run × methodology) Score for the API."""
    return {
        "run_id": row.run_id,
        "methodology_version": row.methodology_version,
        "is_at_measure": row.is_at_measure,
        "comparability": row.comparability,
        "missing_metrics": row.missing_metrics or [],
        "axis_scores": row.axis_scores or {},
        "subscores": row.subscores or {},
        "weights_used": row.weights_used or {},
        "metric_values": row.metric_values or {},
        "bands": row.bands or {},
        "computed_at": row.computed_at.isoformat() if row.computed_at else None,
    }


def _band(stdev, lo, hi) -> dict | None:
    band = {"stdev": stdev, "min": lo, "max": hi}
    return band if any(v is not None for v in band.values()) else None


def score_fields_from_score_result(sr: ScoreResult) -> dict:
    """Translate a legacy ``ScoreResult`` into the unified (run × methodology) Score
    fields, merging the SOPS and Completion axes into one record."""
    metric_values = {**(sr.completion_metric_values or {}), **(sr.metric_values or {})}
    axis_scores: dict[str, float] = {"sops": sr.sops}
    if sr.completion is not None:
        axis_scores["completion"] = sr.completion
    bands: dict[str, dict] = {}
    sb = _band(sr.sops_stdev, sr.sops_min, sr.sops_max)
    if sb:
        bands["sops"] = sb
    cb = _band(sr.completion_stdev, sr.completion_min, sr.completion_max)
    if cb:
        bands["completion"] = cb
    comparability, missing = at_measure_comparability(sr.metric_values)
    return {
        "axis_scores": axis_scores,
        "subscores": {**(sr.subscores or {}), **(sr.completion_subscores or {})},
        "weights_used": {**(sr.weights_used or {}), **(sr.completion_weights_used or {})},
        "metric_values": metric_values,
        "bands": bands or None,
        "comparability": comparability,
        "missing_metrics": missing or None,
    }


def upsert_score(session: Session, run_id: int, version: str, *, is_at_measure: bool, **fields) -> Score:
    """Create or refresh the Score row for ``(run, methodology)``.

    Used both at capture (at-measure) and, later, by re-grading (at-present). The
    UNIQUE(run_id, methodology_version) constraint guarantees one row per pairing."""
    row = session.scalar(
        select(Score).where(Score.run_id == run_id, Score.methodology_version == version)
    )
    if row is None:
        row = Score(run_id=run_id, methodology_version=version)
        session.add(row)
    row.is_at_measure = is_at_measure
    for k, v in fields.items():
        setattr(row, k, v)
    return row


def record_at_measure(session: Session, run: Run, sr: ScoreResult, version: str) -> Score:
    """Write a run's at-measure Score (its capture-time interpretation) and stamp
    the run with the methodology it was scored under. Caller commits."""
    run.methodology_version = version
    return upsert_score(
        session, sr.run_id, version, is_at_measure=True, **score_fields_from_score_result(sr)
    )


# Note: there is deliberately no migration of historical ScoreResults into the
# Score table. The raw observations ("state of the internet") are the only thing
# worth preserving; because raw + methodology → score is deterministic, historical
# runs are (re)scored from their preserved raw under whatever methodology we choose
# (Phase 3's rederive), rather than carrying forward churny pre-foundation scores.
