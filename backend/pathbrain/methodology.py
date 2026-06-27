"""Methodology snapshots — how raw becomes a score, frozen and versioned.

A methodology is the immutable bundle of *derivation* (raw → metric scalars) and
*rubric* (metric scalars → axis scores). This module builds a self-contained
``definition`` from the live registry + effective config and persists it once per
version, so every score can be read/reproduced under the exact interpretation that
was in play (see ``docs/methodology.md``). Methodologies are append-only: a weight,
threshold, or metric change is published as a new version, never an edit.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import metrics as metrics_mod
from .config_store import get_config
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

# The version new runs are scored under (the "published now" methodology).
CURRENT_METHODOLOGY = "speed-smoothness-v3"

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
        "derivation_version": DERIVATION_VERSION,  # derive-v3: perceived-time w_unoccupied 3→4
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
        "derivation_version": DERIVATION_VERSION,  # derive-v3 (no formula change)
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
}


def build_definition_from_spec(spec: dict) -> dict:
    """Build a full frozen definition from a registry spec + the metric catalog."""
    catalog = {m.key: m for m in metrics_mod.METRICS}
    assignments = spec.get("assignments", {})
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
                    "required": a.get("required", False), "order": metrics_mod._order(m.key),
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
    return {"axes": spec["axes"], "metrics": out_metrics}


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


def seed_current_methodology() -> None:
    """Startup hook: ensure the current methodology is recorded. Best-effort."""
    try:
        with session_scope() as session:
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
        "required_metrics": [m["key"] for m in scored if m.get("required")],
    }


def serialize(row: Methodology) -> dict:
    """Full methodology including the frozen definition."""
    return {**summarize(row), "definition": row.definition or {}}


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
    "not comparable — needs metric X"."""
    metrics = (definition or {}).get("metrics", [])
    required = [m["key"] for m in metrics if m.get("required")]
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
