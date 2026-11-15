"""Real-browser field records (S32/S33): the extension's upload endpoint.

Accepted only with the LAN token from config (``PATHBRAIN_FIELD_INGEST_TOKEN``), sent as
``Authorization: Bearer <token>``. With no token set, ingest is disabled and the route answers
404, as if it were not there. A missing token answers 401 and a wrong one 403. The token is
compared in constant time and never logged, echoed or stored. Storing never touches the
firewall (see :mod:`pathbrain.field_ingest`).
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from .. import field_ingest
from ..config import get_settings
from ..database import get_session

router = APIRouter()


def _token_ok(authorization: str | None = Header(default=None)) -> None:
    expected = get_settings().field_ingest_token.get_secret_value()
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    scheme, _, given = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not given.strip():
        raise HTTPException(status_code=401, detail="A field ingest token is required.",
                            headers={"WWW-Authenticate": "Bearer"})
    if not hmac.compare_digest(given.strip().encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="That field ingest token is not accepted.")


@router.post("/field/records", dependencies=[Depends(_token_ok)])
def ingest_field_records(payload: dict = Body(...), session: Session = Depends(get_session)) -> dict:
    """Append a batch ``{"records": [...]}`` of extension records, unchanged."""
    try:
        records = field_ingest.validate_batch(payload.get("records"))
    except field_ingest.BatchRefused as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return field_ingest.store_batch(session, records)
