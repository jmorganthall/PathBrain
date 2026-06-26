"""Tests for quarantining legacy (pre-current-rubric) scores."""
from __future__ import annotations

import pytest

from pathbrain.database import session_scope
from pathbrain.metrics import has_latest_metrics
from pathbrain.models import BenchmarkResult, Run, RunStatus, ScoreResult


@pytest.fixture(autouse=True)
def _isolate_runs():
    def _wipe():
        with session_scope() as s:
            s.query(ScoreResult).delete()
            s.query(BenchmarkResult).delete()
            s.query(Run).delete()

    _wipe()
    yield
    _wipe()


def _seed(metric_values: dict, sops: float = 80.0) -> int:
    with session_scope() as s:
        run = Run(status=RunStatus.COMPLETE)
        s.add(run)
        s.flush()
        s.add(
            ScoreResult(
                run_id=run.id, sops=sops, subscores={}, weights_used={},
                metric_values=metric_values,
            )
        )
        return run.id


def test_has_latest_metrics_helper():
    # Speed Index marks the trajectory-aware rubric.
    assert has_latest_metrics({"speed_index": 1500, "fcp": 400, "lcp": 600}) is True
    assert has_latest_metrics({"fcp": 400, "lcp": 600, "ttfb": 200}) is False  # no SI
    assert has_latest_metrics({"ttfb": 200, "render": 1500}) is False
    assert has_latest_metrics({}) is False
    assert has_latest_metrics(None) is False


def test_history_flags_legacy(client):
    current = _seed({"speed_index": 1500.0, "fcp": 400.0, "lcp": 600.0})
    legacy = _seed({"ttfb": 200.0})
    by_id = {r["id"]: r for r in client.get("/api/history").json()}
    assert by_id[current]["legacy"] is False
    assert by_id[legacy]["legacy"] is True


def test_run_detail_flags_legacy(client):
    legacy = _seed({"ttfb": 200.0})
    body = client.get(f"/api/results/{legacy}").json()
    assert body["score"]["legacy"] is True


def test_series_excludes_legacy_by_default(client):
    current = _seed({"speed_index": 1500.0, "fcp": 400.0, "lcp": 600.0})
    _seed({"ttfb": 200.0})  # legacy
    default = client.get("/api/history/series").json()["points"]
    assert {p["run_id"] for p in default} == {current}
    allpts = client.get("/api/history/series?include_legacy=true").json()["points"]
    assert len(allpts) == 2


def test_rolling_excludes_legacy(client):
    _seed({"speed_index": 1500.0, "fcp": 400.0, "lcp": 600.0}, sops=90.0)
    _seed({"ttfb": 200.0}, sops=40.0)  # legacy — must not drag the headline
    body = client.get("/api/score/rolling?hours=24").json()
    assert body["count"] == 1
    assert body["median"] == 90.0
