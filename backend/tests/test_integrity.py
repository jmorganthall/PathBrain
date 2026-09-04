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


def test_a_skipped_plugin_runs_no_iterations_at_all(monkeypatch):
    """`{"<plugin>": {"skip": True}}` leaves a plugin out of the run entirely (the duel's
    browser-only legs). It is still torn down with the rest, and the run completes with
    that plugin's metrics simply absent — never fabricated."""
    from pathbrain.models import BenchmarkResult

    probe = _CountingPlugin("probe")
    browser = _CountingPlugin("browserish")
    monkeypatch.setattr(runner, "iter_plugins", lambda: [probe, browser])
    monkeypatch.setattr(settings_profile, "fingerprint", lambda _norm: "stable-fp")

    rid = create_run(label="skip", iterations=2, config_overrides={"probe": {"skip": True}})
    execute_run(rid)

    assert probe.runs == 0
    assert browser.runs == 2
    assert probe.teardowns == 1
    with session_scope() as s:
        run = s.get(Run, rid)
        assert run.status == RunStatus.COMPLETE
        plugins = {r.plugin for r in s.query(BenchmarkResult).filter_by(run_id=rid).all()}
        assert "browserish" in plugins and "probe" not in plugins
