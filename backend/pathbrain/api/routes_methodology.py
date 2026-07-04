"""Methodology endpoints — the versioned interpretation layer.

Read access to the published methodologies: how raw becomes a score at each point
in time. "Here's the methodology used when this was collected." Snapshots are
created by the scoring path / startup; these endpoints are read-only (plus a lazy
ensure so the current methodology always appears, even on a fresh database).
"""
from __future__ import annotations

import copy

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs
from ..config_store import get_config, save_config
from ..database import get_session, session_scope
from ..logging_config import get_logger
from ..methodology import ensure_current_methodology, serialize, summarize
from ..models import Methodology
from ..runner import score_history_under_current

router = APIRouter()
log = get_logger(__name__)


def _fmt_num(v: float) -> str:
    """Compact number for a version id: integers without a trailing '.0'."""
    return str(int(v)) if float(v).is_integer() else str(v)


@router.get("/methodologies")
def list_methodologies(session: Session = Depends(get_session)) -> dict:
    """All published methodologies (newest current first), compact view.

    Lazily records the current methodology so a fresh instance still shows the
    interpretation in play before any re-grade has happened.
    """
    ensure_current_methodology(session, get_config(session))
    rows = session.scalars(
        select(Methodology).order_by(Methodology.is_current.desc(), Methodology.created_at.desc())
    ).all()
    return {"methodologies": [summarize(r) for r in rows], "count": len(rows)}


@router.get("/methodologies/current")
def current_methodology(session: Session = Depends(get_session)) -> dict:
    """The published-now methodology, with its full frozen definition."""
    row = ensure_current_methodology(session, get_config(session))
    return serialize(row)


@router.get("/methodologies/{version}")
def get_methodology(version: str, session: Session = Depends(get_session)) -> dict:
    """One methodology's full definition (axes + every metric's weight/thresholds)."""
    ensure_current_methodology(session, get_config(session))
    row = session.get(Methodology, version)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No methodology '{version}'")
    return serialize(row)


@router.post("/methodologies/reanchor", status_code=202)
def reanchor_threshold(
    body: dict = Body(...), session: Session = Depends(get_session)
) -> dict:
    """Publish a new methodology version that re-anchors one scored metric's ``best``
    threshold, then re-grade history onto it — the "apply" behind the saturation alert.

    This keeps the append-only invariant: nothing is edited in place. We fork the *current*
    methodology's frozen definition (so axes, the Overall corner spec, and every other
    metric carry over unchanged), override just this metric's ``best``, write it as a **new**
    version, point the runtime config at it, and kick the standard re-grade so the crown
    reflects the tightened threshold. Re-issuing the same change is idempotent (same version
    id). Body: ``{"metric_key": str, "best": number}``. Returns ``{version, job_id}`` (202)."""
    metric_key = (body or {}).get("metric_key")
    raw_best = (body or {}).get("best")
    # Whether to kick the (heavy) re-grade now. Defaults on for back-compat / single edits;
    # the UI sends ``false`` when several metrics are saturated so the user can re-anchor them
    # all first (each forks the then-current version, so the overrides accumulate) and re-grade
    # ONCE at the end via the Methodology "Re-grade history under current" button.
    regrade = bool((body or {}).get("regrade", True))
    if not metric_key or raw_best is None:
        raise HTTPException(status_code=400, detail="metric_key and best are required")
    try:
        new_best = float(raw_best)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="best must be a number")

    config = get_config(session)
    base = ensure_current_methodology(session, config)
    base_def = base.definition or {}
    target = next(
        (m for m in base_def.get("metrics", []) if m.get("key") == metric_key), None
    )
    if target is None or target.get("axis") is None:
        raise HTTPException(
            status_code=400,
            detail=f"'{metric_key}' is not a scored metric in {base.version}",
        )
    old_best, worst = target.get("best"), target.get("worst")
    if old_best is None:
        raise HTTPException(status_code=400, detail=f"'{metric_key}' has no 'best' threshold")
    # Keep 'best' on the good side of 'worst' so the curve doesn't invert.
    higher = bool(target.get("higher_is_better"))
    if worst is not None and ((higher and new_best <= worst) or (not higher and new_best >= worst)):
        raise HTTPException(
            status_code=400,
            detail=f"best ({new_best}) must be on the better side of worst ({worst})",
        )

    new_def = copy.deepcopy(base_def)
    for m in new_def.get("metrics", []):
        if m.get("key") == metric_key:
            m["best"] = new_best
    new_version = f"{base.version}+{metric_key}-best{_fmt_num(new_best)}"

    row = session.get(Methodology, new_version)
    notes = (
        f"Re-anchored {metric_key} best {_fmt_num(float(old_best))} → {_fmt_num(new_best)} "
        f"(forked from {base.version}) to de-saturate the metric so it can rank profiles."
    )
    if row is None:
        row = Methodology(
            version=new_version,
            rubric_version=new_version,
            derivation_version=base.derivation_version,
            notes=notes,
            definition=new_def,
            is_current=True,
        )
        session.add(row)
    else:  # re-issue: refresh the definition in case the suggested value changed
        row.definition = new_def
        row.notes = notes
        row.is_current = True
    for other in session.scalars(select(Methodology).where(Methodology.version != new_version)):
        other.is_current = False
    # Point the runtime config at the forked version so it becomes "current" everywhere
    # (partial save — merged over stored config, not the full effective dict).
    save_config(session, {"methodology_version": new_version})
    session.commit()
    log.info("Published re-anchored methodology %s (%s)", new_version, notes)

    # Publish-only when the caller defers the re-grade (batching several re-anchors): the new
    # version is current, but history isn't re-scored until they run the re-grade explicitly.
    if not regrade:
        return {"version": new_version, "job_id": None, "regrade_deferred": True}

    # Re-grade history under the new version (background job; surfaces in the jobs feed).
    def task(job: jobs.Job) -> dict:
        with session_scope() as s:
            return score_history_under_current(s, progress=job.set_progress)

    job_id = jobs.start(
        "regrade", f"Re-grade under {new_version}", task, href="/methodology"
    )
    return {"version": new_version, "job_id": job_id}
