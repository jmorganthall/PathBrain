"""Tests for the "Test this profile up to the minimum" feature."""
from __future__ import annotations

import time

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
            if pt and pt.status in (ProfileTestStatus.COMPLETE, ProfileTestStatus.FAILED):
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
