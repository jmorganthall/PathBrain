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
