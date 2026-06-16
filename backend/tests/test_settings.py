"""Tests for settings fingerprinting and the correlation endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain.database import session_scope
from pathbrain.models import Run, RunStatus, ScoreResult
from pathbrain.settings_profile import diff_profiles, fingerprint, normalize, summarize
from pathbrain.providers.mock import MockProvider


def test_diff_profiles_reports_direction():
    a = [{"label": "wan", "target": "10ms", "quantum": 1514, "download_bandwidth": "880Mbit", "ecn": False}]
    b = [{"label": "wan", "target": "5ms", "quantum": 2640, "download_bandwidth": "1Gbit", "ecn": True}]
    changes = {c["field"]: c for c in diff_profiles(a, b)}
    # CoDel target lowered 10ms -> 5ms (the kind of win that should seed experiments)
    assert changes["target"]["direction"] == "lower"
    assert changes["target"]["from_value"] == "10ms"
    assert changes["target"]["to_value"] == "5ms"
    assert changes["quantum"]["direction"] == "higher"  # 1514 -> 2640
    assert changes["download_bandwidth"]["direction"] == "higher"  # 880Mbit -> 1Gbit
    assert changes["ecn"]["direction"] == "higher"  # off -> on
    assert "scheduler" not in changes  # unchanged fields are omitted


def test_diff_profiles_identical_is_empty():
    a = [{"label": "wan", "target": "5ms", "quantum": 1514}]
    assert diff_profiles(a, a) == []


def test_fingerprint_stable_and_distinct():
    base = normalize(MockProvider().discover())
    fp1 = fingerprint(base)
    fp2 = fingerprint(list(reversed(base)))  # order-independent
    assert fp1 == fp2
    changed = [dict(p) for p in base]
    changed[0]["quantum"] = 6000
    assert fingerprint(changed) != fp1


def test_summarize_is_human_readable():
    s = summarize(normalize(MockProvider().discover()))
    assert "q" in s and ":" in s


def _seed_run(
    fp: str,
    sops: float,
    when: datetime,
    completion: float | None = None,
    completion_metrics: dict | None = None,
) -> None:
    with session_scope() as session:
        run = Run(
            status=RunStatus.COMPLETE,
            created_at=when,
            settings_fingerprint=fp,
            settings=[{"label": "wan", "quantum": 1514}],
        )
        session.add(run)
        session.flush()
        session.add(
            ScoreResult(
                run_id=run.id,
                sops=sops,
                subscores={},
                weights_used={},
                metric_values={},
                completion=completion,
                completion_metric_values=completion_metrics,
            )
        )


def test_profiles_and_impact(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # Older profile "aaa" ~70 (6 runs), then a change to "bbb" ~85 (6 runs) so
    # both clear the default min_runs=5 confidence threshold.
    for i, s in enumerate([70, 72, 68, 71, 69, 70]):
        _seed_run("aaaaaaaaaaaa", s, t0 - timedelta(minutes=120 - i))
    for i, s in enumerate([84, 86, 85, 83, 87, 85]):
        _seed_run("bbbbbbbbbbbb", s, t0 - timedelta(minutes=30 - i))

    body = client.get("/api/settings/profiles").json()
    profiles = body["profiles"]
    fps = {p["fingerprint"] for p in profiles}
    assert {"aaaaaaaaaaaa", "bbbbbbbbbbbb"} <= fps
    assert profiles[0]["fingerprint"] == "bbbbbbbbbbbb"  # higher median first
    assert all(p["confident"] for p in profiles)  # 6 runs each >= min_runs
    assert body["min_runs"] == 5
    # Each profile tracks total iterations (default 1 per run here -> 6).
    assert all(p["iterations"] == 6 for p in profiles)

    # best_diff compares the best profile to the next-ranked one.
    bd = body["best_diff"]
    assert bd is not None
    assert bd["best"]["fingerprint"] == "bbbbbbbbbbbb"
    assert bd["comparison"]["fingerprint"] == "aaaaaaaaaaaa"
    assert bd["delta_abs"] > 0
    # These two profiles use identical seeded settings, so no field changes.
    assert bd["changes"] == []

    impact = client.get("/api/settings/impact").json()
    assert impact["changed"] is True
    assert impact["enough_data"] is True
    assert impact["before"]["fingerprint"] == "aaaaaaaaaaaa"
    assert impact["after"]["fingerprint"] == "bbbbbbbbbbbb"
    assert impact["delta_abs"] > 0
    assert impact["significant"] is True  # ~70 -> ~85 over 5%, both confident


def test_impact_not_significant_without_enough_runs(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    _seed_run("cccccccccccc", 60, t0 - timedelta(minutes=20))
    _seed_run("dddddddddddd", 90, t0 - timedelta(minutes=5))  # only 1 run each
    impact = client.get("/api/settings/impact").json()
    # A change is detected, but it must NOT be flagged significant on 1+1 runs.
    assert impact["changed"] is True
    assert impact["enough_data"] is False
    assert impact["significant"] is False


def test_backfill_stamps_null_runs(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # A run with no captured settings (NULL fingerprint).
    with session_scope() as session:
        run = Run(status=RunStatus.COMPLETE, created_at=t0)
        session.add(run)
        session.flush()
        session.add(ScoreResult(run_id=run.id, sops=77, subscores={}, weights_used={}, metric_values={}))

    resp = client.post("/api/settings/backfill")
    assert resp.status_code == 200
    assert resp.json()["updated"] >= 1
    assert resp.json()["fingerprint"]  # mock provider yields a stable fingerprint


# Kept last: seeds a distinct profile and only queries by its own fingerprint, so
# it can't perturb the order-sensitive profile/impact assertions above.
def test_profiles_expose_completion_axis(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(6):
        _seed_run(
            "completionfp1",
            80 + i,
            t0 - timedelta(minutes=60 - i),
            completion=70 + i,
            completion_metrics={"dns": 12.0, "tcp": 30.0, "tls": 40.0},
        )
    body = client.get("/api/settings/profiles").json()
    prof = next(p for p in body["profiles"] if p["fingerprint"] == "completionfp1")
    # Completion aggregates as its own axis, gated like SOPS.
    assert prof["completion"] is not None
    assert prof["completion"]["count"] == 6
    assert prof["completion"]["confident"] is True  # 6 >= min_runs (5)
    # Raw infra metric medians are exposed per profile.
    assert prof["completion_metrics"]["tls"]["median"] == 40.0
    assert prof["completion_metrics"]["dns"]["count"] == 6
