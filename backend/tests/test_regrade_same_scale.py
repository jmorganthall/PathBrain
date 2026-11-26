"""A same-scale re-grade (v19 over v17) copies the sibling's scores once copying is proven.

Two versions on one scale grade every run alike, so re-scoring ~40k runs from cached metrics
or raw only to write the same numbers under a new label wasted hours while the crown and the
ring read a half-graded version. The pass now scores the first runs the usual way, compares
each with its sibling row, and copies the rest only if every comparison matched.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from pathbrain import runner
from pathbrain.config_store import get_config, save_config
from pathbrain.database import session_scope
from pathbrain.methodology import METHODOLOGY_REGISTRY, ensure_current_methodology, latest_score, same_scale
from pathbrain.models import Run, RunStatus, Score

FIELDS = runner._CARRIED_FIELDS


def _current() -> str:
    with session_scope() as s:
        return ensure_current_methodology(s, get_config(s)).version


def _sibling(version: str) -> str:
    sib = next((v for v in METHODOLOGY_REGISTRY if v != version and same_scale(v, version)), None)
    if sib is None:
        pytest.skip(f"no registry version shares {version}'s scale")
    return sib


def _row(overall: float) -> dict:
    return {"comparability": "exact", "missing_metrics": None,
            "axis_scores": {"overall": overall, "speed": overall - 1}, "subscores": {"fcp": overall},
            "weights_used": {"fcp": 1.0}, "metric_values": {"fcp": 1200.0}, "bands": {}}


def _seed(version: str, overalls: list[float]) -> list[int]:
    with session_scope() as s:
        ids = []
        for ov in overalls:
            run = Run(status=RunStatus.COMPLETE)
            s.add(run)
            s.flush()
            s.add(Score(run_id=run.id, methodology_version=version, is_at_measure=False, **_row(ov)))
            ids.append(run.id)
        return ids


def _clear(ids):
    with session_scope() as s:
        s.execute(delete(Score).where(Score.run_id.in_(ids)))
        s.execute(delete(Run).where(Run.id.in_(ids)))


def test_copying_starts_only_after_the_proof_runs_match(monkeypatch):
    monkeypatch.setattr(runner, "SAME_SCALE_PROOF_RUNS", 2)
    old = _current()
    new = _sibling(old)
    ids = _seed(old, [61.0, 62.0, 63.0])
    try:
        with session_scope() as s:
            carry = runner.SameScaleCarry(s, new, ids)
            assert carry.siblings and not carry.copying
            for rid, ov in zip(ids[:2], (61.0, 62.0)):
                assert not carry.can_copy(rid)
                carry.check(rid, SimpleNamespace(**_row(ov + 1e-9)))  # a recomputed float
            assert carry.copying and carry.can_copy(ids[2])
            copied = carry.copy(s, ids[2])
            s.flush()
            assert copied.methodology_version == new and copied.is_at_measure is False
            assert copied.axis_scores == {"overall": 63.0, "speed": 62.0}
            s.commit()
        with session_scope() as s:
            row = s.scalars(select(Score).where(Score.run_id == ids[2], Score.methodology_version == new,
                                                latest_score())).one()
            assert all(runner._same_value(getattr(row, f), _row(63.0)[f]) for f in FIELDS)
    finally:
        _clear(ids)


def test_one_mismatch_turns_the_copy_off_for_the_pass(monkeypatch):
    monkeypatch.setattr(runner, "SAME_SCALE_PROOF_RUNS", 2)
    old = _current()
    new = _sibling(old)
    ids = _seed(old, [61.0, 62.0, 63.0])
    try:
        with session_scope() as s:
            carry = runner.SameScaleCarry(s, new, ids)
            carry.check(ids[0], SimpleNamespace(**_row(61.0)))
            carry.check(ids[1], SimpleNamespace(**{**_row(62.0), "subscores": {"fcp": 50.0}}))
            assert carry.off_reason and "subscores" in carry.off_reason
            carry.check(ids[2], SimpleNamespace(**_row(63.0)))
            assert not carry.copying and not carry.can_copy(ids[2])
    finally:
        _clear(ids)


def test_a_version_with_no_same_scale_sibling_never_copies():
    with session_scope() as s:
        carry = runner.SameScaleCarry(s, "test-own-scale-v0", [1, 2, 3])
        assert carry.off_reason == "no same-scale version" and not carry.copying


def test_the_regrade_pass_copies_same_scale_runs_and_scores_the_rest(monkeypatch):
    """End to end: with the current pin on a same-scale sibling, the pass scores the proof
    runs, copies the rest of the runs that have a sibling row, and reports both counts."""
    monkeypatch.setattr(runner, "SAME_SCALE_PROOF_RUNS", 1)
    old = _current()
    new = _sibling(old)
    ids = _seed(old, [70.0, 71.0, 72.0, 73.0])

    def fake_score(session, run, methodology, artifact_base=None):
        # Score "from raw": the same numbers the sibling holds, so the proof passes.
        sib = session.scalars(select(Score).where(Score.run_id == run.id,
                                                  Score.methodology_version == old)).first()
        if sib is None:
            return None
        return runner.SameScaleCarry(session, methodology.version, [run.id]).copy(session, run.id)

    with session_scope() as s:
        original = get_config(s).get("methodology_version")
        save_config(s, {"methodology_version": new})
    monkeypatch.setattr(runner, "score_run_under", fake_score)
    monkeypatch.setattr(runner, "regrade_order", lambda session, mi=None: (
        list(ids), {"ordered_by": "test", "unscorable": 0}))
    try:
        with session_scope() as s:
            out = runner.score_history_under_current(s)
        assert out["methodology"] == new
        assert out["copied"] == 3 and out["scored"] == 4
        assert out["same_scale"]["copying"] is True
        with session_scope() as s:
            got = {r.run_id: r.axis_scores["overall"] for r in s.scalars(
                select(Score).where(Score.run_id.in_(ids), Score.methodology_version == new))}
        assert got == {ids[0]: 70.0, ids[1]: 71.0, ids[2]: 72.0, ids[3]: 73.0}
    finally:
        with session_scope() as s:
            save_config(s, {"methodology_version": original})
        _clear(ids)
