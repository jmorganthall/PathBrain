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
    with session_scope() as s:
        save_config(s, {"duel": {"min_margin": 0.0}})  # back to the default (no floor)


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


def test_the_reigning_champion_defends_the_next_session():
    """The belt has to mean something: last session's winner starts the next one holding
    it, instead of every session restarting from the pooled crown while the badge names a
    profile that isn't even in the ring."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    field = _field(("champ", 80.0), ("pooled", 90.0), ("other", 70.0), best="pooled")

    with session_scope() as s:
        s.query(Duel).delete()
        # No champion yet → the pooled crown defends.
        fp, why = duel_mod.select_incumbent(s, field, None, {"rematch_days": 7})
        assert fp == "pooled" and "no fresh decisive champion" in why

    with session_scope() as s:
        _finished_duel(
            s,
            matchups=[_mu("champ", "someone", "incumbent", delta=-3.0)],  # decisive
            champion="champ",
            when=now - timedelta(hours=2),
        )
    with session_scope() as s:
        fp, why = duel_mod.select_incumbent(s, field, None, {"rematch_days": 7})
    assert fp == "champ", "the champion should carry its belt into the next session"
    assert "defends the belt" in why

    # And the pooled crown becomes a CHALLENGER — the matchup the disagreement demands.
    heirs = {"items": [{"fingerprint": "other", "reason": "stale"}]}
    queue = duel_mod.build_queue(field, heirs, "champ", contenders="leaders", top_n=5)
    assert queue[0] == "pooled", "the pooled crown must get to challenge the belt holder"
    assert "champ" not in queue

    # A champion that only inherited by draws doesn't get to hold the belt.
    with session_scope() as s:
        s.query(Duel).delete()
        _finished_duel(
            s,
            matchups=[_mu("champ", "someone", "draw")],
            champion="champ",
            when=now - timedelta(hours=1),
        )
    with session_scope() as s:
        fp, _ = duel_mod.select_incumbent(s, field, None, {"rematch_days": 7})
    assert fp == "pooled"

    # An expired verdict hands the belt back to the pooled crown too.
    with session_scope() as s:
        s.query(Duel).delete()
        _finished_duel(
            s,
            matchups=[_mu("champ", "someone", "incumbent", delta=-3.0)],
            champion="champ",
            when=now - timedelta(days=30),
        )
    with session_scope() as s:
        fp, _ = duel_mod.select_incumbent(s, field, None, {"rematch_days": 7})
        s.query(Duel).delete()
    assert fp == "pooled"


def test_a_champion_the_environment_cant_reach_does_not_defend():
    """A belt holder the firewall can no longer be set to would abort the session on the
    first apply — the pooled crown stands in instead."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    field = {
        "best_fingerprint": "pooled",
        "profiles": [
            {"fingerprint": "champ", "label": "champ", "overall": 80.0,
             "settings": [{"label": "wan", "scheduler": "fq_codel", "queues": 8}]},
            {"fingerprint": "pooled", "label": "pooled", "overall": 90.0, "settings": []},
        ],
    }
    live = [{"label": "wan", "scheduler": "fq_pie", "queues": 2}]  # different environment
    with session_scope() as s:
        s.query(Duel).delete()
        _finished_duel(
            s,
            matchups=[_mu("champ", "someone", "incumbent", delta=-3.0)],
            champion="champ",
            when=now - timedelta(hours=2),
        )
    with session_scope() as s:
        fp, why = duel_mod.select_incumbent(s, field, live, {"rematch_days": 7})
        s.query(Duel).delete()
    assert fp == "pooled" and "can't be applied" in why


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
        with session_scope() as s:
            picks = []
            while queue:
                fp, why = duel_mod.next_matchup(s, queue, tiers, "belt", 7)
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
    tiers = duel_mod.contender_tiers(field, queue)

    with session_scope() as s:
        s.query(Duel).delete()
        _finished_duel(
            s, matchups=[_mu("belt", "fought", "incumbent")], champion="belt",
            when=now - timedelta(days=1),
        )

    try:
        with session_scope() as s:
            first, why = duel_mod.next_matchup(s, queue, tiers, "belt", 7)
        # Same tier, so the cooldown decides: the question we haven't asked yet goes first.
        assert first == "unfought" and "re-rac" not in why
        assert queue == ["fought"], "the cooled contender is still raced, just after"
    finally:
        with session_scope() as s:
            s.query(Duel).delete()
