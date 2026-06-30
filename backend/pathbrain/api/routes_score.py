"""Score endpoints: fetch a run's score, preview scoring, inspect weights."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median, quantiles

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs
from ..config_store import get_config
from ..database import get_session, session_scope
from ..metrics import has_latest_metrics
from ..config import get_settings
from ..interpret import DERIVATION_VERSION
from ..models import BenchmarkResult, Run, RunStatus, Score, ScoreResult
from ..runner import rederive_run, rescore_run, score_history_under_current
from ..schemas import ScoreOut
from ..scoring import compute_score

router = APIRouter()

# Commit cadence for the long re-score/re-derive passes (resumable progress).
_COMMIT_EVERY = 25


def _attribution(network_ms: float | None, render_ms: float | None, unknown_ms: float | None) -> dict | None:
    """Summarize where the window's stall time came from (PRD R7).

    Returns the per-source median stall time plus a ``dominant`` tag
    (``network`` | ``render`` | ``mixed`` | ``unknown``), or ``None`` when no
    meaningful stall time was recorded. ``dominant`` is ``mixed`` unless one source
    accounts for ≥60% of attributed stall time — so a clearly network-bound stall
    reads "network" (tunable) and a main-thread one reads "render" (not tunable)."""
    n, r, u = network_ms or 0.0, render_ms or 0.0, unknown_ms or 0.0
    total = n + r + u
    if total < 1.0:  # under a millisecond of stall — nothing worth attributing
        return None
    parts = {"network": n, "render": r, "unknown": u}
    top = max(parts, key=parts.get)
    dominant = top if parts[top] / total >= 0.6 else "mixed"
    return {
        "network_ms": round(n, 1),
        "render_ms": round(r, 1),
        "unknown_ms": round(u, 1),
        "dominant": dominant,
    }


@router.post("/score/rescore", status_code=202)
def rescore_history() -> dict:
    """Re-grade every completed run with the current scoring rubric, **in the
    background**. Returns ``{job_id}`` immediately; track it in the jobs feed.

    Run this after changing thresholds/weights so historical scores stay
    comparable (no discontinuity in the SOPS timeline at the change).
    """

    def task(job: jobs.Job) -> dict:
        with session_scope() as session:
            cfg = get_config(session)
            weights = cfg.get("weights", {})
            thresholds = cfg.get("thresholds", {})
            rubric_version = cfg.get("rubric_version")
            c_weights = cfg.get("completion_weights", {})
            c_thresholds = cfg.get("completion_thresholds", {})
            runs = session.scalars(select(Run).where(Run.status == RunStatus.COMPLETE)).all()
            total = len(runs)
            rescored = 0
            for i, run in enumerate(runs):
                if rescore_run(run, weights, thresholds, rubric_version, c_weights, c_thresholds):
                    rescored += 1
                if (i + 1) % _COMMIT_EVERY == 0:
                    session.commit()
                job.set_progress(i + 1, total, f"re-scored {rescored}/{total}")
            session.commit()
            return {"rescored": rescored, "rubric_version": rubric_version}

    job_id = jobs.start("rescore", "Re-score history (current rubric)", task, href="/config")
    return {"job_id": job_id}


@router.post("/score/rederive", status_code=202)
def rederive_history() -> dict:
    """Re-derive *and* re-grade every completed run from its stored raw observations,
    **in the background**. Returns ``{job_id}`` immediately.

    Heavier than ``/score/rescore`` (which only re-applies the rubric to cached
    metric scalars): this re-runs the full interpretation, so a new metric or a
    changed derivation formula (e.g. a better Speed Index) lands on history without
    re-collecting. Runs whose raw lacks a signal just don't gain that metric.
    """
    import os

    def task(job: jobs.Job) -> dict:
        with session_scope() as session:
            cfg = get_config(session)
            weights = cfg.get("weights", {})
            thresholds = cfg.get("thresholds", {})
            rubric_version = cfg.get("rubric_version")
            c_weights = cfg.get("completion_weights", {})
            c_thresholds = cfg.get("completion_thresholds", {})
            artifact_base = os.path.abspath(get_settings().artifact_dir)
            runs = session.scalars(select(Run).where(Run.status == RunStatus.COMPLETE)).all()
            total = len(runs)
            rederived = 0
            for i, run in enumerate(runs):
                if rederive_run(
                    run, weights, thresholds, rubric_version, c_weights, c_thresholds, artifact_base
                ):
                    rederived += 1
                if (i + 1) % _COMMIT_EVERY == 0:
                    session.commit()
                job.set_progress(i + 1, total, f"re-derived {rederived}/{total}")
            session.commit()
            return {"rederived": rederived, "derivation_version": DERIVATION_VERSION}

    job_id = jobs.start("rederive", "Re-derive history from raw", task, href="/config")
    return {"job_id": job_id}


@router.post("/score/regrade", status_code=202)
def regrade_history() -> dict:
    """Score every completed run from its preserved raw under the *current*
    methodology, **in the background**. Returns ``{job_id}`` immediately.

    This is the methodology-aware successor to ``/score/rescore`` + ``/score/rederive``:
    it never mutates a run's at-measure score, it works straight from raw (so a
    re-weight *or* a new metric both land), and it tags each run exact / partial /
    incomparable. Progress + the final comparability summary surface in the jobs feed.
    """

    def task(job: jobs.Job) -> dict:
        with session_scope() as session:
            return score_history_under_current(session, progress=job.set_progress)

    job_id = jobs.start(
        "regrade", "Re-grade history under current methodology", task, href="/methodology"
    )
    return {"job_id": job_id}


@router.get("/score/{run_id}/methodologies")
def run_scores(run_id: int, session: Session = Depends(get_session)) -> dict:
    """All (run × methodology) scores for a run — its at-measure record plus any
    at-present re-grades — for the RTINGS-style "then vs now" view."""
    from ..methodology import serialize_score

    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No run {run_id}")
    rows = session.scalars(
        select(Score).where(Score.run_id == run_id).order_by(Score.is_at_measure.desc())
    ).all()
    return {
        "run_id": run_id,
        "at_measure_version": run.methodology_version,
        "scores": [serialize_score(r) for r in rows],
    }


def _percentile(s: list[float], p: float) -> float:
    """Linear-interpolated percentile (p in 0..1) over a sorted list."""
    if len(s) == 1:
        return s[0]
    import math

    k = (len(s) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def _spread(vals: list[float]) -> dict:
    s = sorted(vals)
    med = round(median(s), 2)
    if len(s) >= 2:
        p25, p75, p95 = (round(_percentile(s, q), 2) for q in (0.25, 0.75, 0.95))
    else:
        p25 = p75 = p95 = med
    return {
        "median": med, "p25": p25, "p75": p75, "p95": p95,
        "min": round(s[0], 2), "max": round(s[-1], 2),
    }


def _median_by_key(dicts: list[dict]) -> dict:
    keys: set[str] = set()
    for d in dicts:
        keys.update((d or {}).keys())
    out: dict[str, float] = {}
    for k in keys:
        vals = [d[k] for d in dicts if (d or {}).get(k) is not None]
        if vals:
            out[k] = round(median(vals), 2)
    return out


def _window_scores(session: Session, hours: int, fingerprint: str | None = None) -> tuple:
    """(methodology, Score rows) for completed runs in the window, scored under the
    current methodology and comparable (incomparable runs carry no axis scores).
    Optionally restricted to one settings profile (configTag)."""
    from ..methodology import ensure_current_methodology

    methodology = ensure_current_methodology(session, get_config(session))
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    conds = [
        Run.status == RunStatus.COMPLETE,
        Run.created_at >= cutoff,
        Score.methodology_version == methodology.version,
        Score.comparability != "incomparable",
    ]
    if fingerprint:
        conds.append(Run.settings_fingerprint == fingerprint)
    rows = (
        session.execute(
            select(Score).join(Run, Run.id == Score.run_id).where(*conds).order_by(Run.created_at)
        )
        .scalars()
        .all()
    )
    return methodology, rows


@router.get("/score/rolling")
def rolling_score(
    hours: int = Query(24, ge=1, le=720),
    fingerprint: str | None = Query(None, description="Restrict to one settings profile (configTag)."),
    session: Session = Depends(get_session),
) -> dict:
    """Windowed scores over completed runs in the last ``hours`` hours, under the
    current methodology — the stable "current responsiveness" figure.

    Per-axis (Speed/Smoothness/…) median + IQR band, plus the median per-metric
    subscore/value/weight over the window for the breakdown, and the network/render
    stall attribution. A median over many runs, so it doesn't swing on noise.
    """
    from ..methodology import scored_axes

    methodology, rows = _window_scores(session, hours, fingerprint)
    axes = scored_axes(methodology.definition or {})
    if not rows:
        return {
            "window_hours": hours,
            "count": 0,
            "methodology": methodology.version,
            "axes": axes,
            "axis_scores": {},
            "subscores": {},
            "metric_values": {},
            "weights": {},
            "attribution": None,
        }

    axis_scores: dict[str, dict] = {}
    for a in axes:
        vals = [r.axis_scores.get(a["key"]) for r in rows if (r.axis_scores or {}).get(a["key"]) is not None]
        if vals:
            axis_scores[a["key"]] = _spread(vals)

    # Stall attribution from the browser plugin's (display-only) metrics for these runs.
    run_ids = [r.run_id for r in rows]
    browser_metrics = (
        session.execute(
            select(BenchmarkResult.metrics).where(
                BenchmarkResult.run_id.in_(run_ids), BenchmarkResult.plugin == "browser"
            )
        )
        .scalars()
        .all()
    )

    def _med_metric(key: str) -> float | None:
        vals = [m[key] for m in browser_metrics if (m or {}).get(key) is not None]
        return median(vals) if vals else None

    return {
        "window_hours": hours,
        "count": len(rows),
        "methodology": methodology.version,
        "axes": axes,
        "axis_scores": axis_scores,
        "subscores": _median_by_key([r.subscores or {} for r in rows]),
        "metric_values": _median_by_key([r.metric_values or {} for r in rows]),
        "weights": _median_by_key([r.weights_used or {} for r in rows]),
        "attribution": _attribution(
            _med_metric("network_stall_ms"),
            _med_metric("render_stall_ms"),
            _med_metric("unknown_stall_ms"),
        ),
    }


@router.get("/score/axis-series")
def axis_series(
    limit: int = Query(100, ge=1, le=1000),
    fingerprint: str | None = Query(None, description="Restrict to one settings profile (configTag)."),
    session: Session = Depends(get_session),
) -> dict:
    """Per-run axis scores over time (current methodology), oldest→newest, for the
    dashboard's over-time chart. Only comparable runs appear."""
    from ..methodology import ensure_current_methodology, scored_axes

    methodology = ensure_current_methodology(session, get_config(session))
    axes = scored_axes(methodology.definition or {})
    # The first-class Overall (corner roll-up) isn't a scored axis, but it's the headline
    # figure — prepend it as a synthetic headline series so the over-time chart trends it
    # alongside the axes (pulled from the same persisted ``axis_scores['overall']``).
    series_axes = [{"key": "overall", "label": "Overall", "role": "headline"}, *axes]
    conds = [
        Run.status == RunStatus.COMPLETE,
        Score.methodology_version == methodology.version,
        Score.comparability != "incomparable",
    ]
    if fingerprint:
        conds.append(Run.settings_fingerprint == fingerprint)
    rows = session.execute(
        select(Score, Run).join(Run, Run.id == Score.run_id).where(*conds)
        .order_by(Run.created_at.desc())
        .limit(limit)
    ).all()
    points = [
        {
            "run_id": score.run_id,
            "timestamp": run.created_at.isoformat(),
            **{a["key"]: (score.axis_scores or {}).get(a["key"]) for a in series_axes},
        }
        for score, run in reversed(rows)
    ]
    return {"methodology": methodology.version, "axes": series_axes, "points": points}


@router.get("/score/weights")
def get_weights(session: Session = Depends(get_session)) -> dict:
    """Current SOPS weights and normalization thresholds."""
    config = get_config(session)
    return {"weights": config["weights"], "thresholds": config["thresholds"]}


@router.post("/score/preview", response_model=ScoreOut)
def preview_score(
    plugin_metrics: dict = Body(..., description="plugin -> metrics, e.g. {'dns': {'lookup_ms': 12}}"),
    session: Session = Depends(get_session),
) -> ScoreOut:
    """Compute a SOPS for ad-hoc metrics using the current weights/thresholds."""
    config = get_config(session)
    breakdown = compute_score(
        plugin_metrics, weights=config["weights"], thresholds=config["thresholds"]
    )
    return ScoreOut(
        sops=breakdown.sops,
        subscores=breakdown.subscores,
        weights_used=breakdown.weights_used,
        metric_values=breakdown.metric_values,
    )


@router.get("/score/{run_id}", response_model=ScoreOut)
def get_score(run_id: int, session: Session = Depends(get_session)) -> ScoreOut:
    run = session.get(Run, run_id)
    if run is None or run.score is None:
        raise HTTPException(status_code=404, detail=f"No score for run {run_id}")
    out = ScoreOut.model_validate(run.score)
    out.legacy = not has_latest_metrics(run.score.metric_values)
    return out
