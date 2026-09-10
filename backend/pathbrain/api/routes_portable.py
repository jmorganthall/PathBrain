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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
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


def _request_ip(request: Request) -> str | None:
    """The address the request arrived from — the first hop of ``X-Forwarded-For`` when a
    reverse proxy (the user's own) sits in front, else the socket peer."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip() or None
    return request.client.host if request.client else None


@router.get("/portable/home")
def portable_home(
    request: Request,
    egress_ip: str | None = Query(default=None),
    egress_ip_v6: str | None = Query(default=None),
    device_id: str | None = Query(default=None, description="Suggest this device's own venue label first."),
    session: Session = Depends(get_session),
) -> dict:
    """What "home" looks like from the internet, for detection: the home WAN address per
    family (stated in config or looked up by the server), the lookup URLs the page should ask
    for its own egress, and — as a fallback where those lookups are blocked — the address
    this request came from, flagged public or not (a private/CGNAT source is a LAN or tunnel
    address and says nothing about where the device's internet traffic leaves). Pass the
    device's ``egress_ip`` / ``egress_ip_v6`` to get the verdict (``detected`` /
    ``detected_by`` / ``reason``) from the one ``decide_home`` the upload will use.

    It also returns ``venue`` — the label this network was given the last time anyone tested
    from it (``portable.recall_venue``), so a place you have been before is recognised rather
    than asked about from scratch. A suggestion only: the page pre-fills it and the user can
    type over it."""
    cfg = get_config(session)
    pc = portable.portable_config(cfg)
    home = portable.home_addresses(cfg)
    rip = _request_ip(request)
    out = {
        "home_ip": home["v4"],
        "home_ip_v6": home["v6"],
        "source": home["source"],
        "checked_at": home["checked_at"],
        "errors": home["errors"],
        "lookup_url": pc.get("ip_lookup_url") or None,
        "lookup_url_v6": pc.get("ip_lookup_url_v6") or None,
        "v6_prefix": int(pc.get("home_ipv6_prefix") or 64),
        "request_ip": rip,
        "request_ip_public": portable.is_public_ip(rip),
        "detected": None,
        "detected_by": None,
        "reason": None,
        "venue": None,
        "network": None,
    }
    if egress_ip or egress_ip_v6:
        egress = portable.split_families(egress_ip, egress_ip_v6)
        # Who owns this network — shown beside the verdict so a person sees "away, on
        # Comcast in Denver" before running. Cached per address; best-effort by rule.
        try:
            out["network"] = portable.network_for_egress(egress, cfg)
        except Exception:  # noqa: BLE001
            log.debug("Portable: network lookup failed", exc_info=True)
        try:
            is_home, by = portable.decide_home(None, egress, home, v6_prefix=out["v6_prefix"])
        except ValueError as exc:
            out["reason"] = str(exc)
        else:
            fam = "IPv4" if by == "ip4" else "IPv6"
            e, h = egress["v4" if by == "ip4" else "v6"], home["v4" if by == "ip4" else "v6"]
            out.update({
                "detected": is_home,
                "detected_by": by,
                "reason": (
                    f"this device leaves the internet through the same {fam} {'address' if by == 'ip4' else 'network'} as PathBrain ({e})"
                    if is_home
                    else f"this device's public {fam} ({e}) is not home's ({h})"
                ),
            })
        # What this network was called last time. Best-effort by rule: a missing suggestion
        # is an empty field, never a failed page.
        try:
            out["venue"] = portable.recall_venue(
                session, egress, v6_prefix=out["v6_prefix"], device_id=device_id
            )
        except Exception:  # noqa: BLE001
            log.debug("Portable: venue recall failed", exc_info=True)
    return out


@router.post("/portable/runs", status_code=201)
def portable_upload(body: PortableRunCreate, session: Session = Depends(get_session)) -> dict:
    cfg = get_config(session)
    current = portable.recipe(cfg)["instrument_version"]
    home = portable.home_addresses(cfg) if body.is_home is None else None
    v6_prefix = int(portable.portable_config(cfg).get("home_ipv6_prefix") or 64)
    network = None
    try:
        network = portable.network_for_egress(portable.split_families(body.egress_ip, body.egress_ip_v6), cfg)
    except Exception:  # noqa: BLE001 — a stamp on the run, never a condition of it
        log.debug("Portable: network lookup at upload failed", exc_info=True)
    try:
        run = portable.build_run(body.model_dump(), current, home=home, v6_prefix=v6_prefix, network=network)
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
        "Portable run #%s stored: device=%s home=%s (%s) venue=%r network=%r score=%s",
        run_id, run.device_id, run.is_home, run.home_detection, run.venue, portable.describe_network(run.network), run.score,
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


@router.get("/portable/standings")
def portable_standings(device_id: str | None = Query(None), session: Session = Depends(get_session)) -> dict:
    """What every device measured at home under each firewall profile, ranked, and whether
    that ranking agrees with the pooled crown — the mission's own check on the crown, read
    off the home runs the Away test has been stamping with the live profile all along.
    Read-only; the portable score never touches the crown."""
    return portable.profile_standings(session, get_config(session), device_id=device_id)


@router.get("/portable/locations")
def portable_locations(session: Session = Depends(get_session)) -> dict:
    """Every measured location as one dot beside home on the current home profile — the
    Settings-Impact quadrant asked of places instead of profiles. Home is the profile the
    firewall is on right now (best-effort read), else the pooled crown. Read-only."""
    fp, summary = portable.home_stamp()
    source = "live"
    if fp is None:
        fp, source = _crown_fp(session), "crown"
    return portable.location_map(session, get_config(session), home_fingerprint=fp, home_summary=summary,
                                 home_source=source if fp else "any")


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
