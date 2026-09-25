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


def _completed_test(fp: str, seconds: float = 60.0) -> int:
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        pt = ProfileTest(
            status=ProfileTestStatus.COMPLETE, fingerprint=fp, target_label="explore",
            iterations=5, started_at=now - timedelta(seconds=seconds), finished_at=now,
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
