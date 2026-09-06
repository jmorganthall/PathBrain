"""Every "Run this" button behaves the same way when the pipeline is busy.

This is the regression test for the report that kept coming back: each engine was fixed
one at a time, and each time some *other* button still refused, or still queued silently
while claiming to have started. So this test does not check one engine — it checks the
contract, across all of them at once, and will fail the moment a new start path is added
that answers a busy pipeline differently.
"""
from __future__ import annotations

import pytest

from pathbrain import coordinator, job_queue
from pathbrain.providers import mock as mock_mod

# Every user-triggered start endpoint, with a body that is otherwise valid.
#
# ``/api/run`` is covered separately (``test_a_manual_run_reports_its_placement_too``): a
# manual run self-queues through a Starlette *background task*, and TestClient runs those
# inline after the response — so posting it while this test's own thread holds the
# coordinator would deadlock the client against itself. That is a property of the test
# harness, not of the endpoint; under uvicorn the task runs on the threadpool.
START_PATHS = [
    ("current_test", "/api/current/test", {"minutes": 1}),
    ("baseline_test", "/api/baseline/test", {"iterations": 1, "settle_seconds": 0}),
    ("duel", "/api/duel/start", {"duration_minutes": 1}),
    ("sweep", "/api/sweep", {"spec": {"quantum": [1000, 2000]}, "iterations": 1}),
    ("refresh", "/api/settings/refresh", {"iterations": 1}),
    ("test_settings", "/api/settings/test-settings",
     {"settings": {"quantum": 6000}, "label": "x", "iterations": 1}),
]

PLACEMENT_KEYS = {"queued", "ticket_id", "queue_position", "queue_ahead", "blocked_by"}


@pytest.fixture(autouse=True)
def _clean():
    mock_mod._OVERRIDES.clear()
    yield
    mock_mod._OVERRIDES.clear()


@pytest.mark.parametrize("kind,path,body", START_PATHS, ids=[p[0] for p in START_PATHS])
def test_a_busy_pipeline_queues_every_kind_of_job(client, kind, path, body):
    """No start path may refuse because something else is running.

    A 409 here means a button that dead-ends: the coordinator has always serialized
    sessions, so an "already running" guard in front of it only removes the user's ability
    to line work up — and it fires hardest exactly when queueing is what they want.
    """
    with coordinator.hold("duel#1"):
        resp = client.post(path, json=body)

        assert resp.status_code != 409, (
            f"{kind}: a busy pipeline must queue, not refuse — got {resp.status_code} "
            f"{resp.text[:200]}"
        )
        assert resp.status_code < 400, f"{kind}: {resp.status_code} {resp.text[:200]}"

        payload = resp.json()
        # The same placement block from every engine, so "did anything happen?" has one
        # answer whichever button was pressed.
        missing = PLACEMENT_KEYS - set(payload)
        assert not missing, f"{kind}: response is missing {sorted(missing)}"
        assert payload["queued"] is True, f"{kind}: should report itself queued"
        assert payload["blocked_by"] == "a duel session", (
            f"{kind}: should name the holder in words, got {payload['blocked_by']!r}"
        )

        # And it is listed as waiting, in the one queue read the UI asks before submitting.
        queue = client.get("/api/queue").json()
        assert queue["busy"] is True
        assert queue["blocked_by"] == "a duel session"
        assert queue["queue_depth"] >= 1, f"{kind}: nothing showed up as waiting"

        # Cancellable before it starts, whichever layer queued it.
        ticket = payload.get("ticket_id")
        if ticket is not None:
            assert client.post(f"/api/queue/{ticket}/cancel").json()["cancelled"] is True
        else:
            # Self-queued (its own row): dropped through its own engine.
            for job in queue["pending"]:
                if job["kind"] == "profile_test" and job.get("id"):
                    client.post(f"/api/settings/test-profile/{job['id']}/cancel")

    _drain(client)


def test_a_manual_run_reports_its_placement_too(client, monkeypatch):
    """The manual run queues on the coordinator directly rather than as a ticket — its row
    id is what the dashboard polls, so it has to exist immediately — but it answers with the
    same placement block as everything else."""
    from pathbrain import runner

    monkeypatch.setattr(runner, "iter_plugins", lambda: [])  # no network
    payload = client.post("/api/run", json={"iterations": 1}).json()
    assert PLACEMENT_KEYS <= set(payload), sorted(PLACEMENT_KEYS - set(payload))
    assert payload["queued"] is False and payload["blocked_by"] is None
    assert payload["id"]


def test_a_free_pipeline_still_starts_immediately(client):
    """Queueing is added, nothing is taken away: with nothing running, a job still starts
    now and still reports the engine's own session id."""
    resp = client.post("/api/current/test", json={"minutes": 1})
    assert resp.status_code == 202
    payload = resp.json()
    assert payload["queued"] is False
    assert payload["blocked_by"] is None
    assert payload.get("id")
    client.post("/api/current/test/cancel")
    _drain(client)


def _drain(client) -> None:
    """Cancel anything this test left queued so it cannot run against the shared fixture."""
    for job in client.get("/api/queue").json().get("pending", []):
        if job.get("ticket_id") is not None:
            client.post(f"/api/queue/{job['ticket_id']}/cancel")
    job_queue._reset_for_tests()
    job_queue.register_engines()
