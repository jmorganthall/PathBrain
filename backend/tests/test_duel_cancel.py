"""Cancelling a duel session takes effect within an ITERATION, and says so.

Reported as *"Can't cancel this parent job"* on a running lever session. The cancel set a
flag that the ring read only between legs — and a leg is an apply, a settle and three
iterations, or half an hour of probe deadlines when a browser wedges — while the jobs
feed kept showing the same "running" row with the same X. A cancel while the session was
still queued for the pipeline was worse: the thread sat in ``coordinator.hold`` until the
holder finished, and only then discovered it had nothing to do.
"""
from __future__ import annotations

import time


from pathbrain import challenger as challenger_mod, coordinator, duel as duel_mod
from pathbrain.config_store import save_config
from pathbrain.database import session_scope
from pathbrain.models import Duel, DuelStatus

A, B, C = "aaa0000000x", "bbb0000000x", "ccc0000000x"


def _field(*profiles):
    return {
        "best_fingerprint": profiles[0][0],
        "profiles": [
            {"fingerprint": fp, "label": fp[:5], "name": fp[:5].title(), "overall": ov,
             "confident": True, "settings": []}
            for fp, ov in profiles
        ],
    }


def _wait(duel_id: int, timeout: float = 15.0) -> Duel:
    terminal = (DuelStatus.COMPLETE, DuelStatus.FAILED, DuelStatus.CANCELLED)
    start = time.time()
    while time.time() - start < timeout:
        with session_scope() as s:
            d = s.get(Duel, duel_id)
            if d and d.status in terminal:
                s.expunge(d)
                return d
        time.sleep(0.02)
    raise AssertionError("duel did not finish in time")


def _mock_ring(monkeypatch, scores: dict[str, float], chunk):
    """A one-seat ring over `scores`; `chunk(run_id, on_created)` decides what each leg's
    run does and returns ``(ok, completed)``."""
    import pathbrain.api.routes_settings as rs

    with session_scope() as s:
        s.query(Duel).delete()
        save_config(s, {"duel": {"settle_seconds": 0, "seats": 1, "belt_every": 2}})
    field = _field(*sorted(scores.items(), key=lambda kv: -kv[1]))
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: field)
    monkeypatch.setattr(rs, "_compute_heirs", lambda result, session, live=None: {
        "items": [{"fingerprint": p["fingerprint"]} for p in field["profiles"][1:]]
    })
    monkeypatch.setattr(duel_mod, "_weather_stamper", lambda meth_version: None)
    applied: list[str] = []
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: applied.append(fp))
    by_run: dict[int, str] = {}
    seq = {"n": 0}

    def fake_chunk(label, notes, iterations, teardown=True, job_group=None,
                   job_group_total=None, on_created=None, **_):
        seq["n"] += 1
        run_id = 9000 + seq["n"]
        by_run[run_id] = applied[-1] if applied else ""
        ok, completed = chunk(run_id, on_created)
        return (run_id, ok, completed if completed is not None else iterations)

    monkeypatch.setattr(duel_mod, "run_chunk", fake_chunk)
    monkeypatch.setattr(duel_mod, "_run_overall", lambda run_id, ver: scores.get(by_run.get(run_id, ""), 0.0))
    return applied


def test_a_cancel_mid_leg_stops_that_legs_run_and_the_session_ends_with_it(monkeypatch):
    """The run measuring the leg in flight is asked to stop (it ends before its next
    iteration), the leg returns, the ring reads the flag: no further leg is applied."""
    stopped: list[int] = []
    monkeypatch.setattr(duel_mod, "request_stop", lambda run_id, reason="": stopped.append(run_id))

    def chunk(run_id, on_created):
        on_created(run_id)          # the run exists: the ring now knows which run to stop
        if run_id == 9002:          # the cancel lands while leg 2 is measuring
            assert duel_mod.cancel() is True
            return (False, 1)       # …so the run stopped after its first iteration
        return (True, None)

    applied = _mock_ring(monkeypatch, {A: 80.0, B: 70.0}, chunk)
    d = _wait(duel_mod.start(duration_minutes=10))

    assert d.status == DuelStatus.CANCELLED
    assert stopped == [9002], "the in-flight run — and only it — was told to stop"
    assert len(applied) == 2, "no third leg was applied after the cancel"
    assert d.stage == "Cancelled — baseline restored"
    assert d.iterations_run == 4  # 3 from leg 1, the 1 leg 2 finished before it stopped
    assert not duel_mod.active() and duel_mod._state["run_id"] is None


def test_a_cancel_that_lands_during_the_apply_stops_the_run_before_its_first_iteration(monkeypatch):
    """Between the apply and the run there is no run to stop yet; the cancel is remembered
    and the run is told to stop the moment it exists."""
    stopped: list[int] = []
    monkeypatch.setattr(duel_mod, "request_stop", lambda run_id, reason="": stopped.append(run_id))

    def chunk(run_id, on_created):
        if run_id == 9002:
            duel_mod.cancel()       # arrives before the leg's run exists
        on_created(run_id)
        return (run_id != 9002, 0 if run_id == 9002 else None)

    applied = _mock_ring(monkeypatch, {A: 80.0, B: 70.0}, chunk)
    d = _wait(duel_mod.start(duration_minutes=10))
    assert d.status == DuelStatus.CANCELLED
    assert stopped == [9002]
    assert len(applied) == 2


def test_cancelling_the_legs_chunk_from_the_feed_cancels_the_session(monkeypatch):
    """The feed's X on a nested chunk promises "its broader job will stop too". For the ring
    the broader job is the session — not the next leg with a fresh run."""
    monkeypatch.setattr(duel_mod, "run_cancelled", lambda run_id: run_id == 9002)

    def chunk(run_id, on_created):
        on_created(run_id)
        return (run_id != 9002, 1 if run_id == 9002 else None)

    applied = _mock_ring(monkeypatch, {A: 80.0, B: 70.0}, chunk)
    d = _wait(duel_mod.start(duration_minutes=10))
    assert d.status == DuelStatus.CANCELLED
    assert len(applied) == 2


def test_a_cancel_while_queued_leaves_the_line_without_taking_the_pipeline(monkeypatch):
    """The session is waiting on the coordinator behind another holder. Cancelled, it must
    abandon the wait now — not when the holder finishes hours later — and apply nothing."""
    monkeypatch.setattr(coordinator, "ABORT_POLL_S", 0.02)
    applied = _mock_ring(monkeypatch, {A: 80.0, B: 70.0}, lambda run_id, on_created: (True, None))
    discovered: list[str] = []
    real_get = duel_mod.get_provider

    def counting_provider():
        p = real_get()
        orig = p.discover

        def discover(*a, **k):
            discovered.append("discover")
            return orig(*a, **k)

        p.discover = discover  # type: ignore[method-assign]
        return p

    monkeypatch.setattr(duel_mod, "get_provider", counting_provider)

    with coordinator.hold("profile-test#7"):
        duel_id = duel_mod.start(duration_minutes=10)
        deadline = time.time() + 2.0
        while coordinator.waiting() == 0 and time.time() < deadline:
            time.sleep(0.005)
        assert coordinator.waiting() == 1
        assert duel_mod.cancel() is True
        d = _wait(duel_id)                      # ends while the OTHER session still holds the lock
        assert coordinator.owner() == "profile-test#7"
        assert coordinator.waiting() == 0
    assert d.status == DuelStatus.CANCELLED
    assert d.stage == "Cancelled before it started — nothing was applied"
    assert d.started_at is None and applied == [] and discovered == []
    assert d.error is None
    assert not duel_mod.active()


def test_cancel_reports_what_it_did_in_words(client, monkeypatch):
    """The route's answer is what the dropdown shows: "nothing to cancel" and "cancel
    received" must not both look like silence."""
    body = client.post("/api/duel/cancel").json()
    assert body["cancelled"] is False and body["message"].startswith("Nothing to cancel")
    monkeypatch.setattr(duel_mod, "active", lambda: True)
    monkeypatch.setattr(duel_mod, "cancel", lambda: True)
    monkeypatch.setattr(duel_mod, "current", lambda: {"status": "running", "id": 1})
    body = client.post("/api/duel/cancel").json()
    assert body["cancelled"] is True and body["message"].startswith("Cancel received")
    monkeypatch.setattr(duel_mod, "current", lambda: {"status": "pending", "id": 1})
    assert "nothing was applied" in client.post("/api/duel/cancel").json()["message"]


def test_the_feed_marks_a_session_that_is_stopping(client, monkeypatch):
    monkeypatch.setattr(duel_mod, "active", lambda: True)
    monkeypatch.setattr(duel_mod, "cancel_requested", lambda: True)
    monkeypatch.setattr(duel_mod, "current", lambda: {
        "id": 5, "status": "running", "stage": "Aaa (belt) — closing reference leg",
        "mode": "levers", "matchups": [], "iterations_run": 42, "duration_s": 600,
        "started_at": "2026-09-09T11:20:00+00:00",
    })
    row = next(j for j in client.get("/api/jobs").json()["jobs"] if j["id"] == "duel-5")
    assert row["cancel_requested"] is True
    assert row["message"].startswith("Cancelling — stops after the iteration in flight")
    assert row["cancel_url"] == "/duel/cancel"


def test_an_evicted_session_never_restores_the_baseline_over_a_live_one(monkeypatch):
    """The ring leaves on its deadline (or a cancel) without asking the lease; the restore
    that follows is a firewall write. A session the watchdog disowned must not make it."""
    restores: list = []
    monkeypatch.setattr(challenger_mod, "_apply_all", lambda provider, changes: restores.append(changes))
    _mock_ring(monkeypatch, {A: 80.0, B: 70.0}, lambda run_id, on_created: (True, None))

    def evicted_ring(**kw):
        lease = kw["lease"]
        lease.last_beat -= 10_000
        assert coordinator.evict_if_stalled(threshold_s=60) is True   # the watchdog stood it down
        return ([], None, 0)

    monkeypatch.setattr(duel_mod, "_run_ring", evicted_ring)
    d = _wait(duel_mod.start(duration_minutes=10))
    assert d.status == DuelStatus.FAILED
    assert d.error.startswith("Stood down mid-session")
    assert restores == [], "no firewall write after the lease was revoked"
    assert not coordinator.busy()


def test_the_feed_and_the_engine_agree_on_who_is_running(monkeypatch):
    """`_state['run_id']` is the in-flight run while a leg measures and None between legs."""
    seen: list[int | None] = []

    def chunk(run_id, on_created):
        on_created(run_id)
        seen.append(duel_mod._state["run_id"])
        return (True, None)

    _mock_ring(monkeypatch, {A: 80.0, B: 70.0}, chunk)
    d = _wait(duel_mod.start(duration_minutes=10))
    assert d.status == DuelStatus.COMPLETE
    assert seen and all(isinstance(x, int) for x in seen)
    assert duel_mod._state["run_id"] is None
