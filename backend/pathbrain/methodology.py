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
    """The methodology version id scores reference (the rubric version)."""
    return str(config.get("rubric_version") or "unversioned")


def ensure_current_methodology(session: Session, config: dict, notes: str | None = None) -> Methodology:
    """Record the current methodology if not already stored, and mark it current.

    Snapshots the definition the *first* time a version is seen and never edits it
    afterward (append-only). Flips ``is_current`` so exactly one row is the
    published-now methodology. Idempotent."""
    version = current_version(config)
    row = session.get(Methodology, version)
    if row is None:
        row = Methodology(
            version=version,
            rubric_version=version,
            derivation_version=DERIVATION_VERSION,
            notes=notes,
            definition=build_definition(config),
            is_current=True,
        )
        session.add(row)
        log.info("Recorded methodology %s (derivation %s)", version, DERIVATION_VERSION)
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
