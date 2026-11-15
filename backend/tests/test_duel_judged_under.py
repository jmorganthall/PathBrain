"""S25: each duel row records the methodology version its verdicts were judged under, and a
past verdict is read under that version's rules (``methodology.judged_rules``), never the
current version's.

The rule change is simulated by registering a stricter version (the signed-rank test needs
six rounds, and a weather shift counts from 40 points): a stored duel judged under v17 keeps
its verdict and its reading, while a new match judged under the stricter version obeys it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from pathbrain import duel as duel_mod
from pathbrain import methodology as m
from pathbrain.database import _backfill_duel_judged_under, engine, session_scope
from pathbrain.models import Duel, DuelStatus

from .test_duel import _ring_session

V17 = "speed-smoothness-v17"
STRICT = "s25-stricter-rules"
FIVE_UP = [1.0, 2.0, 3.0, 4.0, 5.0]  # five rounds, all one way, distinct magnitudes
DIALS = dict(p1=0.7, alpha=0.10, min_margin=0.0, min_pairs=5, max_pairs=5, streak_wins=0)


@pytest.fixture()
def stricter(monkeypatch):
    monkeypatch.setitem(m.METHODOLOGY_REGISTRY, STRICT, {
        "verdict_rules": {"wilcoxon_min_pairs": 6, "round_weather_shift": 40.0},
    })
    m._verdict_rules.cache_clear()
    yield
    m._verdict_rules.cache_clear()


def _judge(deltas, rules=None):
    seat = duel_mod._Seat("cha", {}, "", "inc", **DIALS)
    if rules is not None:
        seat.sprt = duel_mod.SprtState(DIALS["p1"], DIALS["alpha"], rules)
        seat.paired = duel_mod.PairedEvidence(DIALS["alpha"], 0.0, 5, 5, rules=rules)
    for x in deltas:
        seat.deltas.append(x)
        seat.sprt.add_pair(x > 0)
        seat.paired.add(x)
    return duel_mod._adjudicate(seat, method="margins", min_pairs=5, max_pairs=5, min_margin=0.0)


def test_null_means_the_default_rules_and_a_version_means_its_own():
    assert m.judged_rules(None) is m.DEFAULT_VERDICT_RULES
    assert m.judged_rules(V17) == m.verdict_rules(V17)


def test_an_old_duel_keeps_its_verdict_and_a_new_match_uses_the_new_rule(stricter):
    # Judged under v17 at the time: five clean rounds clear the signed-rank test.
    with duel_mod.judging_under(V17):
        verdict, reason = _judge(FIVE_UP)
    assert verdict == "challenger" and reason.startswith("margins consistently one-sided")
    stored = {"incumbent": "inc", "challenger": "cha", "verdict": verdict, "reason": reason,
              "deltas": FIVE_UP, "methodology": V17, "weather_shifts": [30.0, 10.0, 35.0, 5.0, 0.0],
              "weather_shifted_rounds": 2}

    # The rules change: a new match under the stricter version cannot speak at five rounds.
    with duel_mod.judging_under(STRICT):
        new_verdict, new_reason = _judge(FIVE_UP)
    assert new_verdict == "challenger" and new_reason.startswith(duel_mod.NARROW_PREFIX)
    assert duel_mod.wilcoxon_p(FIVE_UP, rules=m.verdict_rules(STRICT)) == 1.0

    # The stored duel is read under the rules it was judged by, not the current ones.
    rules = m.judged_rules(V17)
    assert rules.wilcoxon_min_pairs == 4 and m.verdict_rules(STRICT).wilcoxon_min_pairs == 6
    assert _judge(stored["deltas"], rules) == (stored["verdict"], stored["reason"])

    # A ledger re-grade re-reads the rounds and leaves the verdict alone; a record with no
    # threshold of its own counts weather shifts by its judged rules (25), not the new 40.
    legs = [{"i": i, "challenger": 100 + i, "belt": [200 + i]} for i in range(5)]
    rec = dict(stored, round_legs=legs)
    overall = {**{100 + i: 60.0 + d for i, d in enumerate(FIVE_UP)}, **{200 + i: 60.0 for i in range(5)}}
    with duel_mod.judging_under(STRICT):
        res = duel_mod._regrade_record(rec, version=STRICT, overall=overall, crown={}, host={},
                                       model=None, now="2026-11-15T00:00:00+00:00")
    assert res["changed"] is True
    assert rec["verdict"] == verdict and rec["reason"] == reason
    assert rec["verdict_methodology"] == V17 and rec["methodology"] == STRICT
    assert rec["weather_shifted_rounds"] == 2


def test_a_session_records_the_version_it_judged_under(monkeypatch):
    scores = {"aaa0000000x": 70.0, "bbb0000000x": 62.0, "ccc0000000x": 55.0}
    d, _ = _ring_session(monkeypatch, scores, seats=1, belt_every=2)
    assert d.status == DuelStatus.COMPLETE, d.error
    assert d.matchups and d.judged_under
    assert {mm["methodology"] for mm in d.matchups} == {d.judged_under}
    assert duel_mod._JUDGING.get() is None  # the session's version does not leak out


def test_old_rows_are_backfilled_from_their_matches_once():
    with session_scope() as s:
        named = Duel(status=DuelStatus.COMPLETE, matchups=[
            {"incumbent": "a", "challenger": "b", "verdict": "draw", "methodology": "speed-smoothness-v12"},
            {"incumbent": "a", "challenger": "c", "verdict": "challenger",
             "methodology": "speed-smoothness-v16", "verdict_methodology": "speed-smoothness-v14"},
        ])
        silent = Duel(status=DuelStatus.COMPLETE, matchups=[{"incumbent": "a", "challenger": "b", "verdict": "draw"}])
        s.add_all([named, silent])
        s.flush()
        ids = (named.id, silent.id)
    try:
        with engine.begin() as conn:
            assert _backfill_duel_judged_under(conn) >= 1
        with engine.begin() as conn:
            again = _backfill_duel_judged_under(conn)  # idempotent: nothing new for these rows
            got = dict(conn.execute(text("SELECT id, judged_under FROM duels WHERE id IN (:a, :b)"),
                                    {"a": ids[0], "b": ids[1]}).all())
        assert again == 0
        # The last decided match's verdict version: the one it was re-graded from, if any.
        assert got == {ids[0]: "speed-smoothness-v14", ids[1]: None}
        with session_scope() as s:
            assert m.judged_rules(s.get(Duel, ids[1]).judged_under) is m.DEFAULT_VERDICT_RULES
    finally:
        with session_scope() as s:
            s.query(Duel).filter(Duel.id.in_(ids)).delete(synchronize_session=False)


def test_the_column_is_on_the_ledger_listing():
    with session_scope() as s:
        d = Duel(status=DuelStatus.COMPLETE, matchups=[], judged_under=V17)
        s.add(d)
        s.flush()
        rid = d.id
    try:
        with session_scope() as s:
            listed = {r["id"]: r for r in duel_mod._ledger_sessions(s, 200)}
        assert listed[rid]["judged_under"] == V17
    finally:
        with session_scope() as s:
            s.query(Duel).filter(Duel.id == rid).delete(synchronize_session=False)
