"""Bradley–Terry strength ratings for the duel ladder.

The head-to-head standings started as a football league table — 3 points a win, 1 a draw
— which is readable but throws away the thing the ladder exists to measure. A league
table records *how many* you beat; it cannot record *who*. Beating the champion and
beating a profile nobody has ever measured are entered as the same 3 points, the
belt-holder accumulates points simply by staying on (the winner defends, so it gets more
bouts than anyone), and two profiles that never met cannot be compared at all.

Bradley–Terry fixes all three from the same ledger. Each profile gets a strength
``γ``; the model says

    P(i beats j) = γᵢ / (γᵢ + γⱼ)

and the strengths are the ones that make the record most likely. Beating a strong
opponent then moves you a lot and beating a weak one barely at all, losing to the
champion costs little and losing to a filler profile costs a lot, and profiles that never
met are still comparable **through their shared opponents** — the whole point of fitting
a network rather than counting a column.

Design notes:

* **Fitted, not accumulated.** This is a maximum-likelihood fit over the entire ledger
  (Zermelo/Ford MM iterations), not a running Elo. So it is deterministic and
  order-independent: the same record always yields the same table, and a rating can be
  recomputed from the ledger at any time — the same property the rest of PathBrain
  insists on for scores.
* **The unit of evidence is the PAIR, not the bout.** Every duel pair is one interleaved
  A/B comparison under the same weather, and the ledger already records them
  (``wins_incumbent`` / ``wins_challenger``). Rating on pairs means a hard-fought 12–8
  contributes more evidence than a 3–0 snap, and a *drawn* bout still informs the rating
  instead of being discarded. (Pair-wins are a weak way to decide a single bout — that's
  why the verdict uses the margins — but pooled across every bout a profile has ever
  fought they're plenty, and they're the comparison the model is defined on.)
* **A prior keeps it honest.** Unbeaten records make plain Bradley–Terry diverge (an
  infinite strength), and a profile that went 3–0 in one snap bout should not outrank a
  profile that has defended fifty times. Each profile is given ``prior_pairs`` virtual
  pairs, split evenly, against a phantom average opponent — so a thin record is pulled
  toward the middle of the field and only real evidence pulls it back out.

Ratings are reported on the familiar Elo scale (mean 1500, 400 points per factor-of-ten
odds) with a standard error from the Fisher information, so a rating built on 8 pairs is
visibly less certain than one built on 800.
"""
from __future__ import annotations

import math

# Elo convention: 400 points = a 10:1 odds ratio.
ELO_SCALE = 400.0 / math.log(10.0)
ELO_ANCHOR = 1500.0

# Virtual pairs against a phantom average opponent, split evenly. Four is calibrated
# against the two behaviours the table has to get right at once, and it is a real
# trade-off, not a default: too light and a 3-0 sweep over the WEAKEST profile outrates a
# veteran that has beaten the whole field (measured: at 2 it does); too heavy and a
# genuine win over the STRONGEST stops moving anyone, which is the entire point of
# rating rather than counting. At 4 the sweep lands mid-table while a 9-3 win over the
# leader still takes the top of the ladder outright. `test_the_prior_is_calibrated_…`
# pins both ends.
DEFAULT_PRIOR_PAIRS = 4.0

# A rating is flagged provisional — shown, ranked, but marked as not yet established —
# until it rests on more than one bout's worth of pairs AND more than one opponent. The
# second condition matters as much as the first: a profile that has only ever fought one
# opponent has no position in the network, so its rating is a single edge restated, and
# one dominant bout against the leader would otherwise read as a settled #1.
PROVISIONAL_PAIRS = 20
PROVISIONAL_OPPONENTS = 2


def fit_bradley_terry(
    pair_wins: dict[tuple[str, str], int],
    *,
    prior_pairs: float = DEFAULT_PRIOR_PAIRS,
    max_iterations: int = 500,
    tolerance: float = 1e-10,
) -> dict[str, dict]:
    """Fit strengths to a pairwise record.

    ``pair_wins[(a, b)]`` is the number of pairs ``a`` won against ``b``. Returns
    ``{fingerprint: {rating, rating_se, pairs, wins, losses, expected_wins}}`` where
    ``rating`` is on the Elo scale and ``expected_wins`` is how many pairs the fitted
    model expected this profile to win — a plain-language readout of whether it is
    over- or under-performing its schedule.
    """
    players: set[str] = set()
    for a, b in pair_wins:
        players.add(a)
        players.add(b)
    if not players:
        return {}

    # n[(a, b)] = pairs played between them (symmetric); w[a] = pairs a won overall.
    played: dict[tuple[str, str], int] = {}
    won: dict[str, int] = {p: 0 for p in players}
    for (a, b), count in pair_wins.items():
        if count <= 0:
            continue
        won[a] += count
        key = (a, b) if a <= b else (b, a)
        played[key] = played.get(key, 0) + count

    opponents: dict[str, dict[str, int]] = {p: {} for p in players}
    for (a, b), count in played.items():
        opponents[a][b] = opponents[a].get(b, 0) + count
        opponents[b][a] = opponents[b].get(a, 0) + count

    # MM (Zermelo/Ford) iterations on γ, with the phantom opponent pinned at γ = 1.
    gamma: dict[str, float] = {p: 1.0 for p in players}
    half_prior = prior_pairs / 2.0
    for _ in range(max_iterations):
        shift = 0.0
        for p in players:
            numerator = won[p] + half_prior
            denominator = half_prior * 2.0 / (gamma[p] + 1.0)
            for opp, count in opponents[p].items():
                denominator += count / (gamma[p] + gamma[opp])
            if denominator <= 0:
                continue
            new = numerator / denominator
            shift = max(shift, abs(math.log(new) - math.log(gamma[p])))
            gamma[p] = new
        # Normalize to a geometric mean of 1 so the scale is anchored and comparable.
        mean_log = sum(math.log(g) for g in gamma.values()) / len(gamma)
        if mean_log:
            for p in players:
                gamma[p] = gamma[p] / math.exp(mean_log)
        if shift < tolerance:
            break

    out: dict[str, dict] = {}
    for p in players:
        # Fisher information for β = ln γ: Σ n·p·(1-p) over real and phantom games. The
        # off-diagonal terms are ignored, which slightly *understates* uncertainty on a
        # tiny graph — acknowledged, and the provisional flag covers that regime.
        info = 0.0
        expected = 0.0
        total_pairs = 0
        for opp, count in opponents[p].items():
            prob = gamma[p] / (gamma[p] + gamma[opp])
            info += count * prob * (1.0 - prob)
            expected += count * prob
            total_pairs += count
        phantom_prob = gamma[p] / (gamma[p] + 1.0)
        info += prior_pairs * phantom_prob * (1.0 - phantom_prob)
        faced = len(opponents[p])
        out[p] = {
            "rating": round(ELO_ANCHOR + ELO_SCALE * math.log(gamma[p]), 1),
            "rating_se": round(ELO_SCALE / math.sqrt(info), 1) if info > 0 else None,
            "pairs": total_pairs,
            "opponents": faced,
            "wins": won[p],
            "losses": total_pairs - won[p],
            "expected_wins": round(expected, 1),
            "provisional": total_pairs < PROVISIONAL_PAIRS or faced < PROVISIONAL_OPPONENTS,
        }
    return out


def win_probability(rating_a: float, rating_b: float) -> float:
    """The fitted model's P(a beats b in one pair) — what the rating actually claims."""
    return 1.0 / (1.0 + math.pow(10.0, (rating_b - rating_a) / 400.0))


__all__ = [
    "DEFAULT_PRIOR_PAIRS",
    "ELO_ANCHOR",
    "PROVISIONAL_OPPONENTS",
    "PROVISIONAL_PAIRS",
    "fit_bradley_terry",
    "win_probability",
]
