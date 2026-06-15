"""Tests for settings fingerprinting and the correlation endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain.database import session_scope
from pathbrain.models import Run, RunStatus, ScoreResult
from pathbrain.settings_profile import fingerprint, normalize, summarize
from pathbrain.providers.mock import MockProvider


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


def _seed_run(fp: str, sops: float, when: datetime) -> None:
    with session_scope() as session:
        run = Run(
            status=RunStatus.COMPLETE,
            created_at=when,
            settings_fingerprint=fp,
            settings=[{"label": "wan", "quantum": 1514}],
        )
        session.add(run)
        session.flush()
        session.add(ScoreResult(run_id=run.id, sops=sops, subscores={}, weights_used={}, metric_values={}))


def test_profiles_and_impact(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # Older profile "aaa" ~70, then a change to "bbb" ~85.
    for i, s in enumerate([70, 72, 68]):
        _seed_run("aaaaaaaaaaaa", s, t0 - timedelta(minutes=60 - i))
    for i, s in enumerate([84, 86, 85]):
        _seed_run("bbbbbbbbbbbb", s, t0 - timedelta(minutes=10 - i))

    profiles = client.get("/api/settings/profiles").json()["profiles"]
    fps = {p["fingerprint"] for p in profiles}
    assert {"aaaaaaaaaaaa", "bbbbbbbbbbbb"} <= fps
    # Best profile (higher median) is sorted first.
    assert profiles[0]["fingerprint"] == "bbbbbbbbbbbb"

    impact = client.get("/api/settings/impact").json()
    assert impact["changed"] is True
    assert impact["before"]["fingerprint"] == "aaaaaaaaaaaa"
    assert impact["after"]["fingerprint"] == "bbbbbbbbbbbb"
    assert impact["delta_abs"] > 0
    assert impact["significant"] is True  # ~70 -> ~85 is well over 5%
