"""A run cancelled while queued must NOT execute when the lock frees (no dirty data)."""
from __future__ import annotations

from datetime import datetime, timezone

from pathbrain.database import session_scope
from pathbrain.models import BenchmarkResult, Run, RunStatus
from pathbrain.runner import create_run, execute_run


def test_execute_skips_a_cancelled_pending_run():
    run_id = create_run(label="manual", iterations=1)

    # Simulate /runs/{id}/cancel firing while the run was still queued behind the lock.
    with session_scope() as session:
        run = session.get(Run, run_id)
        run.status = RunStatus.FAILED
        run.error = "Cancelled by user."
        run.finished_at = datetime.now(timezone.utc)

    # When the lock frees, the queued background task still calls execute_run — which
    # must no-op rather than run the suite and overwrite the cancellation.
    execute_run(run_id)

    with session_scope() as session:
        run = session.get(Run, run_id)
        assert run.status == RunStatus.FAILED  # not flipped to RUNNING/COMPLETE
        assert run.started_at is None  # never actually started
        results = session.scalars(
            select_results(run_id)
        ).all()
        assert results == []  # no benchmark data written


def select_results(run_id: int):
    from sqlalchemy import select

    return select(BenchmarkResult).where(BenchmarkResult.run_id == run_id)


# ── A RUNNING run stops between iterations ────────────────────────────────────────────
#
# The cancel route flipped the row to FAILED and ``execute_run`` never read it back: the
# loop ran every remaining iteration and wrote COMPLETE over the cancel. So "cancel" was
# cosmetic for a running run, and a session's cancel had to wait for a whole leg.


class _Counting:
    """A plugin that counts its calls and can act on the first one."""

    name = "counting"

    def __init__(self, on_first=None) -> None:
        self.calls = 0
        self.on_first = on_first

    def run(self, config: dict) -> "PluginResult":
        from pathbrain.plugins import PluginResult

        self.calls += 1
        if self.calls == 1 and self.on_first is not None:
            self.on_first()
        return PluginResult(self.name, success=True, raw={})

    def teardown(self) -> None:  # pragma: no cover - lifecycle only
        pass


def _register(monkeypatch, plugin) -> None:
    from pathbrain import plugins as plugins_pkg, runner

    monkeypatch.setattr(runner, "iter_plugins", lambda: [plugin])
    monkeypatch.setattr(plugins_pkg, "iter_plugins", lambda: [plugin])


def test_a_stop_request_ends_a_running_run_before_its_next_iteration(monkeypatch):
    from pathbrain import runner

    run_id = create_run(label="manual", iterations=3)
    plugin = _Counting(on_first=lambda: runner.request_stop(run_id, "Cancelled by user."))
    _register(monkeypatch, plugin)

    execute_run(run_id)

    assert plugin.calls == 1, "the iteration in flight finishes; the next never starts"
    with session_scope() as session:
        run = session.get(Run, run_id)
        assert run.status == RunStatus.FAILED
        assert run.error.startswith("Cancelled by user")
        assert "after 1 of 3 iteration(s)" in run.error
        assert run.iterations_completed == 1
        assert run.finished_at is not None
        assert session.scalars(select_results(run_id)).all() == []  # nothing scored
    assert runner.run_cancelled(run_id) is True
    assert runner._stop_reason(run_id) is None, "the request is dropped once the loop ends"


def test_a_cancel_written_to_the_row_by_someone_else_is_honoured_too(monkeypatch):
    """The route in another process, or the watchdog: the row is the shared truth, and it
    is re-read before every iteration because this session never expires on commit."""
    run_id = create_run(label="manual", iterations=3)

    def flip_row() -> None:
        with session_scope() as other:
            row = other.get(Run, run_id)
            row.status = RunStatus.FAILED
            row.error = "Cancelled by user."
            row.finished_at = datetime.now(timezone.utc)

    plugin = _Counting(on_first=flip_row)
    _register(monkeypatch, plugin)

    execute_run(run_id)

    assert plugin.calls == 1
    with session_scope() as session:
        run = session.get(Run, run_id)
        assert run.status == RunStatus.FAILED, "COMPLETE must never be written over a cancel"
        assert run.error.startswith("Cancelled by user")
        assert session.scalars(select_results(run_id)).all() == []


def test_the_cancel_route_stops_a_running_run_in_this_process(client, monkeypatch):
    """End to end through the route: the row flips AND the loop is told, so the run ends
    within one iteration whether or not the row read happens to see the write first."""
    run_id = create_run(label="manual", iterations=3)
    plugin = _Counting(on_first=lambda: client.post(f"/api/runs/{run_id}/cancel"))
    _register(monkeypatch, plugin)

    execute_run(run_id)

    assert plugin.calls == 1
    with session_scope() as session:
        run = session.get(Run, run_id)
        assert run.status == RunStatus.FAILED and run.error.startswith("Cancelled by user")


def test_a_run_that_nobody_cancels_runs_every_iteration(monkeypatch):
    """The check must cost nothing on the ordinary path."""
    plugin = _Counting()
    _register(monkeypatch, plugin)
    run_id = create_run(label="manual", iterations=3)
    execute_run(run_id)
    assert plugin.calls == 3
    with session_scope() as session:
        assert session.get(Run, run_id).status == RunStatus.COMPLETE
