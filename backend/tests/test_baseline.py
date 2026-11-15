"""Tests for the run baseline endpoint (this-run-vs-best-profile comparison)."""
from __future__ import annotations

import pytest

from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.methodology import ensure_current_methodology
from pathbrain.models import BenchmarkResult, Run, RunStatus, Score, ScoreResult


@pytest.fixture(autouse=True)
def _isolate_runs():
    """Wipe runs around each test.

    The baseline endpoint picks the best profile *globally*, so these tests need a
    clean slate — and must not leave fingerprinted/scored runs behind that would
    perturb the shared session-scoped DB used by other suites (e.g. settings)."""

    def _wipe():
        with session_scope() as session:
            session.query(ScoreResult).delete()
            session.query(Score).delete()
            session.query(BenchmarkResult).delete()
            session.query(Run).delete()

    _wipe()
    yield
    _wipe()


def _seed_run(fp: str | None, lookup_ms: float, sops: float | None,
              overall: float | None = None) -> int:
    """A run with a legacy ``sops`` and (S26) a current-methodology Overall, which is what
    the baseline ranks by; ``overall`` defaults to the same number."""
    overall = sops if overall is None else overall
    with session_scope() as session:
        version = ensure_current_methodology(session, get_config(session)).version
        run = Run(
            status=RunStatus.COMPLETE,
            settings_fingerprint=fp,
            settings=[{"label": "wan", "quantum": 1514}] if fp else None,
        )
        session.add(run)
        session.flush()
        session.add(
            BenchmarkResult(
                run_id=run.id,
                plugin="dns",
                success=True,
                metrics={"lookup_ms": lookup_ms},
            )
        )
        if sops is not None:
            session.add(
                ScoreResult(
                    run_id=run.id, sops=sops, subscores={}, weights_used={}, metric_values={}
                )
            )
        if overall is not None:
            session.add(Score(run_id=run.id, methodology_version=version, is_at_measure=True,
                              comparability="exact", axis_scores={"overall": overall},
                              subscores={}, weights_used={}, metric_values={}))
        return run.id


def test_baseline_compares_against_best_profile(client):
    # A "good" profile (high SOPS, fast 10ms lookups) and a "bad" profile
    # (low SOPS, slow 40ms lookups). A run on the bad profile should be compared
    # against the good profile's average, not its own.
    good = "goodprofile1"
    bad = "badprofile01"
    for v in (8.0, 10.0, 12.0):
        _seed_run(good, v, sops=90.0)  # best profile: avg lookup 10ms
    for v in (38.0, 40.0, 42.0):
        _seed_run(bad, v, sops=50.0)
    target = _seed_run(bad, 40.0, sops=50.0)  # a run on the worse profile

    body = client.get(f"/api/results/{target}/baseline").json()
    assert body["scope"] == "best_profile"
    assert body["profile_fingerprint"] == good
    assert body["is_best_profile"] is False
    assert body["profile_median_sops"] == 90.0
    assert body["metrics"]["dns"]["lookup_ms"] == 10.0  # best profile's average


def test_baseline_flags_run_on_best_profile(client):
    best = "bestprofileX"
    worse = "worseprofile"
    for v in (5.0, 6.0, 7.0):
        _seed_run(best, v, sops=95.0)
    for v in (50.0, 51.0, 52.0):
        _seed_run(worse, v, sops=40.0)
    target = _seed_run(best, 6.0, sops=95.0)  # viewing a run on the best profile

    body = client.get(f"/api/results/{target}/baseline").json()
    assert body["scope"] == "best_profile"
    assert body["profile_fingerprint"] == best
    assert body["is_best_profile"] is True
    # Compared against the best profile's own average, excluding the viewed run.
    assert body["metrics"]["dns"]["lookup_ms"] == 6.0


def test_baseline_falls_back_to_recent_without_profiles(client):
    # Runs with no settings fingerprint and no scores → recent-runs fallback.
    _seed_run(None, 40.0, sops=None)
    target = _seed_run(None, 12.0, sops=None)
    body = client.get(f"/api/results/{target}/baseline").json()
    assert body["scope"] == "all"
    assert body["is_best_profile"] is False
    assert "dns" in body["metrics"]


def test_baseline_ranks_by_the_versioned_overall_not_the_legacy_sops(client):
    """S26: the legacy SOPS says the first profile is best; the current methodology's
    Overall (what the page labels it) says the second is, and the Overall decides."""
    for v in (8.0, 10.0, 12.0):
        _seed_run("sopsfavour01", v, sops=95.0, overall=40.0)
    for v in (30.0, 32.0, 34.0):
        _seed_run("overallbest1", v, sops=20.0, overall=88.0)
    target = _seed_run("sopsfavour01", 9.0, sops=95.0, overall=40.0)
    body = client.get(f"/api/results/{target}/baseline").json()
    assert body["profile_fingerprint"] == "overallbest1"
    assert body["profile_median_sops"] == 88.0


def test_baseline_404_for_missing_run(client):
    assert client.get("/api/results/99999999/baseline").status_code == 404
