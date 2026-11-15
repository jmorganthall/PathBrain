"""Real-browser field records (S32): the raw, append-only store behind ``POST /api/field/records``.

The field recorder extension (``extension/``) measures page loads in Josh's own browser and
posts them here. Each record is stored **unchanged** in ``field_records``, stamped with the
firewall profile that was live when it arrived.

That stamp comes only from what PathBrain already knows. Ingest never asks the firewall: no
provider discover, apply or reload happens per upload (a browser uploading every few minutes
must not become a firewall poller). The one live reading PathBrain keeps is the crown
follower's last check, which discovered the firewall and fingerprinted it; it is used only
while no firewall write has been logged since that check (``firewall_writes``, any outcome),
because any write since may have moved the firewall. Otherwise the stamp is NULL. The reading's
time is stored with it, since a change made outside PathBrain (by hand in OPNsense) leaves no
ledger row.

Off by default: ingest needs a LAN token in config (``PATHBRAIN_FIELD_INGEST_TOKEN``, S33).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import FieldRecord, FirewallWrite

log = logging.getLogger(__name__)

#: A batch larger than this is refused whole (the extension sends far fewer).
MAX_BATCH = 500
#: One record's JSON larger than this is refused (the extension's are ~300 bytes).
MAX_RECORD_BYTES = 4096

SOURCE_CROWN_CHECK = "crown_follower_check"


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    try:
        return _as_utc(datetime.fromisoformat(str(ts)))
    except ValueError:
        return None


def known_fingerprint(session: Session) -> tuple[str | None, str | None, datetime | None]:
    """``(fingerprint, source, seen_at)`` of the live firewall, from what PathBrain already
    knows, or ``(None, None, None)``. Reads module state and the write ledger only: it never
    calls a provider."""
    from . import crown_follower

    try:
        last = (crown_follower.status() or {}).get("last_result") or {}
    except Exception:  # noqa: BLE001 — a stamp is best-effort; the record is still stored
        log.debug("field_ingest: crown follower state unreadable", exc_info=True)
        return None, None, None
    fp = last.get("live_fingerprint")
    seen = _parse(last.get("checked_at"))
    if not fp or seen is None:
        return None, None, None
    newest_write = _as_utc(session.execute(select(func.max(FirewallWrite.at))).scalar())
    if newest_write is not None and newest_write >= seen:
        return None, None, None  # the firewall may have moved since that reading
    return str(fp), SOURCE_CROWN_CHECK, seen


#: The one path the extension's origin may call cross-origin (main._field_ingest_cors).
INGEST_PATH = "/api/field/records"


def allowed_origins() -> frozenset[str]:
    """The extension origins from config (PATHBRAIN_FIELD_INGEST_ORIGINS)."""
    from .config import get_settings

    raw = get_settings().field_ingest_origins or ""
    return frozenset(o.strip().rstrip("/") for o in raw.split(",") if o.strip())


class BatchRefused(ValueError):
    """The batch is not a list of JSON objects within the size limits."""


def validate_batch(records) -> list[dict]:
    if not isinstance(records, list):
        raise BatchRefused("records must be a list")
    if len(records) > MAX_BATCH:
        raise BatchRefused(f"at most {MAX_BATCH} records per batch")
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise BatchRefused(f"record {i} is not an object")
        if len(json.dumps(rec, separators=(",", ":"))) > MAX_RECORD_BYTES:
            raise BatchRefused(f"record {i} is larger than {MAX_RECORD_BYTES} bytes")
    return records


def store_batch(session: Session, records: list[dict]) -> dict:
    """Append ``records`` unchanged, all with one fingerprint stamp. Returns a summary."""
    fp, source, seen = known_fingerprint(session)
    now = datetime.now(timezone.utc)
    for rec in records:
        session.add(FieldRecord(received_at=now, record=rec, firewall_fingerprint=fp,
                                fingerprint_source=source, fingerprint_seen_at=seen))
    session.commit()
    return {"stored": len(records), "firewall_fingerprint": fp, "fingerprint_source": source,
            "fingerprint_seen_at": seen.isoformat() if seen else None}
