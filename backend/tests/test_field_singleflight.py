"""`compute_profiles` runs ONE field pass per (key, stamp) however many callers ask.

The memo used to let two callers racing a cold cache both compute — "wasteful once, never
wrong" — which was true when the callers were a page and a ladder session an hour apart.
Once several pages asked for the field on load, a cold cache meant that many concurrent
pure-Python passes, each holding a copy of the field and each taking the GIL in turn,
and the process went dark. Now the second caller waits on the first's pass and reads the
cache; a leader that raises clears its slot so a waiter computes for itself rather than
waiting forever.
"""
from __future__ import annotations

import threading
import time

from pathbrain.api import routes_settings as rs
from pathbrain.database import session_scope


def _clear():
    rs.invalidate_profiles_cache()
    with rs._FIELD_LOCK:
        rs._FIELD_INFLIGHT.clear()


def test_concurrent_cold_callers_share_one_field_pass(monkeypatch):
    calls: list[int] = []

    def slow(session, *args, **kwargs):
        calls.append(threading.get_ident())
        time.sleep(0.4)
        return {"pass": len(calls)}

    monkeypatch.setattr(rs, "_compute_profiles_uncached", slow)
    _clear()
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker():
        try:
            with session_scope() as s:
                results.append(rs.compute_profiles(s, include_weather=False))
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    try:
        assert not errors, errors
        assert len(calls) == 1, f"{len(calls)} field passes for 6 concurrent identical callers"
        assert len(results) == 6 and all(r is results[0] for r in results)
        assert not rs._FIELD_INFLIGHT
    finally:
        _clear()


def test_a_failed_leader_does_not_strand_the_waiters(monkeypatch):
    n = {"calls": 0}
    release = threading.Event()

    def flaky(session, *args, **kwargs):
        n["calls"] += 1
        if n["calls"] == 1:
            release.wait(5)
            raise RuntimeError("the field pass blew up")
        return {"ok": True}

    monkeypatch.setattr(rs, "_compute_profiles_uncached", flaky)
    _clear()
    outcome: dict = {}

    def leader():
        try:
            with session_scope() as s:
                rs.compute_profiles(s, include_weather=False)
        except RuntimeError as exc:
            outcome["leader"] = str(exc)

    def waiter():
        with session_scope() as s:
            outcome["waiter"] = rs.compute_profiles(s, include_weather=False)

    tl = threading.Thread(target=leader)
    tl.start()
    deadline = time.monotonic() + 5
    while not rs._FIELD_INFLIGHT and time.monotonic() < deadline:
        time.sleep(0.01)
    assert rs._FIELD_INFLIGHT, "the leader never registered its flight"
    tw = threading.Thread(target=waiter)
    tw.start()
    time.sleep(0.1)  # the waiter is now parked on the leader's event
    release.set()
    tl.join(timeout=10)
    tw.join(timeout=10)
    try:
        assert outcome.get("leader") == "the field pass blew up"
        assert outcome.get("waiter") == {"ok": True}
        assert n["calls"] == 2
        assert not rs._FIELD_INFLIGHT
    finally:
        _clear()
