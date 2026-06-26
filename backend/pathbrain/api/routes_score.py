"""Score endpoints: fetch a run's score, preview scoring, inspect weights."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median, quantiles

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config_store import get_config
from ..database import get_session
from ..metrics import has_latest_metrics
from ..config import get_settings
from ..interpret import DERIVATION_VERSION
from ..models import BenchmarkResult, Run, RunStatus, ScoreResult
from ..runner import rederive_run, rescore_run
from ..schemas import ScoreOut
from ..scoring import compute_score

router = APIRouter()


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


@router.post("/score/rescore")
def rescore_history(session: Session = Depends(get_session)) -> dict:
    """Re-grade every completed run with the current scoring rubric.

    Run this after changing thresholds/weights so historical scores stay
    comparable (no discontinuity in the SOPS timeline at the change).
    """
    cfg = get_config(session)
    weights = cfg.get("weights", {})
    thresholds = cfg.get("thresholds", {})
    rubric_version = cfg.get("rubric_version")
    c_weights = cfg.get("completion_weights", {})
    c_thresholds = cfg.get("completion_thresholds", {})
    runs = session.scalars(select(Run).where(Run.status == RunStatus.COMPLETE)).all()
    rescored = sum(
        1
        for run in runs
        if rescore_run(run, weights, thresholds, rubric_version, c_weights, c_thresholds)
    )
    session.commit()
    return {"rescored": rescored, "rubric_version": rubric_version}


@router.post("/score/rederive")
def rederive_history(session: Session = Depends(get_session)) -> dict:
    """Re-derive *and* re-grade every completed run from its stored raw observations.

    Heavier than ``/score/rescore`` (which only re-applies the rubric to cached
    metric scalars): this re-runs the full interpretation, so a new metric or a
    changed derivation formula (e.g. a better Speed Index) lands on history without
    re-collecting. Runs whose raw lacks a signal just don't gain that metric.
    """
    import os

    cfg = get_config(session)
    weights = cfg.get("weights", {})
    thresholds = cfg.get("thresholds", {})
    rubric_version = cfg.get("rubric_version")
    c_weights = cfg.get("completion_weights", {})
    c_thresholds = cfg.get("completion_thresholds", {})
    artifact_base = os.path.abspath(get_settings().artifact_dir)
    runs = session.scalars(select(Run).where(Run.status == RunStatus.COMPLETE)).all()
    rederived = sum(
        1
        for run in runs
        if rederive_run(
            run, weights, thresholds, rubric_version, c_weights, c_thresholds, artifact_base
        )
    )
    session.commit()
    return {"rederived": rederived, "derivation_version": DERIVATION_VERSION}


@router.get("/score/rolling")
def rolling_score(
    hours: int = Query(24, ge=1, le=720),
    session: Session = Depends(get_session),
) -> dict:
    """Windowed SOPS over completed runs in the last ``hours`` hours.

    This is the stable "current responsiveness" figure: a median over many runs,
    with an interquartile band, so it doesn't swing on point-in-time noise. Also
    returns the median per-metric subscore + metric value over the window (and the
    most recent weights) so the dashboard can show an aggregated breakdown.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    rows = (
        session.execute(
            select(ScoreResult)
            .join(Run, Run.id == ScoreResult.run_id)
            .where(Run.status == RunStatus.COMPLETE, Run.created_at >= cutoff)
            .order_by(Run.created_at)
        )
        .scalars()
        .all()
    )
    # The headline must be trustworthy: drop legacy scores (pre-current-rubric).
    rows = [r for r in rows if has_latest_metrics(r.metric_values)]
    if not rows:
        return {
            "window_hours": hours,
            "count": 0,
            "median": None,
            "p25": None,
            "p75": None,
            "min": None,
            "max": None,
            "subscores": {},
            "metric_values": {},
            "weights": {},
            "attribution": None,
        }

    def median_by_key(dicts: list[dict]) -> dict:
        keys: set[str] = set()
        for d in dicts:
            keys.update((d or {}).keys())
        out: dict[str, float] = {}
        for k in keys:
            vals = [d[k] for d in dicts if (d or {}).get(k) is not None]
            if vals:
                out[k] = round(median(vals), 2)
        return out

    vals = sorted(r.sops for r in rows)
    med = round(median(vals), 2)
    if len(vals) >= 2:
        q = quantiles(vals, n=4)  # [p25, p50, p75]
        p25, p75 = round(q[0], 2), round(q[2], 2)
    else:
        p25 = p75 = med

    # Stall attribution: the network/render split lives on the browser plugin's
    # (display-only) metrics, not in the score row, so pull it for the same runs.
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

    attribution = _attribution(
        _med_metric("network_stall_ms"),
        _med_metric("render_stall_ms"),
        _med_metric("unknown_stall_ms"),
    )

    return {
        "window_hours": hours,
        "count": len(vals),
        "median": med,
        "p25": p25,
        "p75": p75,
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
        "subscores": median_by_key([r.subscores or {} for r in rows]),
        "metric_values": median_by_key([r.metric_values or {} for r in rows]),
        "weights": rows[-1].weights_used or {},
        "attribution": attribution,
    }


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
