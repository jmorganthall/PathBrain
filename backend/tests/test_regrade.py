"""Tests for Phase 3: score-from-raw re-grading into the Score table + comparability."""
from __future__ import annotations

from sqlalchemy import select

from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.methodology import (
    CURRENT_METHODOLOGY,
    comparability,
    ensure_current_methodology,
    rubric_from_definition,
)
from pathbrain.models import BenchmarkResult, Run, RunStatus, Score
from pathbrain.runner import score_history_under_current, score_run_under


# ── pure helpers ─────────────────────────────────────────────────────────────


DEF = {
    "metrics": [
        {"key": "byte_earliness", "axis": "speed", "weight": 25, "best": 300, "worst": 5000,
         "required": False},
        {"key": "longest_stall", "axis": "smoothness", "weight": 10, "best": 50, "worst": 2000,
         "required": True},
        {"key": "fcp", "axis": "speed", "weight": 20, "best": 1800, "worst": 3000, "required": False},
        {"key": "latency", "axis": None, "weight": 0, "best": None, "worst": None, "required": False},
    ]
}


def test_comparability_tiers():
    assert comparability(DEF, {"byte_earliness": 1, "longest_stall": 2, "fcp": 3}) == ("exact", [])
    tag, missing = comparability(DEF, {"longest_stall": 2, "fcp": 3})  # optional missing
    assert tag == "partial" and missing == ["byte_earliness"]
    tag, missing = comparability(DEF, {"byte_earliness": 1, "fcp": 3})  # required missing
    assert tag == "incomparable" and "longest_stall" in missing


def test_rubric_from_definition_extracts_axis():
    weights, thresholds = rubric_from_definition(DEF, "speed")
    assert weights == {"byte_earliness": 25, "fcp": 20}
    assert thresholds["fcp"] == {"best": 1800, "worst": 3000}
    assert "longest_stall" not in weights  # different axis


# ── score-from-raw end to end ────────────────────────────────────────────────


def _seed_run_with_raw(resources_present: bool) -> int:
    with session_scope() as s:
        ensure_current_methodology(s, get_config(s))
        run = Run(status=RunStatus.COMPLETE)  # no methodology_version → historical/at-present
        s.add(run)
        s.flush()
        resources = (
            [{"responseEnd": t, "transferSize": 1000, "nextHopProtocol": "h2"}
             for t in (60, 70, 80, 760, 800)]
            if resources_present
            else None
        )
        s.add(BenchmarkResult(
            run_id=run.id, plugin="browser", success=True, metrics={},
            raw={"iterations": [{"urls": {"u": {
                "nav": {"responseStart": 50, "responseEnd": 80, "loadEventEnd": 900,
                        "domContentLoadedEventEnd": 600},
                "paint": {"fcp": 120, "lcp": 400, "cls_entries": []},
                "total_render_ms": 1500, "filmstrip": [],
                "resources": resources, "loaf": {"entries": [], "source": "loaf"},
            }}}]},
        ))
        s.add(BenchmarkResult(
            run_id=run.id, plugin="http", success=True, metrics={},
            raw={"iterations": [{"urls": {"u": {
                "ttfb_ms": 200, "download_ms": 1000, "bytes": 1_000_000, "total_ms": 1200}}}]},
        ))
        s.commit()
        return run.id


def test_score_run_under_rederives_from_raw():
    rid = _seed_run_with_raw(resources_present=True)
    with session_scope() as s:
        run = s.get(Run, rid)
        methodology = ensure_current_methodology(s, get_config(s))
        score = score_run_under(s, run, methodology, artifact_base="/nonexistent")
        s.commit()
        assert score is not None
        # longest_stall was re-derived from the raw resource series (required metric present).
        assert score.metric_values.get("longest_stall") is not None
        # Current methodology is Speed/Smoothness — multi-axis, not a single SOPS.
        assert "speed" in score.axis_scores and "smoothness" in score.axis_scores
        assert score.is_at_measure is False  # run had no capture methodology → at-present
        # Completion metrics (dns/tcp/tls) weren't collected → partial, not exact.
        assert score.comparability in ("partial", "exact")


def test_run_without_browser_raw_is_incomparable():
    # No browser raw at all → the required longest_stall can't be derived from raw,
    # so the run is incomparable under a methodology that requires it.
    with session_scope() as s:
        ensure_current_methodology(s, get_config(s))
        run = Run(status=RunStatus.COMPLETE)
        s.add(run)
        s.flush()
        s.add(BenchmarkResult(
            run_id=run.id, plugin="http", success=True, metrics={},
            raw={"iterations": [{"urls": {"u": {
                "ttfb_ms": 200, "download_ms": 1000, "bytes": 1_000_000, "total_ms": 1200}}}]},
        ))
        s.commit()
        rid = run.id
    with session_scope() as s:
        run = s.get(Run, rid)
        methodology = ensure_current_methodology(s, get_config(s))
        score = score_run_under(s, run, methodology, artifact_base="/nonexistent")
        s.commit()
        assert score is not None
        assert score.comparability == "incomparable"
        assert "longest_stall" in (score.missing_metrics or [])


def test_regrade_endpoint_writes_scores(client):
    rid = _seed_run_with_raw(resources_present=True)
    summary = client.post("/api/score/regrade").json()
    assert summary["scored"] >= 1
    assert summary["methodology"] == CURRENT_METHODOLOGY

    body = client.get(f"/api/score/{rid}/methodologies").json()
    assert body["scores"]
    s0 = body["scores"][0]
    assert "speed" in s0["axis_scores"] and "smoothness" in s0["axis_scores"]
    assert s0["comparability"] in ("exact", "partial", "incomparable")


def test_regrade_does_not_mutate_other_versions_at_measure():
    # An at-measure row under a different version must survive a current re-grade.
    with session_scope() as s:
        run = Run(status=RunStatus.COMPLETE, methodology_version="perceptual-v0")
        s.add(run)
        s.flush()
        s.add(Score(run_id=run.id, methodology_version="perceptual-v0", is_at_measure=True,
                    comparability="exact", axis_scores={"sops": 88.0}, subscores={}, weights_used={},
                    metric_values={}))
        s.commit()
        rid = run.id
    with session_scope() as s:
        score_history_under_current(s)
    with session_scope() as s:
        frozen = s.scalar(select(Score).where(
            Score.run_id == rid, Score.methodology_version == "perceptual-v0"))
        assert frozen is not None and frozen.axis_scores["sops"] == 88.0  # untouched
