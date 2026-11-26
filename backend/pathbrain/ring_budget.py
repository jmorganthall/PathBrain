"""The speed budget judged back to back: from the ring's paired milliseconds.

The speed budget says how much slower than the fastest the crown may be. Up to v20 it was
judged on the pooled record's medians, runs taken at different times under conditions that
were never held equal. A methodology whose judgement says ``budget_judged_on == "ring"``
judges it here instead: on the raw FCP and LCP milliseconds both sides of a ring round
measured minutes apart (`RingRoundMs`, written by `duel._record_round_ms`).

A profile is over the budget on a leg when, against some opponent it met in the ring, its
mean paired gap on that leg, less ``sigma`` standard errors, still exceeds the leg's limit:
clearly slower than that opponent by more than the methodology allows. Only rounds scored on
the methodology's own scale count, and a pair needs `MIN_PAIRED_ROUNDS` rounds before it can
say anything. "Cannot tell" (`None`) never refuses a crown.

Read-only.
"""
from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping

from sqlalchemy import or_, select

from .domain.judgement import BudgetOver, RingBudgetCheck, SpeedBudget
from .models import RingRoundMs

#: Paired rounds a (profile, opponent, leg) needs before its gap counts. Five rounds is
#: under a night's worth for one pairing and enough for a standard error to mean something.
MIN_PAIRED_ROUNDS = 5


def _limit(budget: SpeedBudget, leg: str) -> float | None:
    """The budget's limit on one leg (``<leg>_pct``), or None for a leg it does not state."""
    from .methodology import BUDGET_LEGS

    return float(getattr(budget, f"{leg}_pct")) if leg in BUDGET_LEGS else None


def ring_budget_checks(
    session,
    fingerprints,
    budget: SpeedBudget,
    version: str,
    sigma: float,
) -> Mapping[str, RingBudgetCheck | None]:
    """`RingBudgetCheck` for each profile, or ``None`` where the ring cannot tell yet."""
    from .methodology import same_scale

    wanted = [fp for fp in dict.fromkeys(fingerprints or []) if fp]
    if not wanted:
        return {}
    rows = session.scalars(
        select(RingRoundMs).where(
            or_(RingRoundMs.incumbent.in_(wanted), RingRoundMs.challenger.in_(wanted))
        )
    ).all()
    want = set(wanted)
    # (profile, opponent, leg) -> [(pct slower, ms slower)], positive = the profile is slower.
    gaps: dict[tuple[str, str, str], list[tuple[float, float]]] = defaultdict(list)
    for r in rows:
        if r.incumbent_ms <= 0 or r.challenger_ms <= 0 or not same_scale(r.methodology, version):
            continue
        if r.challenger in want:
            gaps[(r.challenger, r.incumbent, r.metric)].append(
                ((r.challenger_ms - r.incumbent_ms) / r.incumbent_ms * 100.0, r.challenger_ms - r.incumbent_ms)
            )
        if r.incumbent in want:
            gaps[(r.incumbent, r.challenger, r.metric)].append(
                ((r.incumbent_ms - r.challenger_ms) / r.challenger_ms * 100.0, r.incumbent_ms - r.challenger_ms)
            )
    over: dict[str, list[BudgetOver]] = defaultdict(list)
    pairs: dict[str, int] = defaultdict(int)
    for (fp, opponent, leg), vals in gaps.items():
        limit = _limit(budget, leg)
        n = len(vals)
        if limit is None or n < MIN_PAIRED_ROUNDS:
            continue
        pairs[fp] += 1
        pcts = [v[0] for v in vals]
        mean = sum(pcts) / n
        sd = math.sqrt(sum((x - mean) ** 2 for x in pcts) / (n - 1))
        if mean - sigma * sd / math.sqrt(n) > limit:
            over[fp].append(BudgetOver(
                leg=leg, pct=mean, limit_pct=limit,
                delta_ms=sum(v[1] for v in vals) / n, against=opponent, rounds=n,
            ))
    out: dict[str, RingBudgetCheck | None] = {}
    for fp in wanted:
        if pairs[fp] == 0:
            out[fp] = None
            continue
        per_leg: dict[str, BudgetOver] = {}
        for o in over[fp]:  # one line per leg: the opponent it trails furthest
            if o.leg not in per_leg or o.pct > per_leg[o.leg].pct:
                per_leg[o.leg] = o
        worst = sorted(per_leg.values(), key=lambda o: -o.pct)
        out[fp] = RingBudgetCheck(within=not worst, over=tuple(worst), pairs=pairs[fp])
    return out


__all__ = ["MIN_PAIRED_ROUNDS", "ring_budget_checks"]
