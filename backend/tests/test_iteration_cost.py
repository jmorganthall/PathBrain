"""One iteration's cost, read recent-first with a fallback ladder — the unit every ETA
multiplies out."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain.database import session_scope
from pathbrain.iteration_cost import (
    FALLBACK_RUNS,
    Sample,
    estimate_from_samples,
    iteration_cost,
    weighted_median,
)
from pathbrain.models import Run, RunStatus

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _s(minutes_ago: float, per_ms: float, iters: int = 3) -> Sample:
    return Sample(at=NOW - timedelta(minutes=minutes_ago), per_iteration_ms=per_ms, iterations=iters)


def test_recent_runs_win_over_older_history():
    # Ninety minutes ago the suite cost 15 s/iteration; since the scope change it costs 6 s.
    samples = [_s(90, 15_000), _s(80, 15_000), _s(70, 15_000), _s(8, 6_000), _s(3, 6_000)]
    c = estimate_from_samples(samples, now=NOW)
    assert c.basis == "recent" and c.ms == 6_000.0
    assert c.runs == 2 and c.iterations == 6 and c.window_minutes == 30.0 and c.newest_age_s == 180.0


def test_falls_back_when_the_recent_window_is_too_thin():
    # One recent single-iteration run isn't enough evidence on its own (MIN_ITERATIONS=3):
    # widen to the six-hour window, which holds the same run plus older ones.
    samples = [_s(200, 15_000), _s(150, 15_000), _s(5, 6_000, iters=1)]
    c = estimate_from_samples(samples, now=NOW)
    assert c.basis == "today" and c.iterations == 7
    # Iteration-weighted: the two 3-iteration runs outweigh the single fast one.
    assert c.ms == 15_000.0
    # Nothing within six hours → the last few runs whatever their age.
    old = [_s(600 + i * 10, 15_000) for i in range(8)]
    c = estimate_from_samples(old, now=NOW)
    assert c.basis == "history" and c.runs == FALLBACK_RUNS and c.ms == 15_000.0 and c.window_minutes is None
    assert estimate_from_samples([], now=NOW).ms is None


def test_a_hung_run_does_not_double_the_countdown():
    # One run sat on a wedged probe until the 10-minute deadline; the median shrugs it off.
    samples = [_s(2, 6_000), _s(4, 6_100), _s(6, 5_900), _s(8, 600_000, iters=1)]
    c = estimate_from_samples(samples, now=NOW)
    assert c.basis == "recent" and 5_900 <= c.ms <= 6_100


def test_takes_only_as_many_recent_runs_as_it_needs():
    # Ten iterations in hand → stop; the older (slower) recent runs never enter the estimate.
    samples = [_s(1, 6_000, 5), _s(2, 6_000, 5), _s(10, 20_000, 5), _s(12, 20_000, 5)]
    c = estimate_from_samples(samples, now=NOW)
    assert c.runs == 2 and c.iterations == 10 and c.ms == 6_000.0


def test_weighted_median():
    assert weighted_median([(1, 1), (100, 5), (2, 1)]) == 100
    assert weighted_median([(5, 3), (7, 3)]) == 5  # lower median on an even split
    assert weighted_median([]) is None


def test_from_the_run_table_recent_wins(client):
    """Against the shared test database (other tests leave timed runs behind, all stamped
    "now"), so the assertions are about the tier and the evidence, not an exact number."""
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        rows = [
            Run(status=RunStatus.COMPLETE, iterations=3, iterations_completed=3, per_iteration_ms=15_000,
                created_at=now - timedelta(hours=2), finished_at=now - timedelta(hours=2)),
            Run(status=RunStatus.COMPLETE, iterations=3, iterations_completed=3, per_iteration_ms=6_000,
                created_at=now - timedelta(minutes=4), finished_at=now - timedelta(minutes=4)),
            Run(status=RunStatus.COMPLETE, iterations=3, iterations_completed=3, per_iteration_ms=6_200,
                created_at=now - timedelta(minutes=2), finished_at=now - timedelta(minutes=2)),
        ]
        s.add_all(rows)
        s.flush()
        ids = [r.id for r in rows]
    try:
        with session_scope() as s:
            c = iteration_cost(s)
            recent_values = {
                float(v) for (v,) in s.execute(
                    __import__("sqlalchemy").select(Run.per_iteration_ms).where(
                        Run.status == RunStatus.COMPLETE, Run.per_iteration_ms.is_not(None),
                    )
                ).all()
            }
        # Enough recent iterations exist, so the recent tier answers, with fresh evidence...
        assert c.basis == "recent" and c.iterations >= 3 and c.newest_age_s is not None and c.newest_age_s < 600
        # ...and the weighted median is always an actually observed per-iteration time.
        assert c.ms in recent_values
        body = client.get("/api/runs/estimate").json()
        assert body["basis"] == "recent" and body["per_iteration_ms"] == c.ms
        assert body["based_on_iterations"] == c.iterations and body["window_minutes"] == 30.0
    finally:
        with session_scope() as s:
            for rid in ids:
                row = s.get(Run, rid)
                if row is not None:
                    s.delete(row)
