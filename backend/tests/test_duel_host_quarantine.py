"""S7: with the host-quiet gate on, a round measured on a busy NAS is recorded but not
counted, and the match runs a replacement round. Off (the default), nothing changes."""
from __future__ import annotations

import pytest

from pathbrain import duel as duel_mod
from pathbrain.config_store import save_config
from pathbrain.database import session_scope

from .test_duel_resilience import A, B, _mock_ring, _wait

HOST = {"host_cpu_pct": 95.0, "container_share_of_busy": 0.05}
QUIET = {"host_cpu_pct": 5.0}
SCORES = {A: 70.0, B: 62.0}


@pytest.fixture(autouse=True)
def _gate_off_afterwards():
    yield
    with session_scope() as s:
        save_config(s, {"host_quiet": {"enabled": False, "max_host_share": 0.0, "max_replacement_rounds": 10}})


def _gate(**block):
    with session_scope() as s:
        save_config(s, {"host_quiet": {"enabled": False, "max_host_share": 0.0,
                                       "max_replacement_rounds": 10, **block}})


def _session(monkeypatch, busy, gate: dict):
    """One session; ``busy(run_id)`` says which runs the NAS was busy for."""
    _mock_ring(monkeypatch, SCORES, lambda fp, n: None)
    _gate(**gate)
    reads: list[list] = []

    def readings(ids):
        reads.append(list(ids))
        return [HOST if busy(r) else QUIET for r in ids]

    monkeypatch.setattr(duel_mod, "_host_readings", readings)
    d = _wait(duel_mod.start(duration_minutes=10))
    assert d.error is None and d.matchups
    return d.matchups[0], reads


def test_off_by_default_reads_nothing_and_counts_every_round(monkeypatch):
    m, reads = _session(monkeypatch, lambda r: True, {})
    assert reads == []
    assert m["host_quarantined_rounds"] is None


def test_one_noisy_round_is_excluded_replaced_and_the_verdict_unchanged(monkeypatch):
    base, _ = _session(monkeypatch, lambda r: False, {"enabled": True})
    first_challenger_run = base["round_legs"][0]["challenger"]
    m, reads = _session(monkeypatch, lambda r: r == first_challenger_run, {"enabled": True})
    assert reads, "the gate read the host readings"
    assert [q["challenger"] for q in m["host_quarantined_rounds"]] == [first_challenger_run]
    assert first_challenger_run not in [r["challenger"] for r in m["round_legs"]]
    assert duel_mod.outcome(m) == duel_mod.outcome(base) == "incumbent"
    assert len(m["deltas"]) == len(base["deltas"])  # a replacement round took its place


def test_replacements_are_capped_so_a_busy_night_still_decides(monkeypatch):
    base, _ = _session(monkeypatch, lambda r: False, {"enabled": True})
    m, _ = _session(monkeypatch, lambda r: True, {"enabled": True, "max_replacement_rounds": 2})
    assert len(m["host_quarantined_rounds"]) == 2
    assert duel_mod.outcome(m) == duel_mod.outcome(base)
    assert len(m["deltas"]) == len(base["deltas"])


def test_each_replacement_costs_one_more_cycle_of_applies(monkeypatch):
    """The firewall cost: a replaced round is one more challenger leg and one more belt
    leg (one more apply each, at belt_every 2), never more."""
    counts = {}
    for name, busy, cap in (("base", lambda r: False, 10), ("two", lambda r: True, 2)):
        applied, _ = _mock_ring(monkeypatch, SCORES, lambda fp, n: None)
        _gate(enabled=True, max_replacement_rounds=cap)
        monkeypatch.setattr(duel_mod, "_host_readings", lambda ids, b=busy: [HOST if b(r) else QUIET for r in ids])
        d = _wait(duel_mod.start(duration_minutes=10))
        assert d.error is None
        counts[name] = len(applied)
    assert counts["two"] - counts["base"] == 4
