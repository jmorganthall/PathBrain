"""The ring names its WINNER off the fitted rating; the lineal belt still decides who
DEFENDS — and the config upgrade that carries the measured defaults onto an install.

Measured (200-400 simulated worlds at the live ring's round noise, sigma 1.47, six
profiles 0.1 points apart at the top): the Bradley-Terry #1 names the true best 7-11
points more often than the belt at every horizon past night 3; who defends (belt or
rating #1) makes no measurable difference (82 / 92 / 94 vs 82 / 92 / 96 at nights 10 /
20 / 30), so the belt keeps that job and the title stays winnable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain import crowning
from pathbrain import duel as duel_mod
from pathbrain.config_store import (
    CONFIG_KEY,
    DEFAULT_CONFIG,
    UPGRADES_KEY,
    applied_upgrades,
    get_config,
    save_config,
    upgrade_config,
)
from pathbrain.database import session_scope
from pathbrain.duel import (
    DEFAULT_ITERATIONS_PER_ROUND,
    FLOOR_RULE,
    LINEAL_RULE,
    PRIOR_ITERATIONS_PER_ROUND,
    RATING_RULE,
    belt_holder,
    defender_reference,
    ledger_leader,
    preset_for,
)
from pathbrain.models import AppConfig, Duel, DuelStatus


def _mu(inc, cha, verdict, *, wins_inc=6, wins_cha=4, delta=-2.0):
    return {
        "incumbent": inc, "challenger": cha,
        "incumbent_label": inc, "challenger_label": cha,
        "pairs": wins_inc + wins_cha, "wins_incumbent": wins_inc, "wins_challenger": wins_cha,
        "median_delta": delta, "llr_incumbent": 3.0, "llr_challenger": -3.0,
        "verdict": verdict, "reason": "test",
    }


# The belt goes to a newcomer on one clean win over the veteran; the sweeper builds the
# strongest record in the ring without ever meeting either holder.
def _split_ledger():
    return [
        _mu("veteran", "b", "incumbent", wins_inc=26, wins_cha=4, delta=-3.0),
        _mu("veteran", "c", "incumbent", wins_inc=24, wins_cha=5, delta=-3.0),
        _mu("sweeper", "newcomer", "incumbent", wins_inc=18, wins_cha=1, delta=-4.0),
        _mu("sweeper", "c", "incumbent", wins_inc=17, wins_cha=2, delta=-4.0),
        _mu("sweeper", "b", "incumbent", wins_inc=16, wins_cha=2, delta=-4.0),
        _mu("veteran", "newcomer", "challenger", wins_inc=0, wins_cha=3, delta=3.0),
    ]


def _with_ledger(matchups, champion="newcomer"):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        s.query(Duel).delete()
        s.add(Duel(status=DuelStatus.COMPLETE, duration_s=600, trigger="manual",
                   matchups=matchups, champion_fingerprint=champion, champion_label=champion,
                   finished_at=now - timedelta(hours=1)))


def _clear():
    with session_scope() as s:
        s.query(Duel).delete()


def test_the_defaults_are_the_measured_ones():
    d = DEFAULT_CONFIG["duel"]
    assert d["crown_rule"] == RATING_RULE == duel_mod.DEFAULT_CROWN_RULE
    assert d["iterations_per_round"] == DEFAULT_ITERATIONS_PER_ROUND == 5
    assert preset_for(d) == "balanced"
    assert duel_mod.CROWN_RULES == (RATING_RULE, LINEAL_RULE, FLOOR_RULE)


def test_the_ring_names_the_rating_leader_while_the_belt_holder_defends():
    _with_ledger(_split_ledger())
    try:
        with session_scope() as s:
            sessions = duel_mod._ledger_sessions(s)
            ratings = duel_mod.ledger_ratings(s)
            # What the ring NAMES: the whole-ledger rating, row 1 of the standings.
            fp, detail, why = belt_holder(sessions, ratings, RATING_RULE, 0.0)
            assert fp == "sweeper" and detail is None, "the champion does not hold the belt"
            assert "belt is with another profile" in why
            # Who DEFENDS: the lineal belt-holder, under the rating rule as under lineal.
            assert defender_reference(sessions, ratings, RATING_RULE, 0.0) == "newcomer"
            assert defender_reference(sessions, ratings, LINEAL_RULE, 0.0) == "newcomer"
            # The lineal rule names the belt itself, with its detail attached.
            fp, detail, _ = belt_holder(sessions, ratings, LINEAL_RULE, 0.0)
            assert fp == "newcomer" and detail is not None and detail["fingerprint"] == "newcomer"
            # And the floor rule still reads the conservative floor.
            assert belt_holder(sessions, ratings, FLOOR_RULE)[0] == ledger_leader(ratings)

            # The profile that walks into the ring is the belt-holder, not the champion.
            field = {"profiles": [{"fingerprint": f, "settings": None}
                                  for f in ("veteran", "sweeper", "newcomer", "b", "c")],
                     "best_fingerprint": "veteran"}
            defender, reason = duel_mod.select_incumbent(s, field, None, {}, ratings)
            assert defender == "newcomer" and "defends its title" in reason
            # `latest_champion` — what automation reads — is the rating leader.
            champ = duel_mod.latest_champion(s, max_age_days=30)
            assert champ is not None and champ["fingerprint"] == "sweeper" and champ["decisive"]
    finally:
        _clear()


def test_the_crowning_policy_applies_the_rating_leader():
    _with_ledger(_split_ledger())
    try:
        with session_scope() as s:
            save_config(s, {"crown_follow": {"policy": "duel"}})
            out = crowning.resolve(s, pooled_best_fp="pooled-one")
        assert out["source"] == "duel" and out["fingerprint"] == "sweeper"
    finally:
        _clear()
        with session_scope() as s:
            save_config(s, {"crown_follow": {"policy": "pooled"}})


def test_the_standings_report_the_belt_beside_the_champion():
    _with_ledger(_split_ledger())
    try:
        table = duel_mod.standings()
        assert table["crown_rule"] == RATING_RULE
        assert table["champion"]["fingerprint"] == table["standings"][0]["fingerprint"] == "sweeper"
        assert table["champion"]["holds_belt"] is False
        assert table["belt"]["fingerprint"] == "newcomer" and table["belt"]["is_champion"] is False
        assert table["belt"]["rank"] > 1
        newcomer = next(r for r in table["standings"] if r["fingerprint"] == "newcomer")
        assert newcomer.get("holds_belt") is True
        assert not table["standings"][0].get("holds_belt")
    finally:
        _clear()


def test_the_champion_is_row_one_by_the_tables_own_key():
    """`ledger_leader(sigma)` and the table sort share `_rank_key`, at any rank_sigma."""
    _with_ledger(_split_ledger())
    try:
        for sigma in (0.0, 0.5, 1.0, 2.0):
            with session_scope() as s:
                save_config(s, {"duel": {"rank_sigma": sigma}})
                ratings = duel_mod.ledger_ratings(s)
            table = duel_mod.standings()
            assert table["rank_sigma"] == sigma
            assert table["champion"]["fingerprint"] == table["standings"][0]["fingerprint"]
            assert ledger_leader(ratings, sigma=sigma) == table["standings"][0]["fingerprint"]
    finally:
        _clear()
        with session_scope() as s:
            save_config(s, {"duel": {"rank_sigma": 0.0}})


def test_the_ring_number_one_promotion_reads_the_standings_order(monkeypatch):
    """Under the rating rule the profile promoted to challenge the belt is the champion the
    ring names (the standings' order), not the floor leader."""
    _with_ledger(_split_ledger())
    try:
        with session_scope() as s:
            ratings = duel_mod.ledger_ratings(s)
            seen: dict = {}
            real = duel_mod._challenger_order

            def spy(field, ratings_, defender, **kw):
                seen["ring_leader_fp"] = kw.get("ring_leader_fp")
                return real(field, ratings_, defender, **kw)

            monkeypatch.setattr(duel_mod, "_challenger_order", spy)
            field = {"profiles": [{"fingerprint": f, "settings": None}
                                  for f in ("veteran", "sweeper", "newcomer", "b", "c")],
                     "best_fingerprint": "veteran"}
            heirs = {"items": []}
            duel_mod.next_challenger(s, field, ratings, "newcomer", heirs=heirs,
                                     cooldown_hours=0, rank_sigma=0.0)
            assert seen["ring_leader_fp"] == "sweeper"
    finally:
        _clear()


# ── The one-time config upgrade ──────────────────────────────────────────────────────


def _reset_upgrades():
    with session_scope() as s:
        for key in (CONFIG_KEY, UPGRADES_KEY):
            row = s.get(AppConfig, key)
            if row is not None:
                s.delete(row)
        s.commit()


def test_the_upgrade_moves_only_the_old_defaults_and_runs_once():
    _reset_upgrades()
    try:
        with session_scope() as s:
            # An install on the snap preset, three iterations a round and the lineal rule —
            # exactly the values the measurements replaced.
            save_config(s, {"duel": {"alpha": 0.10, "min_pairs": 3, "max_pairs": 12,
                                     "streak_wins": 3, "iterations_per_round": 3,
                                     "crown_rule": "lineal", "min_margin": 0.0, "hour": 4},
                            # …and on the pooled crowning policy, the old default the
                            # popover stored the moment a chip was pressed.
                            "crown_follow": {"policy": "pooled"}})
            applied = upgrade_config(s)
            assert applied == ["duel-rounds-of-five", "duel-balanced-stopping-rule",
                               "duel-winner-by-rating", "crown-policy-fused"]
            d = get_config(s)["duel"]
            assert d["iterations_per_round"] == 5 and d["crown_rule"] == "rating"
            assert preset_for(d) == "balanced"
            assert d["hour"] == 4 and d["min_margin"] == 0.0, "untouched fields stay"
            assert get_config(s)["crown_follow"]["policy"] == "fused"
            # Recorded, so the next start does nothing — even after a hand edit back.
            assert set(applied_upgrades(s)) == set(applied)
            save_config(s, {"duel": {"iterations_per_round": 3, "crown_rule": "lineal"},
                            "crown_follow": {"policy": "pooled"}})
            assert upgrade_config(s) == []
            d = get_config(s)["duel"]
            assert d["iterations_per_round"] == 3 and d["crown_rule"] == "lineal"
            assert get_config(s)["crown_follow"]["policy"] == "pooled"
    finally:
        _reset_upgrades()


def test_the_upgrade_leaves_a_deliberate_choice_alone():
    _reset_upgrades()
    try:
        with session_scope() as s:
            save_config(s, {"duel": {"alpha": 0.10, "min_pairs": 5, "max_pairs": 20,
                                     "streak_wins": 0,  # the quick preset
                                     "iterations_per_round": 4, "crown_rule": "rating_floor"},
                            "crown_follow": {"policy": "duel"}})
            assert upgrade_config(s) == []
            d = get_config(s)["duel"]
            assert preset_for(d) == "quick"
            assert d["iterations_per_round"] == 4 and d["crown_rule"] == "rating_floor"
            assert get_config(s)["crown_follow"]["policy"] == "duel"
            # Every upgrade is still recorded as considered.
            assert len(applied_upgrades(s)) == 4
    finally:
        _reset_upgrades()


def test_the_upgrade_leaves_a_fresh_install_alone():
    _reset_upgrades()
    try:
        with session_scope() as s:
            assert upgrade_config(s) == []
            assert s.get(AppConfig, CONFIG_KEY) is None, "nothing was written to config"
            assert len(applied_upgrades(s)) == 4
            d = get_config(s)["duel"]
            assert d["iterations_per_round"] == DEFAULT_ITERATIONS_PER_ROUND
            assert d["crown_rule"] == RATING_RULE and preset_for(d) == "balanced"
            assert PRIOR_ITERATIONS_PER_ROUND == 3
    finally:
        _reset_upgrades()
