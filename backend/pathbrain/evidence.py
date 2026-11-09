"""The evidence layer: one place where PathBrain compares two things.

``docs/plan-learning-engine.md`` (2026-11-09) calls this the **silver** layer of one evidence
backbone. Bronze is the runs and raw store, never modified. Silver, this module, turns
those observations into **margins**: a candidate measured against a control measured in the
same conditions. Gold (``beliefs``, phase 3) fits a posterior from the margins.

Why it exists. Each engine used to keep its own yardstick. The duel compared profiles as
same-window paired margins, while the recommendation ledger graded each Explore bet against
the all-time pooled best. When the instrument stepped (LCP roughly doubled twice in late
October, with nothing on record to explain it) the ledger read ten bets as misses by 7 to
21 points. Against what ran the same night they were level. A comparison made across an
instrument step measures the step, so this module refuses to make one.

Three things live here, and every consumer is meant to read them rather than re-derive
them:

* :func:`instrument_epochs`: the **eras** the instrument has been stable in. A sustained
  step in a crown reading (``instrument_drift``'s own quantities) is an epoch boundary;
  pooled numbers are only compared within an epoch.
* :func:`control` and :func:`margin`: the **comparison primitive**. A claim is graded as
  the candidate's margin over its parent measured in the same window, else in the same
  epoch, never against a number from a different era.
* :func:`search_space`: **what may be proposed**. Writable (the axes are already
  ``WRITABLE_FIELDS``) and not held (both pipes' bandwidth is held: decision 11) and either
  live, or inert with its one surprise check still owed. A lever the mechanism model
  predicts inert gets one measured attempt as a hypothesis to refute, not open space to
  fill, unless the ring has already caught it moving the Overall.

Everything here is pure except :func:`instrument_epochs`, which reads the run store once
and caches the answer, and never raises: an unreadable store means "one epoch", which is
exactly the behaviour before this module existed.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from statistics import median

from .levers import MECHANISM as LEVER_MECHANISM
from .logging_config import get_logger

log = get_logger("evidence")

# ── instrument epochs ────────────────────────────────────────────────────────────────

#: The crown readings whose step makes a new epoch. ``instrument_drift.CROWN_KEYS`` minus
#: cadence, whose daily medians are too coarse to place a boundary on. A step in FCP or LCP
#: is a step in what the Overall is built from.
EPOCH_KEYS = ("fcp_ms", "lcp_ms")
#: Days either side of a boundary that must agree. A step is a level change sustained over
#: this many qualifying days on both sides, all but one of them on their own side of the
#: midpoint, so a single bad day is never an epoch.
EPOCH_SPAN = 3
#: How far back epochs are looked for, and how many runs are sampled to find them.
EPOCH_DAYS = 120
EPOCH_SAMPLE_LIMIT = 6000
#: How long a computed set of boundaries is reused. A boundary needs three days of evidence
#: after it, so nothing about the answer can change faster than this.
EPOCH_CACHE_SECONDS = 15 * 60

_cache_lock = threading.Lock()
_cache: dict = {}


def _as_utc(dt: datetime | None) -> datetime | None:
    """Stored times are naive UTC; samples are aware UTC. Compare them as aware UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def epoch_boundaries(cohorts_: list[dict], *, keys=EPOCH_KEYS, span: int = EPOCH_SPAN) -> list[dict]:
    """Sustained steps in the daily cohorts of ``instrument_drift.cohorts``, oldest first.

    ``instrument_drift.steps`` reports the single largest day-over-day change per quantity,
    which is the right answer for a card and the wrong one here: LCP stepped twice, and both
    steps are boundaries. So each day boundary is tested on its own: the median of the
    ``span`` qualifying days before it against the median of the ``span`` days from it on,
    using the same thresholds the drift card uses (``STEP_PCT`` of the earlier level, and at
    least the quantity's floor), plus one more, that the change is sustained. Adjacent
    boundaries that are the same step seen from neighbouring days collapse to the largest.

    Pure: cohorts in, ``[{"at": "YYYY-MM-DD", "key", "before", "after", "shift_pct"}]`` out.
    """
    from .instrument_drift import QUANTITIES, STEP_MIN_RUNS, STEP_PCT

    days = [c for c in cohorts_ if int(c.get("runs") or 0) >= STEP_MIN_RUNS]
    found: list[tuple[int, dict]] = []
    for i in range(span, len(days) - span + 1):
        best: dict | None = None
        for q in keys:
            before = [d["medians"].get(q) for d in days[i - span:i]]
            after = [d["medians"].get(q) for d in days[i:i + span]]
            before = [v for v in before if v is not None]
            after = [v for v in after if v is not None]
            if len(before) < span or len(after) < span:
                continue
            a, b = median(before), median(after)
            if not a:
                continue
            delta = b - a
            rel = delta / a * 100.0
            floor = float((QUANTITIES.get(q) or {}).get("floor") or 0.0)
            if abs(delta) < floor or abs(rel) < STEP_PCT:
                continue
            # Sustained, not a spike: all but one day on each side sit on their own side of
            # the midpoint. One bad day near a real step is tolerated; a lone bad day with
            # normal days around it never moves the medians far enough to get here.
            mid = (a + b) / 2.0
            low, high = (before, after) if delta > 0 else (after, before)
            conforming = sum(1 for v in low if v < mid) + sum(1 for v in high if v > mid)
            if conforming < 2 * (span - 1):
                continue
            score = (conforming, abs(rel))
            if best is None or score > best["_score"]:
                best = {
                    "_score": score,
                    "at": days[i]["key"],
                    "key": q,
                    "before": round(a, 3),
                    "after": round(b, 3),
                    "shift_pct": round(rel, 1),
                }
        if best is not None:
            found.append((i, best))

    # One step seen from neighbouring days is one boundary: keep the cleanest of any run of
    # candidates closer together than ``span`` days (most days on their own side, then the
    # largest shift, then the earliest).
    out: list[dict] = []
    group: list[tuple[int, dict]] = []

    def _flush() -> None:
        pick = dict(max(group, key=lambda g: g[1]["_score"])[1])
        pick.pop("_score", None)
        out.append(pick)

    for item in found:
        if group and item[0] - group[-1][0] >= span:
            _flush()
            group = []
        group.append(item)
    if group:
        _flush()
    return out


def boundary_times(boundaries: list[dict]) -> list[datetime]:
    """Each boundary as the UTC midnight its first day starts at, oldest first."""
    out: list[datetime] = []
    for b in boundaries:
        try:
            out.append(datetime.strptime(str(b["at"])[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc))
        except (KeyError, ValueError):
            continue
    return sorted(out)


def epoch_of(at: datetime | None, boundaries: list[datetime]) -> int:
    """The epoch index of a moment: 0 before the first boundary, 1 after it, and so on.

    A run on a boundary day belongs to the epoch the day starts, since boundaries are placed
    at day resolution and the new level is what that day mostly measured."""
    t = _as_utc(at)
    if t is None:
        return 0
    return sum(1 for b in boundaries if t >= b)


def instrument_epochs(session, *, now: datetime | None = None, refresh: bool = False) -> dict:
    """``{"boundaries": [...], "times": [datetime, ...]}`` for the run store, cached.

    Never raises. Anything that goes wrong reading the store answers "no boundaries", which
    is one epoch: exactly how every comparison behaved before epochs existed."""
    with _cache_lock:
        hit = _cache.get("epochs")
        if hit and not refresh and time.monotonic() - hit[0] < EPOCH_CACHE_SECONDS:
            return hit[1]
    try:
        from .instrument_drift import cohorts, load_samples

        samples = load_samples(session, days=EPOCH_DAYS, limit=EPOCH_SAMPLE_LIMIT, now=now)
        found = epoch_boundaries(cohorts(samples, bucket="day"))
    except Exception:  # noqa: BLE001 — an audit that fails must not fail its consumers
        log.debug("Evidence: could not compute instrument epochs", exc_info=True)
        found = []
    result = {"boundaries": found, "times": boundary_times(found)}
    with _cache_lock:
        _cache["epochs"] = (time.monotonic(), result)
    return result


def clear_cache() -> None:
    """Forget the cached epochs (tests, and after a re-grade)."""
    with _cache_lock:
        _cache.clear()


# ── the comparison primitive ─────────────────────────────────────────────────────────

#: How close in time a control run must be to the candidate's runs to count as the same
#: window. Two hours either side covers an ABBA bout and the duel's own interleave while
#: staying inside one time-of-day and one weather stamp.
WINDOW_HOURS = 2.0
#: The fewest comparable control iterations a margin is graded on. Below it the control is
#: one noisy run and the margin is that run's noise.
MIN_CONTROL_ITERATIONS = 3

CONTROL_LABELS = {
    "same_window": "the parent measured within two hours of the candidate",
    "same_epoch": "the parent measured in the same instrument epoch",
}


def _comparable_iterations(rows) -> int:
    return sum(int(r[1] or 1) for r in rows if r[3] != "incomparable")


def control(
    bet_times: list[datetime],
    parent_rows: list[tuple],
    boundaries: list[datetime],
    *,
    window_hours: float = WINDOW_HOURS,
    min_iterations: int = MIN_CONTROL_ITERATIONS,
) -> dict:
    """The control a candidate is compared with: which of the parent's runs, and why.

    ``parent_rows`` are ``(created_at, iterations, subscores, comparability)``, the shape
    the ledger already reads. Tried in order:

    1. ``same_window``: the parent's runs within ``window_hours`` of the candidate's first
       and last runs. Time of day, weather and the instrument are shared, so they cancel.
    2. ``same_epoch``: the parent's runs in the instrument epoch the candidate was measured
       in. Weather and time of day no longer cancel, but the instrument does.
    3. none: there is nothing comparable to grade against, and the caller says so rather
       than reach for a number from another era.

    Returns ``{"kind", "rows", "epoch"}``; ``rows`` is the subset to grade the control on.
    """
    times = sorted(t for t in (_as_utc(t) for t in bet_times) if t is not None)
    if not times:
        return {"kind": None, "rows": [], "epoch": None}
    epoch = epoch_of(times[0], boundaries)
    rows = [r for r in parent_rows if _as_utc(r[0]) is not None]
    lo = times[0] - timedelta(hours=window_hours)
    hi = times[-1] + timedelta(hours=window_hours)
    window = [r for r in rows if lo <= _as_utc(r[0]) <= hi]
    if _comparable_iterations(window) >= min_iterations:
        return {"kind": "same_window", "rows": window, "epoch": epoch}
    same = [r for r in rows if epoch_of(r[0], boundaries) == epoch]
    if _comparable_iterations(same) >= min_iterations:
        return {"kind": "same_epoch", "rows": same, "epoch": epoch}
    return {"kind": None, "rows": [], "epoch": epoch}


def margin(
    *,
    predicted: float | None,
    reference: float | None,
    actual: float | None,
    control_overall: float | None,
) -> dict | None:
    """A claim restated as a margin, and graded as one.

    ``predicted - reference`` is the margin the model claimed over its parent, where
    ``reference`` is the starting point the prediction was built on (its shrunk anchor when
    recorded, else the parent's Overall at the time). ``actual - control_overall`` is the
    margin measured against the parent in the same conditions. The error is the difference,
    so a field that moved by eight points between the claim and the test moves both sides
    and cancels.
    """
    if None in (predicted, reference, actual, control_overall):
        return None
    predicted_margin = float(predicted) - float(reference)
    measured_margin = float(actual) - float(control_overall)
    return {
        "predicted": round(predicted_margin, 2),
        "measured": round(measured_margin, 2),
        "error": round(measured_margin - predicted_margin, 2),
    }


# ── what may be proposed ─────────────────────────────────────────────────────────────

#: Fields no proposer may move, on any pipe (decision 11). A pipe's speed changes what the
#: test measures (throughput the household gives up) rather than how the queue behaves, so
#: it is a deliberate choice for a person to make, not a lever for a search to fill.
HELD_FIELDS = frozenset({"download_bandwidth", "upload_bandwidth"})

SEARCH_STATUSES = ("live", "surprise", "inert", "inert_checked", "held")


def lever_status(
    axis: dict,
    ring_by_key: dict[str, dict] | None = None,
    checked_axes: set[str] | None = None,
) -> tuple[str, str]:
    """``(status, reason)`` for one axis.

    * ``held``: never proposed (bandwidth, either pipe).
    * ``surprise``: the mechanism predicts no effect and the ring has measured one anyway,
      so the link's model is what is wrong, and the lever is fully open.
    * ``inert``: predicted inert, not refuted, and not yet tried; its one check is owed.
    * ``inert_checked``: predicted inert and already tried at least once; closed until the
      ring catches it moving.
    * ``live``: everything else (quantum, and any lever with a mechanism here).
    """
    key = str(axis.get("key") or "")
    fld = str(axis.get("field") or "")
    if fld in HELD_FIELDS:
        return "held", "a pipe's speed is held: it trades throughput rather than tuning the queue"
    if (LEVER_MECHANISM.get(fld) or {}).get("prediction") != "null":
        return "live", "the mechanism model expects this lever can move the Overall here"
    transitions = ((ring_by_key or {}).get(key) or {}).get("transitions") or []
    if any(t.get("significant") for t in transitions):
        return "surprise", "predicted inert, but the ring measured it moving the Overall"
    if key in (checked_axes or set()):
        return "inert_checked", (
            "predicted inert on an unsaturated link and already tried; closed until the ring "
            "finds it moving"
        )
    return "inert", "predicted inert and not yet tried: one check is owed, to refute or confirm"


def search_space(
    axes: dict[str, dict],
    ring_by_key: dict[str, dict] | None = None,
    claimed: list[dict] | None = None,
) -> dict[str, dict]:
    """``{axis key: {"key", "status", "proposable", "reason"}}``: the one space every
    proposer draws from.

    ``claimed`` is ``explore_tracker.claimed_moves``: every proposal a benchmark has already
    been spent on. An inert lever's owed check is paid by any of them that moved it.
    """
    checked: set[str] = set()
    for claim in claimed or []:
        if claim.get("unreachable"):
            continue  # attempted, never measured: the check is still owed
        checked.update(str(k) for k in (claim.get("moves") or {}))
    out: dict[str, dict] = {}
    for key, axis in axes.items():
        status, reason = lever_status({**axis, "key": key}, ring_by_key, checked)
        out[key] = {
            "key": key,
            "status": status,
            "proposable": status in ("live", "surprise", "inert"),
            "reason": reason,
        }
    return out


def proposable(key: str, space: dict[str, dict]) -> bool:
    """Whether a proposer may move ``key``. An axis the space has never heard of may not."""
    return bool((space.get(key) or {}).get("proposable"))


def in_search_space(candidate: dict | None, space: dict[str, dict]) -> bool:
    """Whether every lever a candidate moves is in the search space."""
    if not candidate:
        return False
    changes = candidate.get("changes") or []
    return bool(changes) and all(
        isinstance(ch, dict) and proposable(str(ch.get("key")), space) for ch in changes
    )


__all__ = [
    "EPOCH_KEYS",
    "HELD_FIELDS",
    "WINDOW_HOURS",
    "MIN_CONTROL_ITERATIONS",
    "epoch_boundaries",
    "boundary_times",
    "epoch_of",
    "instrument_epochs",
    "clear_cache",
    "control",
    "margin",
    "lever_status",
    "search_space",
    "proposable",
    "in_search_space",
]
