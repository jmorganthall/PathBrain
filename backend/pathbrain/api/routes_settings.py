"""Settings-vs-responsiveness correlation endpoints.

Groups completed runs by the firewall/SQM profile that was live when they ran,
and flags the most recent settings change when it moved the median SOPS beyond a
configurable threshold.
"""
from __future__ import annotations

from statistics import median, quantiles

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config_store import get_config
from ..database import get_session
from ..models import Run, RunStatus, ScoreResult
from ..settings_profile import summarize

router = APIRouter()


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
    """Chronological (Run, sops) for completed runs that captured settings."""
    return session.execute(
        select(Run, ScoreResult.sops)
        .join(ScoreResult, ScoreResult.run_id == Run.id)
        .where(Run.status == RunStatus.COMPLETE, Run.settings_fingerprint.is_not(None))
        .order_by(Run.created_at)
    ).all()


@router.get("/settings/profiles")
def settings_profiles(session: Session = Depends(get_session)) -> dict:
    """One row per distinct settings profile, with its SOPS distribution."""
    rows = _completed_runs_with_scores(session)
    groups: dict[str, dict] = {}
    for run, sops in rows:
        g = groups.setdefault(
            run.settings_fingerprint,
            {
                "fingerprint": run.settings_fingerprint,
                "settings": run.settings,
                "sops": [],
                "first_seen": run.created_at,
                "last_seen": run.created_at,
            },
        )
        g["sops"].append(sops)
        g["settings"] = run.settings
        g["last_seen"] = run.created_at

    profiles = []
    for g in groups.values():
        profiles.append(
            {
                "fingerprint": g["fingerprint"],
                "label": summarize(g["settings"]),
                "settings": g["settings"],
                "count": len(g["sops"]),
                "first_seen": g["first_seen"].isoformat(),
                "last_seen": g["last_seen"].isoformat(),
                **_spread(g["sops"]),
            }
        )
    profiles.sort(key=lambda p: p["median"], reverse=True)
    return {"profiles": profiles, "count": len(profiles)}


@router.get("/settings/impact")
def settings_impact(session: Session = Depends(get_session)) -> dict:
    """Compare the current settings profile to the one before the last change."""
    threshold = float(
        (get_config(session).get("correlation", {}) or {}).get("significant_change_pct", 5) or 5
    )
    rows = _completed_runs_with_scores(session)

    # Build contiguous segments of runs sharing a fingerprint (chronological).
    segments: list[dict] = []
    for run, sops in rows:
        fp = run.settings_fingerprint
        if not segments or segments[-1]["fingerprint"] != fp:
            segments.append(
                {"fingerprint": fp, "settings": run.settings, "sops": [], "changed_at": run.created_at}
            )
        segments[-1]["sops"].append(sops)
        segments[-1]["settings"] = run.settings

    base = {"changed": False, "threshold_pct": threshold}
    if len(segments) < 2:
        return base

    prev, cur = segments[-2], segments[-1]
    before = round(median(prev["sops"]), 2)
    after = round(median(cur["sops"]), 2)
    delta_abs = round(after - before, 2)
    delta_pct = round((delta_abs / before) * 100, 1) if before else None
    significant = delta_pct is not None and abs(delta_pct) >= threshold
    return {
        "changed": True,
        "changed_at": cur["changed_at"].isoformat(),
        "threshold_pct": threshold,
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
