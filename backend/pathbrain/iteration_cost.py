"""What one benchmark iteration costs right now — the unit every ETA multiplies out.

Every countdown in PathBrain (the Dashboard's run ETA, the jobs feed's ``measured`` basis,
the re-run preview) is ``iterations left × the cost of one iteration``, and the cost used
to be a flat average over the last five completed runs regardless of age. That is wrong
in exactly the situation an ETA matters most: after anything changes what a run does (a
methodology-only scope, a new site list, a different idle cap) the freshest runs are the
only ones that describe the work ahead, and a run from ninety minutes ago is describing a
different job.

So the cost is read **recent-first, weighted by evidence, with a fallback ladder**:

1. ``recent`` — runs finished in the last ``RECENT_WINDOW_MIN`` minutes, newest first,
   taken until ``TARGET_ITERATIONS`` iterations are in hand; used when at least
   ``MIN_ITERATIONS`` iterations exist in the window.
2. ``today`` — the same, over ``WIDE_WINDOW_MIN``.
3. ``history`` — the last ``FALLBACK_RUNS`` completed runs whatever their age.
4. ``None`` — nothing timed yet; the display says so rather than inventing a number.

Within a tier the estimate is the **iteration-weighted median** of the runs' per-iteration
times: weighted, because a 5-iteration run is five observations of the unit and a
1-iteration run is one; a median, because one run that sat on a hung probe until its
10-minute deadline must not double every countdown that follows it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .models import Run, RunStatus

RECENT_WINDOW_MIN = 30.0
WIDE_WINDOW_MIN = 360.0
TARGET_ITERATIONS = 10
MIN_ITERATIONS = 3
FALLBACK_RUNS = 5
SCAN_LIMIT = 120


@dataclass(frozen=True)
class Sample:
    at: datetime          # when the run finished (UTC)
    per_iteration_ms: float
    iterations: int       # completed iterations the mean was taken over (≥ 1)


@dataclass(frozen=True)
class IterationCost:
    ms: float | None
    basis: str | None           # "recent" | "today" | "history" | None
    iterations: int             # iterations the estimate rests on
    runs: int                   # runs the estimate rests on
    window_minutes: float | None  # the window the winning tier looked at (None for history)
    newest_age_s: float | None  # how old the newest run used is


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    """Median of values under integer-ish weights (``(value, weight)``); None when empty."""
    rows = sorted((float(v), float(w)) for v, w in pairs if w and w > 0)
    if not rows:
        return None
    total = sum(w for _, w in rows)
    acc = 0.0
    for v, w in rows:
        acc += w
        if acc >= total / 2.0:
            return v
    return rows[-1][0]


def _take(samples: list[Sample], target: int) -> list[Sample]:
    """Newest-first, until ``target`` iterations are in hand (always at least one run)."""
    out: list[Sample] = []
    have = 0
    for s in samples:
        out.append(s)
        have += max(1, s.iterations)
        if have >= target:
            break
    return out


def estimate_from_samples(samples: list[Sample], *, now: datetime | None = None) -> IterationCost:
    """The pure ladder over already-loaded samples (newest first or not — sorted here)."""
    now = now or datetime.now(timezone.utc)
    ordered = sorted((s for s in samples if s.per_iteration_ms and s.per_iteration_ms > 0), key=lambda s: s.at, reverse=True)
    if not ordered:
        return IterationCost(None, None, 0, 0, None, None)

    def _tier(window_min: float | None, basis: str) -> IterationCost | None:
        pool = ordered if window_min is None else [s for s in ordered if s.at >= now - timedelta(minutes=window_min)]
        if not pool:
            return None
        chosen = _take(pool, TARGET_ITERATIONS) if window_min is not None else pool[:FALLBACK_RUNS]
        iters = sum(max(1, s.iterations) for s in chosen)
        if window_min is not None and iters < MIN_ITERATIONS:
            return None
        ms = weighted_median([(s.per_iteration_ms, max(1, s.iterations)) for s in chosen])
        return IterationCost(
            round(ms, 3) if ms is not None else None, basis, iters, len(chosen), window_min,
            round((now - chosen[0].at).total_seconds(), 1),
        )

    return (
        _tier(RECENT_WINDOW_MIN, "recent")
        or _tier(WIDE_WINDOW_MIN, "today")
        or _tier(None, "history")
        or IterationCost(None, None, 0, 0, None, None)
    )


def iteration_cost(session, *, now: datetime | None = None) -> IterationCost:
    """The current cost of one iteration from the run table (see the module docstring)."""
    rows = session.execute(
        select(Run.finished_at, Run.created_at, Run.per_iteration_ms, Run.iterations_completed)
        .where(Run.status == RunStatus.COMPLETE, Run.per_iteration_ms.is_not(None))
        .order_by(Run.id.desc())
        .limit(SCAN_LIMIT)
    ).all()
    samples = [
        Sample(
            at=_as_utc(finished) or _as_utc(created) or datetime.now(timezone.utc),
            per_iteration_ms=float(per_ms),
            iterations=int(done or 1),
        )
        for finished, created, per_ms, done in rows
        if per_ms
    ]
    return estimate_from_samples(samples, now=now)


def estimate_ms(session) -> float | None:
    """The one number the ETAs multiply out, or None when nothing has been timed."""
    return iteration_cost(session).ms
