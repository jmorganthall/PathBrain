"""The continuous ping beside every write.

The load-bearing claims, in the order they matter:

* every write's window is scored and written onto its own ledger row, so "which write
  broke the firewall?" is a column and not a reconstruction;
* a gap with **no write in flight** is recorded as unattributed — the control, without
  which the instrument can only ever conclude that writes cause gaps;
* one gap is one row whichever path sees it first;
* nothing here can make a firewall write fail, however badly it goes wrong.
"""
from __future__ import annotations

import time

import pytest

from pathbrain import firewall_guard, link_watch
from pathbrain.database import session_scope
from pathbrain.models import FirewallWrite, LinkGap


# --------------------------------------------------------------------------- gap finding

def series(spec: str, *, t0: float = 1000.0, step: float = 0.2) -> list[tuple[float, float | None]]:
    """``"..X X.."`` → a sample series; ``.`` answered, ``X`` lost (spaces ignored)."""
    out: list[tuple[float, float | None]] = []
    for i, ch in enumerate(spec.replace(" ", "")):
        out.append((t0 + i * step, None if ch == "X" else 5.0))
    return out


def test_a_scattered_loss_is_not_a_gap():
    # Twenty scattered drops and two seconds of nothing are the same loss percentage, and
    # only the second is an outage. One missed 200ms sample is under the threshold.
    assert link_watch.find_gaps(series("..X..X..X..")) == []


def test_a_continuous_run_is_a_gap_measured_to_the_next_answer():
    gaps = link_watch.find_gaps(series("..XXXXX.."))
    assert len(gaps) == 1
    # Five losses at 200ms spacing: from the first miss to the next answer is 1.0s.
    assert gaps[0]["duration_ms"] == pytest.approx(1000.0, abs=1.0)
    assert gaps[0]["open"] is False


def test_a_gap_still_open_at_the_end_is_reported_as_the_floor_we_watched():
    gaps = link_watch.find_gaps(series("..XXXXXX"))
    assert len(gaps) == 1 and gaps[0]["open"] is True


def test_summarize_reports_the_worst_gap_not_the_loss_rate():
    out = link_watch.summarize(series(".X.X.X. XXXXXXXXXX ."))
    assert out["worst_gap_ms"] > 1500.0
    assert out["loss_pct"] > 0


def test_no_samples_is_no_opinion_never_a_clean_bill():
    out = link_watch.summarize([])
    # None, not 0.0: "nothing was watching" and "nothing went wrong" are different answers
    # and a card that renders them identically is lying about its coverage.
    assert out["worst_gap_ms"] is None and out["sent"] == 0


# ------------------------------------------------------------------------------ verdicts

def test_the_verdict_leads_with_the_box_going_silent():
    said = link_watch.verdict({"firewall": {"worst_gap_ms": 3000.0},
                               "through": {"worst_gap_ms": 3000.0}})
    assert "firewall itself stopped answering" in said and "3.0s" in said


def test_a_through_only_gap_reads_as_a_queue_rebuild():
    said = link_watch.verdict({"firewall": {"worst_gap_ms": 0.0},
                               "through": {"worst_gap_ms": 2000.0}})
    assert "through the firewall stopped" in said and "kept answering" in said


def test_a_clean_window_says_so_plainly():
    assert "cost nothing measurable" in link_watch.verdict(
        {"firewall": {"worst_gap_ms": 0.0}, "through": {"worst_gap_ms": 0.0}})


# ------------------------------------------------------------------------- attribution

class _FakeTarget:
    """A target whose samples are handed in, so a test can place a gap exactly."""

    def __init__(self, label: str, samples: list[tuple[float, float | None]]) -> None:
        self.label, self.address, self.error = label, f"{label}.test", None
        self.sent, self.lost = len(samples), sum(1 for _, r in samples if r is None)
        self._samples = samples

    def window(self, start: float, end: float) -> list[tuple[float, float | None]]:
        return [s for s in self._samples if start <= s[0] <= end]


@pytest.fixture(autouse=True)
def db():
    """Both ledgers empty at the start of each test: these assertions count rows, and a
    row left by the test before is indistinguishable from the event under test."""
    with session_scope() as s:
        s.query(LinkGap).delete()
        s.query(FirewallWrite).delete()
    yield


@pytest.fixture
def watching(monkeypatch):
    """A watch with hand-placed samples and no threads — the scoring under test is pure
    bookkeeping over a series, and sampling it for real would make the test a coin toss."""
    state = dict(link_watch._state)

    def install(targets: dict):
        link_watch._state.update({
            "running": True, "targets": targets, "pending": [], "recent_windows": [],
            "swept_to": None, "error": None, "hz": 5.0, "started_at": time.time(),
        })
    yield install
    link_watch._state.clear()
    link_watch._state.update(state)


def _write_row() -> int:
    wid = firewall_guard.record("apply_many", changes=[{"pipe_uuid": "p1", "param": "flows",
                                                        "value": 1024}],
                                reconfigures=1, outcome="ok", latency_ms=120.0)
    assert wid is not None
    return wid


def test_a_writes_cost_lands_on_its_own_ledger_row(watching):
    """The whole point: the ledger row itself says what the write cost."""
    wid = _write_row()
    t = 5000.0
    watching({
        "firewall": _FakeTarget("firewall", series("." * 20, t0=t)),
        "through": _FakeTarget("through", series("....XXXXXXXXXX....", t0=t)),
    })
    link_watch._score_write({"write_id": wid, "op": "apply_many", "owner": "duel#1",
                             "start": t, "end": t + 10.0})

    with session_scope() as s:
        row = s.get(FirewallWrite, wid)
        assert row.through_gap_ms > 1500.0
        assert row.box_gap_ms == 0.0
        assert row.gap_ms == row.through_gap_ms
        assert "through the firewall stopped" in row.watch["verdict"]


def test_the_gap_is_filed_against_the_write_that_was_in_flight(watching):
    wid = _write_row()
    t = 6000.0
    watching({"through": _FakeTarget("through", series("..XXXXXXXX..", t0=t))})
    link_watch._score_write({"write_id": wid, "op": "apply_many", "owner": "duel#1",
                             "start": t, "end": t + 10.0})
    with session_scope() as s:
        gaps = s.query(LinkGap).all()
        assert len(gaps) == 1
        assert gaps[0].write_id == wid and gaps[0].op == "apply_many"


def test_a_gap_with_no_write_in_flight_is_recorded_as_unattributed(watching, monkeypatch):
    """The control group, and the reason any of this means anything.

    An instrument that only looked at write windows would find that every gap it saw
    happened during a write, because it never looked anywhere else.
    """
    now = time.time()
    start = now - 120.0
    watching({"through": _FakeTarget("through", series("..XXXXXXXX.." * 1, t0=start))})
    link_watch._state["swept_to"] = start - 1.0
    link_watch._sweep()

    with session_scope() as s:
        gaps = s.query(LinkGap).all()
        assert len(gaps) == 1
        assert gaps[0].write_id is None  # nobody wrote; this one is not PathBrain's
        assert gaps[0].target == "through"


def test_a_swept_gap_inside_a_scored_window_is_not_filed_twice(watching):
    """One outage is one row. The sweep runs behind the settling by design, so without the
    memory of already-scored windows a gap would be filed once against its write and once
    as unattributed — which reads as two events and inverts the control."""
    wid = _write_row()
    now = time.time()
    start = now - 120.0
    watching({"through": _FakeTarget("through", series("..XXXXXXXX..", t0=start))})
    link_watch._score_write({"write_id": wid, "op": "apply_many", "owner": None,
                             "start": start, "end": start + 30.0})
    link_watch._state["swept_to"] = start - 1.0
    link_watch._sweep()

    with session_scope() as s:
        gaps = s.query(LinkGap).all()
        assert len(gaps) == 1
        assert gaps[0].write_id == wid  # it keeps the attribution, not the sweep's None


def test_the_summary_splits_gaps_by_whether_a_write_was_in_flight(watching):
    wid = _write_row()
    now = time.time()
    # Both gaps in the summary's own window: it asks "in the last 24 hours", and a gap
    # placed in 1970 would be excluded for a reason that has nothing to do with the split.
    t = now - 600.0
    watching({"through": _FakeTarget("through", series("..XXXXXXXX..", t0=t))})
    link_watch._score_write({"write_id": wid, "op": "apply_many", "owner": None,
                             "start": t, "end": t + 10.0})
    watching({"through": _FakeTarget("through", series("..XXXXXXXXXXXX..", t0=now - 200.0))})
    link_watch._state["swept_to"] = now - 201.0
    link_watch._sweep()

    out = link_watch.gap_summary(hours=24.0)
    assert out["gaps"] == 2
    assert out["during_a_write"] == 1 and out["unattributed"] == 1


# ------------------------------------------------------------ never breaking the write

def test_note_write_is_a_no_op_when_nothing_is_watching():
    link_watch._state["running"] = False
    link_watch.note_write(1, "apply", time.time(), time.time())  # must not raise


def test_note_write_never_raises_even_when_the_state_is_nonsense(monkeypatch):
    monkeypatch.setitem(link_watch._state, "running", True)
    monkeypatch.setitem(link_watch._state, "pending", None)  # not a list
    link_watch.note_write(1, "apply", time.time(), time.time())


def test_scoring_a_write_that_no_longer_exists_does_not_raise(watching):
    watching({"through": _FakeTarget("through", series("..XXXXXX..", t0=8000.0))})
    link_watch._score_write({"write_id": 999999, "op": "apply", "owner": None,
                             "start": 8000.0, "end": 8010.0})


def test_starting_with_no_targets_reports_why_and_stays_down(monkeypatch):
    monkeypatch.setattr(link_watch, "config",
                        lambda: {"enabled": True, "hz": 5.0, "through_target": ""})
    monkeypatch.setattr("pathbrain.write_probe.firewall_address", lambda: None)
    try:
        out = link_watch.start()
        assert out["running"] is False
        assert "no ping targets" in (out["error"] or "")
    finally:
        link_watch.stop()


# ------------------------------------------------------- through the real write path

def test_a_real_write_registers_its_window_and_lands_a_cost(watching, monkeypatch):
    """End to end through ``get_provider()``, because the unit tests above would all still
    pass with the hook missing from the write path entirely — which is the whole feature.

    The write is a real one through the guarded provider; only the ping series is planted,
    so what is under test is the wiring: does a write PathBrain makes, by itself, end up
    with what it cost on its own ledger row?
    """
    from pathbrain.providers import get_provider

    provider = get_provider()
    pipes = provider.discover()
    assert pipes, "the mock provider should expose at least one pipe"
    uuid = (pipes[0].extra or {}).get("uuid")

    # Samples straddling the write: the gap sits a second after it, inside the recovery
    # window, which is where a queue rebuild's damage actually lands.
    t = time.time()
    watching({
        "firewall": _FakeTarget("firewall", series("." * 120, t0=t - 5.0)),
        "through": _FakeTarget("through", series("." * 30 + "X" * 15 + "." * 60, t0=t - 5.0)),
    })

    provider.apply_many([{"pipe_uuid": uuid, "param": "flows", "value": 1024}])

    pending = list(link_watch._state["pending"])
    assert len(pending) == 1, "the write did not register a window with the watch"
    assert pending[0]["op"] == "apply_many"
    assert pending[0]["write_id"] is not None, "the ledger row id never reached the watch"

    # Score it now rather than waiting out the recovery window in a test.
    link_watch._score_write(pending[0])
    with session_scope() as s:
        row = s.get(FirewallWrite, pending[0]["write_id"])
        assert row.op == "apply_many"
        assert row.through_gap_ms > 1000.0
        assert row.box_gap_ms == 0.0
        assert row.watch["verdict"]


def test_a_failed_write_is_measured_too(watching, monkeypatch):
    """A write that failed is the one most likely to have cost the household something, so
    leaving the unhappy path unmeasured blinds the instrument exactly where it matters."""
    from pathbrain.providers import get_provider
    from pathbrain.session_runtime import FirewallUnavailable

    provider = get_provider()
    watching({"through": _FakeTarget("through", series("." * 50, t0=time.time() - 10.0))})

    import requests

    def boom(*_a, **_k):
        raise requests.exceptions.ReadTimeout("timed out")

    monkeypatch.setattr(provider._inner, "apply_many", boom)
    monkeypatch.setattr(provider, "_verify_applied", lambda *_a, **_k: False)
    with pytest.raises((FirewallUnavailable, requests.exceptions.ReadTimeout)):
        provider.apply_many([{"pipe_uuid": "p1", "param": "flows", "value": 1024}])

    assert len(link_watch._state["pending"]) == 1
    assert link_watch._state["pending"][0]["write_id"] is not None


def test_the_ledger_separates_the_guards_pacing_from_the_firewalls_own_latency(monkeypatch):
    """A wait PathBrain chose is not the firewall being slow.

    The write probe timed the whole call and reported a five-second guard gap as a
    five-second firewall cost, which sends the reader after entirely the wrong thing.
    """
    from pathbrain import firewall_guard as fg
    from pathbrain.providers import get_provider

    monkeypatch.setattr(fg, "config", lambda: dict(
        fg.DEFAULTS, min_reconfigure_gap_s=30, max_reconfigures_per_hour=0,
        cooldown_after_outage_s=0, arm_required_after_deploy=False,
    ))
    slept: list[float] = []
    monkeypatch.setattr(fg, "_sleep", lambda s: slept.append(s))

    provider = get_provider()
    uuid = (provider.discover()[0].extra or {}).get("uuid")
    provider.apply_many([{"pipe_uuid": uuid, "param": "flows", "value": 2048}])  # sets "last"
    provider.apply_many([{"pipe_uuid": uuid, "param": "flows", "value": 4096}])  # must be paced

    assert slept and slept[-1] > 0, "the guard did not pace the second write"
    with session_scope() as s:
        row = s.query(FirewallWrite).order_by(FirewallWrite.id.desc()).first()
        assert row.waited_ms == pytest.approx(slept[-1] * 1000.0, rel=0.01)
        # The firewall's own latency is the call alone, and the mock answers instantly.
        assert row.latency_ms < 1000.0


# ------------------------------------------------------------------------------- the API

def test_the_watch_endpoint_reports_status_summary_and_gaps(client, watching):
    watching({"through": _FakeTarget("through", series("." * 20, t0=time.time() - 4.0))})
    body = client.get("/api/firewall/watch").json()
    assert body["status"]["running"] is True
    assert set(body["summary"]) >= {"gaps", "during_a_write", "unattributed"}
    assert isinstance(body["gaps"], list)


def test_the_ledger_endpoint_carries_what_each_write_cost(client, watching):
    wid = _write_row()
    t = time.time()
    watching({"through": _FakeTarget("through", series("..XXXXXXXX..", t0=t))})
    link_watch._score_write({"write_id": wid, "op": "apply_many", "owner": None,
                             "start": t, "end": t + 10.0})
    rows = client.get("/api/firewall/writes").json()
    row = next(r for r in rows if r["id"] == wid)
    assert row["gap_ms"] > 1000.0
    assert "verdict" in (row["watch"] or {})


def test_an_unwatched_write_reports_no_cost_rather_than_a_clean_one(client):
    """The distinction the whole card rests on: null is "nobody was watching", and a zero
    there would be a clean bill of health nobody measured."""
    wid = _write_row()
    rows = client.get("/api/firewall/writes").json()
    row = next(r for r in rows if r["id"] == wid)
    assert row["gap_ms"] is None


# ------------------------------------------------------- the watch writes nothing, ever

def test_the_watch_never_touches_the_firewall(watching, monkeypatch):
    """Measured with the fault-injecting provider, as the firewall gate requires.

    This module is read-only by design: it sends ICMP echoes and writes its own rows. If a
    watcher could ever issue a write it would be the one unsupervised write path in the
    system — a diagnostic about writes that makes writes.
    """
    from tests.faults import FaultyProvider

    provider = FaultyProvider()
    monkeypatch.setattr("pathbrain.providers.get_provider", lambda: provider)

    t = time.time()
    watching({"through": _FakeTarget("through", series("..XXXXXXXX..", t0=t))})
    link_watch.start()          # already running; a no-op, but it must not write either
    link_watch.status()
    link_watch._sweep()
    link_watch._settle_due()
    link_watch.gap_summary(1.0)
    link_watch.recent_gaps(10)

    assert provider.calls == [], f"the link watch called the firewall: {provider.calls}"
    assert provider.reconfigures == 0


def test_two_distinct_gaps_close_together_are_two_rows(watching):
    """The de-dup must not swallow real events.

    Both paths derive a gap's start from the same samples, so one event seen twice arrives
    with the same instant — while two genuinely separate gaps need an answered ping between
    them and so start further apart than the de-dup window.
    """
    wid = _write_row()
    t = time.time()
    # "..XXXX.XXXX.." — two gaps with a single answered ping between them.
    watching({"through": _FakeTarget("through", series("..XXXX.XXXX..", t0=t))})
    link_watch._score_write({"write_id": wid, "op": "apply_many", "owner": None,
                             "start": t, "end": t + 10.0})
    with session_scope() as s:
        assert s.query(LinkGap).count() == 2


def test_a_write_registered_while_the_worker_drains_is_not_dropped(watching):
    """The pending list is appended from the write thread and rebuilt on the worker; a
    rebuild straddling an append would drop that window, leaving a write unmeasured."""
    watching({"through": _FakeTarget("through", series("." * 10, t0=time.time()))})
    now = time.time()
    link_watch.note_write(1, "apply", now - 100.0, now - 100.0)   # already due
    link_watch.note_write(2, "apply", now, now)                   # not due for POST_S
    link_watch._settle_due()
    assert [p["write_id"] for p in link_watch._state["pending"]] == [2]
