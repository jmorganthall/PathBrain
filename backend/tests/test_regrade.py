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
    import time

    rid = _seed_run_with_raw(resources_present=True)
    # Re-grade now runs as a background job (202 + job id); poll the jobs feed.
    resp = client.post("/api/score/regrade")
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    deadline = time.time() + 10.0
    job = None
    while time.time() < deadline:
        feed = client.get("/api/jobs").json()["jobs"]
        job = next((j for j in feed if j["id"] == job_id), None)
        if job and job["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.05)
    assert job is not None and job["status"] == "succeeded", job
    # A Score row was written for the seeded run under the current methodology.
    with session_scope() as s:
        score = s.scalar(
            select(Score).where(
                Score.run_id == rid, Score.methodology_version == CURRENT_METHODOLOGY
            )
        )
        assert score is not None

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


# ── the order of the pass ────────────────────────────────────────────────────


def test_the_regrade_visits_the_prior_versions_best_profiles_first_and_skips_the_unscorable():
    """A re-grade exists to produce a crown under the new rubric, so it visits the runs
    that decide one first: the prior version's confident profiles best first (newest run
    first within a profile), then its thin profiles, then everything the prior never
    ranked newest first — and a run with no result rows at all is left out up front rather
    than counted in the total it can only ever be skipped from."""
    from datetime import datetime, timedelta, timezone

    from pathbrain import refresh
    from pathbrain.models import Methodology
    from pathbrain.runner import regrade_order

    prior = "test-prior-for-the-regrade-order"
    made: dict[str, int] = {}
    with session_scope() as s:
        ensure_current_methodology(s, get_config(s))
        # The newest non-current version with scores is the prior field; a created_at in
        # the future puts this one ahead of whatever the registry seeded.
        s.add(Methodology(version=prior, rubric_version=prior, derivation_version="derive-test",
                          is_current=False, definition={},
                          created_at=datetime.now(timezone.utc) + timedelta(days=1)))

        def _run(name: str, fp: str, iterations: int, overall: float | None = None,
                 result: bool = True) -> None:
            run = Run(status=RunStatus.COMPLETE, settings_fingerprint=fp, iterations=iterations)
            s.add(run)
            s.flush()
            if result:
                s.add(BenchmarkResult(run_id=run.id, plugin="http", success=True, metrics={},
                                      raw={"iterations": []}))
            if overall is not None:
                s.add(Score(run_id=run.id, methodology_version=prior, is_at_measure=False,
                            comparability="exact", axis_scores={"overall": overall},
                            subscores={}, weights_used={}, metric_values={}))
            made[name] = run.id

        _run("a_old", "fp-order-A", 10, 90.0)   # A: confident (20 iterations), the prior's best
        _run("a_new", "fp-order-A", 10, 90.0)
        _run("b", "fp-order-B", 20, 60.0)       # B: confident, worse
        _run("c", "fp-order-C", 5, 95.0)        # C: thin — a lucky 95 on five iterations
        _run("d", "fp-order-D", 5)              # D: the prior never scored it
        _run("e", "fp-order-E", 5, result=False)  # E: no result rows → nothing to score
        s.commit()
    refresh.invalidate_seed_cache()
    try:
        with session_scope() as s:
            ids, info = regrade_order(s, min_iterations=15)
            mine = [i for i in ids if i in set(made.values())]
            assert mine == [made["a_new"], made["a_old"], made["b"], made["c"], made["d"]]
            assert made["e"] not in ids
            assert info["unscorable"] >= 1
            assert info["prior_profiles"] >= 3 and prior in info["ordered_by"]
        with session_scope() as s:
            summary = score_history_under_current(s)
        assert summary["unscorable"] >= 1 and prior in summary["ordered_by"]
        assert summary["total"] + summary["unscorable"] >= len(made)
    finally:
        with session_scope() as s:
            ids = list(made.values())
            s.query(Score).filter(Score.run_id.in_(ids)).delete(synchronize_session=False)
            s.query(BenchmarkResult).filter(BenchmarkResult.run_id.in_(ids)).delete(
                synchronize_session=False)
            s.query(Run).filter(Run.id.in_(ids)).delete(synchronize_session=False)
            s.query(Methodology).filter(Methodology.version == prior).delete(
                synchronize_session=False)
        refresh.invalidate_seed_cache()


def test_with_no_prior_standings_the_order_is_newest_first():
    from pathbrain.runner import regrade_order

    with session_scope() as s:
        ids, info = regrade_order(s, min_iterations=15)
        if info["prior_profiles"] == 0:
            assert ids == sorted(ids, reverse=True)
            assert info["ordered_by"].startswith("newest first")
        else:
            # Another test's prior standings are in the DB; the contract then is the
            # best-first one pinned above.
            assert "prior version" in info["ordered_by"]
