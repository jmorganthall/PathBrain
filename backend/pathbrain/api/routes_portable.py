"""Portable (away) test endpoints — the **Away test** page.

* ``GET /portable/recipe`` — what the page should run (resources, stream, round-trip probe,
  iterations) + the instrument version the upload must carry.
* ``POST /portable/runs`` — upload one measured run: derived, scored, stamped (home runs get
  the live firewall profile) and stored in its own table; returns the run + its "vs home".
* ``GET /portable/runs`` / ``GET /portable/runs/{id}`` — the device's history / one run with
  its comparison; ``DELETE`` drops a botched run.
* ``GET /portable/devices`` / ``PUT /portable/devices/{id}`` — known devices + rename.

Nothing here touches ``runs``/``scores``: portable runs are a separate instrument.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import crown_follower, portable
from ..config_store import get_config
from ..database import get_session, session_scope
from ..logging_config import get_logger
from ..models import PortableRun
from ..schemas import PortableDeviceUpdate, PortableRunCreate

router = APIRouter()
log = get_logger("api.portable")


def _crown_fp(session: Session) -> str | None:
    try:
        crown = crown_follower.current_crown(session)
    except Exception:  # noqa: BLE001 — the crown is a preference for the reference, not a need
        return None
    return (crown or {}).get("fingerprint")


def _detail(session: Session, run: PortableRun, cfg: dict) -> dict:
    out = portable.serialize_run(run)
    out["compare"] = portable.compare(session, run, cfg, crown_fingerprint=_crown_fp(session))
    return out


@router.get("/portable/recipe")
def portable_recipe(session: Session = Depends(get_session)) -> dict:
    cfg = get_config(session)
    out = portable.recipe(cfg)
    out["metrics"] = portable.metric_catalog()
    out["rubric"] = portable.PORTABLE_RUBRIC
    return out


@router.post("/portable/runs", status_code=201)
def portable_upload(body: PortableRunCreate, session: Session = Depends(get_session)) -> dict:
    cfg = get_config(session)
    current = portable.recipe(cfg)["instrument_version"]
    try:
        run = portable.build_run(body.model_dump(), current)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not run.device_id:
        raise HTTPException(status_code=422, detail="device_id is required")
    # Persist in an own session: the request session is the read-only dependency.
    with session_scope() as s:
        s.add(run)
        s.flush()
        run_id = run.id
    log.info(
        "Portable run #%s stored: device=%s home=%s venue=%r score=%s",
        run_id, run.device_id, run.is_home, run.venue, run.score,
    )
    session.expire_all()
    stored = session.get(PortableRun, run_id)
    return _detail(session, stored, cfg)


@router.get("/portable/runs")
def portable_runs(
    device_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = select(PortableRun).order_by(PortableRun.id.desc()).limit(limit)
    if device_id:
        stmt = stmt.where(PortableRun.device_id == device_id)
    return [portable.serialize_run(r) for r in session.scalars(stmt).all()]


@router.get("/portable/runs/{run_id}")
def portable_run(run_id: int, session: Session = Depends(get_session)) -> dict:
    run = session.get(PortableRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="portable run not found")
    return _detail(session, run, get_config(session))


@router.delete("/portable/runs/{run_id}", status_code=204, response_class=Response)
def portable_delete(run_id: int) -> Response:
    with session_scope() as s:
        run = s.get(PortableRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="portable run not found")
        s.delete(run)
    log.info("Portable run #%s deleted", run_id)
    return Response(status_code=204)


@router.get("/portable/devices")
def portable_devices(session: Session = Depends(get_session)) -> list[dict]:
    return portable.devices(session)


@router.put("/portable/devices/{device_id}")
def portable_device_rename(device_id: str, body: PortableDeviceUpdate) -> dict:
    label = (body.label or "").strip()[:120] or None
    with session_scope() as s:
        rows = s.scalars(select(PortableRun).where(PortableRun.device_id == device_id)).all()
        if not rows:
            raise HTTPException(status_code=404, detail="unknown device")
        for r in rows:
            r.device_label = label
    return {"device_id": device_id, "label": label}
