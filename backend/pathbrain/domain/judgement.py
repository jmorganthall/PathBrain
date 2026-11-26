"""The methodology's judgement: every number that decides which profile is best.

One chain: a methodology version defines best, all of it. The Overall rubric says how a
run is graded; the **judgement** says how graded runs become a crown: what counts as a
tie, how many ring rounds a profile needs, how much speed the crown may give up, who
breaks a tie, and what a ring match must show to move the crown. No setting or constant
may move the crown once a version states its judgement; changing it is a publish.

Versions published before the judgement block (v20 and earlier) state none. They are read
exactly as they always were: from the settings and constants that held these numbers
(``source == "settings"``). See `pathbrain.methodology.judgement`.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: Where a judgement's numbers came from: the version's own ``judgement`` block, or (a
#: version that states none) the settings and code constants that decided it before.
SOURCES = ("methodology", "settings")

#: Who names the crown. ``ring``: only a head-to-head match in the overnight ring moves it
#: (v21 on). ``policy``: the Auto-apply card's crown policy picks it (v20 and earlier).
CROWNED_BY = ("ring", "policy")

#: What the speed budget is judged on. ``ring``: paired head-to-head milliseconds from
#: ring rounds (v21 on). ``pooled``: the pooled record's medians (v20 and earlier).
BUDGET_JUDGED_ON = ("ring", "pooled")

#: The keys a methodology's ``judgement`` block must state, every one: a block that leaves
#: one out would fall back to a setting, and a setting must not be able to move the crown.
BLOCK_KEYS = (
    "crowned_by",
    "ring_crown_rule",
    "ring_min_margin",
    "tie_sigma",
    "tie_min_margin",
    "min_ring_rounds",
    "speed_budget",
    "budget_judged_on",
    "allow_unshaped_crown",
)
SPEED_BUDGET_KEYS = ("enabled", "lcp_pct", "fcp_pct")


@dataclass(frozen=True)
class SpeedBudget:
    """How much slower than the field's fastest the crown may be.

    * ``enabled`` — whether the budget is enforced at all.
    * ``lcp_pct`` / ``fcp_pct`` — the most the crown's LCP / FCP may trail the fastest, in
      percent. A gap counts only when it clears the noise rule.
    """

    enabled: bool
    lcp_pct: float
    fcp_pct: float


@dataclass(frozen=True)
class Judgement:
    """Every number that decides the crown under one methodology version.

    * ``version`` — the methodology version this judgement belongs to.
    * ``source`` — ``"methodology"`` when the version states a ``judgement`` block,
      ``"settings"`` when it states none and the settings and constants stand in.
    * ``crowned_by`` — ``"ring"`` or ``"policy"`` (`CROWNED_BY`).
    * ``ring_crown_rule`` — how the ring names its champion (``lineal`` / ``rating`` /
      ``floor``, `duel.CROWN_RULES`).
    * ``ring_min_margin`` — Overall points a ring match's median pair delta must clear for a
      decided match to count as a win. 0 means a win is a win.
    * ``tie_sigma`` — standard errors of the difference a lead must clear to be called real.
    * ``tie_min_margin`` — the practical tie floor in Overall points, under any SE.
    * ``min_ring_rounds`` — ring rounds a profile without a confident pooled record needs
      before it can be ranked for the crown.
    * ``speed_budget`` — `SpeedBudget`.
    * ``budget_judged_on`` — ``"ring"`` or ``"pooled"`` (`BUDGET_JUDGED_ON`).
    * ``allow_unshaped_crown`` — whether a profile with the shaper off (SQM off) may hold
      the crown. It races either way.
    """

    version: str
    source: str
    crowned_by: str
    ring_crown_rule: str
    ring_min_margin: float
    tie_sigma: float
    tie_min_margin: float
    min_ring_rounds: int
    speed_budget: SpeedBudget
    budget_judged_on: str
    allow_unshaped_crown: bool


@dataclass(frozen=True)
class BudgetOver:
    """One way a profile breaks the speed budget, judged back to back in the ring.

    * ``leg`` — ``"lcp"`` or ``"fcp"``.
    * ``pct`` — how much slower than ``against`` it was, in percent: the mean over the
      paired ring rounds the two fought.
    * ``limit_pct`` — the most the methodology allows on that leg.
    * ``delta_ms`` — the same gap in milliseconds (mean over the rounds).
    * ``against`` — the opponent it trails: the fastest profile it met on that leg.
    * ``rounds`` — paired rounds the gap rests on.
    """

    leg: str
    pct: float
    limit_pct: float
    delta_ms: float
    against: str
    rounds: int


@dataclass(frozen=True)
class RingBudgetCheck:
    """Whether a profile is within the speed budget, judged on ring milliseconds.

    * ``within`` — no leg is clearly over (`over` is empty).
    * ``over`` — every leg it is clearly over on, worst first.
    * ``pairs`` — how many (opponent, leg) pairs had enough rounds to judge.
    """

    within: bool
    over: tuple[BudgetOver, ...]
    pairs: int

    def as_dict(self) -> Mapping[str, object]:
        """The shape `crowning.budget_check` serves, so a reader reads either alike."""
        return {
            "within": self.within,
            "over": [
                {"leg": o.leg, "pct": o.pct, "limit_pct": o.limit_pct, "delta_ms": o.delta_ms,
                 "against": o.against, "rounds": o.rounds}
                for o in self.over
            ],
            "pairs": self.pairs,
        }


__all__ = [
    "BLOCK_KEYS",
    "BudgetOver",
    "RingBudgetCheck",
    "BUDGET_JUDGED_ON",
    "CROWNED_BY",
    "Judgement",
    "SOURCES",
    "SPEED_BUDGET_KEYS",
    "SpeedBudget",
]
