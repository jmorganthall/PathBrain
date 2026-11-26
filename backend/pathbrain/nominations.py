"""Ring nominations: how an engine that finds a winner hands it to the ring.

PathBrain had several writers of the firewall: Auto-apply, plus a challenger race and an
experiment that could each "auto-promote" their winner straight onto the link. That is
three ways to decide the crown. Under the one chain only Auto-apply writes the crown, and
only the ring moves it, so an engine's winner is a **nomination**: recorded here, seated
by the ring against the crown as a live threat, and crowned only if it wins there.

Read-only for the ring (`open_nominations`); written by the engines (`nominate`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .logging_config import get_logger
from .domain.verdict import OpenNomination
from .models import Duel, DuelStatus, RingNomination

log = get_logger("nominations")

#: How long a nomination waits for the ring before it lapses. Two weeks covers several
#: duel nights even with the ring busy on closer questions.
OPEN_DAYS = 14

#: How many recent finished duels are read to see whether a nominee has been answered.
_RECENT_DUELS = 60


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def nominate(session, fingerprint: str, source: str, reason: str) -> RingNomination | None:
    """Put ``fingerprint`` forward for the ring. Returns the new row, or ``None`` when the
    profile already has an open nomination. Caller commits."""
    if any(n.fingerprint == fingerprint for n in open_nominations(session)):
        return None
    row = RingNomination(fingerprint=fingerprint, source=source, reason=reason)
    session.add(row)
    session.flush()  # so the next read in this session sees it open
    log.info("Nominated %s for the ring (%s): %s", fingerprint, source, reason)
    return row


def open_nominations(session, now: datetime | None = None) -> list[OpenNomination]:
    """Every nomination the ring has not yet answered: made in
    the last `OPEN_DAYS`, with no decided match involving the profile since."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=OPEN_DAYS)
    rows = session.scalars(
        select(RingNomination).where(RingNomination.created_at >= since).order_by(RingNomination.id)
    ).all()
    if not rows:
        return []
    duels = session.scalars(
        select(Duel)
        .where(Duel.status == DuelStatus.COMPLETE, Duel.finished_at.is_not(None))
        .order_by(Duel.id.desc())
        .limit(_RECENT_DUELS)
    ).all()
    from .duel import ABORTED, outcome  # the ring's own reading of a match; lazy, duel imports us

    last_fought: dict[str, datetime] = {}
    for d in duels:
        for m in d.matchups or []:
            if not isinstance(m, dict) or outcome(m) in ("", ABORTED):
                continue
            for side in ("incumbent", "challenger"):
                fp = str(m[side]) if side in m else ""
                at = _utc(d.finished_at)
                if fp and (fp not in last_fought or at > last_fought[fp]):
                    last_fought[fp] = at
    out: list[OpenNomination] = []
    for r in rows:
        made = _utc(r.created_at)
        if r.fingerprint in last_fought and last_fought[r.fingerprint] >= made:
            continue
        out.append(OpenNomination(fingerprint=r.fingerprint, source=r.source, reason=r.reason, made=made))
    return out


__all__ = ["OPEN_DAYS", "nominate", "open_nominations"]
