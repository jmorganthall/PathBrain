"""Tests for the duel ladder (sequential head-to-head adjudication) + crowning policy."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from pathbrain import challenger as challenger_mod
from pathbrain import crowning
from pathbrain import duel as duel_mod
from pathbrain.database import session_scope
from pathbrain.duel import SprtState
from pathbrain.models import Duel, DuelStatus


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

    seq = {"n": 0}

    def fake_chunk(label, notes, iterations, teardown=True, job_group=None, job_group_total=None):
        seq["n"] += 1
        return (9000 + seq["n"], True, iterations)

    monkeypatch.setattr(duel_mod, "run_chunk", fake_chunk)
    # Odd run ids = incumbent leg (applied first each pair) at 60; even = challenger at 66.
    monkeypatch.setattr(
        duel_mod, "_run_overall", lambda run_id, ver: 60.0 if (run_id - 9000) % 2 == 1 else 66.0
    )

    duel_id = duel_mod.start(duration_minutes=10)
    d = _wait_finish(duel_id)

    assert d.status == DuelStatus.COMPLETE, d.error
    assert len(d.matchups) == 1
    m = d.matchups[0]
    assert m["verdict"] == "challenger"
    assert m["pairs"] == 10  # SPRT sweep decides exactly at min_pairs
    assert m["median_delta"] == 6.0
    assert d.champion_fingerprint == "cha0000000x"
    # Both sides were applied per pair, alternating.
    assert applied[:2] == ["inc0000000x", "cha0000000x"]
    assert not duel_mod.active()


def test_duel_margin_floor_records_a_draw(monkeypatch):
    """A statistically real but sub-margin edge (Δ ~0.4 < min_margin 1.0) is a draw."""
    import pathbrain.api.routes_settings as rs

    with session_scope() as s:
        s.query(Duel).delete()  # no rematch-cooldown carryover between tests
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
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: None)
    seq = {"n": 0}

    def fake_chunk(label, notes, iterations, teardown=True, job_group=None, job_group_total=None):
        seq["n"] += 1
        return (9500 + seq["n"], True, iterations)

    monkeypatch.setattr(duel_mod, "run_chunk", fake_chunk)
    monkeypatch.setattr(
        duel_mod, "_run_overall", lambda run_id, ver: 60.0 if (run_id - 9500) % 2 == 1 else 60.4
    )

    duel_id = duel_mod.start(duration_minutes=10)
    d = _wait_finish(duel_id)
    assert d.status == DuelStatus.COMPLETE, d.error
    m = d.matchups[0]
    assert m["verdict"] == "draw"
    assert "practically equal" in m["reason"]
    # The incumbent keeps the crown on a draw.
    assert d.champion_fingerprint == "inc0000000x"


# ── Crowning policy resolution ───────────────────────────────────────────────────────


def _seed_completed_duel(champion_fp: str, champion_label: str, decisive: bool, days_ago: float = 0.0) -> None:
    with session_scope() as s:
        s.add(
            Duel(
                status=DuelStatus.COMPLETE,
                finished_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
                duration_s=600,
                matchups=[{
                    "incumbent": "someinc0000", "challenger": champion_fp,
                    "verdict": "challenger" if decisive else "draw",
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


def test_standings_rank_by_head_to_head_record():
    """Ranking is earned in the ring: match points, then decisive-win rate, then pair rate."""
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

    # C: 1 win + 1 draw = 4 points; A: 1 win + 1 draw + 1 loss = 4 points, but a worse
    # decisive-win rate (1/2 vs 1/1) — so C ranks first.
    assert [r["fingerprint"] for r in out["standings"]][:2] == ["ccc", "aaa"]
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

    # Opponent lists + head-to-head cells.
    assert by_fp["aaa"]["beaten"] == ["bbb"] and by_fp["aaa"]["lost_to"] == ["ccc"]
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
