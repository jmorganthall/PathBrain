"""Perceived load-smoothness endpoints.

Surfaces the smoothness instrument: the full per-load record for one run
(recomputed from stored raw observations, so it includes the categorical
attribution + protocol mix that don't fit the numeric metric cache), and a
two-config comparison that lays the smoothness metrics next to the speed-side
finish metrics (loadEventEnd, LCP) so the tradeoff between two settings is legible.

The active network configuration *is* the run's ``settings_fingerprint`` (the same
tag ``/api/settings/profiles`` groups on), so smoothness records are attributable
to a setting with no extra tagging machinery.
"""
from __future__ import annotations

import math

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_session
from ..interpret.smoothness import PERCEIVED_DEFAULTS, smoothness_record
from ..logging_config import get_logger
from ..models import BenchmarkResult, Run, RunStatus, ScoreResult
from ..settings_profile import summarize

router = APIRouter()
log = get_logger("api.smoothness")

# Smoothness metric keys (Resource-Timing-derived) plus the speed-side finish
# metrics they trade off against. Both live in the browser plugin's metric cache.
SMOOTHNESS_KEYS = [
    "longest_stall_ms",
    "cadence_cov",
    "byte_earliness_ms",
    "delivery_gini",
    "perceived_time_ms",
    "network_stall_ms",
    "render_stall_ms",
]
SPEED_KEYS = ["load_event_ms", "lcp_ms"]


def _pct(sorted_vals: list[float], p: float) -> float | None:
    """Linear-interpolated percentile (p in 0..1) over a sorted list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return round(sorted_vals[0], 3)
    k = (len(sorted_vals) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return round(sorted_vals[int(k)], 3)
    return round(sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f), 3)


def _distribution(values: list[float]) -> dict | None:
    if not values:
        return None
    s = sorted(values)
    return {
        "count": len(s),
        "p50": _pct(s, 0.50),
        "p75": _pct(s, 0.75),
        "p95": _pct(s, 0.95),
        "min": round(s[0], 3),
        "max": round(s[-1], 3),
    }


def _browser_result(session: Session, run_id: int) -> BenchmarkResult | None:
    return session.scalars(
        select(BenchmarkResult).where(
            BenchmarkResult.run_id == run_id, BenchmarkResult.plugin == "browser"
        )
    ).first()


def _records_from_raw(browser: BenchmarkResult, perceived_params: dict) -> list[dict]:
    """Full smoothness records, one per (iteration, URL), from stored raw.

    Raw is the source of truth, so attribution + protocol mix (which the numeric
    metric cache can't hold) are recomputed here — and recomputed under whatever
    perceived-time weights the caller passes, without re-running the benchmark."""
    out: list[dict] = []
    iterations = (browser.raw or {}).get("iterations") or []
    for i, it in enumerate(iterations):
        for url, u in ((it or {}).get("urls") or {}).items():
            if not isinstance(u, dict) or "nav" not in u:
                continue
            rec = smoothness_record(
                u.get("nav"), u.get("resources"), u.get("paint"), u.get("loaf"),
                perceived_params=perceived_params,
            )
            rec["iteration"] = i
            rec["url"] = url
            out.append(rec)
    return out


@router.get("/smoothness/run/{run_id}")
def smoothness_for_run(
    run_id: int,
    session: Session = Depends(get_session),
    w_unoccupied: float = Query(
        PERCEIVED_DEFAULTS["w_unoccupied"], description="Perceived-time weight for stall slices."
    ),
    w_occupied: float = Query(
        PERCEIVED_DEFAULTS["w_occupied"], description="Perceived-time weight for occupied slices."
    ),
) -> dict:
    """Full per-load smoothness records for a run, recomputed from raw.

    Each record carries the smoothness metrics, the speed-side finish metrics
    (loadEventEnd, LCP) they trade off against, the network-vs-render attribution
    of the longest stall, and the protocol mix — plus the perceived-time weights
    used (override via the query params to explore the tradeoff)."""
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    browser = _browser_result(session, run_id)
    if browser is None:
        raise HTTPException(status_code=404, detail="Run has no browser results")

    pp = {**PERCEIVED_DEFAULTS, "w_unoccupied": w_unoccupied, "w_occupied": w_occupied}
    records = _records_from_raw(browser, pp)
    return {
        "run_id": run_id,
        "config_tag": run.settings_fingerprint,
        "config_label": summarize(run.settings) if run.settings else None,
        "perceived_time_params": pp,
        "records": records,
        "count": len(records),
    }


def _group_metrics(rows: list[tuple[Run, BenchmarkResult]]) -> dict[str, list[float]]:
    """Collect each metric's per-run median value across a config's browser runs."""
    collected: dict[str, list[float]] = {k: [] for k in SMOOTHNESS_KEYS + SPEED_KEYS}
    for _run, br in rows:
        metrics = br.metrics or {}
        for k in collected:
            v = metrics.get(k)
            if isinstance(v, (int, float)):
                collected[k].append(float(v))
    return collected


def _attribution_mix(rows: list[tuple[Run, BenchmarkResult]]) -> dict[str, int]:
    """Aggregate longest-stall attribution tags across a config's loads (from raw)."""
    tally: dict[str, int] = {}
    for _run, br in rows:
        for rec in _records_from_raw(br, PERCEIVED_DEFAULTS):
            attr = rec.get("longest_stall_attribution")
            if attr:
                tally[attr] = tally.get(attr, 0) + 1
    return tally


def _config_summary(rows: list[tuple[Run, BenchmarkResult]], fingerprint: str) -> dict:
    metrics = _group_metrics(rows)
    sample = next((r for r, _ in rows), None)
    return {
        "config_tag": fingerprint,
        "label": summarize(sample.settings) if sample and sample.settings else None,
        "runs": len(rows),
        "smoothness": {k: _distribution(metrics[k]) for k in SMOOTHNESS_KEYS},
        "speed": {k: _distribution(metrics[k]) for k in SPEED_KEYS},
        "attribution": _attribution_mix(rows),
    }


@router.get("/smoothness/compare")
def compare_smoothness(
    a: str = Query(..., description="configTag (settings fingerprint) of profile A."),
    b: str = Query(..., description="configTag (settings fingerprint) of profile B."),
    session: Session = Depends(get_session),
    complete_only: bool = Query(
        True, description="Only include runs scored under the latest (paint) rubric."
    ),
) -> dict:
    """Compare two network configs on perceived load smoothness.

    Surfaces p50/p75/p95 of the smoothness metrics (longest stall, cadence, byte
    earliness, perceived time, …) plus the speed-side loadEventEnd/LCP for each
    configTag, so the smoothness-vs-speed tradeoff between two settings is legible."""
    from ..metrics import has_latest_metrics

    rows = session.execute(
        select(Run, BenchmarkResult, ScoreResult)
        .join(BenchmarkResult, BenchmarkResult.run_id == Run.id)
        .join(ScoreResult, ScoreResult.run_id == Run.id)
        .where(
            Run.status == RunStatus.COMPLETE,
            BenchmarkResult.plugin == "browser",
            Run.settings_fingerprint.in_([a, b]),
        )
        .order_by(Run.created_at)
    ).all()

    grouped: dict[str, list[tuple[Run, BenchmarkResult]]] = {a: [], b: []}
    for run, br, score in rows:
        if complete_only and not has_latest_metrics(score.metric_values):
            continue
        grouped[run.settings_fingerprint].append((run, br))

    return {
        "complete_only": complete_only,
        "a": _config_summary(grouped[a], a),
        "b": _config_summary(grouped[b], b),
    }
