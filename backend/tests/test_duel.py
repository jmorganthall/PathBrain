"""Tests for the duel ladder (sequential head-to-head adjudication) + crowning policy."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from pathbrain import challenger as challenger_mod
from pathbrain import crowning
from pathbrain import duel as duel_mod
from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.duel import SprtState
from pathbrain.models import Duel, DuelStatus
from pathbrain.rating import RANK_SIGMA


# ── The sequential stopping rule ──────────────────────────────────────────────────────


def test_sprt_decides_a_sweep_at_min_pairs():
    s = SprtState(p1=0.70, alpha=0.05)
    for _ in range(10):
        s.add_pair(challenger_won=True)
    assert s.decision(min_pairs=10, max_pairs=40) == "challenger"
    # And symmetrically for the incumbent.
    s2 = SprtState(p1=0.70, alpha=0.05)
    for _ in range(10):
        s2.add_pair(challenger_won=False)
    assert s2.decision(min_pairs=10, max_pairs=40) == "incumbent"


def test_sprt_never_decides_before_min_pairs():
    s = SprtState(p1=0.70, alpha=0.05)
    for _ in range(9):
        s.add_pair(challenger_won=True)
    assert s.decision(min_pairs=10, max_pairs=40) is None


def test_sprt_mutual_futility_is_an_early_draw():
    """Alternating wins drift BOTH walks negative — a 50/50 matchup exits early instead of
    burning the whole window."""
    s = SprtState(p1=0.70, alpha=0.05)
    verdict = None
    for i in range(40):
        s.add_pair(challenger_won=(i % 2 == 0))
        verdict = s.decision(min_pairs=10, max_pairs=40)
        if verdict:
            break
    assert verdict == "draw"
    assert s.pairs < 40  # decided by futility, not the cap


def test_sprt_cap_forces_a_draw():
    """A noisy near-55/45 stream that never crosses a boundary ends at max_pairs."""
    s = SprtState(p1=0.70, alpha=0.05)
    pattern = [True, True, False, True, False, False, True, False, True, False, False, True]
    verdict = None
    i = 0
    while verdict is None:
        s.add_pair(pattern[i % len(pattern)])
        verdict = s.decision(min_pairs=10, max_pairs=20)
        i += 1
    assert verdict == "draw"
    assert s.pairs <= 20


# ── The engine (mocked runs) ─────────────────────────────────────────────────────────


def _no_settle():
    """Turn off the post-apply settle for the mocked-engine tests — they fake the runs, so
    the only thing a real sleep would measure is the test suite's patience."""
    from pathbrain.config_store import save_config

    with session_scope() as s:
        save_config(s, {"duel": {"settle_seconds": 0}})


def _score_by_profile(monkeypatch, applied: list[str], scores: dict[str, float]):
    """Fake a pair's two runs, scoring each leg by the profile that was applied for it.

    Pairs alternate which side runs first, so run order is deliberately NOT a proxy for
    which profile ran — a fake keyed on position would bake in the exact confound the
    alternation exists to remove.
    """
    by_run: dict[int, str] = {}
    seq = {"n": 0}

    def fake_chunk(label, notes, iterations, teardown=True, job_group=None, job_group_total=None):
        seq["n"] += 1
        run_id = 9000 + seq["n"]
        by_run[run_id] = applied[-1] if applied else ""
        return (run_id, True, iterations)

    monkeypatch.setattr(duel_mod, "run_chunk", fake_chunk)
    monkeypatch.setattr(
        duel_mod, "_run_overall", lambda run_id, ver: scores.get(by_run.get(run_id, ""), 0.0)
    )
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


def test_duel_ladder_crowns_a_challenger_and_restores(monkeypatch):
    """Challenger beats the incumbent every pair → SPRT crowns it at min_pairs, the ladder
    ends (queue exhausted), the champion is recorded, and the baseline is restored."""
    import pathbrain.api.routes_settings as rs

    with session_scope() as s:
        s.query(Duel).delete()  # no rematch-cooldown carryover between tests
    applied: list[str] = []

    fake_field = {
        "best_fingerprint": "inc0000000x",
        "profiles": [
            {"fingerprint": "inc0000000x", "label": "incumbent", "settings": [{"label": "wan", "quantum": 1514}]},
            {"fingerprint": "cha0000000x", "label": "challenger", "settings": [{"label": "wan", "quantum": 300}]},
        ],
    }
    monkeypatch.setattr(rs, "compute_profiles", lambda session: fake_field)
    monkeypatch.setattr(
        rs, "_compute_heirs", lambda result, session, live=None: {"items": [{"fingerprint": "cha0000000x"}]}
    )
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: applied.append(fp))

    _no_settle()
    _score_by_profile(monkeypatch, applied, {"inc0000000x": 60.0, "cha0000000x": 66.0})

    duel_id = duel_mod.start(duration_minutes=10)
    d = _wait_finish(duel_id)

    assert d.status == DuelStatus.COMPLETE, d.error
    assert len(d.matchups) == 1
    m = d.matchups[0]
    assert m["verdict"] == "challenger"
    # A clean sweep is decided as soon as the evidence clears the bar — not at some fixed
    # pair count, which would only be re-asserting whatever the defaults happen to be.
    with session_scope() as s:
        cfg = get_config(s).get("duel", {})
    assert cfg["min_pairs"] <= m["pairs"] <= cfg["max_pairs"]
    assert m["wins_challenger"] == m["pairs"] and m["wins_incumbent"] == 0
    assert m["median_delta"] == 6.0
    assert d.champion_fingerprint == "cha0000000x"
    # Both sides were applied per pair, alternating.
    assert applied[:2] == ["inc0000000x", "cha0000000x"]
    assert not duel_mod.active()


def test_pairs_alternate_which_profile_runs_first(monkeypatch):
    """Counterbalancing, not hoping.

    The incumbent used to run first in every single pair, which makes "went first" and "is
    the incumbent" the same variable: any position-in-pair effect — state the previous run
    left behind, a still-warm cache, the shaper freshly reconfigured — lands on the same
    side every time and is indistinguishable from a real difference between the profiles.
    Alternating the lead (ABBA) cancels it instead of assuming it's zero, and the margin is
    still challenger minus incumbent, so the verdict doesn't care who ran first.
    """
    import pathbrain.api.routes_settings as rs

    with session_scope() as s:
        s.query(Duel).delete()
    applied: list[str] = []
    fake_field = {
        "best_fingerprint": "inc0000000x",
        "profiles": [
            {"fingerprint": "inc0000000x", "label": "incumbent", "settings": [{"label": "wan", "quantum": 1514}]},
            {"fingerprint": "cha0000000x", "label": "challenger", "settings": [{"label": "wan", "quantum": 300}]},
        ],
    }
    monkeypatch.setattr(rs, "compute_profiles", lambda session: fake_field)
    monkeypatch.setattr(
        rs, "_compute_heirs", lambda result, session, live=None: {"items": [{"fingerprint": "cha0000000x"}]}
    )
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: applied.append(fp))
    _no_settle()
    _score_by_profile(monkeypatch, applied, {"inc0000000x": 60.0, "cha0000000x": 66.0})

    d = _wait_finish(duel_mod.start(duration_minutes=10))
    assert d.status == DuelStatus.COMPLETE, d.error

    leads = applied[0::2]  # whoever was applied first in each pair
    assert len(leads) >= 4
    assert leads[0] == "inc0000000x" and leads[1] == "cha0000000x", "the lead must alternate"
    assert all(a != b for a, b in zip(leads, leads[1:])), "no side may lead twice running"
    # Each pair still ran both profiles, exactly once each.
    for i in range(0, len(applied) - 1, 2):
        assert set(applied[i : i + 2]) == {"inc0000000x", "cha0000000x"}
    # And the verdict is unaffected: the challenger scores better on every pair whichever
    # side happened to go first.
    assert d.matchups[0]["verdict"] == "challenger"
    assert d.matchups[0]["lead_alternated"] is True


def test_duel_margin_floor_records_a_draw(monkeypatch):
    """A statistically real but sub-margin edge (Δ ~0.4) is a draw *when the user asks for
    a floor*. The floor is opt-in now — by default a consistent win counts however small,
    matching the pooled crown — so this test sets it explicitly."""
    import pathbrain.api.routes_settings as rs

    from pathbrain.config_store import save_config

    with session_scope() as s:
        s.query(Duel).delete()  # no rematch-cooldown carryover between tests
        save_config(s, {"duel": {"min_margin": 1.0}})
    fake_field = {
        "best_fingerprint": "inc0000000x",
        "profiles": [
            {"fingerprint": "inc0000000x", "label": "incumbent", "settings": [{"label": "wan", "quantum": 1514}]},
            {"fingerprint": "cha0000000x", "label": "challenger", "settings": [{"label": "wan", "quantum": 300}]},
        ],
    }
    monkeypatch.setattr(rs, "compute_profiles", lambda session: fake_field)
    monkeypatch.setattr(
        rs, "_compute_heirs", lambda result, session, live=None: {"items": [{"fingerprint": "cha0000000x"}]}
    )
    applied: list[str] = []
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: applied.append(fp))
    _no_settle()
    _score_by_profile(monkeypatch, applied, {"inc0000000x": 60.0, "cha0000000x": 60.4})

    duel_id = duel_mod.start(duration_minutes=10)
    d = _wait_finish(duel_id)
    assert d.status == DuelStatus.COMPLETE, d.error
    m = d.matchups[0]
    assert m["verdict"] == "draw"
    assert "practically equal" in m["reason"]
    # The belt goes to the ring's #1, which is now the challenger: the bout is a DRAW for
    # verdict purposes (the margin floor says a 0.4-point edge isn't worth acting on), but
    # the rating is fitted to PAIRS and the challenger took every one of them. The two
    # mechanisms answer different questions on purpose — "is this difference worth acting
    # on?" vs "which profile is stronger?" — and the belt follows the standings, so it can
    # never name a profile the table underneath it doesn't have at #1. Note this only
    # arises when a practical floor is opted into; `min_margin` defaults to 0.
    assert d.champion_fingerprint == "cha0000000x"
    with session_scope() as s:
        save_config(s, {"duel": {"min_margin": 0.0}})  # back to the default (no floor)


# ── Crowning policy resolution ───────────────────────────────────────────────────────


def _seed_completed_duel(champion_fp: str, champion_label: str, decisive: bool, days_ago: float = 0.0) -> None:
    with session_scope() as s:
        # The champion is fitted over the WHOLE ledger, so a seeded scenario has to start
        # from an empty one — a leftover row from another test is extra evidence, and the
        # fit will (correctly) take it into account.
        s.query(Duel).delete()
        s.add(
            Duel(
                status=DuelStatus.COMPLETE,
                finished_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
                duration_s=600,
                # Pair counts matter: the champion is fitted from the pair record, so a
                # bout with a verdict but no pairs is no evidence at all (and the engine
                # never records one).
                matchups=[{
                    "incumbent": "someinc0000", "challenger": champion_fp,
                    "verdict": "challenger" if decisive else "draw",
                    "pairs": 12 if decisive else 10,
                    "wins_incumbent": 2 if decisive else 5,
                    "wins_challenger": 10 if decisive else 5,
                }],
                champion_fingerprint=champion_fp,
                champion_label=champion_label,
            )
        )


def test_crowning_policy_resolves_duel_champion_with_pooled_fallback(client):
    from pathbrain.config_store import save_config

    _seed_completed_duel("duelchamp0x", "duel champ", decisive=True)
    with session_scope() as s:
        save_config(s, {"crown_follow": {"policy": "duel"}})
    with session_scope() as s:
        out = crowning.resolve(s, pooled_best_fp="pooledbest0")
        assert out["policy"] == "duel"
        assert out["source"] == "duel"
        assert out["fingerprint"] == "duelchamp0x"

    # Pooled policy ignores the champion (but still reports it for the UI).
    with session_scope() as s:
        save_config(s, {"crown_follow": {"policy": "pooled"}})
    with session_scope() as s:
        out = crowning.resolve(s, pooled_best_fp="pooledbest0")
        assert out["source"] == "pooled" and out["fingerprint"] == "pooledbest0"
        assert out["duel_champion"] is not None

    # A stale champion (older than rematch_days) falls back to pooled even under "duel".
    with session_scope() as s:
        save_config(s, {"crown_follow": {"policy": "duel"}, "duel": {"rematch_days": 7}})
    with session_scope() as s:
        s.query(Duel).delete()
    _seed_completed_duel("oldchamp00x", "old champ", decisive=True, days_ago=30)
    with session_scope() as s:
        out = crowning.resolve(s, pooled_best_fp="pooledbest0")
        assert out["source"] == "pooled" and out["fingerprint"] == "pooledbest0"
    # Cleanup: back to the default policy.
    with session_scope() as s:
        save_config(s, {"crown_follow": {"policy": "pooled"}})
        s.query(Duel).delete()


def test_reconcile_marks_orphaned_duel_failed():
    with session_scope() as s:
        d = Duel(status=DuelStatus.RUNNING, duration_s=600, baseline=[])
        s.add(d)
        s.flush()
        did = d.id
    assert duel_mod.reconcile_interrupted_duels() >= 1
    with session_scope() as s:
        assert s.get(Duel, did).status == DuelStatus.FAILED
        s.query(Duel).delete()


def test_duel_endpoints(client):
    # Config roundtrip + validation.
    got = client.get("/api/duel/config").json()
    assert set(got) >= {"enabled", "hour", "minute", "timezone", "duration_minutes", "min_pairs"}
    upd = client.put("/api/duel/config", json={"hour": 4, "timezone": "America/Chicago"}).json()
    assert upd["hour"] == 4 and upd["timezone"] == "America/Chicago"
    assert client.put("/api/duel/config", json={"hour": 24}).status_code == 422
    assert client.put("/api/duel/config", json={"timezone": "Not/AZone"}).status_code == 422
    # Status/history are queryable when idle.
    assert "status" in client.get("/api/duel/status").json()
    assert "duels" in client.get("/api/duel/history").json()
    # Crown-follow surface carries the policy + choices.
    cf = client.get("/api/settings/crown-follow").json()
    assert cf["config"]["policy"] in ("pooled", "duel")
    assert set(cf["policies"]) == {"pooled", "duel"}
    assert "duel_champion" in cf
    out = client.post("/api/settings/crown-follow", json={"policy": "duel"}).json()
    assert out["config"]["policy"] == "duel"
    assert client.post("/api/settings/crown-follow", json={"policy": "nope"}).status_code == 400
    client.post("/api/settings/crown-follow", json={"policy": "pooled"})


# ── The head-to-head league table (the dueling-champions view) ───────────────────────


def _finished_duel(session, *, matchups, champion, when):
    d = Duel(
        status=DuelStatus.COMPLETE,
        duration_s=600,
        trigger="manual",
        matchups=matchups,
        champion_fingerprint=champion,
        champion_label=champion,
        finished_at=when,
    )
    session.add(d)
    return d


def _mu(inc, cha, verdict, *, wins_inc=6, wins_cha=4, delta=-2.0):
    return {
        "incumbent": inc,
        "challenger": cha,
        "incumbent_label": inc,
        "challenger_label": cha,
        "pairs": wins_inc + wins_cha,
        "wins_incumbent": wins_inc,
        "wins_challenger": wins_cha,
        "median_delta": delta,
        "llr_incumbent": 3.0,
        "llr_challenger": -3.0,
        "verdict": verdict,
        "reason": "SPRT boundary crossed",
    }


def test_a_one_opponent_record_does_not_top_the_ladder():
    """The reported defect, end to end: "why is the top profile ranked #1 with only one
    opponent?" — it was, because the table sorted on the fitted rating alone and a five-pair
    record can fit high. The standings rank on the conservative floor instead, so the thin
    record keeps its rating (and its row) but has to be measured before it can lead."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        s.query(Duel).delete()
        # A veteran with a long, winning record across several opponents.
        _finished_duel(
            s,
            matchups=[
                _mu("veteran", "opp1", "incumbent", wins_inc=6, wins_cha=3, delta=-2.0),
                _mu("veteran", "opp2", "incumbent", wins_inc=5, wins_cha=2, delta=-2.0),
                _mu("veteran", "opp3", "incumbent", wins_inc=5, wins_cha=2, delta=-2.0),
                _mu("veteran", "opp4", "incumbent", wins_inc=5, wins_cha=2, delta=-2.0),
            ],
            champion="veteran",
            when=now - timedelta(days=2),
        )
        # A newcomer that has fought exactly once, and won 4-1.
        _finished_duel(
            s,
            matchups=[_mu("opp1", "newcomer", "challenger", wins_inc=1, wins_cha=4, delta=3.0)],
            champion="newcomer",
            when=now,
        )

    out = duel_mod.standings()
    by_fp = {r["fingerprint"]: r for r in out["standings"]}
    assert by_fp["newcomer"]["opponents"] == 1
    assert by_fp["newcomer"]["rating_provisional"] is True
    # The fit is free to like it; the ladder is not free to crown it on that alone.
    assert by_fp["newcomer"]["rating_se"] > by_fp["veteran"]["rating_se"]
    assert out["standings"][0]["fingerprint"] == "veteran"
    assert by_fp["veteran"]["rating_floor"] > by_fp["newcomer"]["rating_floor"]


def test_standings_rank_by_head_to_head_record():
    """Ranking is earned in the ring — by the Bradley-Terry rating fitted to every pair,
    with the W-L-D / points columns kept beside it as the readable record."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        s.query(Duel).delete()
        # A beats B (A is incumbent, keeps the crown), then A draws C.
        _finished_duel(
            s,
            matchups=[
                _mu("aaa", "bbb", "incumbent", wins_inc=8, wins_cha=2, delta=-5.0),
                _mu("aaa", "ccc", "draw", wins_inc=5, wins_cha=5, delta=0.2),
            ],
            champion="aaa",
            when=now - timedelta(days=2),
        )
        # Newer session: C beats A — C ends as this session's champion.
        _finished_duel(
            s,
            matchups=[_mu("aaa", "ccc", "challenger", wins_inc=3, wins_cha=9, delta=4.0)],
            champion="ccc",
            when=now,
        )

    out = duel_mod.standings()
    by_fp = {r["fingerprint"]: r for r in out["standings"]}
    assert out["matchups_analyzed"] == 3
    assert out["decisive_matchups"] == 2

    # C beat A, and A beat B — so C rates above A above B, and the ledger columns still
    # read as before (C: 1 win + 1 draw = 4 points).
    assert [r["fingerprint"] for r in out["standings"]] == ["ccc", "aaa", "bbb"]
    assert out["ranked_by"] == "rating_floor"
    assert out["rank_sigma"] == RANK_SIGMA
    assert by_fp["ccc"]["rating"] > by_fp["aaa"]["rating"] > by_fp["bbb"]["rating"]
    assert (
        by_fp["ccc"]["rating_floor"]
        > by_fp["aaa"]["rating_floor"]
        > by_fp["bbb"]["rating_floor"]
    )
    assert by_fp["ccc"]["points"] == 4 and by_fp["ccc"]["win_rate"] == 1.0
    assert by_fp["aaa"]["wins"] == 1 and by_fp["aaa"]["losses"] == 1 and by_fp["aaa"]["draws"] == 1
    assert by_fp["bbb"]["points"] == 0 and by_fp["bbb"]["losses"] == 1
    assert by_fp["aaa"]["rank"] == 2

    # Margins are signed from each side's own point of view (stored delta is cha − inc).
    assert by_fp["bbb"]["median_margin"] == -5.0  # lost by 5 Overall points
    assert by_fp["ccc"]["median_margin"] == 2.1   # median of +0.2 (draw) and +4.0

    # Pair tallies mirror across the two sides of every matchup.
    assert by_fp["aaa"]["pair_wins"] == 8 + 5 + 3
    assert by_fp["aaa"]["pair_losses"] == 2 + 5 + 9

    # Opponent lists carry CALL SIGNS, resolved by fingerprint — so a row recorded before
    # naming (or before a rename) still reads under the name shown everywhere else.
    assert by_fp["aaa"]["beaten"] == [by_fp["bbb"]["name"]]
    assert by_fp["aaa"]["lost_to"] == [by_fp["ccc"]["name"]]
    assert all(" " in r["name"] for r in out["standings"])  # "Adjective Noun"
    assert len({r["name"] for r in out["standings"]}) == len(out["standings"])  # unique
    assert out["champion"]["name"] == by_fp["ccc"]["name"]
    assert out["head_to_head"]["aaa"]["ccc"] == {
        "wins": 0,
        "losses": 1,
        "draws": 1,
        "pairs": 22,
        "median_margin": -2.1,
    }

    # The reigning champion is the newest completed session's final incumbent.
    assert out["champion"]["fingerprint"] == "ccc"
    assert out["champion"]["consecutive_sessions"] == 1
    assert by_fp["ccc"]["is_champion"] and by_fp["ccc"]["championships"] == 1
    assert not by_fp["aaa"]["is_champion"]

    with session_scope() as s:
        s.query(Duel).delete()


def test_standings_empty_ledger_is_a_quiet_payload():
    with session_scope() as s:
        s.query(Duel).delete()
    out = duel_mod.standings()
    assert out["standings"] == [] and out["champion"] is None
    assert out["matchups_analyzed"] == 0


def test_standings_endpoint_and_stopping_rule_config(client):
    assert set(client.get("/api/duel/standings").json()) >= {
        "champion",
        "standings",
        "head_to_head",
        "matchups_analyzed",
    }
    # The page edits the stopping rule too — and the pair bounds can't cross.
    upd = client.put("/api/duel/config", json={"min_pairs": 8, "max_pairs": 30}).json()
    assert upd["min_pairs"] == 8 and upd["max_pairs"] == 30
    assert client.put("/api/duel/config", json={"max_pairs": 4}).status_code == 422
    assert client.put("/api/duel/config", json={"min_pairs": 1}).status_code == 422
    assert client.put("/api/duel/config", json={"min_margin": -1}).status_code == 422
    assert client.put("/api/duel/config", json={"min_margin": 2.5}).json()["min_margin"] == 2.5
    client.put("/api/duel/config", json={"min_pairs": 10, "max_pairs": 40, "min_margin": 1.0})


# ── Scheduling the window as a start/end pair ────────────────────────────────────────


def test_window_minutes_wraps_past_midnight():
    from pathbrain.api.routes_duel import _end_clock, _window_minutes

    assert _window_minutes(3, 0, 5, 30) == 150
    assert _window_minutes(23, 30, 1, 0) == 90  # crosses midnight
    assert _window_minutes(3, 0, 3, 0) == 0  # zero-length — the route rejects this
    assert _end_clock(3, 0, 150) == (5, 30)
    assert _end_clock(23, 30, 90) == (1, 0)


def test_schedule_accepts_an_end_time_and_derives_the_duration(client):
    # A start/end pair (what the page sends) sets the duration the engine counts down.
    out = client.put(
        "/api/duel/config", json={"hour": 22, "minute": 15, "end_hour": 1, "end_minute": 45}
    ).json()
    assert (out["hour"], out["minute"]) == (22, 15)
    assert (out["end_hour"], out["end_minute"]) == (1, 45)
    assert out["duration_minutes"] == 210  # wraps past midnight

    # The end time round-trips as a derived field even when only the duration is set.
    out = client.put("/api/duel/config", json={"duration_minutes": 60}).json()
    assert (out["end_hour"], out["end_minute"]) == (23, 15)

    # A zero-length window is refused rather than silently stored.
    assert (
        client.put("/api/duel/config", json={"end_hour": 22, "end_minute": 15}).status_code == 422
    )
    assert client.put("/api/duel/config", json={"end_hour": 24}).status_code == 422
    client.put("/api/duel/config", json={"hour": 3, "minute": 0, "duration_minutes": 120})


# ── Timezone handling (a missing tz database must not block saving a schedule) ───────


def test_timezone_validation_blames_the_name_only_when_it_can(monkeypatch):
    from pathbrain import timezones

    assert timezones.validate_timezone("America/Chicago") == "America/Chicago"
    assert timezones.validate_timezone("  ") == ""

    # With a tz database present, a bogus name is a real error.
    monkeypatch.setattr(timezones, "tzdata_available", lambda: True)
    try:
        timezones.validate_timezone("Not/AZone")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "unknown timezone" in str(exc)

    # Without one, NO name resolves — so a well-formed name is accepted (the schedule
    # stays saveable and falls back to container-local) and only garbage is refused.
    monkeypatch.setattr(timezones, "tzdata_available", lambda: False)
    monkeypatch.setattr(timezones, "zone_or_none", lambda name: None)
    assert timezones.validate_timezone("America/Chicago") == "America/Chicago"
    try:
        timezones.validate_timezone("not a zone!")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "not a valid IANA" in str(exc)


def test_schedule_zone_never_raises():
    from pathbrain.timezones import schedule_zone

    assert schedule_zone({"timezone": "America/Chicago"}) is not None
    assert schedule_zone({"timezone": "Not/AZone"}) is not None  # falls back, no raise
    assert schedule_zone({}) is not None


def test_browser_timezone_saves_on_the_duel_page(client):
    """The page sends its browser zone with every patch — that must never 422."""
    out = client.put("/api/duel/config", json={"timezone": "America/Chicago"}).json()
    assert out["timezone"] == "America/Chicago"
    client.put("/api/duel/config", json={"timezone": ""})


# ── Both crowns, side by side (the Dashboard readout) ────────────────────────────────


def test_crowns_endpoint_shows_both_verdicts(client):
    """The dashboard needs the pooled crown AND the duel champion, and to say which one
    automation actually follows."""
    from pathbrain.models import CrownEvent

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        s.query(Duel).delete()
        s.query(CrownEvent).delete()
        s.add(
            CrownEvent(
                kind="change",
                fingerprint="pooledwinner",
                previous_fingerprint="oldone",
                label="pooled winner",
                overall=87.5,
                created_at=now - timedelta(hours=5),
            )
        )
        _finished_duel(
            s,
            matchups=[_mu("duelwinner", "someoneelse", "incumbent", wins_inc=9, wins_cha=1, delta=-4.0)],
            champion="duelwinner",
            when=now - timedelta(hours=2),
        )

    out = client.get("/api/settings/crowns").json()
    assert out["pooled"]["fingerprint"] == "pooledwinner"
    assert out["pooled"]["overall"] == 87.5
    assert out["pooled"]["reign_hours"] >= 4.9  # reign measured from the ledger row
    assert out["duel"]["fingerprint"] == "duelwinner"
    assert (out["duel"]["wins"], out["duel"]["losses"]) == (1, 0)
    assert out["duel"]["fresh"] is True
    assert out["agree"] is False  # the two verdicts disagree — that's the point of showing both

    # Under the default policy the pooled crown governs; switching makes the duel govern.
    assert out["policy"] == "pooled" and out["governing"]["source"] == "pooled"
    client.post("/api/settings/crown-follow", json={"policy": "duel"})
    out = client.get("/api/settings/crowns").json()
    assert out["governing"]["source"] == "duel"
    assert out["governing"]["fingerprint"] == "duelwinner"
    client.post("/api/settings/crown-follow", json={"policy": "pooled"})

    with session_scope() as s:
        s.query(Duel).delete()
        s.query(CrownEvent).delete()


def test_crowns_endpoint_is_quiet_before_anything_is_crowned(client):
    from pathbrain.models import CrownEvent

    with session_scope() as s:
        s.query(Duel).delete()
        s.query(CrownEvent).delete()
    out = client.get("/api/settings/crowns").json()
    assert out["pooled"] is None and out["duel"] is None and out["agree"] is False


# ── Why a bout ends in a draw (the stopping rule's reach vs the pair cap) ─────────────


def test_sprt_requirements_expose_an_unwinnable_cap():
    """A cap below the rule's reach turns real winners into draws — the failure that made
    a whole night of duels come back "0 decisive"."""
    from pathbrain.duel import sprt_requirements

    # The reported case: p1=0.70, alpha=0.05, cap 15 → a winner needs 13 of 15 (87%), so
    # a profile taking 12 of 15 pairs (80%) is recorded as a draw.
    tight = sprt_requirements(0.70, 0.05, 5, 15)
    assert tight["sweep_pairs"] == 9  # fastest possible verdict is a 9-pair sweep
    assert tight["wins_needed"] == 13
    assert tight["restrictive"] is True

    s = SprtState(p1=0.70, alpha=0.05)
    for i in range(15):
        s.add_pair(challenger_won=i < 12)  # 12–3
    assert s.decision(min_pairs=5, max_pairs=15) == "draw"

    # The default cap is comfortably above the rule's reach.
    roomy = sprt_requirements(0.70, 0.05, 10, 40)
    assert roomy["wins_needed"] == 28 and roomy["restrictive"] is False

    # A cap below the sweep length can never decide anything at all.
    impossible = sprt_requirements(0.70, 0.05, 2, 5)
    assert impossible["wins_needed"] is None and impossible["restrictive"] is True


def test_duel_config_exposes_the_evidence_bar(client):
    out = client.get("/api/duel/config").json()
    assert "p1" in out and "alpha" in out
    assert {"sweep_pairs", "wins_needed", "win_rate_needed", "restrictive"} <= set(out["decision"])

    out = client.put("/api/duel/config", json={"p1": 0.8, "alpha": 0.1, "max_pairs": 20}).json()
    assert out["p1"] == 0.8 and out["alpha"] == 0.1
    # Under the default (margins) rule there is no win COUNT to hit — the margins decide.
    assert out["decision"]["wins_needed"] is None
    legacy = client.put("/api/duel/config", json={"method": "pair_wins"}).json()
    assert legacy["decision"]["wins_needed"] <= 20
    client.put("/api/duel/config", json={"method": "margins"})
    assert client.put("/api/duel/config", json={"p1": 0.4}).status_code == 422
    assert client.put("/api/duel/config", json={"alpha": 0.9}).status_code == 422
    client.put("/api/duel/config", json={"p1": 0.7, "alpha": 0.05, "max_pairs": 40})


# ── Magnitude-aware adjudication (the sensitivity fix) ───────────────────────────────


def test_wilcoxon_matches_the_exact_null_distribution():
    from pathbrain.duel import wilcoxon_p

    # n consistently one-sided pairs with distinct magnitudes: p = 1/2^n exactly.
    for n in (5, 6, 8, 10):
        deltas = [float(i + 1) for i in range(n)]
        assert abs(wilcoxon_p(deltas) - 1 / 2**n) < 1e-12
        assert wilcoxon_p(deltas, direction=-1) == 1.0  # the wrong direction proves nothing
    # Symmetric evidence is worthless in either direction.
    assert wilcoxon_p([1.0, -1.0, 2.0, -2.0, 3.0, -3.0]) > 0.4


def test_margins_decide_the_bout_the_sign_test_could_not():
    """The reported case: 12 pair wins to 3, with real margins. The sign test recorded a
    draw; the paired test calls it — because the margins were consistently one-sided."""
    from pathbrain.duel import PairedEvidence

    deltas = [2.0, 1.5, 3.0, 0.8, 2.2, 1.1, 4.0, 0.6, 1.9, 2.5, 1.2, 3.1, -0.4, -1.0, -0.2]

    sign = SprtState(p1=0.70, alpha=0.05)
    for d in deltas:
        sign.add_pair(d > 0)
    assert sign.decision(min_pairs=5, max_pairs=15) == "draw"  # what the user saw

    paired = PairedEvidence(alpha=0.05, min_margin=0.01, min_pairs=5, max_pairs=15)
    verdict = None
    for d in deltas:
        paired.add(d)
        verdict = verdict or paired.decision()
    assert verdict == "challenger"
    assert paired.p_value(1) < paired.nominal_alpha


def test_the_practical_floor_still_governs():
    """Sensitivity must not mean calling meaningless differences. A dead-consistent but
    tiny edge stays a draw — the floor is a separate question from significance."""
    from pathbrain.duel import PairedEvidence

    paired = PairedEvidence(alpha=0.05, min_margin=1.0, min_pairs=5, max_pairs=20)
    for _ in range(20):
        paired.add(0.05)  # unmistakably one-sided, and unmistakably irrelevant
    assert paired.decision() is None

    worth_it = PairedEvidence(alpha=0.05, min_margin=1.0, min_pairs=5, max_pairs=20)
    for i in range(10):
        worth_it.add(1.5 + i * 0.1)
    assert worth_it.decision() == "challenger"


def test_peek_correction_holds_the_false_positive_rate():
    """Peeking after every pair inflates false positives; the fitted penalty holds them
    near alpha. Simulated against true ties (fixed seed, so this is deterministic)."""
    import random

    from pathbrain.duel import PairedEvidence, peek_penalty

    assert peek_penalty(36) > peek_penalty(11) > peek_penalty(6) >= 2.0

    rng = random.Random(4242)
    false_calls = 0
    trials = 400
    for _ in range(trials):
        ev = PairedEvidence(alpha=0.05, min_margin=0.0, min_pairs=5, max_pairs=15)
        for _ in range(15):
            ev.add(rng.gauss(0.0, 1.5))  # no true difference at all
            if ev.decision() is not None:
                false_calls += 1
                break
    assert false_calls / trials <= 0.09  # ~5% target, sampling slack on 400 trials


def test_margins_are_far_more_sensitive_than_pair_wins():
    """The point of the change, measured: against a true 1-point edge with realistic run
    noise, the paired test calls the winner far more often than the sign test."""
    import random

    from pathbrain.duel import PairedEvidence

    rng = random.Random(99)
    sign_calls = paired_calls = 0
    trials = 300
    for _ in range(trials):
        deltas = [rng.gauss(1.0, 1.5) for _ in range(15)]

        sign = SprtState(p1=0.70, alpha=0.05)
        called = False
        for d in deltas:
            sign.add_pair(d > 0)
            if sign.decision(min_pairs=5, max_pairs=15) in ("challenger", "incumbent"):
                called = True
                break
        sign_calls += called

        ev = PairedEvidence(alpha=0.05, min_margin=0.01, min_pairs=5, max_pairs=15)
        called = False
        for d in deltas:
            ev.add(d)
            if ev.decision() is not None:
                called = True
                break
        paired_calls += called

    assert paired_calls > sign_calls * 1.5, (paired_calls, sign_calls)


def test_duel_config_carries_the_method(client):
    out = client.get("/api/duel/config").json()
    assert out["method"] == "margins"  # magnitude-aware by default
    assert set(out["methods"]) == {"margins", "pair_wins"}
    # The margins rule has no cap at which a verdict becomes unreachable.
    assert out["decision"]["restrictive"] is False
    assert out["decision"]["sweep_pairs"] >= 4

    legacy = client.put("/api/duel/config", json={"method": "pair_wins"}).json()
    assert legacy["method"] == "pair_wins"
    assert legacy["decision"]["wins_needed"] is not None  # back to a win-count rule
    assert client.put("/api/duel/config", json={"method": "vibes"}).status_code == 422
    client.put("/api/duel/config", json={"method": "margins"})


# ── One dial instead of six ──────────────────────────────────────────────────────────


def test_presets_round_trip_and_report_custom():
    from pathbrain.duel import PRESETS, preset_config, preset_for

    for name, preset in PRESETS.items():
        assert preset_for(preset_config(name)) == name
        # Each preset states a consequence, because a dial you can't predict is no
        # simpler than the six fields it replaced.
        assert preset["summary"] and preset["detail"] and preset["label"]
    # Hand-tuned numbers read back honestly rather than being snapped to a preset.
    assert preset_for({"alpha": 0.05, "min_pairs": 5, "max_pairs": 15}) == "custom"
    try:
        preset_config("nope")
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "unknown preset" in str(exc)


def test_preset_behaviour_matches_its_promise():
    """The presets advertise measured numbers; check they still hold (fixed seed)."""
    import random

    from pathbrain.duel import PRESETS, PairedEvidence

    def rate(cfg, effect, trials=400, seed=3):
        rng = random.Random(seed)
        hits = 0
        for _ in range(trials):
            ev = PairedEvidence(
            cfg["alpha"],
            0.0,
            cfg["min_pairs"],
            cfg["max_pairs"],
            streak_wins=cfg.get("streak_wins", 0),
        )
            for _ in range(cfg["max_pairs"]):
                ev.add(rng.gauss(effect, 1.5))
                if ev.decision() is not None:
                    hits += 1
                    break
        return hits / trials

    quick, strict = PRESETS["quick"], PRESETS["strict"]
    # Stricter settings must be harder to fool and no less able to find a real edge given
    # their longer window — that ordering is the whole point of the dial.
    assert rate(quick, 0.0) > rate(strict, 0.0)
    assert rate(strict, 1.0) >= rate(quick, 1.0) - 0.05
    assert rate(strict, 0.0) < 0.05
    assert rate(quick, 1.0) > 0.6


def test_preset_endpoint_sets_the_numbers(client):
    out = client.put("/api/duel/config", json={"preset": "strict"}).json()
    assert out["preset"] == "strict"
    assert (out["alpha"], out["min_pairs"], out["max_pairs"]) == (0.01, 12, 60)
    assert any(p["key"] == "strict" and p["summary"] for p in out["presets"])

    # A preset never touches the practical margin or the schedule — different questions.
    client.put("/api/duel/config", json={"min_margin": 0.5, "hour": 2})
    out = client.put("/api/duel/config", json={"preset": "quick"}).json()
    assert out["min_margin"] == 0.5 and out["hour"] == 2

    # Hand-editing a derived field reads back as custom rather than lying about a preset.
    out = client.put("/api/duel/config", json={"max_pairs": 17}).json()
    assert out["preset"] == "custom"
    assert client.put("/api/duel/config", json={"preset": "nope"}).status_code == 422
    client.put("/api/duel/config", json={"preset": "balanced", "min_margin": 1.0, "hour": 3})


def test_a_clean_streak_ends_a_bout_and_is_the_length_the_card_advertises():
    """"If it wins back to back, it wins" — true, at the length that isn't just luck.

    Two ways a bout can end on a streak: the length derived from the statistical threshold
    (1/2^n), or an explicit "N in a row wins" the user set. Either way the number printed on
    the preset card must be the number the engine actually acts on.
    """
    from pathbrain.duel import PRESETS, PairedEvidence, preset_config, streak_to_decide

    for name, preset in PRESETS.items():
        cfg = preset_config(name)
        n = streak_to_decide(
            cfg["alpha"], cfg["min_pairs"], cfg["max_pairs"], cfg.get("streak_wins", 0)
        )
        assert n is not None
        # The card's promise must be the code's behavior.
        assert f"{n} wins in a row" in preset["summary"], (name, preset["summary"], n)

        ev = PairedEvidence(
            cfg["alpha"],
            0.0,
            cfg["min_pairs"],
            cfg["max_pairs"],
            streak_wins=cfg.get("streak_wins", 0),
        )
        for i in range(n - 1):
            ev.add(1.0 + i * 0.1)
            assert ev.decision() is None, f"{name}: decided early at {i + 1} straight"
        ev.add(9.9)
        assert ev.decision() == "challenger", f"{name}: {n} straight should end it"


def test_a_consistent_win_counts_however_small_by_default():
    """The pooled crown has no margin floor — the duel shouldn't invent one. A profile
    that wins every pair by a hair is the winner unless the user says otherwise."""
    from pathbrain.config_store import DEFAULT_CONFIG
    from pathbrain.duel import PairedEvidence

    assert float(DEFAULT_CONFIG["duel"]["min_margin"]) == 0.0

    ev = PairedEvidence(alpha=0.05, min_margin=0.0, min_pairs=8, max_pairs=30)
    for i in range(8):
        ev.add(0.02 + i * 0.001)  # tiny, but relentlessly one-sided
    assert ev.decision() == "challenger"


def test_an_explicit_streak_rule_means_exactly_what_it_says():
    """"3 wins in a row wins it" has to mean 3 — not "3 unless some other field disagrees".

    On a nightly ladder this trade is deliberate: a verdict is cheap and self-correcting, so
    speed beats certainty per-bout. Measured against a true 1-point edge, 3-in-a-row names
    the better profile ~91% of the time and the worse one ~7%; between genuinely equal
    profiles it's a coin toss, which costs nothing because either answer is right.
    """
    from pathbrain.duel import PairedEvidence, streak_to_decide

    # The explicit rule overrides min_pairs — otherwise the field would be lying.
    ev = PairedEvidence(alpha=0.05, min_margin=0.0, min_pairs=10, max_pairs=30, streak_wins=3)
    ev.add(1.0)
    ev.add(1.2)
    assert ev.decision() is None
    ev.add(0.8)
    assert ev.decision() == "challenger"

    # A broken run resets it: 2 up, 1 down, 2 up is not 3 in a row.
    ev2 = PairedEvidence(alpha=0.05, min_margin=0.0, min_pairs=10, max_pairs=30, streak_wins=3)
    for d in (1.0, 1.0, -1.0, 1.0, 1.0):
        ev2.add(d)
        assert ev2.decision() is None
    ev2.add(1.0)  # now 3 straight
    assert ev2.decision() == "challenger"

    # It works for the holder too, and it's what the config readout reports.
    ev3 = PairedEvidence(alpha=0.05, min_margin=0.0, min_pairs=10, max_pairs=30, streak_wins=3)
    for d in (-1.0, -2.0, -0.5):
        ev3.add(d)
    assert ev3.decision() == "incumbent"
    assert streak_to_decide(0.05, 10, 30, streak_wins=3) == 3


def test_snap_preset_endpoint(client):
    out = client.put("/api/duel/config", json={"preset": "snap"}).json()
    assert out["preset"] == "snap"
    assert out["streak_wins"] == 3
    assert out["decision"]["streak_pairs"] == 3
    # A 1-win "streak" isn't a rule, it's a coin flip; refuse it rather than pretend.
    assert client.put("/api/duel/config", json={"streak_wins": 1}).status_code == 422
    assert client.put("/api/duel/config", json={"streak_wins": -1}).status_code == 422
    # Turning it off returns to the derived streak.
    out = client.put("/api/duel/config", json={"preset": "balanced"}).json()
    assert out["streak_wins"] == 0 and out["decision"]["streak_pairs"] == 8


# ── Racing the leaders, continuously ─────────────────────────────────────────────────


def test_queue_races_the_leaders_not_arbitrary_profiles():
    """A perpetual ladder is only worth running if it fights the matchups that can change
    the answer — the profiles nearest the crown, not whatever happens to be unmeasured."""
    from pathbrain.duel import build_queue

    field = {
        "profiles": [
            {"fingerprint": "crown", "overall": 90.0},
            {"fingerprint": "close", "overall": 89.0},
            {"fingerprint": "near", "overall": 88.0},
            {"fingerprint": "midfield", "overall": 70.0},
            {"fingerprint": "nodata", "overall": None},
        ]
    }
    # Everything reachable shows up in the heirs pass (that's where the filter lives).
    heirs = {"items": [{"fingerprint": fp} for fp in ("nodata", "midfield", "near", "close")]}

    leaders = build_queue(field, heirs, "crown", contenders="leaders", top_n=3)
    # Strongest contender first: the matchup most likely to change the answer.
    assert leaders[:3] == ["close", "near", "midfield"]
    assert leaders[-1] == "nodata"  # unmeasured profiles wait their turn, but aren't lost
    assert "crown" not in leaders  # the champion doesn't fight itself
    assert set(leaders) == {"close", "near", "midfield", "nodata"}  # nothing is lost

    # The exploring order is still available unchanged.
    assert build_queue(field, heirs, "crown", contenders="heirs") == [
        "nodata",
        "midfield",
        "near",
        "close",
    ]

    # Strong profiles that are NOT heirs must still be raced — heirs are by definition the
    # under-sampled and stale ones, so drawing contenders only from them was exactly the
    # "why is it duelling randoms?" bug.
    no_heirs = {"items": []}
    assert duel_mod.build_queue(field, no_heirs, "crown", contenders="leaders", top_n=3) == [
        "close",
        "near",
        "midfield",
    ]

    # A profile the live environment can't be set to never enters the queue.
    env_field = {
        "best_fingerprint": "crown",
        "profiles": [
            {"fingerprint": "crown", "overall": 90.0, "settings": [], "confident": True},
            {"fingerprint": "reachable", "overall": 88.0, "confident": True,
             "settings": [{"label": "wan", "scheduler": "fq_codel", "queues": 2}]},
            {"fingerprint": "elsewhere", "overall": 89.0, "confident": True,
             "settings": [{"label": "wan", "scheduler": "fq_pie", "queues": 8}]},
        ],
    }
    live = [{"label": "wan", "scheduler": "fq_codel", "queues": 2}]
    assert duel_mod.build_queue(
        env_field, {"items": []}, "crown", contenders="leaders", top_n=5, baseline=live
    ) == ["reachable"]


def test_a_well_measured_contender_outranks_a_thin_one():
    """Confidence first, then Overall: a profile with two runs and a lucky score is noise,
    not a contender — it queues behind the established ones (but still gets its turn)."""
    field = {
        "best_fingerprint": "crown",
        "profiles": [
            {"fingerprint": "crown", "overall": 90.0, "confident": True, "settings": []},
            {"fingerprint": "solid", "overall": 85.0, "confident": True, "settings": []},
            {"fingerprint": "fluke", "overall": 99.0, "confident": False, "settings": []},
        ],
    }
    queue = duel_mod.build_queue(field, {"items": []}, "crown", contenders="leaders", top_n=5)
    assert queue == ["solid", "fluke"]


def test_continuous_mode_waits_its_turn_and_leaves_a_gap(monkeypatch):
    """Continuous duelling must not hog the pipeline: it defers while anything else holds
    the coordinator, and leaves a configured gap between sessions."""
    import time as _time

    from pathbrain import coordinator, scheduler
    from pathbrain.config_store import save_config

    started: list[int] = []
    monkeypatch.setattr(scheduler, "_state", {})
    monkeypatch.setattr(duel_mod, "active", lambda: False)
    monkeypatch.setattr(duel_mod, "start", lambda minutes, trigger="manual": started.append(1))

    with session_scope() as s:
        save_config(s, {"duel": {"enabled": True, "continuous": True, "continuous_gap_minutes": 10}})
    try:
        # Something else is benchmarking → defer rather than queue up behind it.
        with coordinator.hold("test"):
            assert scheduler._maybe_run_duel() is False
        assert started == []

        # Free pipeline → run.
        assert scheduler._maybe_run_duel() is True
        assert len(started) == 1

        # A session that just finished holds the gap open.
        scheduler._state["duel_last_finished"] = _time.monotonic()
        assert scheduler._maybe_run_duel() is False
        scheduler._state["duel_last_finished"] = _time.monotonic() - 11 * 60
        assert scheduler._maybe_run_duel() is True
    finally:
        with session_scope() as s:
            save_config(s, {"duel": {"enabled": False, "continuous": False}})


def test_continuous_config_endpoint(client):
    out = client.put(
        "/api/duel/config", json={"continuous": True, "contenders": "leaders", "contender_top_n": 5}
    ).json()
    assert out["continuous"] is True and out["contenders"] == "leaders"
    assert out["contender_top_n"] == 5
    assert client.put("/api/duel/config", json={"contenders": "everyone"}).status_code == 422
    assert client.put("/api/duel/config", json={"contender_top_n": 0}).status_code == 422
    assert client.put("/api/duel/config", json={"continuous_gap_minutes": -1}).status_code == 422
    client.put("/api/duel/config", json={"continuous": False, "contender_top_n": 8})


# ── The fight card ("are we just racing randoms?") ───────────────────────────────────


def test_fight_card_lists_the_actual_queue(monkeypatch):
    """The line-up must be built by the same code the engine runs, so the page can't
    promise one order and the duel fight another."""
    import pathbrain.api.routes_settings as rs

    field = {
        "best_fingerprint": "crown0000001",
        "profiles": [
            {"fingerprint": "crown0000001", "label": "crown", "name": "Crown", "overall": 90.0, "iterations": 200},
            {"fingerprint": "close0000001", "label": "close", "name": "Close", "overall": 89.0, "iterations": 40},
            {"fingerprint": "mid000000001", "label": "mid", "name": "Mid", "overall": 60.0, "iterations": 30},
            {"fingerprint": "new000000001", "label": "new", "name": "New", "overall": None, "iterations": 2},
        ],
    }
    monkeypatch.setattr(rs, "compute_profiles", lambda session: field)
    monkeypatch.setattr(
        rs,
        "_compute_heirs",
        lambda result, session, live=None: {
            "items": [
                {"fingerprint": "new000000001", "reason": "untested"},
                {"fingerprint": "close0000001", "reason": "limited-data"},
                {"fingerprint": "mid000000001", "reason": "stale"},
            ]
        },
    )
    with session_scope() as s:
        s.query(Duel).delete()
        card = duel_mod.fight_card(s)

    assert card["incumbent"]["fingerprint"] == "crown0000001"
    assert card["incumbent"]["name"] == "Crown"
    # Leaders first: the profile nearest the crown leads, the unmeasured one waits.
    assert [c["fingerprint"] for c in card["queue"]][0] == "close0000001"
    assert [c["fingerprint"] for c in card["queue"]][-1] == "new000000001"
    # Every entry says why it's there and whether it's on cooldown.
    assert {c["reason"] for c in card["queue"]} == {"limited-data", "stale", "untested"}
    assert all(c["on_cooldown"] is False for c in card["queue"])
    assert card["contenders"] == "leaders"

    # A matchup already settled inside the rematch window is marked, not silently dropped.
    with session_scope() as s:
        _finished_duel(
            s,
            matchups=[_mu("crown0000001", "close0000001", "incumbent")],
            champion="crown0000001",
            when=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    with session_scope() as s:
        card = duel_mod.fight_card(s)
        s.query(Duel).delete()
    assert next(c for c in card["queue"] if c["fingerprint"] == "close0000001")["on_cooldown"] is True


def test_fight_card_endpoint_is_honest_when_there_is_nothing_to_race(client):
    out = client.get("/api/duel/card").json()
    assert set(out) >= {"incumbent", "queue", "contenders", "top_n"}
    if out["incumbent"] is None:
        assert out["reason"]  # says why, rather than showing an empty table


def test_the_holder_is_recorded_after_every_bout(monkeypatch):
    """On a ladder that runs for hours (or continuously), the belt has to reflect who holds
    it NOW — not who held it when the session finally ends."""
    from pathbrain.models import Duel as DuelModel

    with session_scope() as s:
        s.query(DuelModel).delete()

    # Two bouts: the challenger takes the belt in the first, the new holder keeps it in the
    # second. After each one, the row must name the current holder.
    holders: list[str | None] = []

    with session_scope() as s:
        d = DuelModel(status=DuelStatus.RUNNING, duration_s=600, matchups=[], baseline=[])
        s.add(d)
        s.flush()
        did = d.id

    for winner, expected in (("challenger", "chal"), ("incumbent", "chal")):
        with session_scope() as s:
            row = s.get(DuelModel, did)
            row.matchups = list(row.matchups or []) + [
                _mu("inc" if not row.champion_fingerprint else row.champion_fingerprint, "chal", winner)
            ]
            row.champion_fingerprint = (
                "chal" if winner == "challenger" else (row.champion_fingerprint or "inc")
            )
        with session_scope() as s:
            holders.append(s.get(DuelModel, did).champion_fingerprint)
        assert holders[-1] == expected

    # And the live status surfaces it while the session is still running.
    live = duel_mod.current()
    assert live["status"] == "running" and live["champion_fingerprint"] == "chal"
    assert len(live["matchups"]) == 2

    # The crowning policy is unaffected: it reads completed sessions only, so an
    # in-progress belt never drives a firewall write.
    with session_scope() as s:
        assert duel_mod.latest_champion(s, max_age_days=30) is None
        s.query(DuelModel).delete()


# ── Two-ledger discipline: duel RUNS pool, duel VERDICTS don't ───────────────────────


def test_duel_runs_feed_the_pooled_profile_history():
    """A duel's runs are ordinary runs: they count toward the profile's iterations and its
    pooled Overall, exactly like a monitoring or manual run.

    This is the half of the two-ledger rule that's easy to break by accident — one origin
    filter in the profile query and a night of duelling would stop counting toward the
    crown it was meant to inform.
    """
    from pathbrain.api.routes_settings import compute_profiles
    from pathbrain.models import Run, Score, ScoreResult

    from .test_settings import _seed_run

    with session_scope() as s:
        s.query(ScoreResult).delete()
        s.query(Score).delete()
        s.query(Run).delete()
        s.query(Duel).delete()

    when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
    _seed_run("pooledprofile", 80.0, when, label="monitoring")
    _seed_run("pooledprofile", 84.0, when + timedelta(minutes=5), label="duel · Speedy Sloth")
    _seed_run("pooledprofile", 82.0, when + timedelta(minutes=10), label="duel · Speedy Sloth")

    with session_scope() as s:
        field = compute_profiles(s)
    profile = next(p for p in field["profiles"] if p["fingerprint"] == "pooledprofile")
    # All three runs counted — the duel's two are not filtered out anywhere.
    assert profile["count"] == 3
    assert profile["iterations"] == 3
    assert profile["overall"] is not None

    # And a duel VERDICT changes nothing about the pooled ranking: the head-to-head ledger
    # sits beside the pooled record, never inside it.
    before = profile["overall"]
    with session_scope() as s:
        _finished_duel(
            s,
            matchups=[_mu("pooledprofile", "someoneelse", "challenger", delta=9.9)],
            champion="someoneelse",
            when=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    with session_scope() as s:
        after_field = compute_profiles(s)
    after = next(p for p in after_field["profiles"] if p["fingerprint"] == "pooledprofile")
    assert after["overall"] == before
    assert after["count"] == 3

    with session_scope() as s:
        s.query(ScoreResult).delete()
        s.query(Score).delete()
        s.query(Run).delete()
        s.query(Duel).delete()


# ── The champion defends its belt ────────────────────────────────────────────────────


def _field(*fps_overalls, best):
    return {
        "best_fingerprint": best,
        "profiles": [
            {"fingerprint": fp, "label": fp, "name": fp.title(), "overall": ov, "settings": []}
            for fp, ov in fps_overalls
        ],
    }


def test_the_ring_number_one_defends_not_last_sessions_survivor():
    """**The best-ranked dueling profile is what stands in the ring.**

    The belt used to go to whoever ended the previous session on top. That is not the same
    profile as the best one: a mid-table profile wins one bout, inherits the belt, and the
    ladder then spends the night defending IT — the "random duels not involving the crown"
    report, at its root. The defender is now the ring's own #1 by the conservative rating
    floor: the same number the standings rank on, so the belt and the top row can never
    name different profiles.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    field = _field(("strong", 80.0), ("pooled", 90.0), ("survivor", 70.0), best="pooled")

    with session_scope() as s:
        s.query(Duel).delete()
        # Nothing on the ledger — the pooled crown stands in until the ring has a verdict.
        fp, why = duel_mod.select_incumbent(s, field, None, {"rematch_days": 7})
        assert fp == "pooled" and "no profile has a ring record yet" in why

    with session_scope() as s:
        # "strong" builds a real record. "survivor" ends the LAST session holding the belt
        # on a single thin bout — under the old rule that alone made it the defender.
        _finished_duel(
            s,
            matchups=[
                _mu("strong", "survivor", "incumbent", wins_inc=12, wins_cha=2, delta=-4.0),
                _mu("strong", "pooled", "incumbent", wins_inc=11, wins_cha=3, delta=-3.0),
            ],
            champion="strong",
            when=now - timedelta(days=1),
        )
        _finished_duel(
            s,
            matchups=[_mu("survivor", "pooled", "incumbent", wins_inc=4, wins_cha=1, delta=-2.0)],
            champion="survivor",
            when=now - timedelta(hours=2),
        )

    with session_scope() as s:
        ratings = duel_mod.ledger_ratings(s)
        fp, why = duel_mod.select_incumbent(s, field, None, {"rematch_days": 7})
    assert fp == "strong", "the ring's #1 defends, not whoever survived the last session"
    assert "the ring's #1 defends" in why
    # The belt is exactly the standings' own ordering — not a second, parallel ranking.
    assert ratings["strong"]["rating_floor"] > ratings["survivor"]["rating_floor"]

    # And the profile the ring's #1 fights is the biggest threat to IT, chosen against its
    # rating — with the pooled crown first, since the two verdicts disagreeing is the most
    # informative bout available.
    heirs = {"items": [{"fingerprint": "survivor", "reason": "stale"}]}
    with session_scope() as s:
        cha, why_cha = duel_mod.next_challenger(
            s, field, ratings, "strong", heirs=heirs, rematch_days=0
        )
    assert cha == "pooled" and "pooled crown" in why_cha


def test_the_defender_is_re_read_from_the_ledger_between_bouts():
    """A challenger that beats the leader takes the belt *when its floor clears* — the same
    bar the standings apply — and then defends. That is what "constantly test the best
    profile" means operationally: no static queue, no winner-stays-on rule, just the ring's
    current #1 re-read before every bout.
    """
    field = _field(("leader", 80.0), ("climber", 70.0), ("filler", 60.0), best="filler")
    with session_scope() as s:
        s.query(Duel).delete()
        _finished_duel(
            s,
            matchups=[
                _mu("leader", "filler", "incumbent", wins_inc=14, wins_cha=3, delta=-4.0),
                _mu("leader", "climber", "incumbent", wins_inc=9, wins_cha=7, delta=-1.0),
            ],
            champion="leader",
            when=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1),
        )
    with session_scope() as s:
        before, _ = duel_mod.select_incumbent(s, field, None, {"rematch_days": 7})
    assert before == "leader"

    # The climber comes back and wins the rematch decisively.
    with session_scope() as s:
        _finished_duel(
            s,
            matchups=[
                _mu("leader", "climber", "challenger", wins_inc=2, wins_cha=18, delta=5.0),
                _mu("climber", "filler", "incumbent", wins_inc=15, wins_cha=2, delta=-4.0),
            ],
            champion="climber",
            when=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    with session_scope() as s:
        after, why = duel_mod.select_incumbent(s, field, None, {"rematch_days": 7})
        s.query(Duel).delete()
    assert after == "climber", "beating the leader on the ledger takes the belt"
    assert "the ring's #1 defends" in why


def test_a_ring_leader_the_environment_cant_reach_does_not_defend():
    """Who DEFENDS is reachability-filtered — an unreachable profile would abort on the
    first apply — so the ring's best *applicable* profile stands in instead."""
    field = {
        "best_fingerprint": "pooled",
        "profiles": [
            {"fingerprint": "unreachable", "label": "unreachable", "overall": 80.0,
             "settings": [{"label": "wan", "scheduler": "fq_codel", "queues": 8}]},
            {"fingerprint": "reachable", "label": "reachable", "overall": 75.0,
             "settings": [{"label": "wan", "scheduler": "fq_pie", "queues": 2}]},
            {"fingerprint": "pooled", "label": "pooled", "overall": 90.0, "settings": []},
        ],
    }
    live = [{"label": "wan", "scheduler": "fq_pie", "queues": 2}]
    with session_scope() as s:
        s.query(Duel).delete()
        _finished_duel(
            s,
            matchups=[
                # The unreachable profile has the best ring record by far.
                _mu("unreachable", "reachable", "incumbent", wins_inc=15, wins_cha=2, delta=-4.0),
                _mu("reachable", "pooled", "incumbent", wins_inc=12, wins_cha=4, delta=-3.0),
            ],
            champion="unreachable",
            when=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2),
        )
    with session_scope() as s:
        ratings = duel_mod.ledger_ratings(s)
        fp, why = duel_mod.select_incumbent(s, field, live, {"rematch_days": 7}, ratings)
        # Unfiltered, it really is the ring's #1 — it just can't be applied here.
        top, _ = duel_mod.ring_leader(field, ratings)
        s.query(Duel).delete()
    assert top == "unreachable"
    assert fp == "reachable", "the best profile the firewall can actually be set to defends"
    assert "the ring's #1 defends" in why


def test_the_reigning_champion_is_always_row_one_of_the_table():
    """The reported bug: the reigning duel champion did not match the table.

    It couldn't have: the badge read a stored `Duel.champion_fingerprint` written at
    session end (whoever survived that session), while the standings are fitted live over
    the whole ledger. So every row written before the belt became the ring's #1 named a
    survivor, and any bout in a RUNNING session moved the table without touching the badge.

    Both now come from the same fit, so disagreement is structurally impossible — pinned
    here in all three situations that used to break it.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def _check(where: str):
        out = duel_mod.standings()
        assert out["champion"] is not None, where
        assert out["champion"]["fingerprint"] == out["standings"][0]["fingerprint"], where
        assert out["standings"][0]["is_champion"] is True, where
        # The value AUTOMATION reads can never name a *different* profile than the page:
        # it either agrees with row 1 or reports nothing (a verdict too stale, too thin, or
        # not yet finished is a reason to fall back to the pooled crown — never a reason to
        # act on some other profile).
        with session_scope() as s:
            fresh = duel_mod.latest_champion(s, max_age_days=3650)
        assert fresh is None or fresh["fingerprint"] == out["champion"]["fingerprint"], where
        return out

    with session_scope() as s:
        s.query(Duel).delete()
        # A stored champion that is NOT the strongest profile — exactly what the old
        # winner-stays-on rule wrote into every historic row.
        _finished_duel(
            s,
            matchups=[
                _mu("strong", "weak", "incumbent", wins_inc=15, wins_cha=2, delta=-4.0),
                _mu("strong", "mid", "incumbent", wins_inc=12, wins_cha=4, delta=-3.0),
                _mu("mid", "weak", "incumbent", wins_inc=10, wins_cha=5, delta=-2.0),
            ],
            champion="weak",  # the stored belt-holder is the worst profile on the ledger
            when=now - timedelta(days=1),
        )
    try:
        out = _check("a stale stored champion must not override the table")
        assert out["champion"]["fingerprint"] == "strong"

        # A RUNNING session's bouts count immediately — the case no stored row could cover.
        with session_scope() as s:
            s.add(
                Duel(
                    status=DuelStatus.RUNNING,
                    duration_s=600,
                    trigger="manual",
                    matchups=[_mu("strong", "climber", "challenger", wins_inc=2, wins_cha=22,
                                  delta=5.0)],
                    champion_fingerprint="strong",
                )
            )
        out = _check("a bout in a running session moves the belt as well as the table")
        assert out["champion"]["fingerprint"] == "climber", (
            "beating the leader takes the top of the table, so it takes the belt too"
        )
        # Displayed immediately, but deliberately not actionable: automation still waits
        # for a completed session, so it reports nothing rather than acting on a holder
        # that is one bout old.
        with session_scope() as s:
            assert duel_mod.latest_champion(s, max_age_days=3650) is None
    finally:
        with session_scope() as s:
            s.query(Duel).delete()


def test_standings_carry_the_pooled_overall(monkeypatch):
    """Each ladder row shows the profile's all-history score beside its ring record — the
    two verdicts on one line, which is the whole point of running both."""
    from pathbrain import duel as dm

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        s.query(Duel).delete()
        _finished_duel(
            s,
            matchups=[_mu("winner", "loser", "incumbent", delta=-2.0)],
            champion="winner",
            when=now,
        )

    # A profile can win in the ring while sitting lower on the pooled score (and vice
    # versa) — the table has to be able to show exactly that.
    monkeypatch.setattr(
        dm, "_pooled_overalls", lambda session, fps: {"winner": (72.5, 40), "loser": (88.0, 300)}
    )
    out = dm.standings()
    by_fp = {r["fingerprint"]: r for r in out["standings"]}
    assert by_fp["winner"]["overall"] == 72.5 and by_fp["winner"]["pooled_iterations"] == 40
    assert by_fp["loser"]["overall"] == 88.0
    # Ring order is unchanged by the pooled score: the ladder still ranks on its own record.
    assert out["standings"][0]["fingerprint"] == "winner"

    # A profile with no comparable runs shows no score rather than a fabricated one.
    monkeypatch.setattr(dm, "_pooled_overalls", lambda session, fps: {})
    out = dm.standings()
    assert all(r["overall"] is None for r in out["standings"])

    with session_scope() as s:
        s.query(Duel).delete()


# ── Matchmaking regressions: the ladder must fight contenders, not filler ─────────────


def test_leaders_are_drawn_from_the_field_not_from_the_heirs_list():
    """The bug that wasted days of duelling.

    Reachability used to be inherited from the heirs pass, which quietly restricted the
    "leaders" pool to the heirs themselves — and heirs are BY DEFINITION the under-sampled,
    stale and untested profiles. So "race the leaders" raced exactly the thin profiles it
    existed to avoid, and the well-measured contenders just below the crown could never
    enter the ring at all.
    """
    field = {
        "best_fingerprint": "crown",
        "profiles": [
            {"fingerprint": "crown", "overall": 90.0, "confident": True, "settings": []},
            # Strong, well-measured, and NOT an heir (it has plenty of fresh data, so it
            # never appears in a list of profiles that need more).
            {"fingerprint": "contender", "overall": 89.5, "confident": True, "settings": []},
            {"fingerprint": "solid", "overall": 88.0, "confident": True, "settings": []},
            # A thin profile that IS an heir.
            {"fingerprint": "thin", "overall": 61.0, "confident": False, "settings": []},
        ],
    }
    heirs = {"items": [{"fingerprint": "thin", "reason": "limited-data"}]}

    queue = duel_mod.build_queue(field, heirs, "crown", contenders="leaders", top_n=8)
    assert queue[0] == "contender", "the strongest measured contender must fight first"
    assert queue[1] == "solid"
    assert queue.index("thin") > queue.index("solid"), "thin profiles queue behind contenders"
    # And every contender is reachable in the queue at all — the old code returned ["thin"].
    assert set(queue) == {"contender", "solid", "thin"}


def test_rematch_cooldown_uses_time_not_a_row_cap():
    """A continuous ladder finishes several sessions a day, so scanning "the last 20
    sessions" covered ~3 days of a 7-day cooldown and settled matchups came back early."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        s.query(Duel).delete()
        # The matchup we care about, decided 4 days ago…
        _finished_duel(
            s, matchups=[_mu("alpha", "beta", "incumbent")], champion="alpha",
            when=now - timedelta(days=4),
        )
        # …then buried under 30 more recent sessions.
        for i in range(30):
            _finished_duel(
                s, matchups=[_mu("gamma", f"other{i}", "draw")], champion="gamma",
                when=now - timedelta(hours=i),
            )

    with session_scope() as s:
        assert duel_mod._recently_decided(s, "alpha", "beta", 7) is True, "4d < 7d cooldown"
        assert duel_mod._recently_decided(s, "alpha", "beta", 2) is False, "4d > 2d cooldown"
        s.query(Duel).delete()


def test_every_bout_defends_the_current_leader_not_a_queue_decided_in_advance(monkeypatch):
    """The operating model, end to end: **always be running the bout most likely to unseat
    the best profile we have.**

    Three profiles, and the middle one is genuinely the strongest. The session used to
    build a queue once, hand the belt to whoever survived, and walk that queue to the end —
    so after one upset the ladder spent the rest of the night defending a profile that was
    no longer the best, against opponents chosen for a defender that had already left the
    ring. Here the ledger is refit before every bout, so the winner of bout 1 is what bout 2
    defends, and its opponent is chosen against *its* rating.
    """
    import pathbrain.api.routes_settings as rs

    with session_scope() as s:
        s.query(Duel).delete()
    applied: list[str] = []
    fake_field = {
        "best_fingerprint": "aaa0000000x",
        "profiles": [
            # Empty settings = reachable from any environment, so this test is about
            # matchmaking rather than the reachability filter (covered separately).
            {"fingerprint": "aaa0000000x", "label": "alpha", "overall": 90.0,
             "confident": True, "settings": []},
            {"fingerprint": "bbb0000000x", "label": "bravo", "overall": 80.0,
             "confident": True, "settings": []},
            {"fingerprint": "ccc0000000x", "label": "charlie", "overall": 70.0,
             "confident": True, "settings": []},
        ],
    }
    monkeypatch.setattr(rs, "compute_profiles", lambda session: fake_field)
    monkeypatch.setattr(
        rs,
        "_compute_heirs",
        lambda result, session, live=None: {
            "items": [{"fingerprint": "bbb0000000x"}, {"fingerprint": "ccc0000000x"}]
        },
    )
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: applied.append(fp))
    _no_settle()
    # bravo is the best profile; alpha only *looks* best on the pooled score.
    _score_by_profile(
        monkeypatch,
        applied,
        {"aaa0000000x": 60.0, "bbb0000000x": 68.0, "ccc0000000x": 55.0},
    )

    duel_id = duel_mod.start(duration_minutes=10)
    d = _wait_finish(duel_id)
    try:
        assert d.status == DuelStatus.COMPLETE, d.error
        bouts = list(d.matchups)
        assert len(bouts) >= 2, "the ladder should keep fighting after the first upset"

        # Bout 1: nothing on the ledger, so the pooled crown stands in and loses to bravo.
        assert bouts[0]["incumbent"] == "aaa0000000x"
        assert bouts[0]["challenger"] == "bbb0000000x"
        assert bouts[0]["verdict"] == "challenger"

        # Bout 2 defends the WINNER — re-read from the ledger, not carried over by a
        # "winner stays on" rule and not taken from a queue built before bout 1.
        assert bouts[1]["incumbent"] == "bbb0000000x", (
            "the profile that just beat the leader is the leader, so it defends next"
        )
        assert bouts[1]["challenger"] == "ccc0000000x"

        # Every bout involves the current best profile — never two also-rans.
        assert all("bbb0000000x" in (b["incumbent"], b["challenger"]) for b in bouts[1:])
        # And the belt on the page is the ring's #1, so it agrees with the standings.
        assert d.champion_fingerprint == "bbb0000000x"
        with session_scope() as s:
            table = duel_mod.standings()["standings"]
        assert table[0]["fingerprint"] == "bbb0000000x"
    finally:
        with session_scope() as s:
            s.query(Duel).delete()


def test_a_challenger_that_wins_without_taking_the_belt_gets_its_rematch(monkeypatch):
    """The reported oddity: "two profiles beat this one but it keeps the belt".

    Beating the leader only takes the belt when it lifts your floor above its — so a thin
    challenger can genuinely win a bout and the leader still ranks first. That part is
    correct. What was wrong is what happened next: the pair went into the already-fought
    set for the rest of the session, so the profile with the single strongest claim to the
    belt was set aside and couldn't finish the job, while the leader went on to defend
    against fresher, weaker challengers.

    A win without a belt change is now re-opened — once — so the bout that would settle it
    actually gets run.
    """
    import pathbrain.api.routes_settings as rs

    with session_scope() as s:
        s.query(Duel).delete()
        # The leader arrives with a deep record AND a long winning head-to-head over the
        # upstart, so one win — even a clean sweep — cannot lift the upstart's floor past
        # it. (Checked numerically: after a 9-0 win the upstart sits at floor ~1504 against
        # the leader's ~1610.) This is the live situation the report came from.
        _finished_duel(
            s,
            matchups=[
                _mu("leader00000", f"opp{i}", "incumbent", wins_inc=12, wins_cha=4, delta=-3.0)
                for i in range(6)
            ]
            + [
                _mu("leader00000", "upstart0000", "incumbent", wins_inc=16, wins_cha=2, delta=-3.0)
            ],
            champion="leader00000",
            when=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30),
        )

    applied: list[str] = []
    fake_field = {
        "best_fingerprint": "leader00000",
        "profiles": [
            {"fingerprint": "leader00000", "label": "leader", "overall": 90.0,
             "confident": True, "settings": []},
            {"fingerprint": "upstart0000", "label": "upstart", "overall": 80.0,
             "confident": True, "settings": []},
        ]
        + [
            {"fingerprint": f"opp{i}", "label": f"opp{i}", "overall": 60.0,
             "confident": True, "settings": []}
            for i in range(6)
        ],
    }
    monkeypatch.setattr(rs, "compute_profiles", lambda session: fake_field)
    monkeypatch.setattr(rs, "_compute_heirs", lambda result, session, live=None: {"items": []})
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: applied.append(fp))
    _no_settle()
    # The upstart is genuinely better and wins every pair it plays.
    scores = {"leader00000": 60.0, "upstart0000": 64.0}
    scores.update({f"opp{i}": 50.0 for i in range(6)})
    _score_by_profile(monkeypatch, applied, scores)

    duel_id = duel_mod.start(duration_minutes=10)
    d = _wait_finish(duel_id)
    try:
        assert d.status == DuelStatus.COMPLETE, d.error
        bouts = list(d.matchups)
        pairs = [frozenset((b["incumbent"], b["challenger"])) for b in bouts]
        upstart_bouts = [
            i for i, b in enumerate(bouts)
            if "upstart0000" in (b["incumbent"], b["challenger"])
        ]
        assert len(upstart_bouts) >= 2, (
            "a profile that beat the belt must get to come back and finish the job, "
            f"not be set aside for the session — bouts were {pairs}"
        )
        # The rematch says why it's in the ring, so the tape explains itself.
        assert any(
            "rematch" in (b.get("challenger_why") or "") for b in bouts
        ), "the rematch should be labelled as one"
        # …and only once: the ladder must not ping-pong on a single pair.
        leader_upstart = frozenset(("leader00000", "upstart0000"))
        assert pairs.count(leader_upstart) <= 2
    finally:
        with session_scope() as s:
            s.query(Duel).delete()


def test_the_cooldown_reorders_contenders_it_does_not_hand_the_ring_to_filler():
    """The bug behind "yet again — random duels not involving the crown".

    The rematch cooldown was an *exclusion*: a contender already fought inside the window
    was set aside, and the loop moved on to the next entry in the queue. On a ladder that
    runs continuously that is catastrophic, because the top of the queue is exactly what
    gets fought first and therefore exactly what goes on cooldown first. Within a day or
    two every crown-and-leaders matchup is cooled, and the only entries left un-fought are
    the ones nobody has ever raced — the unmeasured tail. So a mode built to race the
    leaders spent every night racing filler, and the pooled crown, first in the queue by
    design, was the very first profile pushed out of the ring.

    The cooldown now orders *within* a priority tier instead of falling through to a lower
    one: re-confirming the most informative matchup beats a first look at a profile the
    field already ranks far below the crown.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    field = {
        "best_fingerprint": "crown",
        "profiles": [
            {"fingerprint": "belt", "overall": 91.0, "confident": True, "settings": []},
            {"fingerprint": "crown", "overall": 90.0, "confident": True, "settings": []},
            {"fingerprint": "contender", "overall": 89.0, "confident": True, "settings": []},
            {"fingerprint": "filler", "overall": 40.0, "confident": False, "settings": []},
        ],
    }
    queue = duel_mod.build_queue(field, {"items": []}, "belt", contenders="leaders", top_n=8)
    assert queue[0] == "crown", "the pooled crown challenges the belt first"
    tiers = duel_mod.contender_tiers(field, queue)
    assert tiers["crown"] < tiers["contender"] < tiers["filler"]

    with session_scope() as s:
        s.query(Duel).delete()
        # Yesterday's session already fought both of the matchups that matter.
        _finished_duel(
            s,
            matchups=[_mu("belt", "crown", "incumbent"), _mu("belt", "contender", "incumbent")],
            champion="belt",
            when=now - timedelta(days=1),
        )

    try:
        # Walk the ladder the way the engine does — pick, fight, pick again — with each
        # fought pair excluded from the next choice.
        picks = []
        fought: set = set()
        with session_scope() as s:
            for _ in range(3):
                fp, why = duel_mod.next_challenger(
                    s, field, {}, "belt", heirs={"items": []}, rematch_days=7,
                    mode="leaders", fought=fought,
                )
                if fp is None:
                    break
                fought.add(frozenset(("belt", fp)))
                picks.append((fp, why))
        order = [fp for fp, _ in picks]
        # The old behaviour: ["filler"] first, with the crown and the contender set aside.
        assert order == ["crown", "contender", "filler"], (
            "a cooled crown must be re-raced before an unmeasured profile gets the ring"
        )
        assert "re-rac" in picks[0][1], "and the reason says it's a re-race, not a fresh matchup"
    finally:
        with session_scope() as s:
            s.query(Duel).delete()


def test_a_fresh_matchup_still_beats_a_re_race_within_the_same_tier():
    """The cooldown hasn't been thrown away — it still decides the order among equals."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    field = {
        "best_fingerprint": "belt",  # the crown is the one defending, so no tier-0 entry
        "profiles": [
            {"fingerprint": "belt", "overall": 91.0, "confident": True, "settings": []},
            {"fingerprint": "fought", "overall": 90.0, "confident": True, "settings": []},
            {"fingerprint": "unfought", "overall": 89.0, "confident": True, "settings": []},
        ],
    }
    queue = duel_mod.build_queue(field, {"items": []}, "belt", contenders="leaders", top_n=8)
    assert queue == ["fought", "unfought"]  # by Overall, strongest first

    with session_scope() as s:
        s.query(Duel).delete()
        _finished_duel(
            s, matchups=[_mu("belt", "fought", "incumbent")], champion="belt",
            when=now - timedelta(days=1),
        )

    try:
        with session_scope() as s:
            first, why = duel_mod.next_challenger(
                s, field, {}, "belt", heirs={"items": []}, rematch_days=7, mode="leaders"
            )
            # Same tier, so the cooldown decides: the unasked question goes first.
            assert first == "unfought" and "re-rac" not in why
            # The cooled contender is still raced, just after — never dropped.
            second, why2 = duel_mod.next_challenger(
                s, field, {}, "belt", heirs={"items": []}, rematch_days=7, mode="leaders",
                fought={frozenset(("belt", "unfought"))},
            )
        assert second == "fought" and "re-rac" in why2
    finally:
        with session_scope() as s:
            s.query(Duel).delete()


def test_beating_the_leader_moves_you_up_the_standings_more_than_beating_the_tail():
    """The reason the league table was replaced.

    Under match points a win is three points whether you beat the profile at the top of
    the ladder or the one at the bottom, and the leader loses nothing for losing — so a
    profile could beat the #1 and stay at #4. Ranking on the fitted Bradley-Terry
    strength makes "who you beat" the thing that moves you, which is what a ladder is
    supposed to measure.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def _standings_after(extra):
        with session_scope() as s:
            s.query(Duel).delete()
            _finished_duel(
                s,
                matchups=[
                    # "leader" has beaten everyone; "tail" has beaten nobody.
                    _mu("leader", "middle", "incumbent", wins_inc=12, wins_cha=3),
                    _mu("leader", "other", "incumbent", wins_inc=12, wins_cha=3),
                    _mu("middle", "tail", "incumbent", wins_inc=12, wins_cha=3),
                    _mu("other", "tail", "incumbent", wins_inc=12, wins_cha=3),
                    # Two identical challengers, each level with the same middle profile.
                    _mu("middle", "climber", "draw", wins_inc=6, wins_cha=6),
                    _mu("other", "padder", "draw", wins_inc=6, wins_cha=6),
                ]
                + extra,
                champion="leader",
                when=now - timedelta(days=1),
            )
        return {r["fingerprint"]: r for r in duel_mod.standings()["standings"]}

    try:
        before = _standings_after([])
        assert abs(before["climber"]["rating"] - before["padder"]["rating"]) < 1.0

        # Same scoreline, same number of pairs, opposite ends of the ladder. A points
        # table awards both exactly three points and calls it even.
        after = _standings_after(
            [
                _mu("leader", "climber", "challenger", wins_inc=2, wins_cha=8),
                _mu("tail", "padder", "challenger", wins_inc=2, wins_cha=8),
            ]
        )
        assert after["climber"]["points"] == after["padder"]["points"], "the old table saw a tie"
        climb = after["climber"]["rating"] - before["climber"]["rating"]
        pad = after["padder"]["rating"] - before["padder"]["rating"]
        assert climb > pad, "beating the leader must move you more than beating the tail"
        assert after["climber"]["rank"] < after["padder"]["rank"]
        # And beating the leader is enough to pass the profiles it beat.
        assert after["climber"]["rank"] < after["middle"]["rank"]
    finally:
        with session_scope() as s:
            s.query(Duel).delete()


def test_a_thin_record_is_flagged_provisional_rather_than_crowned():
    """One snap bout shouldn't put a profile on top of a ladder full of veterans."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        s.query(Duel).delete()
        _finished_duel(
            s,
            matchups=[
                _mu("veteran", "solid", "incumbent", wins_inc=30, wins_cha=10),
                _mu("veteran", "weak", "incumbent", wins_inc=18, wins_cha=4),
                _mu("solid", "weak", "incumbent", wins_inc=20, wins_cha=5),
                # A newcomer's entire record: one 3-0 snap bout against the weakest.
                _mu("newcomer", "weak", "incumbent", wins_inc=3, wins_cha=0),
            ],
            champion="veteran",
            when=now,
        )
    try:
        rows = {r["fingerprint"]: r for r in duel_mod.standings()["standings"]}
        assert rows["newcomer"]["rating_provisional"] is True
        assert rows["veteran"]["rating_provisional"] is False
        assert rows["veteran"]["rating"] > rows["newcomer"]["rating"]
        # The thin rating carries a visibly wider error bar, so the page can say so.
        assert rows["newcomer"]["rating_se"] > rows["veteran"]["rating_se"]
    finally:
        with session_scope() as s:
            s.query(Duel).delete()


# ── The operating model: always be trying to beat whoever holds the belt ─────────────


def _ring_field(*profiles):
    return {
        "best_fingerprint": profiles[0][0],
        "profiles": [
            {"fingerprint": fp, "label": fp, "overall": ov, "confident": True, "settings": []}
            for fp, ov in profiles
        ],
    }


def test_the_queue_is_ordered_by_the_rings_findings_not_the_pooled_score():
    """The circularity fix, stated as a test.

    The duel exists to be the independent check on the pooled verdict, so the pooled verdict
    must not decide who gets checked. Here the ring has proven "provenstrong" — it beat the
    belt-holder decisively — while its pooled Overall is mid-table, and "pooledfave" has the
    best pooled Overall but a ring record of nothing but losses. Ordering by pooled sends the
    loser in first and buries the profile that actually beat the champion.
    """
    field = _ring_field(("pooledfave", 90.0), ("belt", 80.0), ("provenstrong", 70.0))
    ratings = {
        "belt": {"rating": 1500.0, "rating_se": 20.0},
        "provenstrong": {"rating": 1650.0, "rating_se": 40.0},   # beat the belt
        "pooledfave": {"rating": 1200.0, "rating_se": 15.0},     # lost, repeatedly and clearly
    }
    order = duel_mod.contender_order(field, ratings, "belt")
    by_fp = {c["fingerprint"]: c for c in order}

    # The pooled crown still leads — the two verdicts disagreeing is the most informative
    # bout there is — but note it is ALSO the profile the ring has beaten, so it's there on
    # its own merit as a verdict, not because its pooled score is high.
    assert order[0]["fingerprint"] == "pooledfave"
    assert order[0]["tier"] == duel_mod.CROWN_TIER
    # …and the profile the ring rates above the belt is next, not last.
    assert order[1]["fingerprint"] == "provenstrong"
    assert by_fp["provenstrong"]["tier"] == duel_mod.CONTENDER_TIER
    assert "plausibly beat the belt" in by_fp["provenstrong"]["why"]


def test_a_profile_the_ring_has_already_beaten_is_raced_last_not_never():
    """Re-asking a question the ring has answered finds nothing — but conditions change, so
    it waits its turn rather than being struck off, the same discipline the cooldown follows."""
    field = _ring_field(("belt", 90.0), ("hopeful", 80.0), ("outclassed", 85.0))
    ratings = {
        "belt": {"rating": 1600.0, "rating_se": 20.0},
        # Ceiling 1560 + 30 = 1590 — just short of the belt, so it can't plausibly win.
        "outclassed": {"rating": 1560.0, "rating_se": 30.0},
        # Thin record, wide bar: could be anything, so it gets the ring first.
        "hopeful": {"rating": 1500.0, "rating_se": 200.0},
    }
    order = duel_mod.contender_order(field, ratings, "belt")
    tiers = {c["fingerprint"]: c["tier"] for c in order}
    assert tiers["hopeful"] == duel_mod.CONTENDER_TIER
    assert tiers["outclassed"] == duel_mod.OUTCLASSED_TIER
    assert [c["fingerprint"] for c in order].index("hopeful") < [
        c["fingerprint"] for c in order
    ].index("outclassed")
    # Still in the queue — a long window gets to it.
    assert "outclassed" in tiers


def test_an_unknown_outranks_a_measured_weakling():
    """The optimistic ceiling does the exploring: a profile nobody has raced could be
    anything, while one the ring has measured as weak is a settled question."""
    field = _ring_field(("belt", 90.0), ("unknown", 50.0), ("weak", 88.0))
    ratings = {
        "belt": {"rating": 1600.0, "rating_se": 20.0},
        "weak": {"rating": 1300.0, "rating_se": 15.0},
    }  # "unknown" has no ring record at all
    order = [c["fingerprint"] for c in duel_mod.contender_order(field, ratings, "belt")]
    assert order.index("unknown") < order.index("weak")


def test_untested_profiles_are_ordered_among_themselves_by_pooled_overall():
    """Pooled keeps exactly one job: separating profiles the ring knows nothing about."""
    field = _ring_field(("belt", 90.0), ("promising", 85.0), ("unpromising", 40.0))
    order = [
        c["fingerprint"]
        for c in duel_mod.contender_order(field, {"belt": {"rating": 1500.0, "rating_se": 20.0}}, "belt")
    ]
    assert order.index("promising") < order.index("unpromising")


def test_a_beltholder_with_no_ring_record_makes_everything_a_contender():
    """Nothing to clear, so nothing is ruled out — the first session of a fresh ladder."""
    field = _ring_field(("belt", 90.0), ("a", 80.0), ("b", 70.0))
    order = duel_mod.contender_order(field, {"a": {"rating": 1200.0, "rating_se": 10.0}}, "belt")
    tiers = {c["fingerprint"]: c["tier"] for c in order}
    assert tiers["a"] == duel_mod.CONTENDER_TIER
    assert "no ring record yet" in next(c for c in order if c["fingerprint"] == "a")["why"]


def test_build_queue_ring_mode_uses_the_ratings_it_is_handed():
    field = _ring_field(("crown", 90.0), ("belt", 88.0), ("riser", 60.0), ("beaten", 87.0))
    ratings = {
        "belt": {"rating": 1500.0, "rating_se": 20.0},
        "riser": {"rating": 1700.0, "rating_se": 30.0},
        "beaten": {"rating": 1100.0, "rating_se": 10.0},
    }
    queue = duel_mod.build_queue(field, {"items": []}, "belt", contenders="ring", ratings=ratings)
    assert queue[0] == "crown", "the pooled crown is still the first bout"
    assert queue[1] == "riser", "then whoever the RING says could take the belt"
    assert queue[-1] == "beaten", "and the settled question goes last"


# ── a thin profile earns the ring by what it could still do, not by what it has done ────
#
# The operating model: measure a proposal briefly to get an initial placement, then spend
# the ring's time maturing the ones that could displace the best profile found so far — and
# give up on the ones whose own runs say no WITHOUT excluding them. A bout does both jobs at
# once: the paired runs mature the pooled record, and the verdict says whether it actually
# beats the crown. That is what stops the ladder racing #432 against #567.


def _thin_field(profiles):
    return {"profiles": profiles, "best_fingerprint": "crown0000000"}


def _thin(fp, overall, optimistic, *, iterations=5, confident=False):
    return {
        "fingerprint": fp, "label": fp, "overall": overall, "optimistic": optimistic,
        "iterations": iterations, "confident": confident, "settings": [],
    }


def test_a_thin_profile_whose_ceiling_reaches_the_crown_is_raced_before_one_that_cannot():
    """Five runs give a wide band, so the question isn't "is it better?" but "could it be?".
    A thin profile whose optimistic pooled reading still reaches the crown can change the
    answer; one whose ceiling falls short cannot, on the evidence it has."""
    field = _thin_field([
        _thin("crown0000000", 80.0, 80.5, iterations=200, confident=True),
        _thin("livethreat00", 78.0, 83.0),      # thin, but could still get there
        _thin("ownrunssayno", 70.0, 72.0),      # thin, and even optimistically short of 80
    ])
    order = duel_mod.contender_order(field, {}, "belt00000000")
    by_fp = {c["fingerprint"]: c for c in order}
    assert by_fp["livethreat00"]["tier"] == duel_mod.LIVE_THREAT_TIER
    assert by_fp["ownrunssayno"]["tier"] == duel_mod.UNTESTED_TIER
    assert [c["fingerprint"] for c in order].index("livethreat00") < \
           [c["fingerprint"] for c in order].index("ownrunssayno")
    # Given up on, never excluded — the ring has not actually asked it yet.
    assert "ownrunssayno" in by_fp
    assert "never excluded" in by_fp["ownrunssayno"]["why"]


def test_the_biggest_potential_threat_goes_first_among_unrated_profiles():
    """Ordered by ceiling, not by point estimate: the profile most likely to dethrone the
    crown gets confirmed or refuted first — the same priority the challenger race uses."""
    field = _thin_field([
        _thin("crown0000000", 80.0, 80.5, iterations=200, confident=True),
        _thin("modestupside", 79.5, 81.0),
        _thin("hugeupside00", 78.0, 88.0),   # lower measured, far wider band
    ])
    order = [c["fingerprint"] for c in duel_mod.contender_order(field, {}, "belt00000000")]
    assert order.index("hugeupside00") < order.index("modestupside")


def test_an_unexamined_claim_on_the_crown_is_raced_before_an_examined_one_on_the_belt():
    """An unresolved claim on the crown outranks a rated contender, deliberately.

    A rated contender has been examined, and its ceiling is a statement about beating the
    *belt-holder*. A thin profile whose pooled ceiling clears the *crown* is an unexamined
    claim that it is already the best thing measured — and it is only interesting while it
    stays unresolved, because the same bout that answers it also matures the profile. So it
    runs first. Ring evidence still governs wherever the ring HAS spoken (below)."""
    field = _thin_field([
        _thin("crown0000000", 80.0, 80.5, iterations=200, confident=True),
        _thin("ratedcontend", 60.0, 61.0, iterations=200, confident=True),
        _thin("thinbutshiny", 79.0, 95.0),
    ])
    ratings = {
        "belt00000000": {"rating": 1500.0, "rating_se": 20.0},
        "ratedcontend": {"rating": 1520.0, "rating_se": 20.0},
    }
    order = [c["fingerprint"] for c in duel_mod.contender_order(field, ratings, "belt00000000")]
    assert order[0] == "crown0000000"          # the two verdicts disagreeing still leads
    assert order.index("thinbutshiny") < order.index("ratedcontend")


def test_the_ring_still_governs_every_profile_it_has_actually_rated():
    """The promotion applies only where the ring has no opinion. A profile it has measured
    and found short stays last however flattering its pooled reading — otherwise a noisy
    pooled number could keep re-litigating a question the ring already answered."""
    field = _thin_field([
        _thin("crown0000000", 80.0, 80.5, iterations=200, confident=True),
        _thin("ringsaysno00", 90.0, 99.0, iterations=200, confident=True),
        _thin("thinbutshiny", 79.0, 95.0),
    ])
    ratings = {
        "belt00000000": {"rating": 1500.0, "rating_se": 10.0},
        "ringsaysno00": {"rating": 1200.0, "rating_se": 10.0},   # examined, and short
    }
    order = duel_mod.contender_order(field, ratings, "belt00000000")
    by_fp = {c["fingerprint"]: c for c in order}
    assert by_fp["ringsaysno00"]["tier"] == duel_mod.OUTCLASSED_TIER
    fps = [c["fingerprint"] for c in order]
    assert fps.index("thinbutshiny") < fps.index("ringsaysno00")


# ── the live scoreboard ────────────────────────────────────────────────────────────────


def test_the_live_scoreboard_says_who_is_ahead_and_by_how_much():
    """"(2-1)" in a sentence cannot say *whose* wins those are, by how much, or how close
    the bout is to ending — which is the whole of what a live duel readout is for."""
    sprt = duel_mod.SprtState(p1=0.7, alpha=0.05)
    paired = duel_mod.PairedEvidence(alpha=0.05, min_margin=0.0, min_pairs=6, max_pairs=40)
    # Challenger takes two of three, and by a clear margin each time.
    for delta in (1.4, -0.3, 1.1):
        sprt.add_pair(delta > 0)
        paired.add(delta)

    live = duel_mod._live_scoreboard(
        bout=1,
        inc={"fingerprint": "belt", "name": "Languid Lavender", "label": "belt"},
        cha={"fingerprint": "thin", "name": "Gritty Gibbon", "label": "thin"},
        sprt=sprt, paired=paired, why_challenger="thin but live",
        min_pairs=6, max_pairs=40, min_margin=0.0, streak_needed=8,
    )
    assert live["incumbent"]["wins"] == 1 and live["incumbent"]["name"] == "Languid Lavender"
    assert live["challenger"]["wins"] == 2 and live["challenger"]["name"] == "Gritty Gibbon"
    assert live["leader"] == "challenger"
    # Margins are challenger − incumbent, so the sign alone says which way it's going.
    assert live["median_margin"] == 1.1 and live["last_margin"] == 1.1
    assert live["margins"] == [1.4, -0.3, 1.1]
    # …and how far there is to go.
    assert live["pairs"] == 3 and live["min_pairs"] == 6 and live["max_pairs"] == 40
    assert live["streak"] == {"length": 1, "side": "challenger", "needed": 8}


def test_a_level_bout_says_level_rather_than_picking_a_side():
    """One-all is not a lead, and a readout that implies one is worse than no readout."""
    sprt = duel_mod.SprtState(p1=0.7, alpha=0.05)
    paired = duel_mod.PairedEvidence(alpha=0.05, min_margin=0.0, min_pairs=6, max_pairs=40)
    for delta in (0.8, -0.9):
        sprt.add_pair(delta > 0)
        paired.add(delta)
    live = duel_mod._live_scoreboard(
        bout=1, inc={"label": "a"}, cha={"label": "b"}, sprt=sprt, paired=paired,
        why_challenger="", min_pairs=6, max_pairs=40, min_margin=0.0, streak_needed=8,
    )
    assert live["leader"] == "level" and live["incumbent"]["wins"] == live["challenger"]["wins"]


def test_the_scoreboard_is_empty_before_any_pair_completes():
    """Nothing measured yet has to read as nothing, not as a nil-all lead."""
    sprt = duel_mod.SprtState(p1=0.7, alpha=0.05)
    paired = duel_mod.PairedEvidence(alpha=0.05, min_margin=0.0, min_pairs=6, max_pairs=40)
    live = duel_mod._live_scoreboard(
        bout=1, inc={"label": "a"}, cha={"label": "b"}, sprt=sprt, paired=paired,
        why_challenger="", min_pairs=6, max_pairs=40, min_margin=0.0, streak_needed=8,
    )
    assert live["pairs"] == 0 and live["leader"] == "level"
    assert live["median_margin"] is None and live["margins"] == []
    assert live["p_value"] is None
