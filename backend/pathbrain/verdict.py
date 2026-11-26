"""THE VERDICT — the one answer to "which profile is best, and why", that every reader asks.

PathBrain grew many judges: the pooled argmax, the fused ranking, the duel ladder's
champion, the math and leg-first crowns, the speed budget's fallback chain. Each reader
(Auto-apply, the Dashboard, Settings, the Overall view, Explore) used to call one or more
of them and draw its own conclusion, so two screens could name two different bests and
Auto-apply could act on a third. The owner's terms: one cohesive system "that follows
whatever we say we value as being best".

This module is that one place. Today :func:`current` wraps exactly what
`crowning.resolve` decides (the active policy, the speed budget and its fallback order),
so this sprint changes no behaviour; it only gives the answer one typed shape and one
door. Later sprints move the judgement itself behind the door: the methodology states
the rules, the overnight ring decides the crown, and everything reads it from here.

Readers take a :class:`Verdict` and never call `crowning.resolve` (or a ranking) to
decide the crown for themselves; ``tests/test_archguard_chain.py`` keeps it that way.
"""
from __future__ import annotations

from typing import Any

from .domain.verdict import EVIDENCE_KEYS, CrownRead, Verdict

__all__ = ["EVIDENCE_KEYS", "CrownRead", "Verdict", "crown", "crown_fingerprint", "current", "history", "now", "record"]


def current(session, pooled_best_fp: str | None) -> Verdict:
    """The verdict now. ``pooled_best_fp`` is the pooled crown, which every policy falls
    back to when its own judge names nobody."""
    from . import crowning

    return Verdict.from_resolution(crowning.resolve(session, pooled_best_fp))


def now(session) -> Verdict:
    """The verdict now, for a reader with no pooled crown of its own to hand in: the
    pooled crown is taken from the crown ledger, as the API has always read it."""
    from . import crown_follower

    pooled = crown_follower.current_crown(session)
    return current(session, (pooled or {}).get("fingerprint"))


def crown(session) -> CrownRead | None:
    """The sitting crown as last decided — one indexed read, cheap enough for any page.

    Reads the newest row of the Verdict's history. Until that history has a row (a fresh
    install, or before the first crown check after this table arrived) the pooled crown
    ledger stands in, and ``basis`` says so. ``None`` only when neither has an answer."""
    from sqlalchemy import select

    from . import crown_follower
    from .models import CrownEvent, VerdictRecord

    row = session.scalars(select(VerdictRecord).order_by(VerdictRecord.id.desc()).limit(1)).first()
    if row is None:
        pooled = crown_follower.current_crown(session)
        if not pooled:
            return None
        return CrownRead(pooled.get("fingerprint"), pooled.get("label"), pooled.get("since"), "pooled")
    label = None
    if row.fingerprint:
        label = session.scalars(
            select(CrownEvent.label)
            .where(CrownEvent.fingerprint == row.fingerprint, CrownEvent.label.is_not(None))
            .order_by(CrownEvent.id.desc())
            .limit(1)
        ).first()
    since = crown_follower._as_utc(row.decided_at).isoformat() if row.decided_at else None
    return CrownRead(row.fingerprint, label, since, "verdict")


def crown_fingerprint(session) -> str | None:
    """Just the sitting crown's fingerprint (`crown`), or ``None``."""
    read = crown(session)
    return read.fingerprint if read else None


def _same_answer(row, v: Verdict) -> bool:
    return (row.fingerprint, row.source, row.policy) == (v.fingerprint, v.source, v.policy)


def record(session, v: Verdict, methodology_version: str | None = None):
    """Write ``v`` to the verdict history when it is a new answer — a different crown,
    source or policy from the newest row — and return the new row, else ``None``.
    Caller commits. A changed ``detail`` alone (a margin, a date) is not a new answer."""
    from sqlalchemy import select

    from .models import VerdictRecord

    last = session.scalars(select(VerdictRecord).order_by(VerdictRecord.id.desc()).limit(1)).first()
    if last is not None and _same_answer(last, v):
        return None
    row = VerdictRecord(
        fingerprint=v.fingerprint,
        previous_fingerprint=last.fingerprint if last is not None else None,
        source=v.source,
        policy=v.policy,
        detail=v.detail,
        methodology_version=methodology_version,
        speed_budget=dict(v.speed_budget) or None,
        evidence={k: (e or {}).get("fingerprint") for k, e in v.evidence.items()} or None,
    )
    session.add(row)
    return row


def history(session, limit: int = 20) -> list[dict[str, Any]]:
    """The newest ``limit`` answers, newest first, as the API serves them."""
    from sqlalchemy import select

    from .models import VerdictRecord

    rows = session.scalars(
        select(VerdictRecord).order_by(VerdictRecord.id.desc()).limit(max(1, min(limit, 200)))
    ).all()
    return [{
        "decided_at": r.decided_at.isoformat() if r.decided_at else None,
        "fingerprint": r.fingerprint,
        "previous_fingerprint": r.previous_fingerprint,
        "source": r.source,
        "policy": r.policy,
        "detail": r.detail,
        "methodology_version": r.methodology_version,
        "speed_budget": r.speed_budget,
        "evidence": r.evidence,
    } for r in rows]
