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


class _CountingPlugin(runner.BenchmarkPlugin):
    """A fake plugin that records how often it's run and torn down."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.runs = 0
        self.teardowns = 0

    def run(self, config: dict):
        self.runs += 1
        return runner.PluginResult(self.name, success=True, raw={"n": self.runs})

    def teardown(self) -> None:
        self.teardowns += 1


def test_plugin_iteration_cap_and_teardown(monkeypatch):
    # "heavy" is capped to 1 iteration via its config section; "light" runs the full 3.
    from pathbrain.config_store import save_config

    heavy = _CountingPlugin("heavy")
    light = _CountingPlugin("light")
    monkeypatch.setattr(runner, "iter_plugins", lambda: [heavy, light])
    monkeypatch.setattr(settings_profile, "fingerprint", lambda _norm: "stable-fp")

    with session_scope() as s:
        save_config(s, {"heavy": {"iterations": 1}, "light": {}})

    rid = create_run(label="cap", iterations=3)
    execute_run(rid)

    # Heavy opted out after its 1 round; light ran every iteration. Both torn down once.
    assert heavy.runs == 1
    assert light.runs == 3
    assert heavy.teardowns == 1
    assert light.teardowns == 1

    with session_scope() as s:
        run = s.get(Run, rid)
        assert run.status == RunStatus.COMPLETE
