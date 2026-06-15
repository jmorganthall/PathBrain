"""Config endpoints: benchmark config + firewall discovery/snapshots."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config_store import get_config, reset_config, save_config
from ..database import get_session
from ..logging_config import get_logger
from ..models import ConfigSnapshot
from ..providers import get_provider
from ..schemas import ConfigSnapshotOut, ConfigUpdate, DiscoverOut

router = APIRouter()
log = get_logger("api.config")


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


@router.get("/config/snapshots", response_model=list[ConfigSnapshotOut])
def list_snapshots(session: Session = Depends(get_session)) -> list[ConfigSnapshot]:
    return list(
        session.scalars(
            select(ConfigSnapshot).order_by(ConfigSnapshot.created_at.desc()).limit(50)
        ).all()
    )
