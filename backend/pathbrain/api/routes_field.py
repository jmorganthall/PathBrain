"""Real-browser field records (S32): the extension's upload endpoint.

Off by default: with ``PATHBRAIN_FIELD_INGEST_ENABLED`` unset the route answers 404, as if it
were not there. Storing never touches the firewall (see :mod:`pathbrain.field_ingest`).
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import field_ingest
from ..config import get_settings
from ..database import get_session

router = APIRouter()


def _enabled() -> None:
    if not get_settings().field_ingest_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


@router.post("/field/records", dependencies=[Depends(_enabled)])
def ingest_field_records(payload: dict = Body(...), session: Session = Depends(get_session)) -> dict:
    """Append a batch ``{"records": [...]}`` of extension records, unchanged."""
    try:
        records = field_ingest.validate_batch(payload.get("records"))
    except field_ingest.BatchRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return field_ingest.store_batch(session, records)
