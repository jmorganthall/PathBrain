"""Settings-vs-responsiveness correlation endpoints.

Groups completed runs by the firewall/SQM profile that was live when they ran,
and flags the most recent settings change when it moved the median SOPS beyond a
configurable threshold.
"""
from __future__ import annotations

from statistics import median, quantiles

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config_store import get_config
from ..database import get_session
from ..logging_config import get_logger
from ..models import Run, RunStatus, ScoreResult
from ..providers import get_provider
from ..settings_profile import fingerprint, normalize, summarize

router = APIRouter()
log = get_logger("api.settings")


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
    """Chronological (Run, sops) for completed runs that captured settings."""
    return session.execute(
        select(Run, ScoreResult.sops)
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
def settings_profiles(session: Session = Depends(get_session)) -> dict:
    """One row per distinct settings profile, with its SOPS distribution."""
    min_runs = _min_runs(session)
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
        count = len(g["sops"])
        profiles.append(
            {
                "fingerprint": g["fingerprint"],
                "label": summarize(g["settings"]),
                "settings": g["settings"],
                "count": count,
                "confident": count >= min_runs,
                "first_seen": g["first_seen"].isoformat(),
                "last_seen": g["last_seen"].isoformat(),
                **_spread(g["sops"]),
            }
        )
    profiles.sort(key=lambda p: p["median"], reverse=True)
    return {"profiles": profiles, "count": len(profiles), "min_runs": min_runs}


@router.get("/settings/impact")
def settings_impact(session: Session = Depends(get_session)) -> dict:
    """Compare the current settings profile to the one before the last change."""
    cfg = get_config(session).get("correlation", {}) or {}
    threshold = float(cfg.get("significant_change_pct", 5) or 5)
    min_runs = int(cfg.get("min_runs", 5) or 5)
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
