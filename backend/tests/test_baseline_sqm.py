"""Tests for the baseline (SQM off) test engine + its schedule/on-demand endpoints.

Covers the write-then-restore lifecycle (disable SQM on every pipe → settle → benchmark →
restore), chunking, cancel, failure handling, startup reconcile, the fingerprint distinction
that keeps SQM-off runs in their own profile, and the schedule config surface.
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import select

from pathbrain import baseline_test as bt_mod
from pathbrain import runner
from pathbrain.database import session_scope
from pathbrain.models import BaselineTest, BaselineTestStatus
from pathbrain.providers.base import ConfigProvider, FqCodelConfig
from pathbrain.settings_profile import fingerprint, normalize, summarize


class FakeProvider(ConfigProvider):
    """A provider whose two pipes both have uuids, recording every on/off toggle so a test can
    assert both pipes were disabled and then restored."""

    name = "fake"

    def __init__(self) -> None:
        self.enabled = {"dl": True, "ul": True}
        self.toggles: list[tuple[str, bool]] = []

    def discover(self) -> list[FqCodelConfig]:
        return [
            FqCodelConfig(quantum=1514, extra={"uuid": "dl", "description": "wan-download",
                                                "enabled": self.enabled["dl"]}),
            FqCodelConfig(quantum=300, extra={"uuid": "ul", "description": "wan-upload",
                                              "enabled": self.enabled["ul"]}),
        ]

    def snapshot(self) -> dict:
        return {}

    def set_pipe_enabled(self, pipe_uuid, enabled) -> dict:
        self.toggles.append((pipe_uuid, bool(enabled)))
        if pipe_uuid in self.enabled:
            self.enabled[pipe_uuid] = bool(enabled)
        return {"ok": True, "uuid": pipe_uuid, "enabled": bool(enabled)}


def _wait_finish(bt_id: int, timeout: float = 10.0) -> BaselineTest:
    terminal = (BaselineTestStatus.COMPLETE, BaselineTestStatus.FAILED, BaselineTestStatus.CANCELLED)
    start = time.time()
    while time.time() - start < timeout:
        with session_scope() as s:
            bt = s.get(BaselineTest, bt_id)
            if bt and bt.status in terminal:
                s.expunge(bt)
                return bt
        time.sleep(0.02)
    raise AssertionError("baseline test did not finish in time")


def _fake_chunk(delay: float = 0.02, ok=lambda n: True):
    state = {"n": 0}

    def chunk(label, notes, iterations):
        state["n"] += 1
        time.sleep(delay)
        return (3000 + state["n"], ok(state["n"]), iterations)

    chunk.state = state
    return chunk


@pytest.fixture
def fake_provider(monkeypatch):
    fp = FakeProvider()
    monkeypatch.setattr(bt_mod, "get_provider", lambda: fp)
    return fp


# ── fingerprint / normalize distinction ────────────────────────────────────────────

def test_sqm_off_is_a_distinct_fingerprint_without_re_keying_normal_profiles():
    on = normalize(FakeProvider().discover())
    fp = FakeProvider()
    fp.enabled = {"dl": False, "ul": False}
    off = normalize(fp.discover())

    # SQM off ⇒ different profile from the same shaper params with SQM on.
    assert fingerprint(on) != fingerprint(off)
    # …but an all-enabled profile hashes exactly as it would with no `enabled` info at all
    # (historical fingerprints are preserved — the marker is only appended when a pipe is off).
    legacy = [{k: v for k, v in p.items() if k != "enabled"} for p in on]
    assert fingerprint(on) == fingerprint(legacy)
    # The summary flags the off state so the profile is obviously the baseline.
    assert "SQM off" in summarize(off)


# ── engine lifecycle ───────────────────────────────────────────────────────────────

def test_baseline_test_disables_settles_benchmarks_restores(fake_provider, monkeypatch):
    monkeypatch.setattr(bt_mod, "run_chunk", _fake_chunk())

    bt_id = bt_mod.start(iterations=12, settle_seconds=0)
    bt = _wait_finish(bt_id)

    assert bt.status == BaselineTestStatus.COMPLETE
    assert bt.iterations_run == 12  # 5 + 5 + 2
    assert bt.runs_created == 3
    # Both pipes were turned off during the test and both turned back on at the end.
    assert (("dl", False) in fake_provider.toggles) and (("ul", False) in fake_provider.toggles)
    assert fake_provider.enabled == {"dl": True, "ul": True}
    assert not bt_mod.active()


def test_baseline_test_restores_even_when_a_chunk_fails(fake_provider, monkeypatch):
    monkeypatch.setattr(bt_mod, "run_chunk", _fake_chunk(ok=lambda n: n < 2))

    bt_id = bt_mod.start(iterations=20, settle_seconds=0)
    bt = _wait_finish(bt_id)

    assert bt.status == BaselineTestStatus.FAILED
    assert bt.runs_created == 2  # chunk 1 ok, chunk 2 failed → stop
    # SQM is restored no matter what.
    assert fake_provider.enabled == {"dl": True, "ul": True}


def test_baseline_test_cancel_during_settle_skips_benchmark_but_restores(fake_provider, monkeypatch):
    chunk = _fake_chunk()
    monkeypatch.setattr(bt_mod, "run_chunk", chunk)

    bt_id = bt_mod.start(iterations=5, settle_seconds=30)  # long settle; cancel during it
    for _ in range(200):
        if (bt_mod.current() or {}).get("stage", "").startswith("Settling"):
            break
        time.sleep(0.02)
    assert bt_mod.cancel() is True

    bt = _wait_finish(bt_id)
    assert bt.status == BaselineTestStatus.CANCELLED
    assert chunk.state["n"] == 0  # never benchmarked
    assert fake_provider.enabled == {"dl": True, "ul": True}


def test_baseline_test_rejects_bad_input(fake_provider):
    with pytest.raises(ValueError):
        bt_mod.start(0, 10)
    with pytest.raises(ValueError):
        bt_mod.start(5, -1)


def test_reconcile_re_enables_sqm(fake_provider):
    fake_provider.enabled = {"dl": False, "ul": False}  # stranded off by a crash
    with session_scope() as s:
        bt = BaselineTest(
            status=BaselineTestStatus.RUNNING,
            iterations=10,
            settle_s=0,
            baseline=[{"uuid": "dl", "label": "wan-download", "enabled": True},
                      {"uuid": "ul", "label": "wan-upload", "enabled": True}],
        )
        s.add(bt)
        s.flush()
        bid = bt.id

    assert bt_mod.reconcile_interrupted_baseline_tests() >= 1
    assert fake_provider.enabled == {"dl": True, "ul": True}  # SQM turned back on
    with session_scope() as s:
        row = s.get(BaselineTest, bid)
        assert row.status == BaselineTestStatus.FAILED
        assert "Interrupted" in (row.error or "")


# ── endpoints ──────────────────────────────────────────────────────────────────────

def test_baseline_endpoints_start_status_conflict_cancel(client, fake_provider, monkeypatch):
    monkeypatch.setattr(bt_mod, "run_chunk", _fake_chunk(delay=0.05))

    resp = client.post("/api/baseline/test", json={"iterations": 50, "settle_seconds": 0})
    assert resp.status_code == 202
    assert resp.json()["status"] in ("pending", "running")

    # A second start while one runs is a conflict.
    assert client.post("/api/baseline/test", json={}).status_code == 409
    assert client.get("/api/baseline/test").json()["status"] in ("pending", "running")
    assert client.post("/api/baseline/test/cancel").json()["cancelled"] is True

    bt = _wait_finish((bt_mod.current() or {}).get("id"))
    assert bt.status == BaselineTestStatus.CANCELLED


def test_baseline_config_get_and_update(client):
    got = client.get("/api/baseline/config").json()
    assert set(got) >= {"enabled", "hour", "minute", "iterations", "settle_seconds", "next_run_at"}

    updated = client.put(
        "/api/baseline/config",
        json={"enabled": True, "hour": 3, "minute": 30, "iterations": 8, "settle_seconds": 45},
    ).json()
    assert updated["enabled"] is True
    assert (updated["hour"], updated["minute"]) == (3, 30)
    assert updated["iterations"] == 8 and updated["settle_seconds"] == 45
    assert updated["next_run_at"]  # armed → a next fire time is computed

    # Validation: out-of-range hour is rejected.
    assert client.put("/api/baseline/config", json={"hour": 24}).status_code == 422
    assert client.put("/api/baseline/config", json={"iterations": 0}).status_code == 422
