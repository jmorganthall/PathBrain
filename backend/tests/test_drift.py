"""Campaign-drift check: separates a drifting absolute metric from a stable ratio."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain.database import session_scope
from pathbrain.drift import metric_time_drift
from pathbrain.models import BenchmarkResult, Run, RunStatus


def _seed_campaign() -> None:
    """12 runs over 12 hours: stall_time climbs monotonically with time (absolute drift),
    jank_fraction zig-zags around a constant (stable ratio, no trend)."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with session_scope() as s:
        for i in range(12):
            run = Run(status=RunStatus.COMPLETE, created_at=base + timedelta(hours=i))
            s.add(run)
            s.flush()
            s.add(
                BenchmarkResult(
                    run_id=run.id,
                    plugin="browser",
                    success=True,
                    metrics={
                        "stall_time_ms": 100.0 + 50.0 * i,          # ← trends with time
                        "jank_fraction": 0.52 if i % 2 == 0 else 0.48,  # ← flat, alternating
                    },
                    raw={},
                )
            )


def test_drift_flags_absolute_stall_and_clears_ratio_jank():
    _seed_campaign()
    with session_scope() as s:
        rows = {r["key"]: r for r in metric_time_drift(s, min_samples=8)}

    assert "stall_time" in rows and "jank_fraction" in rows
    stall, jank = rows["stall_time"], rows["jank_fraction"]

    # Absolute stall_time rises monotonically with time → strong drift, flagged.
    assert stall["rho"] == 1.0 and stall["drifts"] is True
    # The ratio jank_fraction has no time trend → ρ≈0, not flagged.
    assert abs(jank["rho"]) < 0.3 and jank["drifts"] is False
    # Both are shape-family (role S) — the point is same bucket, opposite drift behaviour.
    assert stall["role"] == "S" and jank["role"] == "S"
    # The biggest drifter sorts first.
    assert list(dict.fromkeys(r["key"] for r in sorted(rows.values(), key=lambda d: -d["abs_rho"])))[0] == "stall_time"
