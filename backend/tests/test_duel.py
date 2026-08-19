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
            ev = PairedEvidence(cfg["alpha"], 0.0, cfg["min_pairs"], cfg["max_pairs"])
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

    Between identical profiles a 30-pair bout throws up a 3-in-a-row streak 99.7% of the
    time, so the streak has to be long enough to mean something; that length falls out of
    the test (1/2^n vs the threshold) rather than being a separate rule.
    """
    from pathbrain.duel import PRESETS, PairedEvidence, preset_config, streak_to_decide

    for name, preset in PRESETS.items():
        cfg = preset_config(name)
        n = streak_to_decide(cfg["alpha"], cfg["min_pairs"], cfg["max_pairs"])
        assert n is not None
        # The card's promise must be the code's behavior.
        assert f"{n} wins in a row" in preset["summary"], (name, preset["summary"], n)

        ev = PairedEvidence(cfg["alpha"], 0.0, cfg["min_pairs"], cfg["max_pairs"])
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
