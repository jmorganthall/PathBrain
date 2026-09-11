"""Acknowledgements: an alert you have read stays read until the situation changes.

PathBrain's diagnostic banners are *conditions*, not events — "56% of matches produced no
result", "the crown is fading", "this threshold saturates every profile". Each is worth
showing the first time and is noise every time after, because the condition is still true
tomorrow and the banner cannot tell "nobody has looked at this" from "somebody looked,
understood it, and is working on it". So every one of them was permanent furniture, which
is the same as not alerting at all: a warning nobody can clear is a warning nobody reads.

**What "until it changes" has to mean.** The obvious implementation — hash the payload,
re-show when the hash moves — is wrong here, and wrong in a way that looks right in a test.
The ladder runs continuously, so the round-health numbers move every session: 577 of 1027
becomes 578 of 1030 within the hour, and a content hash re-fires on a banner nobody
dismissed for any reason to do with the numbers. Nor is the raw text stable: the causes
carry profile names and fingerprints, so the same failure under a different profile reads
as a new one.

So an alert is identified by its **situation** rather than its contents:

* a **signature** over the *kinds* of thing happening — which causes, not how many, with
  the profile-specific parts of each cause folded away (``duel._reason_class``). A new kind
  of failure appearing is a new alert and says so; the same failures continuing are not.
* a **state** the alert chooses, which a ``supersedes`` predicate reads to decide whether
  today's situation is materially *worse* than the acknowledged one — so an acknowledged
  56% that drifts to 57% stays quiet and one that reaches 75% comes back. Getting *better*
  never re-fires: the concern is resolved, and announcing that is what the page is for.

Both halves are the alert's own to define; this module only stores the acknowledgement and
asks the question. Nothing here is a score, a gate or a measurement — dismissing a banner
changes what is displayed and nothing else.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select

from .database import session_scope
from .logging_config import get_logger
from .models import AlertAck

log = get_logger("alerts")


def signature(*parts: Any) -> str:
    """A stable short hash of whatever identifies a situation.

    Canonicalized through JSON with sorted keys, so the caller can hand it sets, dicts and
    numbers without thinking about ordering — two readings of the same situation must hash
    the same however they were assembled.
    """
    blob = json.dumps(parts, sort_keys=True, default=_canonical)
    return hashlib.blake2b(blob.encode(), digest_size=16).hexdigest()


def _canonical(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, tuple):
        return list(value)
    return str(value)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _row(session, key: str) -> AlertAck | None:
    return session.scalar(select(AlertAck).where(AlertAck.key == key))


def acknowledge(key: str, sig: str, state: dict | None = None, *, by: str = "user") -> dict:
    """Record that this situation has been seen. Re-acknowledging replaces the previous one
    — the newest reading is the one a later comparison should be made against."""
    with session_scope() as session:
        row = _row(session, key)
        if row is None:
            row = AlertAck(key=key)
            session.add(row)
        row.signature = sig
        row.state = dict(state or {})
        row.acked_at = datetime.now(timezone.utc)
        row.acked_by = by
        session.flush()
        log.info("Alert %s acknowledged by %s (signature %s)", key, by, sig[:12])
        return _snapshot(row)


def clear(key: str) -> bool:
    """Un-acknowledge: the alert shows again from the next read. Returns False if it wasn't
    acknowledged, which is not an error — the caller asked for a state that already holds."""
    with session_scope() as session:
        row = _row(session, key)
        if row is None:
            return False
        session.delete(row)
        log.info("Alert %s un-acknowledged", key)
        return True


def acks() -> list[dict]:
    """Every standing acknowledgement, newest first — so a page can say what is muted
    rather than leaving a cleared alert invisible with no way back to it."""
    with session_scope() as session:
        rows = session.scalars(select(AlertAck).order_by(AlertAck.acked_at.desc())).all()
        return [_snapshot(r) for r in rows]


def _snapshot(row: AlertAck) -> dict:
    return {
        "key": row.key,
        "signature": row.signature,
        "state": row.state or {},
        "acked_at": _as_utc(row.acked_at).isoformat() if row.acked_at else None,
        "acked_by": row.acked_by,
    }


def status(
    key: str,
    sig: str,
    *,
    supersedes: Callable[[dict], bool] | None = None,
) -> dict:
    """Should this alert be shown? ``{acknowledged, signature, acked_at, why}``.

    Not acknowledged when there is no row, when the situation's signature has changed (a
    different set of causes), or when ``supersedes(acked_state)`` says the current reading
    is materially worse than the one acknowledged. ``why`` is the sentence the page shows
    when an alert comes back, because "this is new" and "this got worse" are different
    answers and a banner that reappears silently teaches the reader to ignore it.

    Best-effort: an unreadable ack shows the alert. Failing open is the only safe direction
    — the cost is one banner too many, against a diagnostic silently lost.
    """
    try:
        with session_scope() as session:
            row = _row(session, key)
            acked = _snapshot(row) if row is not None else None
    except Exception:  # noqa: BLE001
        log.warning("alerts: could not read the acknowledgement for %s", key, exc_info=True)
        return {"acknowledged": False, "signature": sig, "acked_at": None, "why": None}

    if acked is None:
        return {"acknowledged": False, "signature": sig, "acked_at": None, "why": None}
    if acked["signature"] != sig:
        return {
            "acknowledged": False, "signature": sig, "acked_at": acked["acked_at"],
            "why": "Something new since you cleared this.",
        }
    if supersedes is not None:
        try:
            worse = bool(supersedes(acked["state"] or {}))
        except Exception:  # noqa: BLE001
            log.warning("alerts: %s could not compare against its ack", key, exc_info=True)
            worse = False
        if worse:
            return {
                "acknowledged": False, "signature": sig, "acked_at": acked["acked_at"],
                "why": "The same causes, but materially worse than when you cleared this.",
            }
    return {
        "acknowledged": True, "signature": sig, "acked_at": acked["acked_at"], "why": None,
    }


__all__ = ["acknowledge", "acks", "clear", "signature", "status"]
