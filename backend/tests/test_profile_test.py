"""Tests for the "Test this profile up to the minimum" feature."""
from __future__ import annotations

import time

from sqlalchemy import func, select

from pathbrain import profile_test as pt_mod
from pathbrain import runner
from pathbrain.database import session_scope
from pathbrain.models import ProfileTest, ProfileTestStatus, Run, RunStatus
from pathbrain.providers import get_provider
from pathbrain.providers import mock as mock_mod
from pathbrain.settings_profile import fingerprint, normalize


def _wait_for_finish(test_id: int, timeout: float = 10.0) -> ProfileTest:
    start = time.time()
    while time.time() - start < timeout:
        with session_scope() as s:
            pt = s.get(ProfileTest, test_id)
            if pt and pt.status in (
                ProfileTestStatus.COMPLETE,
                ProfileTestStatus.FAILED,
                ProfileTestStatus.CANCELLED,
            ):
                # Return a detached snapshot of the fields we assert on.
                s.expunge(pt)
                return pt
        time.sleep(0.05)
    raise AssertionError("profile test did not finish in time")


def test_profile_test_runs_and_restores(monkeypatch):
    mock_mod._OVERRIDES.clear()
    # Avoid real network: execute_run with no plugins still drives the run lifecycle.
    monkeypatch.setattr(runner, "iter_plugins", lambda: [])

    target = normalize(get_provider().discover())
    target_fp = fingerprint(target)

    test_id = pt_mod.start(target_fp, target, "wan profile", iterations=4)
    pt = _wait_for_finish(test_id)

    assert pt.status == ProfileTestStatus.COMPLETE
    assert pt.iterations == 4
    assert pt.run_id is not None
    # The benchmark run it produced ran the requested iteration count.
    with session_scope() as s:
        run = s.get(Run, pt.run_id)
        assert run.iterations == 4
        assert run.status == RunStatus.COMPLETE
    # Firewall is back to baseline (mock default quantum) and the lock is free.
    assert get_provider().discover()[0].quantum == 1514
    assert not pt_mod.active()


def test_profile_test_chunks_into_blocks(monkeypatch):
    """A "run to minimum confidence" of more than CHUNK_ITERATIONS iterations must be split
    into a series of runs of at most CHUNK_ITERATIONS each (so an interruption keeps every
    completed block), not one long single run."""
    mock_mod._OVERRIDES.clear()
    monkeypatch.setattr(runner, "iter_plugins", lambda: [])

    target = normalize(get_provider().discover())
    target_fp = fingerprint(target)

    total = runner.CHUNK_ITERATIONS * 2 + 1  # 11 with CHUNK_ITERATIONS=5 → chunks 5,5,1
    test_id = pt_mod.start(target_fp, target, "chunky", iterations=total)
    pt = _wait_for_finish(test_id)

    assert pt.status == ProfileTestStatus.COMPLETE, pt.error
    # All runs this test produced (tagged with the test id in their notes).
    with session_scope() as s:
        runs = [
            r
            for r in s.scalars(select(Run)).all()
            if r.notes and f"Profile test #{test_id}:" in r.notes
        ]
        assert len(runs) == 3, f"expected 3 chunks, got {[r.iterations for r in runs]}"
        assert all(r.iterations <= runner.CHUNK_ITERATIONS for r in runs)
        assert all(r.status == RunStatus.COMPLETE for r in runs)
        assert sum(r.iterations for r in runs) == total
        # The representative run_id is the first chunk.
        assert pt.run_id in {r.id for r in runs}
    # Baseline restored, lock free.
    assert get_provider().discover()[0].quantum == 1514
    assert not pt_mod.active()
    mock_mod._OVERRIDES.clear()


def test_profile_test_verifies_semantically_despite_format(monkeypatch):
    """A target whose writable value is in a different *format* than discover() reports back
    (e.g. an AI's int where the firewall stores a string) must still verify as reached — the
    old exact-fingerprint check false-negatived here and failed before benchmarking."""
    mock_mod._OVERRIDES.clear()
    monkeypatch.setattr(runner, "iter_plugins", lambda: [])

    target = normalize(get_provider().discover())
    # Force a format-mismatched writable value on the download pipe: an int `target`. The mock
    # stores it and reports it back as the string "7" (str(7)), so fingerprint(target) with the
    # int != fingerprint(after) with the string — yet they're the same profile.
    target[0]["target"] = 7
    target_fp = fingerprint(target)

    test_id = pt_mod.start(target_fp, target, "format-mismatch", iterations=2)
    pt = _wait_for_finish(test_id)

    assert pt.status == ProfileTestStatus.COMPLETE, pt.error
    assert pt.run_id is not None
    assert pt.stage == "Done — baseline restored"
    # Baseline restored (mock default quantum + target).
    assert get_provider().discover()[0].quantum == 1514
    assert not pt_mod.active()
    mock_mod._OVERRIDES.clear()


def test_profile_test_reports_unreached_field(monkeypatch):
    """When the firewall doesn't accept a change, the test fails with a readable per-field
    reason instead of an opaque fingerprint mismatch — the step-by-step readout users need."""
    mock_mod._OVERRIDES.clear()
    monkeypatch.setattr(runner, "iter_plugins", lambda: [])

    # A provider whose apply() silently drops writes: discover never changes, so the target
    # is never reached.
    class _DeafProvider(mock_mod.MockProvider):
        def apply(self, changes):  # noqa: D401 — swallow the write
            return {"provider": self.name, "ok": True}

    monkeypatch.setattr("pathbrain.profile_test.get_provider", lambda: _DeafProvider())

    target = normalize(_DeafProvider().discover())
    target[0]["quantum"] = 4242  # a change the deaf provider will never accept
    target_fp = fingerprint(target)

    test_id = pt_mod.start(target_fp, target, "deaf", iterations=2)
    pt = _wait_for_finish(test_id)

    assert pt.status == ProfileTestStatus.FAILED
    assert "did not accept" in (pt.error or "")
    assert "quantum" in (pt.error or "")
    mock_mod._OVERRIDES.clear()


def test_reconcile_interrupted_profile_tests_restores():
    mock_mod._OVERRIDES.clear()
    mock_mod._OVERRIDES["quantum"] = 8888  # firewall stranded on a test value
    with session_scope() as s:
        pt = ProfileTest(
            status=ProfileTestStatus.RUNNING,
            fingerprint="abc123",
            target_label="wan",
            iterations=5,
            baseline=[{"label": "wan-download", "quantum": 1514, "target": "5ms"}],
        )
        s.add(pt)
        s.flush()
        pid = pt.id

    assert pt_mod.reconcile_interrupted_profile_tests() >= 1
    assert get_provider().discover()[0].quantum == 1514  # restored
    with session_scope() as s:
        assert s.get(ProfileTest, pid).status == ProfileTestStatus.FAILED
    mock_mod._OVERRIDES.clear()


def test_test_profile_endpoint_already_at_minimum(client):
    # A profile with no runs / 0 iterations is below the minimum; an unknown one 404s.
    resp = client.post("/api/settings/test-profile", json={"fingerprint": "no-such-profile"})
    assert resp.status_code == 404

    resp = client.post("/api/settings/test-profile", json={})
    assert resp.status_code == 400


def test_profile_test_cancel_endpoint_when_idle(client):
    # Cancel is wired and safe to call with nothing running.
    assert pt_mod.cancel() is False
    body = client.post("/api/settings/test-profile/cancel").json()
    assert body["cancelled"] is False


def test_profile_test_cancel_stops_after_chunk(monkeypatch):
    """A running profile test cancels after its current chunk, ends CANCELLED, and still
    restores the baseline."""
    mock_mod._OVERRIDES.clear()
    monkeypatch.setattr(runner, "iter_plugins", lambda: [])

    # Slow chunks so we can request cancel between them (each creates a real completed run).
    def slow_chunk(label, notes, iterations, teardown=True, job_group=None,
                   job_group_total=None, config_overrides=None, on_created=None):
        rid = runner.create_run(label=label, notes=notes, iterations=iterations, job_group=job_group)
        runner.execute_run(rid)
        time.sleep(0.12)
        return (rid, True, iterations)

    monkeypatch.setattr(pt_mod, "run_chunk", slow_chunk)

    target = normalize(get_provider().discover())
    fp = fingerprint(target)
    test_id = pt_mod.start(fp, target, "cancelme", iterations=50)  # 10 chunks of 5
    for _ in range(200):
        if (pt_mod.current() or {}).get("status") == "running":
            break
        time.sleep(0.02)
    assert pt_mod.cancel() is True

    pt = _wait_for_finish(test_id)
    assert pt.status == ProfileTestStatus.CANCELLED
    assert get_provider().discover()[0].quantum == 1514  # baseline restored
    assert not pt_mod.active()
    mock_mod._OVERRIDES.clear()


def test_a_confident_profile_can_still_be_re_measured_with_an_explicit_count(client, monkeypatch):
    """"Test this profile" on a profile that is already confident is not a top-up — there is
    nothing to top up — it is "how is this doing right now?". So an explicit iteration count
    runs exactly that many whatever the profile already has, while omitting it keeps the
    top-up contract (and its refusal), the same split `start_settings_test` uses."""
    from pathbrain.api import routes_settings as rs

    started: dict = {}

    monkeypatch.setattr(rs, "_profile_settings", lambda s, fp: [{"label": "wan-download"}])
    monkeypatch.setattr(rs, "_min_iterations", lambda s: 15)
    monkeypatch.setattr(rs, "_profile_iterations", lambda s, fp: 120)  # long past confident
    monkeypatch.setattr(
        rs.profile_test_mod,
        "start",
        lambda fp, target, label, iterations: started.setdefault("iterations", iterations) or 7,
    )

    # No count → nothing to top up, and the refusal says what to do instead.
    resp = client.post("/api/settings/test-profile", json={"fingerprint": "settled"})
    assert resp.status_code == 400
    assert "explicit iteration count" in resp.json()["detail"]

    # An explicit count runs exactly that many.
    body = client.post(
        "/api/settings/test-profile", json={"fingerprint": "settled", "iterations": 5}
    ).json()
    assert body["iterations"] == 5 and body["mode"] == "exact"
    assert body["current_iterations"] == 120
    assert started["iterations"] == 5


# ── The queue ──────────────────────────────────────────────────────────────────────────
#
# Profile tests used to be a strict singleton: a second one raised, the API turned that
# into a 409, and the button dead-ended. The flag was also set at *creation*, not at start,
# so a test queued behind a duel window refused every other test for the whole night —
# which is exactly when someone wants to line one up.


def test_a_second_test_queues_instead_of_being_refused(monkeypatch):
    """The regression this exists for: pressing "Test now" while another test is in flight
    must line the second one up, not reject it."""
    mock_mod._OVERRIDES.clear()
    monkeypatch.setattr(runner, "iter_plugins", lambda: [])
    target = normalize(get_provider().discover())
    fp = fingerprint(target)

    first = pt_mod.start(fp, target, "first", 1)
    second = pt_mod.start(fp, target, "second", 1)  # must not raise

    assert second != first
    for test_id in (first, second):
        assert _wait_for_finish(test_id, timeout=20.0).status == ProfileTestStatus.COMPLETE
    # Both really ran, in the order they were asked for.
    with session_scope() as s:
        a, b = s.get(ProfileTest, first), s.get(ProfileTest, second)
        assert a.started_at is not None and b.started_at is not None
        assert a.started_at <= b.started_at
    assert not pt_mod.active()


def test_each_queued_test_keeps_its_own_target(monkeypatch):
    """The target used to live in one module-level slot, so a second test would overwrite
    the first one's settings. With a queue that silently measures the wrong profile."""
    mock_mod._OVERRIDES.clear()
    monkeypatch.setattr(runner, "iter_plugins", lambda: [])
    base = normalize(get_provider().discover())

    one = [dict(p) for p in base]
    one[0]["quantum"] = 3000
    two = [dict(p) for p in base]
    two[0]["quantum"] = 6000

    a = pt_mod.start(fingerprint(one), one, "q3000", 1)
    b = pt_mod.start(fingerprint(two), two, "q6000", 1)
    for test_id in (a, b):
        _wait_for_finish(test_id, timeout=20.0)

    with session_scope() as s:
        assert s.get(ProfileTest, a).target[0]["quantum"] == 3000
        assert s.get(ProfileTest, b).target[0]["quantum"] == 6000
    mock_mod._OVERRIDES.clear()


def test_a_queued_test_is_dropped_without_touching_the_firewall(monkeypatch):
    """Cancelling something that has not started must not cost an apply-and-restore round
    trip: a queued test has read nothing and written nothing, so it just leaves the line."""
    from pathbrain import coordinator

    mock_mod._OVERRIDES.clear()
    monkeypatch.setattr(runner, "iter_plugins", lambda: [])
    target = normalize(get_provider().discover())
    fp = fingerprint(target)

    applied: list = []
    real_apply = pt_mod._apply_all
    monkeypatch.setattr(
        pt_mod, "_apply_all", lambda p, ch: (applied.extend(ch), real_apply(p, ch))[1]
    )

    # Hold the pipeline so nothing can start, then queue two and drop the second.
    with coordinator.hold("test-holder"):
        first = pt_mod.start(fp, target, "runs", 1)
        second = pt_mod.start(fp, target, "dropped", 1)
        assert pt_mod.cancel(second) is True
        with session_scope() as s:
            assert s.get(ProfileTest, second).status == ProfileTestStatus.CANCELLED
            assert s.get(ProfileTest, second).baseline is None  # never snapshotted anything

    assert _wait_for_finish(first, timeout=20.0).status == ProfileTestStatus.COMPLETE
    # The dropped test never became a run of its own.
    with session_scope() as s:
        assert s.scalar(
            select(func.count()).select_from(Run).where(Run.job_group == f"profile_test-{second}")
        ) == 0


def test_queue_status_names_what_is_in_the_way(monkeypatch):
    """The "Busy now — queue this?" prompt has to say what is holding the pipeline. A raw
    lock label ("duel#412") is not an answer, so the owner is described in words."""
    from pathbrain import coordinator

    mock_mod._OVERRIDES.clear()
    monkeypatch.setattr(runner, "iter_plugins", lambda: [])
    target = normalize(get_provider().discover())
    fp = fingerprint(target)

    with coordinator.hold("duel#412"):
        queued = pt_mod.start(fp, target, "waiting", 1)
        status = pt_mod.queue_status()
        assert status["busy"] is True
        assert status["owner"] == "duel#412"
        assert status["owner_label"] == "a duel session"
        assert status["queue_depth"] >= 1
        assert any(p["id"] == queued for p in status["pending"])
        assert pt_mod.cancel(queued) is True

    assert coordinator.describe("run-series#3") == "a benchmark run"
    assert coordinator.describe(None) is None
    assert coordinator.describe("brand-new-engine#1") == "brand-new-engine#1"
