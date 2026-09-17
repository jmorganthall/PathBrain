"""Measure the fused ranking against its two parents before trusting it.

A simulated field of profiles with true Overalls a few tenths of a point apart at the top,
a pooled record whose per-profile medians carry a hidden weather bias (the firewall sits
on one profile for hours, so each profile samples its own slice of conditions), and a ring
that runs paired rounds under shared weather with the measured per-round noise. Each
night the ring fights a few matches chosen the way the ladder does (the pooled crown and
the profiles nearest it), and after each night three verdicts name a best profile:

* pooled — the argmax of the biased pooled medians;
* ring   — the argmax of a ring-only fit (τ → ∞);
* fused  — the argmax of the joint fit with τ measured from the data.

Reported: how often each names the TRUE best, by night. Run from ``backend/``:

    python scripts/sim_overall_ranking.py [--worlds 200] [--nights 30] [--bias 0.6]
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pathbrain import overall_ranking as o  # noqa: E402


def simulate(worlds: int, nights: int, bias: float, profiles: int, sigma_round: float,
             matches_per_night: int, rounds_per_match: int, seed: int) -> dict:
    rng = random.Random(seed)
    checkpoints = sorted({1, 5, 10, 20, nights} | {n for n in (30, 60) if n <= nights})
    hits = {k: {n: 0 for n in checkpoints} for k in ("pooled", "ring", "fused")}
    taus: list[float] = []
    for _ in range(worlds):
        fps = [f"p{i}" for i in range(profiles)]
        # True Overalls: a tight top (0.1-0.3 apart) over a wider tail.
        truth = {fp: 70.0 - (0.15 * i if i < 5 else 0.75 + 0.6 * (i - 5)) for i, fp in enumerate(fps)}
        best = max(truth, key=truth.get)
        # The pooled record: many iterations (tiny SE), but a per-profile bias of ±bias.
        pooled = {
            fp: {"overall": truth[fp] + rng.gauss(0, bias), "se": rng.uniform(0.03, 0.12)}
            for fp in fps
        }
        rounds: list[dict] = []
        for night in range(1, nights + 1):
            # The ladder seats the pooled crown and its nearest rivals (by pooled score).
            order = sorted(fps, key=lambda f: pooled[f]["overall"], reverse=True)
            belt = order[0]
            for m in range(matches_per_night):
                challenger = order[1 + (night + m) % min(6, profiles - 1)]
                edge = truth[challenger] - truth[belt]
                for _r in range(rounds_per_match):
                    rounds.append({"a": belt, "b": challenger, "margin": edge + rng.gauss(0, sigma_round),
                                   "session_id": night, "weather_shifted": False})
            if night in checkpoints:
                pairs = o.pair_summaries(rounds)
                tau = o.pooled_slack(pooled, pairs, sigma_round)["tau"]
                if night == nights:
                    taus.append(tau)
                fused = o.fuse(pooled, rounds, sigma_round, tau)
                ring = o.fuse(pooled, rounds, sigma_round, tau, anchor_scale=0.0)
                fought = {fp for fp, f in fused.items() if fp != "__cov__" and f["rounds"] >= 8}
                names = {
                    "pooled": max(fps, key=lambda f: pooled[f]["overall"]),
                    "ring": max(fought, key=lambda f: ring[f]["fused"]) if fought else None,
                    "fused": max(fps, key=lambda f: fused[f]["fused"]),
                }
                for k, fp in names.items():
                    hits[k][night] += int(fp == best)
    return {
        "checkpoints": checkpoints,
        "rate": {k: {n: round(100 * v / worlds) for n, v in row.items()} for k, row in hits.items()},
        "tau_measured_median": sorted(taus)[len(taus) // 2] if taus else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worlds", type=int, default=200)
    ap.add_argument("--nights", type=int, default=30)
    ap.add_argument("--bias", type=float, default=0.6, help="hidden per-profile pooled bias σ, Overall pts")
    ap.add_argument("--profiles", type=int, default=12)
    ap.add_argument("--sigma-round", type=float, default=1.47)
    ap.add_argument("--matches", type=int, default=3)
    ap.add_argument("--rounds", type=int, default=8)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    out = simulate(a.worlds, a.nights, a.bias, a.profiles, a.sigma_round, a.matches, a.rounds, a.seed)
    print(f"bias σ={a.bias}  round σ={a.sigma_round}  {a.matches} matches × {a.rounds} rounds a night  "
          f"{a.worlds} worlds  τ measured (median at night {a.nights}) = {out['tau_measured_median']}")
    print("night:   " + "  ".join(f"{n:>4}" for n in out["checkpoints"]))
    for k in ("pooled", "ring", "fused"):
        print(f"{k:<8} " + "  ".join(f"{out['rate'][k][n]:>3}%" for n in out["checkpoints"]))


if __name__ == "__main__":
    main()
