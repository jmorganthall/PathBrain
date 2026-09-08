"""A duel's setup is progress, and the feed says when the watchdog stood a session down.

Setup — the pooled standings, the heirs, the weather yardstick — runs no probe, and only the
runner beats, so a session was silent to the coordinator from the moment it took the lock; a
field pass past the stale bar got the ladder evicted for making progress, a monitoring run
started beside it, and the duel row kept reading "running". Pinned here: each finished setup
step beats the lease (the yardstick step sees a fresh beat after a slow standings pass); the
stage names the step and what has finished; and the jobs feed reports an eviction of the
running session as "stood down" with the stall chip, and labels a lever session as one.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from pathbrain import challenger as challenger_mod
from pathbrain import coordinator
from pathbrain import duel as duel_mod
from pathbrain.api import routes_jobs
from pathbrain.config_store import save_config
from pathbrain.database import session_scope
from pathbrain.models import Duel, DuelStatus


def _wait_finish(duel_id: int, timeout: float = 20.0) -> Duel:
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


def test_each_setup_step_beats_the_lease_and_names_itself(monkeypatch):
    import pathbrain.api.routes_settings as rs

    with session_scope() as s:
        save_config(s, {"duel": {"settle_seconds": 0, "seats": 1, "belt_every": 2, "contenders": "ring"}})
        s.query(Duel).delete()
        s.commit()
    fake_field = {
        "best_fingerprint": "inc0000000x",
        "profiles": [
            {"fingerprint": "inc0000000x", "label": "incumbent", "settings": [{"label": "wan", "quantum": 1514}]},
            {"fingerprint": "cha0000000x", "label": "challenger", "settings": [{"label": "wan", "quantum": 300}]},
        ],
    }
    seen: dict = {}

    def slow_field(session, **_):
        # A field pass long enough that, without a beat after it, the lease would read as
        # quiet for at least this long by the time the next step runs.
        time.sleep(0.4)
        return fake_field

    def stamper(meth_version):
        # The yardstick step runs right after the heirs step: its stage should name it and
        # list the finished steps, and the lease must have beaten since the slow pass.
        seen["quiet"] = coordinator.stalled_for()
        with session_scope() as s:
            row = s.scalars(duel_mod.select(Duel).order_by(Duel.id.desc())).first()
            seen["stage"] = row.stage if row else None
        return None

    monkeypatch.setattr(rs, "compute_profiles", slow_field)
    monkeypatch.setattr(rs, "_compute_heirs", lambda result, session, live=None: {"items": [{"fingerprint": "cha0000000x"}]})
    monkeypatch.setattr(duel_mod, "_weather_stamper", stamper)
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: None)
    applied: list[str] = []
    by_run: dict[int, str] = {}
    seq = {"n": 0}

    def fake_chunk(label, notes, iterations, teardown=True, job_group=None, job_group_total=None, **_):
        seq["n"] += 1
        by_run[9900 + seq["n"]] = applied[-1] if applied else ""
        return (9900 + seq["n"], True, iterations)

    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: applied.append(fp))
    monkeypatch.setattr(duel_mod, "run_chunk", fake_chunk)
    monkeypatch.setattr(duel_mod, "_run_overall",
                        lambda run_id, ver: {"inc0000000x": 60.0, "cha0000000x": 66.0}.get(by_run.get(run_id, ""), 0.0))
    monkeypatch.setattr(duel_mod, "_run_crown", lambda run_id, ver: None)
    try:
        d = _wait_finish(duel_mod.start(duration_minutes=10))
        assert d.status == DuelStatus.COMPLETE, d.error
        # The yardstick step saw a lease that had beaten AFTER the 0.4 s field pass.
        assert seen["quiet"] is not None and seen["quiet"] < 0.3, seen
        # …and a stage that names the step and what has finished, with how long it took.
        assert seen["stage"].startswith("Ranking the field for matchmaking — weather yardstick"), seen["stage"]
        assert "standings" in seen["stage"] and "heirs" in seen["stage"]
    finally:
        with session_scope() as s:
            s.query(Duel).delete()
            s.commit()


def test_the_feed_says_stood_down_when_the_watchdog_evicted_this_session():
    with session_scope() as s:
        row = Duel(status=DuelStatus.RUNNING, trigger="manual", duration_s=6 * 3600, matchups=[],
                   mode="levers", stage="Ranking the field for matchmaking — pooled standings",
                   started_at=datetime.now(timezone.utc))
        s.add(row)
        s.flush()
        duel_id = row.id
    prior_state = dict(duel_mod._state)
    prior_eviction = coordinator._last_eviction
    try:
        duel_mod._state.update({"active": True, "id": duel_id})
        # Nothing evicted yet: an ordinary running row, labelled as the lever session it is.
        coordinator._last_eviction = None
        entry = routes_jobs._active_duel_job()[0]
        assert entry["label"] == "Lever session" and entry["href"] == "/levers"
        assert "Stood down" not in entry["message"] and entry["stalled_ms"] is None
        # The watchdog evicts this session's lease: the row must say so, not "running".
        coordinator._last_eviction = {"owner": f"duel#{duel_id}", "quiet_s": 1200.0, "held_s": 1300.0, "at": time.time()}
        entry = routes_jobs._active_duel_job()[0]
        assert entry["message"].startswith("Stood down by the pipeline watchdog — no progress for 20 min while")
        assert "pooled standings" in entry["message"]
        assert entry["stalled_ms"] == 1_200_000
        # An eviction of some EARLIER session with the same label is not this one's.
        coordinator._last_eviction = {"owner": f"duel#{duel_id}", "quiet_s": 1200.0, "held_s": 1300.0, "at": time.time() - 86400}
        entry = routes_jobs._active_duel_job()[0]
        assert "Stood down" not in entry["message"]
    finally:
        coordinator._last_eviction = prior_eviction
        duel_mod._state.clear()
        duel_mod._state.update(prior_state)
        with session_scope() as s:
            s.query(Duel).filter(Duel.id == duel_id).delete()
            s.commit()
