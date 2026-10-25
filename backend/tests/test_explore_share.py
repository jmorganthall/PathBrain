"""The duel's exploration share: a slice of every window spent on Explore's best bet, paced
across the night, scaled by the window, and folded back into the field the ring fights over."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain import explore_share
from pathbrain import duel as duel_mod
from pathbrain.database import session_scope
from pathbrain.models import ProfileTest, ProfileTestStatus

from tests.test_duel_resilience import A, B, _mock_ring, _wait  # noqa: F401

C = "ccc0000000x"


def _due(**kw):
    base = dict(share_=0.05, elapsed_s=0.0, window_s=8 * 3600.0, spent_s=0.0,
                remaining_s=8 * 3600.0, bet_cost_s=300.0)
    base.update(kw)
    return explore_share.due(**base)


def test_the_first_seam_owes_a_bet_so_even_a_short_session_explores():
    assert _due()
    assert _due(window_s=3600.0, remaining_s=3600.0)


def test_a_bet_is_owed_again_only_once_the_share_of_elapsed_time_catches_up():
    # One 5-minute bet spent: at 5% it is paid back after 100 minutes of window.
    assert not _due(spent_s=300.0, elapsed_s=99 * 60.0)
    assert _due(spent_s=300.0, elapsed_s=100 * 60.0)


def test_the_share_scales_with_the_window():
    """An eight-hour night buys about six five-minute bets; a two-hour window about two."""
    def bets(window_s: float, cost: float = 300.0, seam: float = 120.0) -> int:
        spent, n, t = 0.0, 0, 0.0
        while t < window_s:
            if _due(elapsed_s=t, window_s=window_s, spent_s=spent,
                    remaining_s=window_s - t, bet_cost_s=cost):
                spent += cost
                n += 1
                t += cost
            t += seam
        return n

    assert bets(8 * 3600.0) in (5, 6)
    assert bets(2 * 3600.0) in (1, 2)
    assert bets(8 * 3600.0) > bets(2 * 3600.0)


def test_the_budget_caps_it_and_no_bet_is_queued_that_cannot_finish():
    window = 3600.0
    assert not _due(window_s=window, spent_s=0.05 * window, elapsed_s=window)
    assert not _due(remaining_s=200.0, bet_cost_s=300.0)


def test_zero_share_is_off_and_the_share_is_clamped():
    assert not _due(share_=0.0)
    assert explore_share.share({"explore_share": 0}) == 0.0
    assert explore_share.share({}) == explore_share.DEFAULT_SHARE
    assert explore_share.share({"explore_share": 0.9}) == explore_share.MAX_SHARE
    assert explore_share.iterations({}) == 5


def _completed_test(fp: str, seconds: float = 60.0, *, reached: str | None = None) -> int:
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        pt = ProfileTest(
            status=ProfileTestStatus.COMPLETE, fingerprint=fp, target_label="explore",
            iterations=5, started_at=now - timedelta(seconds=seconds), finished_at=now,
            reached_fingerprint=reached,
        )
        s.add(pt)
        s.flush()
        return pt.id


def test_spent_time_is_read_off_the_bets_own_clocks(monkeypatch):
    share = explore_share.ExploreShare(1, {"explore_share": 0.05}, 3600.0)
    share.bets.append({"test_id": _completed_test(C, seconds=90.0)})
    assert 89.0 <= share.spent_s() <= 91.0


def test_the_ring_queues_a_bet_and_duels_the_profile_it_found(monkeypatch):
    """End to end: the ring queues Explore's bet at a seam, the bet lands, the field is
    re-read, and the new profile is seated against the belt the same session."""
    import pathbrain.api.routes_settings as rs

    applied, _ = _mock_ring(monkeypatch, {A: 70.0, B: 62.0, C: 75.0}, lambda fp, n: None)
    from pathbrain.config_store import save_config

    with session_scope() as s:
        save_config(s, {"duel": {"explore_share": 0.05}})
    base = rs.compute_profiles(None)
    found = {"yet": False}
    without_c = {**base, "profiles": [p for p in base["profiles"] if p["fingerprint"] != C],
                 "best_fingerprint": A}
    monkeypatch.setattr(rs, "compute_profiles",
                        lambda session, **_: base if found["yet"] else without_c)
    monkeypatch.setattr(explore_share, "QUEUE_SETTLE_S", 0.0)
    calls = {"n": 0}

    def fake_queue(self):
        calls["n"] += 1
        found["yet"] = True
        return {"test_id": _completed_test(C), "fingerprint": C, "label": "Download quantum 3000",
                "iterations": self.iterations}

    monkeypatch.setattr(explore_share.ExploreShare, "_queue_top_bet", fake_queue)
    d = _wait(duel_mod.start(duration_minutes=60), timeout=30)
    assert d.status.value == "complete", d.error
    assert calls["n"] == 1  # one bet: the next is not owed until the share catches up
    assert d.explore and len(d.explore["bets"]) == 1
    assert d.explore["bets"][0]["status"] == "complete"
    assert d.explore["budget_s"] == 180
    assert C in applied, "the explored profile was never raced"
    assert any(m["challenger"] == C for m in d.matchups)


def test_a_lever_session_never_explores():
    """A lever session measures one base's settings; it must not spend its window on bets."""
    import inspect

    src = inspect.getsource(duel_mod._run_ring)
    assert 'campaign is None and mode != "levers"' in src


# ── the maturing turn ────────────────────────────────────────────────────────────────


def test_the_shares_turns_alternate_a_bet_then_a_top_up():
    assert explore_share.kind_for(0, True) == "bet", "the first turn still explores"
    assert explore_share.kind_for(1, True) == "mature"
    assert explore_share.kind_for(2, True) == "bet"
    assert explore_share.kind_for(3, True) == "mature"
    assert all(explore_share.kind_for(n, False) == "bet" for n in range(6))
    assert explore_share.mature_enabled({}) is True
    assert explore_share.mature_enabled({"explore_mature": False}) is False
    assert explore_share.mature_enabled({"explore_mature": None}) is True


def test_a_maturing_turn_tops_up_the_thinnest_live_threat_not_in_the_ring(monkeypatch):
    """The heir with the highest ceiling that is not seated, under-sampled (never a stale
    confident one), each at most once a session, through an ordinary profile test."""
    from pathbrain import profile_test

    started: list[tuple] = []

    def fake_start(fp, settings, label, iterations):
        started.append((fp, settings, label, iterations))
        return 4242

    monkeypatch.setattr(profile_test, "start", fake_start)
    share = explore_share.ExploreShare(7, {"explore_share": 0.05, "explore_iterations": 5}, 3600.0)
    heirs = [
        {"fingerprint": "seated00000", "reason": "limited-data", "optimistic": 81.0, "iterations": 5,
         "name": "In The Ring", "crown_overall": 78.0},
        {"fingerprint": "stale000000", "reason": "stale", "optimistic": 80.5, "iterations": 60,
         "name": "Old Timer", "crown_overall": 78.0},
        {"fingerprint": "thin0000000", "reason": "limited-data", "optimistic": 80.0, "iterations": 5,
         "name": "Thin Threat", "crown_overall": 78.0, "overall": 83.0},
        {"fingerprint": "thin0000001", "reason": "limited-data", "optimistic": 79.0, "iterations": 10,
         "name": "Second Thin", "crown_overall": 78.0},
    ]
    share.heirs_source = lambda: heirs
    share.settings_source = lambda fp: [{"label": "Download", "quantum": 3000}] if fp.startswith("thin") else None
    share.exclude_source = lambda: {"seated00000"}

    bet = share._queue_top_heir()
    assert bet is not None and bet["kind"] == "mature"
    assert bet["fingerprint"] == "thin0000000" and bet["test_id"] == 4242
    assert bet["iterations"] == 5 and bet["iterations_before"] == 5 and bet["ceiling"] == 80.0
    assert "ceiling 80.0 against a crown of 78.0" in bet["summary"]
    assert started == [("thin0000000", [{"label": "Download", "quantum": 3000}],
                        "Mature (duel #7): Thin Threat", 5)]
    # Once a session: the next maturing turn goes to the next heir, then to nobody.
    assert share._queue_top_heir()["fingerprint"] == "thin0000001"
    assert share._queue_top_heir() is None
    assert "already in the ring or was matured" in (share.note or "")


def test_a_turn_with_nobody_to_mature_falls_through_to_a_bet_and_vice_versa(monkeypatch):
    monkeypatch.setattr(explore_share, "QUEUE_SETTLE_S", 0.0)
    share = explore_share.ExploreShare(8, {"explore_share": 0.5}, 3600.0)
    share.heirs_source = lambda: []
    calls: list[str] = []

    def fake_bet(self):
        calls.append("bet")
        return {"kind": "bet", "test_id": _completed_test(C, seconds=1.0), "fingerprint": C,
                "label": "q3000", "iterations": 5}

    monkeypatch.setattr(explore_share.ExploreShare, "_queue_top_bet", fake_bet)
    monkeypatch.setattr(explore_share, "due", lambda **_: True)
    assert share.at_seam(3600.0)["kind"] == "bet"           # turn 1: a bet
    assert share.at_seam(3600.0)["kind"] == "bet"           # turn 2: maturing owed, nobody → a bet
    assert calls == ["bet", "bet"]
    # And the other way: Explore has nothing, but an heir is waiting.
    share2 = explore_share.ExploreShare(9, {"explore_share": 0.5}, 3600.0)
    monkeypatch.setattr(explore_share.ExploreShare, "_queue_top_bet", lambda self: None)
    monkeypatch.setattr(explore_share.ExploreShare, "_queue_top_heir",
                        lambda self: {"kind": "mature", "test_id": _completed_test(C, seconds=1.0),
                                      "fingerprint": C, "label": "Thin", "iterations": 5})
    assert share2.at_seam(3600.0)["kind"] == "mature"        # turn 1 wanted a bet; none → matured
    assert share2.summary()["matured"] == 1 and share2.summary()["explored"] == 0
    # Switched off, the share only ever bets.
    share3 = explore_share.ExploreShare(10, {"explore_share": 0.5, "explore_mature": False}, 3600.0)
    assert share3.at_seam(3600.0) is None and share3.summary()["mature"] is False


def test_the_ring_hands_the_maturing_turn_its_own_heirs_and_settings(monkeypatch):
    """End to end: the ring wires the share to its heirs list, its settings and its seats, so
    a maturing turn tops up a thin live threat through a real profile test."""
    import pathbrain.api.routes_settings as rs
    from pathbrain import profile_test
    from pathbrain.config_store import save_config

    applied, _ = _mock_ring(monkeypatch, {A: 70.0, B: 62.0, C: 75.0}, lambda fp, n: None)
    with session_scope() as s:
        save_config(s, {"duel": {"explore_share": 0.05}})
    base = rs.compute_profiles(None)
    for i, p in enumerate(base["profiles"]):
        # A maturing turn applies the profile's STORED settings; the mock field carries none.
        # Distinct per profile, or decidability refuses every bout as "the same profile".
        if not p.get("settings"):
            p["settings"] = [{"label": "Download", "quantum": 1000 + 1000 * i, "scheduler": "fq_codel"}]
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: base)
    # C wears the belt and the single seat takes one of A/B; the other is the thin live threat
    # a maturing turn may top up — never the seated one. The engine reads `all_items`
    # (`_engine_heirs`), so the fake fills both.
    heirs = [
        {"fingerprint": fp, "name": f"Thin {fp[0].upper()}", "reason": "limited-data",
         "optimistic": 90.0, "iterations": 5, "crown_overall": 75.0, "overall": 62.0}
        for fp in (A, B)
    ]
    monkeypatch.setattr(rs, "_compute_heirs", lambda field, session, live=None: {
        "items": list(heirs), "all_items": list(heirs), "total": 2, "limit": 5, "crown_overall": 75.0,
    })
    monkeypatch.setattr(explore_share, "QUEUE_SETTLE_S", 0.0)
    monkeypatch.setattr(explore_share, "due", lambda **_: True)
    monkeypatch.setattr(explore_share.ExploreShare, "_queue_top_bet",
                        lambda self: {"kind": "bet", "test_id": _completed_test(C), "fingerprint": C,
                                      "label": "bet", "iterations": self.iterations})
    matured: list[tuple] = []

    def fake_start(fp, settings, label, iterations):
        matured.append((fp, settings, label, iterations))
        return _completed_test(fp)

    monkeypatch.setattr(profile_test, "start", fake_start)
    d = _wait(duel_mod.start(duration_minutes=60), timeout=30)
    assert d.status.value == "complete", d.error
    kinds = [b.get("kind") for b in d.explore["bets"]]
    assert "bet" in kinds and "mature" in kinds, kinds
    fp, settings, label, iterations = matured[0]
    assert fp in (A, B) and settings and iterations == 5
    assert label == "Mature (duel #%d): Thin %s" % (d.id, fp[0].upper())
    first_seated = d.matchups[0]["challenger"]
    assert fp != first_seated, "a profile in the ring is never matured from under its match"
    # Each profile is matured at most once a session; the belt never is.
    assert d.explore["matured"] <= 2 and all(m[0] != C for m in matured)
    assert len({m[0] for m in matured}) == len(matured)


def test_a_landed_bet_reports_the_fingerprint_its_runs_were_filed_under():
    """A bet is queued under the fingerprint Explore predicted; the firewall settles on the
    profile it actually reports and the runs are filed there (`ProfileTest.reached_fingerprint`).
    The board used to keep showing the prediction, so a reader following the bet to its profile
    found a profile with no runs. Once known, the reached fingerprint is the bet's and the
    prediction sits beside it."""
    share = explore_share.ExploreShare(1, {"explore_share": 0.05}, 3600.0)
    reached_id = _completed_test("predicted000", reached="reached00000")
    pending_id = _completed_test("predicted111")        # no reached stamp (older row)
    share.bets = [
        {"kind": "bet", "test_id": reached_id, "fingerprint": "predicted000", "label": "a", "iterations": 5},
        {"kind": "bet", "test_id": pending_id, "fingerprint": "predicted111", "label": "b", "iterations": 5},
    ]
    landed = {b["label"]: b for b in share.newly_landed()}
    assert landed["a"]["fingerprint"] == "reached00000"
    assert landed["a"]["predicted_fingerprint"] == "predicted000"
    assert landed["b"]["fingerprint"] == "predicted111", "no reached stamp → the prediction stands"
    assert landed["b"]["predicted_fingerprint"] == "predicted111"
    board = {b["label"]: b for b in share.summary()["bets"]}
    assert board["a"]["fingerprint"] == "reached00000" and board["a"]["predicted_fingerprint"] == "predicted000"
    assert board["b"]["fingerprint"] == "predicted111"
