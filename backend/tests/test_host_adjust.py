"""Host-adjusted duel margins (``host_adjust``): the measuring machine's own speed is taken
out of a round, only when the fit earns its place, and the raw margin is always kept."""
from __future__ import annotations

import math
import random

import pytest

from pathbrain import challenger as challenger_mod
from pathbrain import duel as duel_mod
from pathbrain import host_adjust as H
from pathbrain.database import session_scope
from pathbrain.models import Duel, DuelStatus, Run, RunStatus, Score

from tests.test_duel import _no_settle, _wait_finish

K = len(H.KEYS)


def _pairs(n: int, beta: list[float], noise: float, seed: int = 7):
    rng = random.Random(seed)
    xs, ys = [], []
    for _ in range(n):
        x = [rng.gauss(0, 0.15) for _ in range(K)]
        xs.append(x)
        ys.append(sum(b * v for b, v in zip(beta, x)) + rng.gauss(0, noise))
    return xs, ys


def test_a_real_host_effect_is_recovered_and_kept():
    beta = [-7.0, -0.5, 0.0, -5.5]
    xs, ys = _pairs(400, beta, noise=0.6)
    m = H.evaluate(xs, ys)
    assert m.active, m.why
    assert m.gain > 0.3
    for got, want in zip(m.beta, beta):
        assert abs(got - want) < 1.0


def test_readings_that_explain_nothing_are_not_used():
    xs, ys = _pairs(400, [0.0] * K, noise=1.0)
    m = H.evaluate(xs, ys)
    assert not m.active
    assert "held-out" in m.why


def test_too_few_pairs_is_no_adjustment_and_says_so():
    xs, ys = _pairs(H.MIN_PAIRS - 1, [-7.0, 0, 0, 0], noise=0.1)
    m = H.evaluate(xs, ys)
    assert not m.active and str(H.MIN_PAIRS) in m.why


def test_adjust_removes_the_host_share_and_never_guesses_a_missing_reading():
    m = H.HostModel([-7.0, 0.0, 0.0, 0.0], 400, 0.4, "test")
    belt = [math.log(350), math.log(120), math.log(2000), math.log(280)]
    slow = [belt[0] + 0.3] + belt[1:]
    # The challenger leg ran on a host 35% slower at context setup and lost 2.1 points for it.
    assert abs(m.adjust(-2.1, slow, [belt, belt]) - 0.0) < 1e-9
    assert m.adjust(-2.1, None, [belt]) is None
    assert m.adjust(-2.1, slow, [belt, None]) is None
    assert H.HostModel(None, 0, None, "off").adjust(-2.1, slow, [belt]) is None


def test_one_wedged_phase_is_clipped_not_trusted():
    m = H.HostModel([0.0, 0.0, -1.0, 0.0], 400, 0.4, "test")
    belt = [math.log(v) for v in (350, 120, 2000, 280)]
    wedged = belt[:2] + [math.log(2000 * 50)] + belt[3:]  # a 100 s timing read
    assert m.adjust(0.0, wedged, [belt]) == H.CLIP  # −β·clip(Δ) = 1.0, never 3.9


def _health(ctx: float) -> dict:
    return {"readings": {"context_ms": ctx, "close_ms": 120.0, "reads_ms": 2000.0, "nav_render_ms": 280.0}}


def test_noise_pairs_are_same_profile_legs_close_together():
    version = "host-adjust-test-v1"
    with session_scope() as s:
        s.query(Duel).delete()
        ids = []
        for fp, ov, ctx in [("A", 60.0, 350), ("B", 61.0, 350), ("A", 58.0, 470), ("B", 61.0, 350),
                            ("A", 60.0, 350)]:
            r = Run(status=RunStatus.COMPLETE, settings_fingerprint=fp, instrument_health=_health(ctx))
            s.add(r)
            s.flush()
            s.add(Score(run_id=r.id, methodology_version=version, comparability="exact",
                        axis_scores={"overall": ov}))
            ids.append(r.id)
        s.add(Duel(status=DuelStatus.COMPLETE, run_ids=ids))
    with session_scope() as s:
        xs, ys = H.noise_pairs(s, version)
    # A→A (2 apart), B→B (2 apart), A→A (2 apart), A→A (4 apart); never A→B.
    assert sorted(ys) == sorted([-2.0, 0.0, 2.0, 0.0])
    slow_leg = [x for x, y in zip(xs, ys) if y == -2.0][0]
    assert abs(slow_leg[0] - math.log(470 / 350)) < 1e-9
    with session_scope() as s:  # the suite shares one database
        s.query(Duel).delete()
        s.query(Score).filter(Score.run_id.in_(ids)).delete(synchronize_session=False)
        s.query(Run).filter(Run.id.in_(ids)).delete(synchronize_session=False)


def test_a_duel_decides_on_adjusted_margins_and_keeps_the_raw_ones(monkeypatch):
    """Two equal profiles; the challenger's legs happen to land on a slower host and lose 2.1
    points for it. Raw, that is a clear loss; adjusted, it is the tie it really is — and the
    record carries both series so the difference is auditable."""
    import pathbrain.api.routes_settings as rs

    with session_scope() as s:
        s.query(Duel).delete()
    applied: list[str] = []
    fake_field = {
        "best_fingerprint": "inc0000000x",
        "profiles": [
            {"fingerprint": "inc0000000x", "label": "incumbent", "settings": [{"label": "wan", "quantum": 1514}]},
            {"fingerprint": "cha0000000x", "label": "challenger", "settings": [{"label": "wan", "quantum": 300}]},
        ],
    }
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: fake_field)
    monkeypatch.setattr(
        rs, "_compute_heirs", lambda result, session, live=None: {"items": [{"fingerprint": "cha0000000x"}]}
    )
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: applied.append(fp))
    _no_settle()

    by_run: dict[int, str] = {}
    seq = {"n": 0}
    rng = random.Random(3)
    noise: dict[int, float] = {}

    def fake_chunk(label, notes, iterations, teardown=True, job_group=None, job_group_total=None, **_):
        seq["n"] += 1
        rid = 77000 + seq["n"]
        by_run[rid] = applied[-1]
        noise[rid] = rng.gauss(0, 0.05)
        return (rid, True, iterations)

    belt = [math.log(v) for v in (350, 120, 2000, 280)]
    slow = [belt[0] + 0.3] + belt[1:]
    monkeypatch.setattr(duel_mod, "run_chunk", fake_chunk)
    monkeypatch.setattr(
        duel_mod, "_run_overall",
        lambda rid, ver: 60.0 + noise[rid] - (2.1 if by_run[rid] == "cha0000000x" else 0.0),
    )
    monkeypatch.setattr(H, "model_for", lambda session, ver: H.HostModel([-7.0, 0.0, 0.0, 0.0], 400, 0.4, "fixed"))
    monkeypatch.setattr(
        H, "leg_vector", lambda session, rid: slow if by_run.get(rid) == "cha0000000x" else belt
    )

    d = _wait_finish(duel_mod.start(duration_minutes=10))
    assert d.status == DuelStatus.COMPLETE, d.error
    m = d.matchups[0]
    assert m["verdict"] != "incumbent", m["reason"]
    assert m["host_adjust"]["active"] is True
    assert m["host_adjusted_rounds"] == m["pairs"] > 0
    assert all(abs(r + 2.1) < 0.3 for r in m["raw_deltas"])
    assert all(abs(a) < 0.3 for a in m["deltas"])


def test_the_pooled_adjustment_reads_a_run_against_the_fields_reference_machine():
    """R9: the ring adjusts a round against its own belt legs; the pooled record has no pair,
    so its reference is the field's mean log host vector, and a run's adjustment is β against
    that — the same β, the same clip, and None rather than a guess whenever anything is
    missing."""
    fast = [math.log(v) for v in (300, 120, 2000, 280)]
    slow = [fast[0] + 0.4] + fast[1:]
    ref = H.pooled_reference([fast, slow, None])
    assert ref is not None and abs(ref[0] - (fast[0] + 0.2)) < 1e-9 and ref[1:] == fast[1:]
    m = H.HostModel([-7.0, 0.0, 0.0, 0.0], 400, 0.4, "test")
    # The fast run gained 1.4 points from its machine, the slow one lost 1.4; the reference
    # machine itself is worth nothing.
    assert H.pooled_adjustment(m, fast, ref) == pytest.approx(+1.4)
    assert H.pooled_adjustment(m, slow, ref) == pytest.approx(-1.4)
    assert H.pooled_adjustment(m, ref, ref) == pytest.approx(0.0)
    # Clipped like the ring: a run ten times slower is priced at ±CLIP, never at ln(10).
    assert H.pooled_adjustment(m, [fast[0] + math.log(10)] + fast[1:], ref) == pytest.approx(-7.0 * H.CLIP)
    # Nothing is ever guessed.
    assert H.pooled_adjustment(None, fast, ref) is None
    assert H.pooled_adjustment(H.HostModel(None, 0, None, "off"), fast, ref) is None
    assert H.pooled_adjustment(m, None, ref) is None
    assert H.pooled_adjustment(m, fast, None) is None
    assert H.pooled_reference([]) is None and H.pooled_reference([None]) is None
    # The reference reads back in the readings' own units for the page.
    assert H.describe_reference(ref)["close_ms"] == 120.0
    assert H.describe_reference(None) is None
