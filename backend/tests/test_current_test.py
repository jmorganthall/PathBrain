"""Tests for the timed "test current for X minutes" engine + manual run chunking."""
from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from pathbrain import current_test as ct_mod
from pathbrain import runner
from pathbrain.database import session_scope
from pathbrain.models import CurrentTest, CurrentTestStatus, Run, RunStatus


def _wait_finish(ct_id: int, timeout: float = 10.0) -> CurrentTest:
    start = time.time()
    terminal = (
        CurrentTestStatus.COMPLETE,
        CurrentTestStatus.FAILED,
        CurrentTestStatus.CANCELLED,
    )
    while time.time() - start < timeout:
        with session_scope() as s:
            ct = s.get(CurrentTest, ct_id)
            if ct and ct.status in terminal:
                s.expunge(ct)
                return ct
        time.sleep(0.02)
    raise AssertionError("test-current did not finish in time")


def _fake_chunk(delay: float = 0.03, ok=lambda n: True):
    """A stand-in for ``runner.run_chunk`` that doesn't touch the network: sleeps briefly,
    counts calls, and reports success per ``ok(call_number)``."""
    state = {"n": 0}

    def chunk(label, notes, iterations):
        state["n"] += 1
        time.sleep(delay)
        return (2000 + state["n"], ok(state["n"]), iterations)

    chunk.state = state
    return chunk


def test_current_test_collects_chunks_until_deadline(monkeypatch):
    # ~0.25s per chunk so a ~2s window collects several (duration is whole seconds).
    monkeypatch.setattr(ct_mod, "run_chunk", _fake_chunk(delay=0.25))

    ct_id = ct_mod.start(minutes=0.03)  # 1.8s → 2s of collecting
    ct = _wait_finish(ct_id)

    assert ct.status == CurrentTestStatus.COMPLETE
    assert ct.runs_created >= 1  # at least one chunk always runs
    # Every chunk contributes CHUNK_ITERATIONS to the total collected.
    assert ct.iterations_run == ct.runs_created * runner.CHUNK_ITERATIONS
    assert len(ct.run_ids) == ct.runs_created
    assert not ct_mod.active()


def test_current_test_cancel_stops_after_current_chunk(monkeypatch):
    monkeypatch.setattr(ct_mod, "run_chunk", _fake_chunk(delay=0.05))

    ct_id = ct_mod.start(minutes=10)  # long; we cancel it well before the deadline
    # Wait until it's actually running a chunk.
    for _ in range(200):
        if ct_mod.active() and (ct_mod.current() or {}).get("status") == "running":
            break
        time.sleep(0.02)
    assert ct_mod.cancel() is True

    ct = _wait_finish(ct_id)
    assert ct.status == CurrentTestStatus.CANCELLED
    assert not ct_mod.active()


def test_current_test_stops_on_failed_chunk(monkeypatch):
    # The 2nd chunk fails → stop early, keep the first chunk's data, mark FAILED.
    monkeypatch.setattr(ct_mod, "run_chunk", _fake_chunk(delay=0.02, ok=lambda n: n < 2))

    ct_id = ct_mod.start(minutes=10)
    ct = _wait_finish(ct_id)

    assert ct.status == CurrentTestStatus.FAILED
    assert ct.runs_created == 2  # ran chunk 1 (ok) + chunk 2 (failed), then stopped
    assert "chunk failed" in (ct.error or "").lower()
    assert not ct_mod.active()


def test_current_test_rejects_bad_duration():
    with pytest.raises(ValueError):
        ct_mod.start(0)


def test_reconcile_interrupted_current_tests_closes_orphan():
    with session_scope() as s:
        ct = CurrentTest(status=CurrentTestStatus.RUNNING, duration_s=300)
        s.add(ct)
        s.flush()
        cid = ct.id

    assert ct_mod.reconcile_interrupted_current_tests() >= 1
    with session_scope() as s:
        row = s.get(CurrentTest, cid)
        assert row.status == CurrentTestStatus.FAILED
        assert "Interrupted" in (row.error or "")


def test_current_test_endpoints_start_status_conflict_cancel(client, monkeypatch):
    monkeypatch.setattr(ct_mod, "run_chunk", _fake_chunk(delay=0.05))

    resp = client.post("/api/current/test", json={"minutes": 10})
    assert resp.status_code == 202
    assert resp.json()["status"] in ("pending", "running")

    # A second start while one is active is a conflict.
    assert client.post("/api/current/test", json={"minutes": 5}).status_code == 409

    # Status is queryable, then cancel winds it down.
    assert client.get("/api/current/test").json()["status"] in ("pending", "running")
    assert client.post("/api/current/test/cancel").json()["cancelled"] is True

    ct_id = (ct_mod.current() or {}).get("id")
    ct = _wait_finish(ct_id)
    assert ct.status == CurrentTestStatus.CANCELLED


# ── Manual run chunking ─────────────────────────────────────────────────────────────

def test_manual_run_over_5_iterations_chunks_into_series(client, monkeypatch):
    monkeypatch.setattr(runner, "iter_plugins", lambda: [])  # no network; runs still complete
    marker = "chunkseries-abc"

    resp = client.post("/api/run", json={"iterations": 12, "notes": marker})
    assert resp.status_code == 202
    assert resp.json()["iterations"] == runner.CHUNK_ITERATIONS  # first chunk is <=5

    # TestClient runs the background series to completion; poll defensively regardless.
    runs = _series_runs(marker, expect=3)
    assert [r.iterations for r in runs] == [5, 5, 2]  # 12 == 5 + 5 + 2
    assert all(r.status == RunStatus.COMPLETE for r in runs)
    assert "part 1/3" in (runs[0].notes or "")


def test_manual_run_5_or_fewer_is_a_single_run(client, monkeypatch):
    monkeypatch.setattr(runner, "iter_plugins", lambda: [])
    marker = "single-abc"

    resp = client.post("/api/run", json={"iterations": 5, "notes": marker})
    assert resp.json()["iterations"] == 5

    runs = _series_runs(marker, expect=1)
    assert len(runs) == 1
    assert "part" not in (runs[0].notes or "")  # not chunked


def test_estimate_exposes_raised_iteration_cap(client):
    body = client.get("/api/runs/estimate").json()
    assert body["max_iterations"] == runner.MAX_ITERATIONS
    assert runner.MAX_ITERATIONS > 20  # the point: long series are now allowed


def _series_runs(marker: str, expect: int, timeout: float = 10.0) -> list[Run]:
    start = time.time()
    while time.time() - start < timeout:
        with session_scope() as s:
            runs = s.scalars(
                select(Run).where(Run.notes.like(f"{marker}%")).order_by(Run.id)
            ).all()
            done = [r for r in runs if r.status in (RunStatus.COMPLETE, RunStatus.FAILED)]
            if len(done) >= expect:
                for r in done:
                    s.expunge(r)
                return done
        time.sleep(0.02)
    raise AssertionError(f"expected {expect} run(s) for {marker}; timed out")
