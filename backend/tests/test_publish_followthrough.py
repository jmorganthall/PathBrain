"""A publish finishes itself: once history is re-graded, the prior version's winners are
queued for their first chunk under the new one — the step that used to be a button."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from pathbrain import job_queue
from pathbrain import publish_followthrough as pf
from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.methodology import ensure_current_methodology
from pathbrain.models import Methodology, Run, RunStatus, Score

PRIOR = "test-reseed-prior-v0"
ENV = {"scheduler": "fq_codel", "queues": 1, "upload_bandwidth": "40Mbit", "flows": 1024}


def _settings(quantum: int) -> list[dict]:
    return [{"label": "wan-download", **ENV, "quantum": quantum}]


def _current() -> str:
    with session_scope() as s:
        return ensure_current_methodology(s, get_config(s)).version


def _seed(session, specs, current_specs=()):
    """A prior version (newest by created_at) with one scored run per (fp, overall, iters,
    quantum), plus runs scored under the CURRENT version for ``current_specs`` (fp, iters)."""
    when = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=400)
    session.add(Methodology(version=PRIOR, rubric_version=PRIOR, derivation_version="derive-v14",
                            definition={"axes": [], "metrics": []}, is_current=False, created_at=when))
    ids = []
    for fp, ov, iters, quantum in specs:
        run = Run(status=RunStatus.COMPLETE, created_at=when, settings_fingerprint=fp,
                  settings=_settings(quantum), iterations=iters, per_iteration_ms=1000.0)
        session.add(run)
        session.flush()
        session.add(Score(run_id=run.id, methodology_version=PRIOR, comparability="exact",
                          axis_scores={"overall": ov}))
        ids.append(run.id)
    current = ensure_current_methodology(session, get_config(session)).version
    for fp, iters in current_specs:
        run = Run(status=RunStatus.COMPLETE, created_at=when, settings_fingerprint=fp,
                  settings=_settings(1), iterations=iters, per_iteration_ms=1000.0)
        session.add(run)
        session.flush()
        session.add(Score(run_id=run.id, methodology_version=current, comparability="exact",
                          axis_scores={"overall": 50.0}))
        ids.append(run.id)
    return ids


def _clear(ids):
    with session_scope() as s:
        s.execute(delete(Score).where(Score.run_id.in_(ids)))
        s.execute(delete(Run).where(Run.id.in_(ids)))
        s.execute(delete(Methodology).where(Methodology.version == PRIOR))


SPECS = [("rshi00000000", 90.0, 20, 1514), ("rsthin000000", 95.0, 2, 300), ("rslo00000000", 30.0, 20, 900)]


def test_the_plan_names_the_prior_winners_that_lack_data_under_the_current_version():
    with session_scope() as s:
        ids = _seed(s, SPECS, current_specs=[("rslo00000000", 10)])
    try:
        with session_scope() as s:
            plan = pf.plan_prior_winners(s, top=2, iterations=5)
        assert plan["prior_version"] == PRIOR and plan["version"] == _current()
        # The thin lucky 95 was never a winner: confident under the prior, best first.
        assert [w["fingerprint"] for w in plan["winners"]] == ["rshi00000000", "rslo00000000"]
        assert [n["fingerprint"] for n in plan["needed"]] == ["rshi00000000"]
        assert plan["needed"][0]["prior_overall"] == 90.0 and plan["needed"][0]["current_iterations"] == 0
        assert plan["skipped"][0]["fingerprint"] == "rslo00000000"
        assert "already has 10 comparable" in plan["skipped"][0]["why"]
        # Switched off is a stated reason, never a silent empty plan.
        with session_scope() as s:
            off = pf.plan_prior_winners(s, top=0, iterations=5)
        assert off["needed"] == [] and "switched off" in off["reason"]
    finally:
        _clear(ids)


def test_queueing_submits_exactly_the_needed_winners_as_a_refresh(monkeypatch):
    with session_scope() as s:
        ids = _seed(s, SPECS, current_specs=[("rslo00000000", 10)])
    calls: list[tuple] = []
    monkeypatch.setattr(job_queue, "kind_active", lambda kind: False)
    monkeypatch.setattr(job_queue, "pending", lambda: [])
    monkeypatch.setattr(
        job_queue, "submit",
        lambda kind, label, start=None, *, spec=None: (
            calls.append((kind, label, spec)) or job_queue.Submission(started=True, result=77)
        ),
    )
    try:
        with session_scope() as s:
            out = pf.queue_prior_winners(s, top=2, iterations=5)
        assert out["submitted"] is True and out["refresh_id"] == 77, out
        assert out["queued"] is False  # the placement block: it started at once
        assert calls == [("refresh", out["label"], {"iterations": 5, "fingerprints": ["rshi00000000"]})]
        assert "1 profile(s) × 5 iteration(s)" in out["label"] and _current() in out["label"]
        # A dry run plans and submits nothing.
        with session_scope() as s:
            dry = pf.queue_prior_winners(s, top=2, iterations=5, dry_run=True)
        assert dry["submitted"] is False and dry["dry_run"] is True and len(calls) == 1
        # Nothing needed → nothing submitted. (top=1 → only rshi, which still needs data, so
        # this one IS submitted; the next case gives rshi its chunk first.)
        with session_scope() as s:
            s.add(Score(run_id=ids[0], methodology_version=_current(), comparability="exact",
                        axis_scores={"overall": 50.0}))
        with session_scope() as s:
            done = pf.queue_prior_winners(s, top=2, iterations=5)
        assert done["submitted"] is False and "already has comparable data" in done["reason"]
        assert len(calls) == 1
    finally:
        _clear(ids)


def test_a_refresh_already_in_flight_is_not_doubled(monkeypatch):
    with session_scope() as s:
        ids = _seed(s, SPECS)
    calls: list[tuple] = []
    monkeypatch.setattr(job_queue, "kind_active", lambda kind: False)
    monkeypatch.setattr(job_queue, "pending", lambda: [{"kind": "refresh", "ticket_id": 3}])
    monkeypatch.setattr(job_queue, "submit", lambda *a, **k: calls.append((a, k)))
    try:
        with session_scope() as s:
            out = pf.queue_prior_winners(s, top=3, iterations=5)
        assert out["submitted"] is False and "already queued or running" in out["reason"]
        assert calls == []
    finally:
        _clear(ids)


def test_the_regrade_job_grades_then_follows_through(monkeypatch):
    import pathbrain.runner as runner

    seen: list[str] = []
    monkeypatch.setattr(runner, "score_history_under_current",
                        lambda session, progress=None: seen.append("regrade") or {"scored": 3})
    import pathbrain.duel as duel

    monkeypatch.setattr(duel, "regrade_ledger", lambda session: seen.append("ledger") or {"matches_regraded": 1})
    monkeypatch.setattr(pf, "queue_prior_winners", lambda session: seen.append("reseed") or {"submitted": False})

    class _Job:
        def set_progress(self, *a, **k):
            pass

    summary = pf.regrade_then_follow_through(_Job())
    assert seen == ["regrade", "ledger", "reseed"]
    assert summary == {"scored": 3, "ledger": {"matches_regraded": 1}, "reseed": {"submitted": False}}


def test_every_publish_route_runs_the_one_follow_through_body():
    import pathbrain.api.routes_methodology as rm
    import pathbrain.api.routes_score as rs

    assert rm._regrade_job is pf.regrade_then_follow_through
    # The plain re-grade route imports the same body (pinned by name, since it imports lazily).
    import inspect

    assert "regrade_then_follow_through" in inspect.getsource(rs.regrade_history)


def test_the_reseed_endpoint_previews_and_queues(client, monkeypatch):
    with session_scope() as s:
        ids = _seed(s, SPECS)
    calls: list[dict] = []
    monkeypatch.setattr(job_queue, "kind_active", lambda kind: False)
    monkeypatch.setattr(job_queue, "pending", lambda: [])
    monkeypatch.setattr(
        job_queue, "submit",
        lambda kind, label, start=None, *, spec=None: (
            calls.append(spec) or job_queue.Submission(started=True, result=5)
        ),
    )
    try:
        r = client.post("/api/methodologies/reseed", json={"dry_run": True, "top": 2, "iterations": 5})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["submitted"] is False and body["dry_run"] is True
        assert [n["fingerprint"] for n in body["needed"]] == ["rshi00000000", "rslo00000000"]
        assert calls == []
        r = client.post("/api/methodologies/reseed", json={"top": 2, "iterations": 5})
        assert r.status_code == 202 and r.json()["submitted"] is True, r.text
        assert calls == [{"iterations": 5, "fingerprints": ["rshi00000000", "rslo00000000"]}]
        assert client.post("/api/methodologies/reseed", json={"top": "x"}).status_code == 400
    finally:
        _clear(ids)
