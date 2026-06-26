"""Tests for the read-before / read-after firewall-drift integrity check in
``runner.execute_run``: a run whose settings change mid-flight is discarded."""
from __future__ import annotations

from pathbrain import runner, settings_profile
from pathbrain.database import session_scope
from pathbrain.models import Run, RunStatus
from pathbrain.runner import create_run, execute_run


def _run_with_no_plugins(monkeypatch):
    """Run id for an execute_run that does no network I/O (no plugins)."""
    monkeypatch.setattr(runner, "iter_plugins", lambda: [])
    return create_run(label="integrity", iterations=1)


def test_run_fails_when_settings_drift_mid_run(monkeypatch):
    rid = _run_with_no_plugins(monkeypatch)

    # Make the fingerprint differ between the start capture and the end re-read,
    # simulating something changing the firewall while we measured.
    seq = iter(["fp-start", "fp-end"])
    monkeypatch.setattr(settings_profile, "fingerprint", lambda _norm: next(seq))

    execute_run(rid)

    with session_scope() as s:
        run = s.get(Run, rid)
        assert run.status == RunStatus.FAILED
        assert "changed mid-run" in (run.error or "")


def test_run_completes_when_settings_stable(monkeypatch):
    rid = _run_with_no_plugins(monkeypatch)
    # Constant fingerprint (the mock provider's normal behavior) => no drift.
    monkeypatch.setattr(settings_profile, "fingerprint", lambda _norm: "stable-fp")

    execute_run(rid)

    with session_scope() as s:
        run = s.get(Run, rid)
        assert run.status == RunStatus.COMPLETE
        assert run.error is None
