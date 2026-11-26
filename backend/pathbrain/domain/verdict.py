"""The Verdict's shape: which profile is the crown, who named it, and why (`pathbrain.verdict`)."""
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


@dataclass(frozen=True)
class CrownRead:
    """The sitting crown as last decided, for a reader that needs only who it is.

    * ``fingerprint`` — the crown; ``None`` when the newest answer named nobody.
    * ``label`` — its settings label as the crown ledger last wrote it; ``None`` when the
      ledger never named it.
    * ``since`` — when that answer was decided (ISO 8601, UTC); ``None`` when unknown.
    * ``basis`` — ``"verdict"`` when read from the Verdict's history, ``"pooled"`` while
      that history is still empty and the older pooled ledger stands in.
    """

    fingerprint: str | None
    label: str | None
    since: str | None
    basis: str
