"""Tests for the background-job registry and the unified /api/jobs feed."""
from __future__ import annotations

import threading
import time

from pathbrain import jobs
from pathbrain.database import session_scope
from pathbrain.models import Run, RunStatus


def _wait(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def _find(job_id: str) -> dict | None:
    return next((j for j in jobs.list_jobs() if j["id"] == job_id), None)


def test_job_runs_to_success_with_progress():
    gate = threading.Event()

    def work(job):
        job.set_progress(1, 2, "halfway")
        gate.wait(2.0)
        job.set_progress(2, 2, "done")
        return {"scored": 7}

    job_id = jobs.start("unit-success", "unit job", work)
    # Visible as running with progress before we release the gate.
    _wait(lambda: (_find(job_id) or {}).get("current") == 1)
    assert _find(job_id)["status"] == "running"
    gate.set()
    _wait(lambda: (_find(job_id) or {}).get("status") == "succeeded")
    done = _find(job_id)
    assert done["current"] == 2 and done["total"] == 2
    assert "scored 7" in (done["message"] or "")  # summary derived from the returned dict


def test_job_failure_is_recorded():
    def boom(job):
        raise ValueError("nope")

    job_id = jobs.start("unit-fail", "boom", boom)
    _wait(lambda: (_find(job_id) or {}).get("status") == "failed")
    assert "nope" in (_find(job_id)["error"] or "")


def test_same_kind_is_not_started_twice():
    gate = threading.Event()

    def work(job):
        gate.wait(2.0)

    first = jobs.start("unit-dedupe", "first", work)
    second = jobs.start("unit-dedupe", "second", work)  # should reuse the running one
    assert first == second
    gate.set()
    _wait(lambda: (_find(first) or {}).get("status") == "succeeded")


def test_jobs_endpoint_merges_registry_and_run_adapter(client):
    # An in-process job appears in the feed...
    gate = threading.Event()
    job_id = jobs.start("unit-feed", "feed job", lambda job: gate.wait(2.0))

    # ...and a live benchmark run is synthesized as an adapter entry.
    with session_scope() as s:
        run = Run(status=RunStatus.RUNNING, label="live run", iterations=3, iterations_completed=1)
        s.add(run)
        s.flush()
        rid = run.id

    body = client.get("/api/jobs").json()
    ids = {j["id"] for j in body["jobs"]}
    assert job_id in ids
    assert f"run-{rid}" in ids
    run_entry = next(j for j in body["jobs"] if j["id"] == f"run-{rid}")
    assert run_entry["kind"] == "run"
    assert run_entry["total"] == 3 and run_entry["current"] == 1
    assert body["running"] >= 2

    gate.set()
    # Clean up: fully remove the seeded run so it can't leak into other tests'
    # aggregations or "active" checks.
    with session_scope() as s:
        s.delete(s.get(Run, rid))
