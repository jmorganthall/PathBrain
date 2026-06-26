"""Shotgun Sweep endpoints: kick off a fast, broad shaper sweep and watch it.

The sweep applies each variant for real, benchmarks it, then restores the original
config (see ``pathbrain.sweep``). Results are returned ranked by SOPS with a
time-adjusted "vs typical" reading per variant.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import sweep as sweep_mod
from ..config_store import get_config
from ..database import get_session
from ..logging_config import get_logger
from ..models import Run, RunStatus, Sweep
from ..providers import get_provider
from ..runner import MAX_ITERATIONS
from ..trends import local_bucket, relative_reading
from .routes_trends import _load_points

router = APIRouter()
log = get_logger("api.sweep")


def _recent_per_iteration_ms(session: Session) -> float | None:
    rows = session.scalars(
        select(Run)
        .where(Run.status == RunStatus.COMPLETE, Run.per_iteration_ms.is_not(None))
        .order_by(Run.created_at.desc())
        .limit(5)
    ).all()
    vals = [r.per_iteration_ms for r in rows if r.per_iteration_ms]
    return round(sum(vals) / len(vals), 3) if vals else None


def _sweep_out(session: Session, sweep_id: int, tz_offset: int = 0) -> dict | None:
    sw = session.get(Sweep, sweep_id)
    if sw is None:
        return None
    cfg = get_config(session).get("trends", {}) or {}
    min_samples = int(cfg.get("min_samples", 3) or 3)
    days = int(cfg.get("lookback_days", 90) or 90)
    points = _load_points(session, days)

    enriched = []
    for r in sw.results or []:
        run = session.get(Run, r["run_id"]) if r.get("run_id") else None
        relative = None
        if run is not None and r.get("sops") is not None:
            wd, hr = local_bucket(run.created_at, tz_offset)
            relative = relative_reading(points, "sops", r["sops"], tz_offset, wd, hr, min_samples)
        enriched.append(
            {
                **r,
                "created_at": run.created_at.isoformat() if run else None,
                "relative": relative,
            }
        )
    # Rank by SOPS (highest first); unscored variants sink to the bottom.
    enriched.sort(key=lambda r: (r["sops"] is None, -(r["sops"] or 0.0)))

    return {
        "id": sw.id,
        "status": sw.status.value if hasattr(sw.status, "value") else str(sw.status),
        "dry_run": sw.dry_run,
        "iterations": sw.iterations,
        "dwell_s": sw.dwell_s,
        "pipe_uuid": sw.pipe_uuid,
        "total_variants": sw.total_variants,
        "completed_variants": sw.completed_variants,
        "baseline": sw.baseline,
        "error": sw.error,
        "created_at": sw.created_at.isoformat(),
        "started_at": sw.started_at.isoformat() if sw.started_at else None,
        "finished_at": sw.finished_at.isoformat() if sw.finished_at else None,
        "active": sweep_mod.active_sweep_id() == sw.id,
        "results": enriched,
    }


@router.get("/sweep/pipes")
def sweep_pipes() -> dict:
    """The shaper pipes available to sweep (download/upload/…), for the pipe picker.

    Only pipes with a stable uuid are returned — those are the ones we can target.
    """
    try:
        configs = get_provider().discover()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"Could not discover pipes: {type(exc).__name__}: {exc}"
        ) from exc
    pipes = []
    for c in configs:
        extra = c.extra or {}
        uuid = extra.get("uuid")
        if not uuid:
            continue
        label = extra.get("description") or extra.get("pipe") or extra.get("direction") or uuid
        pipes.append({"uuid": uuid, "label": label, "direction": extra.get("direction")})
    return {"pipes": pipes}


@router.post("/sweep/preview")
def sweep_preview(body: dict = Body(...), session: Session = Depends(get_session)) -> dict:
    """Variant count + ETA for a sweep spec, without starting it."""
    spec = body.get("spec") or {}
    iterations = max(1, min(int(body.get("iterations") or 2), MAX_ITERATIONS))
    dwell_s = max(0.0, float(body.get("dwell_minutes") or 0) * 60.0)
    variants = sweep_mod.generate_variants(spec)
    per = _recent_per_iteration_ms(session)
    est = sweep_mod.estimate(variants, iterations, dwell_s, per)
    return {
        "variants": variants,
        "total_variants": est["total_variants"],
        "eta_ms": est["eta_ms"],
        "per_iteration_ms": per,
        "cap": sweep_mod.MAX_VARIANTS,
    }


@router.post("/sweep")
def start_sweep(
    body: dict = Body(...),
    tz_offset: int = Query(0),
    session: Session = Depends(get_session),
) -> dict:
    """Start an immediate foreground sweep. 409 if one is already running."""
    spec = body.get("spec") or {}
    iterations = max(1, min(int(body.get("iterations") or 2), MAX_ITERATIONS))
    dwell_s = max(0.0, float(body.get("dwell_minutes") or 0) * 60.0)
    dry_run = bool(body.get("dry_run", False))
    pipe_uuid = body.get("pipe_uuid") or None
    try:
        sweep_id = sweep_mod.start(spec, iterations, dwell_s, dry_run, pipe_uuid)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — provider discovery etc.
        log.exception("Could not start sweep")
        raise HTTPException(
            status_code=502, detail=f"Could not start sweep: {type(exc).__name__}: {exc}"
        ) from exc
    return _sweep_out(session, sweep_id, tz_offset) or {}


@router.get("/sweep/current")
def current_sweep(tz_offset: int = Query(0), session: Session = Depends(get_session)) -> dict:
    """The active sweep, or the most recent one, enriched with ranked results."""
    sw = session.scalars(select(Sweep).order_by(Sweep.created_at.desc()).limit(1)).first()
    return {"sweep": _sweep_out(session, sw.id, tz_offset) if sw else None}


@router.get("/sweep/{sweep_id}")
def get_sweep(sweep_id: int, tz_offset: int = Query(0), session: Session = Depends(get_session)) -> dict:
    out = _sweep_out(session, sweep_id, tz_offset)
    if out is None:
        raise HTTPException(status_code=404, detail=f"Sweep {sweep_id} not found")
    return {"sweep": out}


@router.post("/sweep/{sweep_id}/cancel")
def cancel_sweep(sweep_id: int) -> dict:
    """Signal the sweep to stop after the current variant; it then restores baseline."""
    return {"cancelling": sweep_mod.cancel(sweep_id)}


@router.post("/sweep/{sweep_id}/apply-best")
def apply_best(sweep_id: int, session: Session = Depends(get_session)) -> dict:
    """Apply the highest-SOPS variant's quantum + target to the firewall."""
    sw = session.get(Sweep, sweep_id)
    if sw is None:
        raise HTTPException(status_code=404, detail=f"Sweep {sweep_id} not found")
    ranked = sorted(
        (r for r in (sw.results or []) if r.get("sops") is not None),
        key=lambda r: -(r["sops"] or 0.0),
    )
    if not ranked:
        raise HTTPException(status_code=400, detail="No scored variant to apply yet.")
    best = ranked[0]
    provider = get_provider()
    pipe = best.get("pipe_uuid") or sw.pipe_uuid or None
    applied: dict = {}
    try:
        for param in ("quantum", "target"):
            if best.get(param) is not None:
                provider.apply({"pipe_uuid": pipe, "param": param, "value": best[param]})
                applied[param] = best[param]
    except Exception as exc:  # noqa: BLE001
        log.exception("apply-best failed for sweep %s", sweep_id)
        raise HTTPException(
            status_code=502, detail=f"Apply failed: {type(exc).__name__}: {exc}"
        ) from exc
    return {
        "ok": True, "applied": applied, "pipe": best.get("pipe_label"),
        "run_id": best.get("run_id"), "sops": best.get("sops"),
    }
