"""The duel ledger re-grades with the record.

A match's margins are Overall points on the rubric they were read under, so the fused
ranking and the lever ledger could only ever exclude a match fought under another
methodology — a publish silently cost the ring every round it had. The legs are runs and
runs re-grade: each round now records which runs made it, and ``duel.regrade_ledger``
re-reads every margin from those runs under the current version.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy import delete

from pathbrain import challenger as challenger_mod
from pathbrain import duel as duel_mod
from pathbrain import host_adjust as H
from pathbrain import overall_ranking
from pathbrain.config_store import DEFAULT_CONFIG, save_config
from pathbrain.database import session_scope
from pathbrain.models import Duel, DuelStatus, Methodology, Run, RunStatus, Score

OLD, NEW = "ledger-regrade-test-v1", "ledger-regrade-test-v2"

# ── the mocked-engine harness (same shape as test_duel_methodology's) ───────────────────

ENV = {"scheduler": "fq_codel", "queues": 1, "upload_bandwidth": "40Mbit", "flows": 1024}


def _settings(quantum: int) -> list[dict]:
    return [{"label": "wan-download", **ENV, "quantum": quantum},
            {"label": "wan-upload", **ENV, "quantum": quantum}]


def _profile(fp: str, quantum: int) -> dict:
    return {"fingerprint": fp, "label": fp, "settings": _settings(quantum)}


def _no_settle():
    with session_scope() as s:
        save_config(s, {"duel": {**DEFAULT_CONFIG["duel"], "settle_seconds": 0, "seats": 1, "belt_every": 2}})


def _score_by_profile(monkeypatch, applied: list[str], scores: dict[str, float]):
    by_run: dict[int, str] = {}
    seq = {"n": 0}

    def fake_chunk(label, notes, iterations, teardown=True, job_group=None, job_group_total=None, **_):
        seq["n"] += 1
        run_id = 9500 + seq["n"]
        by_run[run_id] = applied[-1] if applied else ""
        return (run_id, True, iterations)

    monkeypatch.setattr(duel_mod, "run_chunk", fake_chunk)
    monkeypatch.setattr(duel_mod, "_run_overall", lambda run_id, ver: scores.get(by_run.get(run_id, ""), 0.0))
    return by_run


def _wait_finish(duel_id: int, timeout: float = 15.0) -> Duel:
    start = time.time()
    terminal = (DuelStatus.COMPLETE, DuelStatus.FAILED, DuelStatus.CANCELLED)
    while time.time() - start < timeout:
        with session_scope() as s:
            d = s.get(Duel, duel_id)
            if d and d.status in terminal:
                s.expunge(d)
                return d
        time.sleep(0.02)
    raise AssertionError("duel did not finish in time")


def _clear():
    with session_scope() as s:
        s.query(Duel).delete()


# ── 1. the ring records which runs made each round ─────────────────────────────────────


def test_a_match_record_names_the_runs_behind_every_round(monkeypatch):
    import pathbrain.api.routes_settings as rs

    _clear()
    applied: list[str] = []
    field = {"best_fingerprint": "incrl000000x",
             "profiles": [_profile("incrl000000x", 1514), _profile("charl000000x", 300)]}
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: dict(field))
    monkeypatch.setattr(rs, "_compute_heirs", lambda result, session, live=None: {"items": [{"fingerprint": "charl000000x"}]})
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: applied.append(fp))
    _no_settle()
    by_run = _score_by_profile(monkeypatch, applied, {"incrl000000x": 60.0, "charl000000x": 66.0})
    try:
        d = _wait_finish(duel_mod.start(duration_minutes=10))
        assert d.status == DuelStatus.COMPLETE, d.error
        m = d.matchups[0]
        legs = m["round_legs"]
        assert len(legs) == len(m["deltas"]) == m["pairs"] > 0
        for rl in legs:
            assert by_run[rl["challenger"]] == "charl000000x"
            assert rl["belt"] and all(by_run[b] == "incrl000000x" for b in rl["belt"])
            # Strict alternation: a challenger leg is flanked by one or two belt legs.
            assert 1 <= len(rl["belt"]) <= 2
    finally:
        _clear()


def test_open_match_snapshots_round_trip_the_leg_ids():
    seat = duel_mod._Seat("cha", {"label": "cha"}, "why", "inc", p1=0.7, alpha=0.05,
                          min_margin=0.0, min_pairs=3, max_pairs=10, streak_wins=0)
    seat.deltas, seat.raw_deltas = [1.0, 0.5], [1.2, 0.4]
    seat.round_legs = [{"challenger": 11, "belt": [10, 12]}, {"challenger": 13, "belt": [12, 14]}]
    snap = duel_mod._seat_snapshot(seat, 7, "v")
    back = duel_mod._seat_from_snapshot(snap, {"label": "cha"}, p1=0.7, alpha=0.05, min_margin=0.0,
                                        min_pairs=3, max_pairs=10, streak_wins=0)
    assert back.round_legs == seat.round_legs
    # A snapshot from before the ids were recorded resumes with none, never with a guess.
    del snap["round_legs"]
    assert duel_mod._seat_from_snapshot(snap, {"label": "cha"}, p1=0.7, alpha=0.05, min_margin=0.0,
                                        min_pairs=3, max_pairs=10, streak_wins=0).round_legs == []


# ── 2. the re-grade re-reads every round from its legs ─────────────────────────────────


def _ledger(session):
    """One session: belt B, challenger C, three rounds B C B C B C B, scored under OLD and
    (all but the last challenger leg) under NEW with different numbers."""
    when = datetime(2026, 10, 1, tzinfo=timezone.utc).replace(tzinfo=None)
    for v in (OLD, NEW):
        session.add(Methodology(version=v, rubric_version=v, derivation_version="derive-v14",
                                definition={"axes": [], "metrics": [], "overall": {"metrics": ["fcp", "lcp"]}},
                                is_current=False, created_at=when))
    legs = [("B", 60.0, 70.0), ("C", 62.0, 69.0), ("B", 60.0, 70.0), ("C", 62.0, 71.0),
            ("B", 60.0, 70.0), ("C", 62.0, None), ("B", 60.0, 70.0)]
    ids = []
    for fp, old_ov, new_ov in legs:
        run = Run(status=RunStatus.COMPLETE, settings_fingerprint=fp, created_at=when)
        session.add(run)
        session.flush()
        session.add(Score(run_id=run.id, methodology_version=OLD, comparability="exact",
                          axis_scores={"overall": old_ov}, subscores={"fcp": old_ov, "lcp": old_ov}))
        if new_ov is not None:
            session.add(Score(run_id=run.id, methodology_version=NEW, comparability="exact",
                              axis_scores={"overall": new_ov}, subscores={"fcp": new_ov + 1, "lcp": new_ov - 1}))
        ids.append(run.id)
    b0, c1, b2, c3, b4, c5, b6 = ids
    round_legs = [{"challenger": c1, "belt": [b0, b2]}, {"challenger": c3, "belt": [b2, b4]},
                  {"challenger": c5, "belt": [b4, b6]}]
    match = {
        "incumbent": "B", "challenger": "C", "methodology": OLD, "incumbent_label": "B",
        "challenger_label": "C", "pairs": 3, "wins_incumbent": 0, "wins_challenger": 3,
        "median_delta": 2.0, "deltas": [2.0, 2.0, 2.0], "raw_deltas": [2.0, 2.0, 2.0],
        "host_adjusted_rounds": 0, "host_adjust": {"active": False}, "round_legs": round_legs,
        "crown_deltas": {"fcp": [2.0, 2.0, 2.0]}, "median_crown_delta": {"fcp": 2.0},
        "weather_shifts": [1.0, 2.0, 3.0], "weather_shifted_rounds": 0, "weather_max_shift": 3.0,
        "weather_shift_threshold": 25.0, "leg_distances": [1, 1, 1], "max_leg_distance": 1,
        "verdict": "challenger", "reason": "margins consistently one-sided", "sessions": [1],
    }
    legacy = {"incumbent": "B", "challenger": "C", "methodology": OLD, "deltas": [1.0], "pairs": 1,
              "wins_incumbent": 0, "wins_challenger": 1, "verdict": "challenger", "reason": "x"}
    snap = {"challenger": "C", "incumbent": "B", "methodology": OLD, "deltas": [2.0, 2.0],
            "raw_deltas": [2.0, 2.0], "host_adjusted": 0, "round_legs": round_legs[:2],
            "weather_shifts": [None, None], "leg_distances": [1, 1], "legs": 2, "sessions": [1]}
    d = Duel(status=DuelStatus.COMPLETE, duration_s=60, run_ids=ids, matchups=[match, legacy],
             open_matches=[snap])
    session.add(d)
    session.flush()
    return d.id, ids


def _teardown(ids):
    with session_scope() as s:
        s.query(Duel).delete()
        s.execute(delete(Score).where(Score.run_id.in_(ids)))
        s.execute(delete(Run).where(Run.id.in_(ids)))
        s.execute(delete(Methodology).where(Methodology.version.in_([OLD, NEW])))


def test_the_ledger_is_re_read_from_its_legs_under_the_new_version(monkeypatch):
    _clear()
    # No host adjustment in this fixture: the arithmetic under test is the raw re-read.
    monkeypatch.setattr(H, "model_for", lambda session, ver: H.HostModel(None, 0, None, "off"))
    with session_scope() as s:
        duel_id, ids = _ledger(s)
    try:
        with session_scope() as s:
            summary = duel_mod.regrade_ledger(s, NEW)
        assert summary["version"] == NEW
        assert summary["matches"] == 2 and summary["matches_regraded"] == 1
        assert summary["matches_without_legs"] == 1          # the legacy record is left alone
        assert summary["rounds"] == 2 and summary["rounds_dropped"] == 1
        assert summary["open_matches_regraded"] == 1
        with session_scope() as s:
            d = s.get(Duel, duel_id)
            m, legacy = d.matchups
            snap = d.open_matches[0]
        # Under NEW: C 69 vs mean(70, 70) → −1; C 71 vs mean(70, 70) → +1; the third round's
        # challenger leg has no NEW score and is dropped, with every aligned series shortened.
        assert m["methodology"] == NEW and m["deltas"] == [-1.0, 1.0] == m["raw_deltas"]
        assert m["pairs"] == 2 and m["wins_challenger"] == 1 and m["wins_incumbent"] == 1
        assert m["median_delta"] == 0.0
        assert m["weather_shifts"] == [1.0, 2.0] and m["leg_distances"] == [1, 1]
        assert len(m["round_legs"]) == 2
        # The unreadable round is set aside with its own weather and distance, never forgotten.
        assert len(m["round_legs_dropped"]) == 1
        assert m["round_legs_dropped"][0]["i"] == 2 and m["round_legs_dropped"][0]["weather_shift"] == 3.0
        # The crown split is re-read from the NEW subscores (fcp = overall + 1 on every leg).
        assert m["crown_deltas"]["fcp"] == [-1.0, 1.0] and m["median_crown_delta"]["fcp"] == 0.0
        # The adjudication is the historical decision and is not touched.
        assert m["verdict"] == "challenger" and m["reason"] == "margins consistently one-sided"
        assert m["verdict_methodology"] == OLD
        assert m["regraded"]["from"] == OLD and m["regraded"]["rounds_dropped"] == 1
        assert legacy["methodology"] == OLD and legacy["deltas"] == [1.0] and "regraded" not in legacy
        # The open-match snapshot moved onto the new scale too, so a carried match resumes.
        assert snap["methodology"] == NEW and snap["deltas"] == [-1.0, 1.0]
        # Idempotent: a second pass finds nothing to change.
        with session_scope() as s:
            again = duel_mod.regrade_ledger(s, NEW)
        assert again["matches_regraded"] == 0 and again["matches_unchanged"] == 1
        assert again["open_matches_regraded"] == 0
        # Once the missing leg IS scored under NEW, the set-aside round comes back in order,
        # with its weather shift and distance restored beside it.
        with session_scope() as s:
            s.add(Score(run_id=ids[5], methodology_version=NEW, comparability="exact",
                        axis_scores={"overall": 73.0}, subscores={"fcp": 74.0, "lcp": 72.0}))
        with session_scope() as s:
            back = duel_mod.regrade_ledger(s, NEW)
            m = s.get(Duel, duel_id).matchups[0]
        assert back["matches_regraded"] == 1 and back["rounds"] == 3 and back["rounds_dropped"] == 0
        assert m["deltas"] == [-1.0, 1.0, 3.0] and m["pairs"] == 3 and m["wins_challenger"] == 2
        assert m["weather_shifts"] == [1.0, 2.0, 3.0] and m["leg_distances"] == [1, 1, 1]
        assert [x["i"] for x in m["round_legs"]] == [0, 1, 2] and "round_legs_dropped" not in m
        assert m["crown_deltas"]["fcp"] == [-1.0, 1.0, 3.0]  # 74 vs mean(71, 71)
        # And the fused ranking now counts the match under NEW instead of excluding it.
        with session_scope() as s:
            sessions_data = duel_mod._ledger_sessions(s)
        rounds = overall_ranking.ring_rounds(sessions_data, NEW)
        # Only the legacy record (no leg ids, still on OLD) is excluded; the re-graded match counts.
        assert rounds["excluded_methodology"] == 1 and rounds["matches"] == 1
        assert sorted(r["margin"] for r in rounds["rounds"] if r["b"] == "C") == [-1.0, 1.0, 3.0]
    finally:
        _teardown(ids)


def test_the_host_adjustment_is_refitted_under_the_new_version(monkeypatch):
    """A re-graded round is adjusted like a live one: with a model under the new version and
    host readings on the legs, ``deltas`` is the adjusted series and ``raw_deltas`` the raw."""
    _clear()
    seen: list[str] = []

    def fake_model(session, ver):
        seen.append(ver)
        return H.HostModel([-7.0, 0.0, 0.0, 0.0], 400, 0.4, "fixed")

    monkeypatch.setattr(H, "model_for", fake_model)
    with session_scope() as s:
        duel_id, ids = _ledger(s)
        # The challenger legs ran on a slower host (context setup 350 → 470 ms).
        for rid in ids:
            run = s.get(Run, rid)
            ctx = 470.0 if run.settings_fingerprint == "C" else 350.0
            run.instrument_health = {"readings": {"context_ms": ctx, "close_ms": 120.0,
                                                  "reads_ms": 2000.0, "nav_render_ms": 280.0}}
    try:
        with session_scope() as s:
            duel_mod.regrade_ledger(s, NEW)
            m = s.get(Duel, duel_id).matchups[0]
        assert seen == [NEW]
        assert m["raw_deltas"] == [-1.0, 1.0]
        # β = −7 on log(470/350) ≈ 0.2945 → the adjustment adds ≈ 2.06 to each margin.
        assert all(abs(a - r - 2.06) < 0.02 for a, r in zip(m["deltas"], m["raw_deltas"]))
        assert m["host_adjusted_rounds"] == 2 and m["host_adjust"]["active"] is True
    finally:
        _teardown(ids)
