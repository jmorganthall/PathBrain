"""A firewall that answers late costs the ring a leg, never a session — and a restart
costs it the minutes lost, never the rest of the window."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, select

from pathbrain import challenger as challenger_mod, duel as duel_mod
from pathbrain.config_store import save_config
from pathbrain.database import session_scope
from pathbrain.models import Duel, DuelStatus, QueuedJob
from pathbrain.session_runtime import FirewallUnavailable

A, B = "aaa0000000x", "bbb0000000x"


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


def _unavailable() -> FirewallUnavailable:
    req = httpx.Request("POST", "https://fw/api")
    return FirewallUnavailable("apply", 3, httpx.ReadTimeout("timed out", request=req))


def _mock_ring(monkeypatch, scores: dict[str, float], apply):
    """A one-seat ring over `scores` with `apply` standing in for the profile apply and
    the runs scored by the profile applied for them."""
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
    attempts = {"n": 0}

    def _apply(provider, settings, fp):
        # `apply` sees the ATTEMPT index (a retried leg is a new attempt), `applied` records
        # only the profiles that actually took.
        n = attempts["n"]
        attempts["n"] += 1
        apply(fp, n)
        applied.append(fp)

    monkeypatch.setattr(challenger_mod, "_apply_profile", _apply)
    by_run: dict[int, str] = {}
    seq = {"n": 0}

    def fake_chunk(label, notes, iterations, teardown=True, job_group=None, job_group_total=None, **_):
        seq["n"] += 1
        run_id = 9000 + seq["n"]
        by_run[run_id] = applied[-1] if applied else ""
        return (run_id, True, iterations)

    monkeypatch.setattr(duel_mod, "run_chunk", fake_chunk)
    monkeypatch.setattr(duel_mod, "_run_overall", lambda run_id, ver: scores.get(by_run.get(run_id, ""), 0.0))
    return applied, seq


def test_one_unanswered_apply_costs_a_leg_and_the_session_carries_on(monkeypatch):
    """The reported failure: one ReadTimeout on one leg ended a lever session with 0 rounds.
    Now the leg is recorded as not measured and the ring moves on."""
    def apply(fp, n):
        if n == 1:  # the first challenger leg
            raise _unavailable()

    applied, seq = _mock_ring(monkeypatch, {A: 70.0, B: 62.0}, apply)
    d = _wait(duel_mod.start(duration_minutes=10))
    assert d.status == DuelStatus.COMPLETE, d.error
    assert d.matchups and d.matchups[0]["challenger"] == B
    assert duel_mod.outcome(d.matchups[0]) != duel_mod.ABORTED
    # The failed leg ran no benchmark: one fewer run than legs attempted.
    assert seq["n"] == len(applied)
    assert d.error is None


def test_three_unanswered_applies_in_a_row_stop_the_session_in_plain_words(monkeypatch):
    def apply(fp, n):
        raise _unavailable()

    applied, seq = _mock_ring(monkeypatch, {A: 70.0, B: 62.0}, apply)
    d = _wait(duel_mod.start(duration_minutes=10))
    assert d.status == DuelStatus.FAILED
    assert d.error.startswith(f"{duel_mod.MAX_CONSECUTIVE_LEG_FAILURES} legs in a row could not be applied")
    assert "settings were restored" in d.error and "carried to the next session" in d.error
    assert "ReadTimeout" in d.error  # the diagnostic detail stays
    assert not d.error.startswith("SessionAbort") and "RuntimeError" not in d.error
    assert seq["n"] == 0  # nothing was ever measured


def test_a_failed_opening_belt_leg_does_not_spend_the_challengers_iterations(monkeypatch):
    """When the reference leg itself cannot be applied, the challenger legs this cycle
    would resolve against nothing; the ring retries the reference instead."""
    order: list[str] = []

    def apply(fp, n):
        order.append(fp)
        if n == 0:
            raise _unavailable()

    applied, seq = _mock_ring(monkeypatch, {A: 70.0, B: 62.0}, apply)
    d = _wait(duel_mod.start(duration_minutes=10))
    assert d.status == DuelStatus.COMPLETE, d.error
    # Failed belt leg, then the belt again — never a challenger between them.
    assert order[:2] == [A, A]
    assert d.matchups and duel_mod.outcome(d.matchups[0]) != duel_mod.ABORTED


def test_a_restart_queues_the_rest_of_an_interrupted_window():
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        s.query(Duel).delete()
        s.execute(delete(QueuedJob).where(QueuedJob.kind == "duel"))
        long = Duel(status=DuelStatus.RUNNING, trigger="scheduled", mode="levers", campaign_id=7,
                    duration_s=3600, baseline=[], started_at=now - timedelta(minutes=10))
        short = Duel(status=DuelStatus.RUNNING, trigger="manual", duration_s=3600, baseline=[],
                     started_at=now - timedelta(minutes=50))
        s.add_all([long, short])
        s.flush()
        long_id, short_id = long.id, short.id
    try:
        assert duel_mod.reconcile_interrupted_duels(now=now) == 2
        with session_scope() as s:
            rows = s.scalars(select(QueuedJob).where(QueuedJob.kind == "duel", QueuedJob.state == "pending")).all()
            assert len(rows) == 1
            spec = rows[0].spec
            assert spec["duration_minutes"] == 50 and spec["contenders"] == "levers"
            assert spec["campaign_id"] == 7 and spec["trigger"] == "scheduled"
            assert rows[0].label.startswith("Lever session")
            assert "50 minutes of its window remained" in s.get(Duel, long_id).error
            short_row = s.get(Duel, short_id)
            assert short_row.status == DuelStatus.FAILED and "remained" not in short_row.error
    finally:
        with session_scope() as s:
            s.execute(delete(QueuedJob).where(QueuedJob.kind == "duel"))
            s.query(Duel).delete()
