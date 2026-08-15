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
