"""Tests for the (run × methodology) Score table (Phase 2)."""
from __future__ import annotations

from sqlalchemy import select

from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.methodology import (
    ensure_current_methodology,
    record_at_measure,
    score_fields_from_score_result,
)
from pathbrain.models import Run, RunStatus, Score, ScoreResult


def _score_for(session, run_id: int, version: str) -> Score | None:
    return session.scalar(
        select(Score).where(Score.run_id == run_id, Score.methodology_version == version)
    )


def test_score_fields_merge_both_axes():
    sr = ScoreResult(
        run_id=1, sops=78.0, sops_stdev=1.0, sops_min=76.0, sops_max=80.0,
        subscores={"byte_earliness": 90.0}, weights_used={"byte_earliness": 1.0},
        metric_values={"byte_earliness_ms": 351.0, "longest_stall": 398.0},
        completion=70.0, completion_subscores={"dns": 88.0},
        completion_weights_used={"dns": 1.0}, completion_metric_values={"lookup_ms": 12.0},
    )
    f = score_fields_from_score_result(sr)
    assert f["axis_scores"] == {"sops": 78.0, "completion": 70.0}
    assert f["subscores"] == {"byte_earliness": 90.0, "dns": 88.0}
    assert f["metric_values"]["lookup_ms"] == 12.0 and f["metric_values"]["longest_stall"] == 398.0
    assert f["bands"]["sops"] == {"stdev": 1.0, "min": 76.0, "max": 80.0}
    assert f["comparability"] == "exact"  # carries the longest_stall marker


def test_legacy_run_is_incomparable_at_measure():
    # A run lacking the current-rubric marker is incomparable under its methodology.
    sr = ScoreResult(
        run_id=1, sops=95.0, subscores={"fcp": 90.0}, weights_used={"fcp": 1.0},
        metric_values={"fcp": 300.0},  # no longest_stall -> legacy
    )
    f = score_fields_from_score_result(sr)
    assert f["comparability"] == "incomparable"
    assert "longest_stall" in (f["missing_metrics"] or [])


def test_record_at_measure_writes_score_and_stamps_run():
    with session_scope() as s:
        version = ensure_current_methodology(s, get_config(s)).version
        run = Run(status=RunStatus.COMPLETE)
        s.add(run)
        s.flush()
        sr = ScoreResult(
            run_id=run.id, sops=72.0, rubric_version=version,
            subscores={"longest_stall": 50.0}, weights_used={"longest_stall": 1.0},
            metric_values={"longest_stall": 300.0},
        )
        s.add(sr)
        score = record_at_measure(s, run, sr, version)
        s.commit()
        rid = run.id

    with session_scope() as s:
        row = _score_for(s, rid, version)
        assert row is not None and row.is_at_measure and row.comparability == "exact"
        assert row.axis_scores["sops"] == 72.0
        assert s.get(Run, rid).methodology_version == version
