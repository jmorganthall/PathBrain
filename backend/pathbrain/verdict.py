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

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Mapping

#: The per-judge crowns a resolution may read on its way to the verdict (shown beside it,
#: never acted on): the fused ranking's crown, the math crown, the two leg-first crowns.
EVIDENCE_KEYS = ("fused", "math", "lcp_first", "fcp_first")


@dataclass(frozen=True)
class Verdict:
    """Which profile is the crown, who named it, and why.

    * ``fingerprint`` — the crown; ``None`` when nothing can be named yet.
    * ``source`` — the judge whose answer stands (``fused``, ``pooled``, ``duel``, ``math``,
      ``lcp_first``, ``fcp_first``), after any speed-budget fallback.
    * ``policy`` — the crowning policy in force when it was decided.
    * ``detail`` — one human sentence on why this profile, in the words the UI shows.
    * ``duel_champion`` — the ladder's latest fresh champion, or ``None``, whatever the policy.
    * ``speed_budget`` — the budget check as decided (``checked``, ``within``, ``over``,
      ``replaced``).
    * ``evidence`` — the other judges' crowns read along the way (`EVIDENCE_KEYS`).
    """

    fingerprint: str | None
    source: str
    policy: str
    detail: str
    duel_champion: Mapping[str, Any] | None = None
    speed_budget: Mapping[str, Any] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_resolution(cls, resolved: Mapping[str, Any]) -> "Verdict":
        """The typed form of a `crowning.resolve` answer. Raises on a missing core key,
        so a resolver that stops saying who decided is caught here, not on a screen."""
        return cls(
            fingerprint=resolved.get("fingerprint"),
            source=str(resolved["source"]),
            policy=str(resolved["policy"]),
            detail=str(resolved["detail"]),
            duel_champion=resolved.get("duel_champion"),
            speed_budget=MappingProxyType(dict(resolved.get("speed_budget") or {})),
            evidence=MappingProxyType({k: resolved[k] for k in EVIDENCE_KEYS if k in resolved}),
        )

    def fallen_back(self, fingerprint: str | None, source: str, detail: str) -> "Verdict":
        """This verdict, overruled to another profile with the reason (Auto-apply's
        "the crown has no stored settings" case). Keeps the policy and the evidence."""
        return replace(self, fingerprint=fingerprint, source=source, detail=detail)

    def as_dict(self) -> dict[str, Any]:
        """The JSON shape the API has always served for a resolution."""
        return {
            "policy": self.policy,
            "fingerprint": self.fingerprint,
            "source": self.source,
            "detail": self.detail,
            "duel_champion": dict(self.duel_champion) if self.duel_champion else None,
            "speed_budget": dict(self.speed_budget) or None,
            **{k: v for k, v in self.evidence.items()},
        }


def current(session, pooled_best_fp: str | None) -> Verdict:
    """The verdict now. ``pooled_best_fp`` is the pooled crown, which every policy falls
    back to when its own judge names nobody."""
    from . import crowning

    return Verdict.from_resolution(crowning.resolve(session, pooled_best_fp))


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
