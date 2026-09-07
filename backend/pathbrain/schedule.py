"""What is scheduled to run next — every source, in one place.

The Dashboard could say when the next **monitoring** run was due and nothing else, so the
nightly duel window, the nightly baseline test and the experiment window were invisible
until they started. That is the wrong half of the answer: monitoring is the one piece of
scheduled work that is small, frequent and unsurprising, while the ones that actually
change what the platform is doing for the next six hours — a ladder opening at 03:00, an
unshaped baseline test, an armed experiment — announced themselves only by taking the
pipeline. "Nothing is running" and "a duel opens in twenty minutes" are different states
and read identically.

Each engine already stores its own schedule in its own shape (an interval for monitoring, a
wall-clock time for the duel and the baseline test, a weekday-plus-hour-range window for the
experiment), and *that* is why this exists as a module rather than as another inline
computation: ``routes_baseline`` had the daily-time arithmetic written out by hand, the duel
had none at all, and a fourth copy would have been the point where they started disagreeing
about what "next" means.

Every time is returned as a real UTC **instant** with an offset, never a naive string: a
schedule is stored in the zone the user saved it from, and the browser renders in the
viewer's zone, so the conversion has to happen exactly once and be labelled when it does.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from .logging_config import get_logger
from .timezones import schedule_zone

log = get_logger("schedule")

#: How far ahead a weekday-gated window is searched before giving up. A window with no
#: allowed days has no next occurrence, and scanning forever to discover that is worse than
#: reporting "not scheduled".
WINDOW_SEARCH_DAYS = 14


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat()


def next_daily(hour: int, minute: int, section: dict) -> datetime | None:
    """The next occurrence of ``hour:minute`` in the schedule's own zone.

    The scheduler fires when the zone the user saved the schedule from reaches that wall
    clock, so the next occurrence is computed there and only then converted — the one place
    this arithmetic lives, instead of once per engine.
    """
    try:
        zone = schedule_zone(section)
        now = datetime.now(zone)
        candidate = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    except Exception:  # noqa: BLE001 — a schedule readout must never raise
        log.debug("schedule: could not compute the next daily occurrence", exc_info=True)
        return None


def next_window(days: list[int], start_hour: int, section: dict) -> datetime | None:
    """The next start of a weekday-gated hour window (the experiment engine's shape).

    ``days`` are weekday numbers (0 = Monday). An empty list means the window can never
    open, which is reported as "not scheduled" rather than as a date a fortnight out.
    """
    allowed = {int(d) for d in (days or [])}
    if not allowed:
        return None
    try:
        zone = schedule_zone(section)
        now = datetime.now(zone)
        for offset in range(WINDOW_SEARCH_DAYS):
            day = now + timedelta(days=offset)
            if day.weekday() not in allowed:
                continue
            candidate = datetime.combine(day.date(), time(hour=int(start_hour)), tzinfo=zone)
            if candidate > now:
                return candidate
    except Exception:  # noqa: BLE001
        log.debug("schedule: could not compute the next window", exc_info=True)
    return None


def _entry(kind: str, label: str, *, enabled: bool, at: datetime | None, detail: str | None) -> dict:
    return {
        "kind": kind,
        "label": label,
        "enabled": enabled,
        "at": _iso(at),
        "detail": detail,
    }


def upcoming(config: dict, monitoring_next: str | None = None, monitoring_enabled: bool = False,
             monitoring_interval: float | None = None) -> list[dict]:
    """Every scheduled source, armed or not, soonest first.

    Disarmed schedules are **included and marked** rather than dropped: "the duel is off" is
    a different and equally useful answer from "the duel is not for another six hours", and
    a list that silently omits the first one cannot distinguish them. They sort last, since
    they have no time.
    """
    out: list[dict] = []

    out.append(_entry(
        "monitoring",
        "Monitoring run",
        enabled=bool(monitoring_enabled),
        at=None,
        detail=(f"every {monitoring_interval:g} min" if monitoring_interval else None),
    ))
    # The scheduler already computes its own next tick; take it rather than re-deriving an
    # interval schedule that only it knows the last fire time for.
    out[-1]["at"] = monitoring_next

    duel = (config.get("duel") or {})
    if duel.get("continuous"):
        gap = duel.get("continuous_gap_minutes")
        out.append(_entry(
            "duel", "Duel ladder", enabled=bool(duel.get("enabled", False)), at=None,
            # Continuous mode has no clock: it starts whenever the pipeline is free and the
            # gap has elapsed, so naming a time would be inventing one.
            detail=f"continuous · {gap:g} min gap between sessions" if gap else "continuous",
        ))
    else:
        enabled = bool(duel.get("enabled", False))
        at = next_daily(duel.get("hour", 3), duel.get("minute", 0), duel) if enabled else None
        minutes = duel.get("duration_minutes")
        out.append(_entry(
            "duel", "Duel ladder", enabled=enabled, at=at,
            detail=f"runs for {int(minutes)} min" if minutes else None,
        ))

    bt = (config.get("baseline_test") or {})
    bt_enabled = bool(bt.get("enabled", False))
    out.append(_entry(
        "baseline_test", "Baseline test (SQM off)", enabled=bt_enabled,
        at=next_daily(bt.get("hour", 4), bt.get("minute", 0), bt) if bt_enabled else None,
        detail=f"{int(bt.get('iterations', 10) or 10)} iterations",
    ))

    exp = (config.get("experiment") or {})
    window = exp.get("window") or {}
    exp_enabled = bool(exp.get("enabled", False))
    out.append(_entry(
        "experiment", "Experiment window", enabled=exp_enabled,
        at=next_window(window.get("days") or [], window.get("start_hour", 2), exp) if exp_enabled else None,
        detail=("dry run" if exp.get("dry_run", True) else "armed — will apply") if exp_enabled else None,
    ))

    cf = (config.get("crown_follow") or {})
    if cf.get("enabled"):
        interval = cf.get("interval_minutes")
        out.append(_entry(
            "crown_follow", "Follow best (crown check)", enabled=True, at=None,
            # An interval backstop with no recorded last-fire is a cadence, not a time.
            detail=f"checks every {int(interval)} min" if interval else None,
        ))

    # Soonest first; anything without a time (disarmed, or a cadence rather than a clock)
    # sorts last rather than being dropped.
    out.sort(key=lambda e: (e["at"] is None, e["at"] or ""))
    return out


#: Sources that are a routine cadence rather than an event. They stay in ``upcoming`` (the
#: hover list is the whole schedule) but never become ``next``: the Dashboard's Monitoring
#: tile already says when the next monitoring run is due, so a "Next scheduled" that named it
#: too was the same fact twice and hid the answer the tile exists for — the overnight duel,
#: the baseline test, the experiment window: the things that change what the platform is
#: doing for hours.
ROUTINE_KINDS = frozenset({"monitoring"})


def status(config: dict, monitoring: dict | None = None) -> dict:
    """``{next, upcoming}`` — the whole schedule, and the one *event* happening soonest.

    ``next`` is the soonest clocked entry outside ``ROUTINE_KINDS``; ``None`` when nothing
    but routine work (or nothing at all) has a time. A cadence source with no clock (a
    continuous duel, the crown-follow backstop) is never ``next`` either — it starts
    whenever the pipeline is free, and naming an hour would be inventing one — but it is in
    ``upcoming`` marked enabled, so the tile can say it is armed.
    """
    monitoring = monitoring or {}
    items = upcoming(
        config,
        monitoring_next=monitoring.get("next_run_at"),
        monitoring_enabled=bool(monitoring.get("enabled")),
        monitoring_interval=monitoring.get("interval_minutes"),
    )
    nxt = next((e for e in items if e["at"] and e["kind"] not in ROUTINE_KINDS), None)
    return {"next": nxt, "upcoming": items}


__all__ = ["upcoming", "status", "next_daily", "next_window"]
