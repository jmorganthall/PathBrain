"""Settings-vs-responsiveness correlation endpoints.

Groups completed runs by the firewall/SQM profile that was live when they ran,
and flags the most recent settings change when it moved the median SOPS beyond a
configurable threshold.
"""
from __future__ import annotations

from statistics import median, quantiles

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config_store import get_config
from ..database import get_session
from ..logging_config import get_logger
from ..models import Run, RunStatus, ScoreResult
from ..providers import get_provider
from ..scoring import COMPLETION_METRIC_SOURCES
from ..settings_profile import diff_profiles, fingerprint, normalize, summarize

router = APIRouter()
log = get_logger("api.settings")

# A run is "latest-rubric complete" once it carries the browser paint metrics that
# the perception-led SOPS leans on (FCP/LCP — the reliably-captured ones; INP is
# best-effort and not required). Older runs predate paint capture, so their SOPS
# comes from a thinner metric set and reads artificially high — not comparable.
# Default the analysis to these runs only so old data doesn't skew the picture.
LATEST_METRIC_KEYS = ("fcp", "lcp")


def _has_latest_metrics(score: ScoreResult) -> bool:
    mv = score.metric_values or {}
    return all(mv.get(k) is not None for k in LATEST_METRIC_KEYS)


def _min_runs(session: Session) -> int:
    return int((get_config(session).get("correlation", {}) or {}).get("min_runs", 5) or 5)


def _spread(vals: list[float]) -> dict:
    vals = sorted(vals)
    med = round(median(vals), 2)
    if len(vals) >= 2:
        q = quantiles(vals, n=4)
        p25, p75 = round(q[0], 2), round(q[2], 2)
    else:
        p25 = p75 = med
    return {
        "median": med,
        "p25": p25,
        "p75": p75,
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
    }


def _completed_runs_with_scores(session: Session):
    """Chronological (Run, ScoreResult) for completed runs that captured settings."""
    return session.execute(
        select(Run, ScoreResult)
        .join(ScoreResult, ScoreResult.run_id == Run.id)
        .where(Run.status == RunStatus.COMPLETE, Run.settings_fingerprint.is_not(None))
        .order_by(Run.created_at)
    ).all()


@router.get("/settings/diagnostics")
def settings_diagnostics(session: Session = Depends(get_session)) -> dict:
    """Visibility into settings capture: how many runs are stamped, how many
    distinct fingerprints, and the most recent runs with their fingerprints.

    Lets us tell apart "old runs never captured" (lots of nulls) from "fingerprint
    changes every run" (lots of distinct fingerprints).
    """
    completed = session.scalars(
        select(Run).where(Run.status == RunStatus.COMPLETE).order_by(Run.created_at.desc())
    ).all()
    stamped = [r for r in completed if r.settings_fingerprint]
    distinct = {r.settings_fingerprint for r in stamped}
    # How many completed runs were scored under the latest (paint) rubric.
    with_latest = sum(
        1
        for score in session.scalars(
            select(ScoreResult).join(Run, Run.id == ScoreResult.run_id).where(
                Run.status == RunStatus.COMPLETE
            )
        )
        if _has_latest_metrics(score)
    )
    recent = [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat(),
            "label": r.label,
            "fingerprint": r.settings_fingerprint,
        }
        for r in completed[:15]
    ]
    return {
        "total_completed": len(completed),
        "stamped": len(stamped),
        "unstamped": len(completed) - len(stamped),
        "distinct_profiles": len(distinct),
        "with_latest_metrics": with_latest,
        "legacy_metrics": len(completed) - with_latest,
        "recent": recent,
    }


@router.post("/settings/backfill")
def backfill_settings(session: Session = Depends(get_session)) -> dict:
    """Stamp the *current* firewall settings onto completed runs that have none.

    Use when historical runs predate settings-capture (or ran while discovery was
    failing) AND the firewall hasn't changed since — they then aggregate into the
    current profile. Only touches runs with no captured settings.
    """
    provider = get_provider()
    try:
        normalized = normalize(provider.discover())
        fp = fingerprint(normalized)
    except Exception as exc:  # noqa: BLE001
        log.exception("Backfill discovery failed")
        raise HTTPException(
            status_code=502, detail=f"{provider.name} discovery failed: {type(exc).__name__}: {exc}"
        ) from exc

    runs = session.scalars(
        select(Run).where(Run.status == RunStatus.COMPLETE, Run.settings_fingerprint.is_(None))
    ).all()
    for run in runs:
        run.settings = normalized
        run.settings_fingerprint = fp
    session.commit()
    return {"updated": len(runs), "fingerprint": fp}


@router.get("/settings/profiles")
def settings_profiles(
    session: Session = Depends(get_session),
    complete_only: bool = Query(
        True, description="Only aggregate runs with the latest (paint) SOPS metrics."
    ),
) -> dict:
    """One row per distinct settings profile, with its SOPS distribution.

    By default only runs scored under the latest (paint-capturing) rubric are
    counted, so legacy runs with a thinner metric set don't inflate/skew a
    profile's SOPS. Set ``complete_only=false`` to include everything. Profiles
    with no qualifying runs drop out entirely.

    Also returns ``best_diff``: how the best (top confident) profile differs from
    the next-ranked one — the at-a-glance "what changed and did it help" view.
    """
    min_runs = _min_runs(session)
    rows = _completed_runs_with_scores(session)
    groups: dict[str, dict] = {}
    for run, score in rows:
        if complete_only and not _has_latest_metrics(score):
            continue
        g = groups.setdefault(
            run.settings_fingerprint,
            {
                "fingerprint": run.settings_fingerprint,
                "settings": run.settings,
                "sops": [],
                "iterations": 0,
                "completion": [],
                "completion_metrics": {m: [] for m in COMPLETION_METRIC_SOURCES},
                "first_seen": run.created_at,
                "last_seen": run.created_at,
            },
        )
        g["sops"].append(score.sops)
        # A run with more iterations is more data; track the total alongside runs.
        g["iterations"] += int(run.iterations or 1)
        if score.completion is not None:
            g["completion"].append(score.completion)
        cv = score.completion_metric_values or {}
        for m in COMPLETION_METRIC_SOURCES:
            if cv.get(m) is not None:
                g["completion_metrics"][m].append(float(cv[m]))
        g["settings"] = run.settings
        g["last_seen"] = run.created_at

    profiles = []
    for g in groups.values():
        count = len(g["sops"])
        comp = g["completion"]
        profiles.append(
            {
                "fingerprint": g["fingerprint"],
                "label": summarize(g["settings"]),
                "settings": g["settings"],
                "count": count,
                "iterations": g["iterations"],
                "confident": count >= min_runs,
                "first_seen": g["first_seen"].isoformat(),
                "last_seen": g["last_seen"].isoformat(),
                # Primary ranking is SOPS (human-feel).
                **_spread(g["sops"]),
                # Completion axis, gated like SOPS: only confident with enough runs
                # that actually captured its metrics.
                "completion": (
                    {
                        "count": len(comp),
                        "confident": len(comp) >= min_runs,
                        **_spread(comp),
                    }
                    if comp
                    else None
                ),
                "completion_metrics": {
                    m: {"median": round(median(v), 1), "count": len(v)}
                    for m, v in g["completion_metrics"].items()
                    if v
                },
            }
        )
    profiles.sort(key=lambda p: p["median"], reverse=True)

    return {
        "profiles": profiles,
        "count": len(profiles),
        "min_runs": min_runs,
        "complete_only": complete_only,
        "best_diff": _best_diff(profiles),
    }


def _best_diff(profiles: list[dict]) -> dict | None:
    """Diff the best (top confident) profile against the next-ranked profile.

    Returns ``None`` until there are two profiles to compare. ``changes`` describe
    what the *best* profile did relative to the comparison one (e.g. CoDel target
    10ms → 5ms, direction "lower"), with the resulting SOPS delta.
    """
    best_idx = next((i for i, p in enumerate(profiles) if p["confident"]), None)
    if best_idx is None or best_idx + 1 >= len(profiles):
        return None
    best = profiles[best_idx]
    comparison = profiles[best_idx + 1]
    delta_abs = round(best["median"] - comparison["median"], 2)
    delta_pct = (
        round((delta_abs / comparison["median"]) * 100, 1) if comparison["median"] else None
    )

    def _comp_median(p: dict) -> float | None:
        c = p.get("completion")
        return c["median"] if c else None

    best_comp, comp_comp = _comp_median(best), _comp_median(comparison)
    completion_delta = (
        round(best_comp - comp_comp, 2)
        if best_comp is not None and comp_comp is not None
        else None
    )
    return {
        "best": {
            "fingerprint": best["fingerprint"],
            "label": best["label"],
            "median": best["median"],
            "completion": best_comp,
            "confident": best["confident"],
        },
        "comparison": {
            "fingerprint": comparison["fingerprint"],
            "label": comparison["label"],
            "median": comparison["median"],
            "completion": comp_comp,
            "confident": comparison["confident"],
        },
        "delta_abs": delta_abs,
        "delta_pct": delta_pct,
        # Completion can move opposite to SOPS — surfacing it here is the whole
        # point (feels-fast vs. raw-completion pulling apart).
        "completion_delta": completion_delta,
        "changes": diff_profiles(comparison["settings"], best["settings"]),
    }


@router.get("/settings/impact")
def settings_impact(
    session: Session = Depends(get_session),
    complete_only: bool = Query(
        True, description="Only consider runs with the latest (paint) SOPS metrics."
    ),
) -> dict:
    """Compare the current settings profile to the one before the last change.

    Like ``/settings/profiles``, defaults to runs scored under the latest rubric so
    legacy data doesn't skew the before/after medians.
    """
    cfg = get_config(session).get("correlation", {}) or {}
    threshold = float(cfg.get("significant_change_pct", 5) or 5)
    min_runs = int(cfg.get("min_runs", 5) or 5)
    rows = _completed_runs_with_scores(session)

    # Build contiguous segments of runs sharing a fingerprint (chronological).
    segments: list[dict] = []
    for run, score in rows:
        if complete_only and not _has_latest_metrics(score):
            continue
        fp = run.settings_fingerprint
        if not segments or segments[-1]["fingerprint"] != fp:
            segments.append(
                {"fingerprint": fp, "settings": run.settings, "sops": [], "changed_at": run.created_at}
            )
        segments[-1]["sops"].append(score.sops)
        segments[-1]["settings"] = run.settings

    base = {"changed": False, "threshold_pct": threshold, "min_runs": min_runs}
    if len(segments) < 2:
        return base

    prev, cur = segments[-2], segments[-1]
    before = round(median(prev["sops"]), 2)
    after = round(median(cur["sops"]), 2)
    delta_abs = round(after - before, 2)
    delta_pct = round((delta_abs / before) * 100, 1) if before else None
    # Don't make significance calls until both profiles have enough runs.
    enough_data = len(prev["sops"]) >= min_runs and len(cur["sops"]) >= min_runs
    significant = enough_data and delta_pct is not None and abs(delta_pct) >= threshold
    return {
        "changed": True,
        "changed_at": cur["changed_at"].isoformat(),
        "threshold_pct": threshold,
        "min_runs": min_runs,
        "enough_data": enough_data,
        "delta_abs": delta_abs,
        "delta_pct": delta_pct,
        "significant": significant,
        "before": {
            "label": summarize(prev["settings"]),
            "fingerprint": prev["fingerprint"],
            "median": before,
            "count": len(prev["sops"]),
        },
        "after": {
            "label": summarize(cur["settings"]),
            "fingerprint": cur["fingerprint"],
            "median": after,
            "count": len(cur["sops"]),
        },
    }
