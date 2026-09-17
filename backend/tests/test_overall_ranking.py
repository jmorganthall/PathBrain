"""One ranking from both kinds of evidence (`overall_ranking`, the Overall page).

The fit is a weighted least-squares problem over pooled anchors and ring rounds; these
tests pin the three properties that make its answer trustworthy:

* **No rounds → pooled, exactly.** The ring can only ever *move* a pooled median; it
  never invents one, and a profile it never fought lands on its pooled value.
* **The slack is the whole argument.** At τ = 0 a well-measured pooled median cannot be
  moved by a handful of rounds (the pooled crown); as τ grows the ring decides (the duel
  champion). Both corners are reachable from one fit, and τ is *measured* from how much
  the two records disagree beyond their own error bars.
* **The crowning policy acts on it**, with the pooled crown as the fallback, and the
  Overall endpoint answers off the same rows the verdict card and the standings read.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timezone

import pytest

from pathbrain import crowning, overall_ranking as o
from pathbrain.config_store import get_config, save_config
from pathbrain.database import session_scope
from pathbrain.models import Duel, DuelStatus

# Reuse the verdict suite's own-field fixture so the pooled side is real rollup rows
# under a version this module owns.
from tests.test_verdict import VERSION, _add, _fp, _own_field  # noqa: F401

SIGMA = 1.47


def _rounds(a: str, b: str, edge: float, n: int, *, seed: int = 0, session_id: int = 1,
            sigma: float = SIGMA) -> list[dict]:
    rng = random.Random(seed)
    return [
        {"a": a, "b": b, "margin": edge + rng.gauss(0, sigma), "session_id": session_id,
         "weather_shifted": False}
        for _ in range(n)
    ]


# ── The fit ────────────────────────────────────────────────────────────────────────────


def test_with_no_rounds_the_fused_ranking_is_the_pooled_ranking_exactly():
    pooled = {"a": {"overall": 70.3, "se": 0.05}, "b": {"overall": 70.1, "se": 0.3}}
    fit = o.fuse(pooled, [], SIGMA, 0.5)
    assert fit["a"]["fused"] == 70.3 and fit["b"]["fused"] == 70.1
    # The bar is the pooled SE widened by the slack — never narrower than either.
    assert fit["a"]["se"] == pytest.approx(math.sqrt(0.05 ** 2 + 0.5 ** 2))
    assert fit["a"]["rounds"] == 0


def test_at_zero_slack_a_well_measured_pooled_median_cannot_be_moved():
    """Today's pooled crown is the τ = 0 corner: three thousand iterations pin the anchor
    and twelve rounds saying otherwise barely register."""
    pooled = {"a": {"overall": 70.3, "se": 0.02}, "b": {"overall": 70.1, "se": 0.02}}
    rounds = _rounds("a", "b", edge=+1.0, n=12)
    fit = o.fuse(pooled, rounds, SIGMA, 0.0)
    assert fit["b"]["fused"] - fit["a"]["fused"] < 0.0, "pooled still says a leads"


def test_as_the_slack_grows_the_ring_decides():
    """The duel champion is the τ → ∞ corner. Same rounds, same pooled numbers; the ring's
    +1 margin takes the lead as the anchors loosen, and the ring-only fit reproduces the
    rounds' own mean."""
    pooled = {"a": {"overall": 70.3, "se": 0.02}, "b": {"overall": 70.1, "se": 0.02}}
    rounds = _rounds("a", "b", edge=+1.0, n=12)
    mean = sum(r["margin"] for r in rounds) / len(rounds)
    diffs = [
        o.fuse(pooled, rounds, SIGMA, tau)["b"]["fused"] - o.fuse(pooled, rounds, SIGMA, tau)["a"]["fused"]
        for tau in (0.0, 0.5, 2.0, 20.0)
    ]
    assert diffs == sorted(diffs), "loosening the anchors moves the gap monotonically toward the ring"
    assert diffs[-1] == pytest.approx(mean, abs=0.05)
    ring_only = o.fuse(pooled, rounds, SIGMA, 0.5, anchor_scale=0.0)
    assert ring_only["b"]["fused"] - ring_only["a"]["fused"] == pytest.approx(mean, abs=0.02)


def test_the_error_bar_of_a_difference_uses_the_covariance():
    """Two profiles the ring fought share evidence, so the SE of their difference is NOT
    the quadrature sum of their bars — it is smaller, because the rounds measured the
    difference directly. That is the whole reason the ring is worth fusing in."""
    pooled = {"a": {"overall": 70.0, "se": 0.5}, "b": {"overall": 70.0, "se": 0.5}}
    rounds = _rounds("a", "b", edge=0.0, n=30)
    fit = o.fuse(pooled, rounds, SIGMA, 0.5)
    naive = math.sqrt(fit["a"]["se"] ** 2 + fit["b"]["se"] ** 2)
    assert o.diff_se(fit, "a", "b") < naive
    # And tends toward the ring's own σ/√n as the anchors stop mattering.
    ring = o.fuse(pooled, rounds, SIGMA, 50.0)
    assert o.diff_se(ring, "a", "b") == pytest.approx(SIGMA / math.sqrt(30), rel=0.05)


def test_a_profile_the_ring_fought_but_the_field_never_scored_still_gets_a_number():
    pooled = {"a": {"overall": 70.0, "se": 0.1}}
    rounds = _rounds("a", "ghost", edge=+2.0, n=10)
    fit = o.fuse(pooled, rounds, SIGMA, 0.5)
    assert fit["ghost"]["anchored"] is False
    assert fit["ghost"]["fused"] - fit["a"]["fused"] == pytest.approx(
        sum(r["margin"] for r in rounds) / 10, abs=0.15)
    assert fit["ghost"]["se"] > fit["a"]["se"]


def test_rounds_to_separate_counts_head_to_head_rounds():
    # A 0.5 gap on a ±0.4 bar at σ_tie 2 needs the bar under 0.25: with σ_round 1.47,
    # 1/0.0625 − 1/0.16 = 9.75 precision units × 2.16 → 22 rounds.
    assert o.rounds_to_separate(0.5, 0.4, SIGMA, 2.0) == 22
    assert o.rounds_to_separate(0.5, 0.1, SIGMA, 2.0) == 0
    assert o.rounds_to_separate(0.0, 0.4, SIGMA, 2.0) is None
    assert o.rounds_to_separate(0.001, 0.4, SIGMA, 2.0) is None, "past the practical cap"


# ── The slack is measured ──────────────────────────────────────────────────────────────


def test_the_slack_is_measured_from_how_much_pooled_disagrees_with_the_ring():
    """Simulate a pooled record whose medians carry a hidden ±τ bias, and a ring that
    reads the true gaps. The estimator should recover τ, and read ~0 when there is none."""
    def _world(tau_true: float, seed: int) -> float:
        rng = random.Random(seed)
        truth = {f"p{i}": 70.0 + rng.uniform(-3, 3) for i in range(12)}
        fps = list(truth)
        pooled = {
            fp: {"overall": truth[fp] + rng.gauss(0, tau_true), "se": 0.05} for fp in fps
        }
        rounds: list[dict] = []
        for k in range(40):
            a, b = rng.sample(fps, 2)
            rounds += _rounds(a, b, truth[b] - truth[a], 8, seed=seed * 100 + k, session_id=k)
        return o.pooled_slack(pooled, o.pair_summaries(rounds), SIGMA)["tau"]

    def _mean(tau_true: float) -> float:
        seeds = range(1, 9)
        return sum(_world(tau_true, s) for s in seeds) / len(seeds)

    # One world of ~30 fought pairs is a noisy read of τ (it is a median of squares), so
    # the check is over eight worlds: no bias reads near zero, a point reads near a
    # point, and more bias reads as more.
    none, one, two = _mean(0.0), _mean(1.0), _mean(2.0)
    assert none < 0.3
    assert 0.7 < one < 1.3
    assert two > one > none


def test_too_few_fought_pairs_falls_back_to_the_default_and_says_so():
    pooled = {"a": {"overall": 70.0, "se": 0.1}, "b": {"overall": 71.0, "se": 0.1}}
    s = o.pooled_slack(pooled, o.pair_summaries(_rounds("a", "b", 0.0, 6)), SIGMA)
    assert s["basis"] == "default" and s["tau"] == o.DEFAULT_SLACK and s["pairs"] == 1


def test_rounds_fought_under_another_methodology_are_excluded_and_counted():
    sessions = [{
        "id": 1, "status": "complete", "finished_at": None, "matchups": [
            {"incumbent": "a", "challenger": "b", "verdict": "challenger",
             "methodology": "v-now", "deltas": [1.0, 1.2, 0.9]},
            {"incumbent": "a", "challenger": "c", "verdict": "draw",
             "methodology": "v-old", "deltas": [5.0, 5.0]},
            {"incumbent": "a", "challenger": "d", "verdict": "draw",
             "deltas": [0.1, 0.2]},                        # pre-stamp: kept, counted
            {"incumbent": "a", "challenger": "e", "verdict": "aborted",
             "methodology": "v-now", "deltas": [9.0]},      # produced no round
        ],
    }]
    rr = o.ring_rounds(sessions, "v-now")
    assert len(rr["rounds"]) == 5 and rr["matches"] == 2
    assert rr["excluded_methodology"] == 1 and rr["unstamped_matches"] == 1
    assert all(r["b"] != "e" for r in rr["rounds"])


# ── End to end: the endpoint and the crowning policy ───────────────────────────────────


def _ledger(matchups: list[dict]) -> None:
    with session_scope() as s:
        s.query(Duel).delete()
        s.add(Duel(status=DuelStatus.COMPLETE, finished_at=datetime.now(timezone.utc),
                   duration_s=600, matchups=matchups, champion_fingerprint=None))


def _clear_ledger() -> None:
    with session_scope() as s:
        s.query(Duel).delete()


def test_the_endpoint_ranks_off_the_rollup_and_the_ledger(client):
    """A pooled leader by a hair, and a ring that says the runner-up is a full point
    better over sixteen rounds: with the measured slack falling back to the default the
    ring should carry it, and the page must say what each record said alone."""
    _add("hi", 70.2, runs=8, spread=2.0)
    _add("lo", 70.0, runs=8, spread=2.0)
    _add("far", 60.0, runs=8, spread=2.0)
    deltas = [1.0 + random.Random(3).gauss(0, 0.8) for _ in range(16)]
    _ledger([{
        "incumbent": _fp("hi"), "challenger": _fp("lo"), "verdict": "challenger",
        "methodology": VERSION, "pairs": 16, "wins_incumbent": 3, "wins_challenger": 13,
        "deltas": deltas,
    }])
    try:
        r = client.get("/api/overall")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["best"]["fingerprint"] == _fp("lo")
        assert body["corners"]["pooled"]["fingerprint"] == _fp("hi")
        assert body["corners"]["ring"]["fingerprint"] == _fp("lo")
        assert body["corners"]["fused"]["fingerprint"] == _fp("lo")
        assert body["corners"]["agree"] is False
        assert body["inputs"]["ring_rounds"] == 16 and body["inputs"]["slack"]["basis"] == "default"
        rows = {p["fingerprint"]: p for p in body["profiles"]}
        assert rows[_fp("far")]["rounds"] == 0 and rows[_fp("far")]["fused"] == rows[_fp("far")]["pooled"]
        assert rows[_fp("lo")]["ring_pull"] > 0 and rows[_fp("hi")]["ring_pull"] < 0
        assert rows[_fp("lo")]["moved"] == 1 and rows[_fp("hi")]["moved"] == -1
        assert body["verdict"].startswith("Run ")
        assert "The ring changed who is on top." in body["verdict"]
        # The what-if: at zero slack the pooled record holds the crown.
        pinned = client.get("/api/overall", params={"slack": 0.0, "backtest": False}).json()
        assert pinned["best"]["fingerprint"] == _fp("hi")
        assert pinned["inputs"]["slack"]["basis"] == "what_if"
    finally:
        _clear_ledger()


def test_the_fused_policy_governs_and_falls_back_to_pooled(client):
    _add("only", 75.0, runs=6, spread=1.0)
    with session_scope() as s:
        save_config(s, {"crown_follow": {"policy": "fused"}})
    try:
        with session_scope() as s:
            assert crowning.active_policy(s) == "fused"
            out = crowning.resolve(s, pooled_best_fp="pooled-best")
        assert out["source"] == "fused" and out["fingerprint"] == _fp("only")
        assert out["fused"]["fused"] == pytest.approx(75.0)
    finally:
        with session_scope() as s:
            save_config(s, {"crown_follow": {"policy": "fused"}})


def test_the_fused_policy_is_the_shipped_default():
    from pathbrain.config_store import DEFAULT_CONFIG

    assert crowning.DEFAULT_POLICY == "fused"
    assert DEFAULT_CONFIG["crown_follow"]["policy"] == "fused"
    assert crowning.POLICIES[0] == "fused"


def test_the_slack_config_round_trips_and_pins_the_fit(client):
    _add("p", 70.0, runs=6, spread=1.0)
    try:
        r = client.put("/api/overall/config", json={"slack": 1.25})
        assert r.status_code == 200 and r.json()["slack"] == 1.25
        body = client.get("/api/overall", params={"backtest": False}).json()
        assert body["inputs"]["slack"]["basis"] == "config" and body["inputs"]["slack"]["tau"] == 1.25
        assert client.put("/api/overall/config", json={"slack": -1}).status_code == 400
        r = client.put("/api/overall/config", json={"slack": None})
        assert r.status_code == 200 and r.json()["slack"] is None
    finally:
        with session_scope() as s:
            save_config(s, {"overall_ranking": {"slack": None}})
