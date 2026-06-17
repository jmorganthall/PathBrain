"""Config endpoints: benchmark config + firewall discovery/snapshots."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config_store import default_rubric, get_config, reset_config, save_config
from ..database import get_session
from ..experiment import is_experiment_running
from ..logging_config import get_logger
from ..models import ConfigSnapshot
from ..providers import get_provider
from ..schemas import (
    ConfigSnapshotOut,
    ConfigUpdate,
    DiscoverOut,
    ManualApplyIn,
    ManualApplyOut,
    ManualApplyResult,
)

router = APIRouter()
log = get_logger("api.config")

# Normalized shaper params a human may set manually from the GUI. Kept in sync
# with the providers' apply() mappings (see providers/opnsense.py _PARAM_FIELD).
APPLY_PARAMS = {"bandwidth", "quantum", "limit", "flows", "target", "interval", "ecn"}


@router.get("/config")
def read_config(session: Session = Depends(get_session)) -> dict:
    """Effective benchmark configuration (targets, weights, thresholds)."""
    return get_config(session)


@router.put("/config")
def update_config(
    payload: ConfigUpdate, session: Session = Depends(get_session)
) -> dict:
    """Update (merge) benchmark configuration."""
    new_config = payload.model_dump(exclude_unset=True)
    return save_config(session, new_config)


@router.post("/config/reset")
def reset(session: Session = Depends(get_session)) -> dict:
    """Reset benchmark configuration to defaults."""
    return reset_config(session)


@router.post("/config/adopt-rubric")
def adopt_rubric(session: Session = Depends(get_session)) -> dict:
    """Adopt the latest default scoring rubric (perception-calibrated weights +
    thresholds + version), leaving targets/monitoring untouched."""
    return save_config(session, default_rubric())


@router.get("/config/provider")
def provider_health() -> dict:
    """Connectivity/health of the configured discovery provider."""
    provider = get_provider()
    return provider.health()


@router.post("/config/discover", response_model=DiscoverOut)
def discover(session: Session = Depends(get_session)) -> DiscoverOut:
    """Discover FQ-CoDel settings from the firewall and store a snapshot."""
    provider = get_provider()
    try:
        configs = provider.discover()
        snapshot_data = provider.snapshot()
    except Exception as exc:  # noqa: BLE001 — surface provider failures clearly
        log.exception("Discovery failed via provider '%s'", provider.name)
        raise HTTPException(
            status_code=502,
            detail=f"{provider.name} discovery failed: {type(exc).__name__}: {exc}",
        ) from exc

    snapshot = ConfigSnapshot(
        provider=provider.name,
        label="discovery",
        data=snapshot_data,
    )
    session.add(snapshot)
    session.commit()
    log.info("Stored config snapshot %s from provider '%s'", snapshot.id, provider.name)

    return DiscoverOut(
        provider=provider.name,
        pipes=[c.to_dict() for c in configs],
        snapshot_id=snapshot.id,
    )


@router.post("/config/apply", response_model=ManualApplyOut)
def apply_manual(
    payload: ManualApplyIn, session: Session = Depends(get_session)
) -> ManualApplyOut:
    """Manually write shaper params to one firewall pipe (guarded).

    This is a deliberate *second* firewall-write path next to the experiment
    engine. Guards: rejects unknown params, refuses to write while an experiment
    is actively driving the firewall, and snapshots the live config before
    writing so a bad change can be traced/rolled back. Each param is applied
    independently; partial success is reported per-param.
    """
    if not payload.changes:
        raise HTTPException(status_code=400, detail="No changes provided")

    unknown = sorted(set(payload.changes) - APPLY_PARAMS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported param(s): {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(APPLY_PARAMS))}",
        )

    if is_experiment_running():
        raise HTTPException(
            status_code=409,
            detail="An experiment is currently running. Abort it before applying "
            "manual changes so the two write paths don't conflict.",
        )

    provider = get_provider()

    # Snapshot-before for safety/traceability (best-effort; never blocks the write).
    snapshot_id = None
    try:
        snapshot = ConfigSnapshot(
            provider=provider.name,
            label="manual-apply-pre",
            data=provider.snapshot(),
        )
        session.add(snapshot)
        session.commit()
        snapshot_id = snapshot.id
    except Exception:  # noqa: BLE001 — snapshot is advisory, don't fail the apply
        session.rollback()
        log.warning("Manual apply: pre-change snapshot failed", exc_info=True)

    results: list[ManualApplyResult] = []
    applied = 0
    for param, value in payload.changes.items():
        try:
            provider.apply(
                {"pipe_uuid": payload.pipe_uuid or None, "param": param, "value": value}
            )
            results.append(ManualApplyResult(param=param, value=value, ok=True))
            applied += 1
            log.info(
                "Manual apply: %s=%s pipe=%s provider=%s",
                param,
                value,
                payload.pipe_uuid or "(first)",
                provider.name,
            )
        except Exception as exc:  # noqa: BLE001 — report per-param, keep going
            log.exception("Manual apply failed for %s", param)
            results.append(
                ManualApplyResult(
                    param=param,
                    value=value,
                    ok=False,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

    return ManualApplyOut(
        provider=provider.name,
        snapshot_id=snapshot_id,
        applied=applied,
        results=results,
    )


@router.get("/config/snapshots", response_model=list[ConfigSnapshotOut])
def list_snapshots(session: Session = Depends(get_session)) -> list[ConfigSnapshot]:
    return list(
        session.scalars(
            select(ConfigSnapshot).order_by(ConfigSnapshot.created_at.desc()).limit(50)
        ).all()
    )
