"""Crown follower: keep the firewall on the crowned "best" profile as it changes.

The Settings-Impact crown (``compute_profiles`` → ``best_fingerprint``) is the confident
profile — total iterations ≥ ``correlation.min_iterations`` — with the highest Overall.
This module makes that verdict *actionable*: on an interval the scheduler asks it to

1. **Track** — recompute the standings and, when the crown differs from the last one
   recorded, write a ``CrownEvent`` ledger row. The ledger powers the crown-**churn**
   statistics (``stats``): how often the best profile changes, how long a reign lasts.
   Tracking is read-only and always on, so the stat accrues *before* the user ever arms
   the follower — exactly the number they need to decide whether auto-following would
   thrash the firewall.
2. **Follow** (only when config ``crown_follow.enabled``) — if the firewall isn't
   semantically on the crown (``plan_apply`` finds writable diffs), apply the crown's
   writable fields via ``provider.apply()`` under the coordination lock. This is a
   one-way write, exactly like the Settings-Impact "Apply this profile" button — there
   is no baseline to restore, because *being on the crown* is the desired steady state.
   Two profiles are never auto-applied: the collapsed **"SQM off"** profile (the follower
   must not disable shaping — that's the baseline test's supervised job) and any profile
   **unreachable** from the live environment (differs in a non-writable field, the same
   ``environment_signature`` guard the challenger race uses).

The apply is deliberately **mirror, no hysteresis**: the crown itself has none (the
profile that wins, wins — see ``settings_profile``), and the follower just keeps the
firewall on whoever that is. The churn ledger is what tells the user whether that
verdict is stable enough to hand the keys to; ``co_leaders``/``crown_confidence`` on the
profiles response show whether a flip was signal or noise.

Concurrency: periodic checks run on the scheduler thread and take the coordination lock
non-blocking (``try_hold``) for the write — a busy pipeline defers the apply to the next
interval rather than queueing behind a long sweep. A crash mid-apply needs no
reconciliation (nothing to restore), and re-applying the crown is a no-op.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from statistics import mean, median

from sqlalchemy import select

from . import coordinator
from .config_store import get_config
from .database import session_scope
from .logging_config import get_logger
from .models import CrownEvent
from .profile_test import _apply_all
from .providers import get_provider
from .settings_profile import (
    SQM_OFF_FINGERPRINT,
    environment_signature,
    fingerprint,
    normalize,
    plan_apply,
)

log = get_logger("crown_follower")

# Module state: interval bookkeeping + the last check's result for status display.
_state: dict = {"last_check": 0.0, "last_result": None}
# One check at a time (scheduler tick vs. a manual "sync now" from the API).
_check_lock = threading.Lock()

MIN_INTERVAL_MINUTES = 5


def _follow_config(session) -> dict:
    cfg = get_config(session).get("crown_follow", {}) or {}
    try:
        interval = float(cfg.get("interval_minutes", 30) or 30)
    except (TypeError, ValueError):
        interval = 30.0
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "interval_minutes": max(interval, MIN_INTERVAL_MINUTES),
    }


def _compute_field(session) -> dict:
    """The full Settings-Impact standings (lazy import — routes_settings imports widely)."""
    from .api.routes_settings import compute_profiles

    return compute_profiles(session, complete_only=True)


def _last_change(session) -> tuple[str | None, str | None]:
    """(fingerprint, label) of the most recently *recorded* crown (None if never tracked)."""
    row = session.scalars(
        select(CrownEvent).where(CrownEvent.kind == "change").order_by(CrownEvent.id.desc())
    ).first()
    return (row.fingerprint, row.label) if row else (None, None)


def step() -> bool:
    """Scheduler tick entry point: run a check when the interval has elapsed.

    Returns True only when the firewall was written (so the scheduler yields the tick);
    a tracking-only pass returns False and monitoring proceeds normally.
    """
    try:
        with session_scope() as session:
            cfg = _follow_config(session)
    except Exception:  # noqa: BLE001 — config read must never kill the scheduler
        log.debug("Crown follower: could not read config", exc_info=True)
        return False
    if time.time() - _state["last_check"] < cfg["interval_minutes"] * 60.0:
        return False
    _state["last_check"] = time.time()
    try:
        result = check()
    except Exception:  # noqa: BLE001 — never let a check kill the scheduler loop
        log.exception("Crown follower check failed")
        return False
    return bool(result.get("applied"))


def poke() -> None:
    """Make the next scheduler tick run a check (used when config just changed)."""
    _state["last_check"] = 0.0


def check() -> dict:
    """One tracking (+ follow, when enabled) pass. Safe to call from any thread."""
    with _check_lock:
        return _do_check()


def _do_check() -> dict:  # noqa: PLR0912, PLR0915 — one linear decision ladder
    now = datetime.now(timezone.utc)
    result: dict = {
        "checked_at": now.isoformat(),
        "enabled": False,
        "crown_fingerprint": None,
        "crown_label": None,
        "crown_changed": False,
        "live_fingerprint": None,
        "on_crown": None,
        "applied": False,
        "apply_skipped": None,
        "error": None,
    }

    with session_scope() as session:
        result["enabled"] = _follow_config(session)["enabled"]
        field = _compute_field(session)
        prev_fp, prev_label = _last_change(session)

    best_fp = field.get("best_fingerprint")
    best = next(
        (p for p in field.get("profiles", []) if p.get("fingerprint") == best_fp), None
    )
    if best_fp is None or best is None:
        # No confident crown right now (bootstrap, or a methodology change quarantined
        # history). Nothing to record — a vacancy isn't a crown change — and nothing to
        # follow. The last applied profile simply stays on the firewall.
        _state["last_result"] = result
        return result

    result["crown_fingerprint"] = best_fp
    result["crown_label"] = best.get("label")
    changed = best_fp != prev_fp

    # Where is the firewall right now? Best-effort; a discovery failure only skips the
    # follow half (tracking still records the change).
    provider = None
    live = None
    live_norm = None
    try:
        provider = get_provider()
        live = provider.discover()
        live_norm = normalize(live)
        result["live_fingerprint"] = fingerprint(live_norm)
    except Exception as exc:  # noqa: BLE001
        log.warning("Crown follower: discovery failed: %s", exc)
        result["error"] = f"discovery failed: {type(exc).__name__}: {exc}"

    target = best.get("settings") or []
    pending: list[dict] = []
    applied = False
    apply_error: str | None = None
    skip: str | None = None
    on_crown: bool | None = None

    if live_norm is not None:
        # "On the crown" is judged *semantically* — no remaining writable diffs and the
        # same non-writable environment — not by fingerprint hash, which is
        # format-sensitive ("5ms" vs "5") and would read a matching firewall as off-crown.
        # Exception: when either side is the collapsed "SQM off" profile the shaper params
        # are inert and invisible to plan_apply, so only the fingerprint can tell them apart.
        pending, _ = plan_apply(target, live)
        same_env = environment_signature(target) == environment_signature(live_norm)
        target_sqm_off = best_fp == SQM_OFF_FINGERPRINT or any(
            (p or {}).get("enabled") is False for p in target
        )
        live_sqm_off = result["live_fingerprint"] == SQM_OFF_FINGERPRINT
        if target_sqm_off or live_sqm_off:
            on_crown = result["live_fingerprint"] == best_fp
        else:
            on_crown = not pending and same_env
        result["on_crown"] = on_crown

        if not on_crown and result["enabled"]:
            if target_sqm_off:
                skip = "crown is the 'SQM off' profile — the follower never disables shaping"
            elif live_sqm_off:
                skip = "SQM is currently off on a pipe — the follower never toggles shaping on/off"
            elif not target:
                skip = "crown profile has no stored settings"
            elif not same_env:
                skip = "unreachable: the crown differs in a non-writable field"
            else:
                try:
                    # Non-blocking: a busy pipeline (sweep/race/monitoring run) defers the
                    # apply to the next interval instead of queueing the scheduler thread.
                    with coordinator.try_hold("crown-follow"):
                        _apply_all(provider, pending)
                        live_after = provider.discover()
                        remaining, _ = plan_apply(target, live_after)
                        if remaining:
                            missed = ", ".join(
                                f"{c['label']}·{c['field']} (wanted {c.get('to')}, is {c.get('from')})"
                                for c in remaining
                            )
                            raise RuntimeError(
                                f"firewall did not accept {len(remaining)} field(s): {missed}"
                            )
                        result["live_fingerprint"] = fingerprint(normalize(live_after))
                        result["on_crown"] = True
                        applied = True
                        log.info(
                            "Crown follower applied crown %s (%s change(s))",
                            best_fp,
                            len(pending),
                        )
                except coordinator.CoordinatorBusy as exc:
                    skip = f"deferred: {exc}"
                except Exception as exc:  # noqa: BLE001
                    log.exception("Crown follower: apply failed")
                    apply_error = f"{type(exc).__name__}: {exc}"
        elif not on_crown:
            skip = "following disabled — crown not applied"

    result["crown_changed"] = changed and prev_fp is not None
    result["applied"] = applied
    result["apply_skipped"] = skip
    if apply_error:
        result["error"] = apply_error

    # Ledger: a "change" row per crown change (the churn stat's raw data; the first-ever
    # observation gets previous=None and only marks when tracking began), plus an "apply"
    # row when the follower wrote the firewall *without* a crown change (just enabled, or
    # the firewall had drifted off-crown).
    overall = best.get("overall")
    detail = skip if not applied else f"{len(pending)} change(s) written"
    try:
        with session_scope() as session:
            if changed:
                session.add(
                    CrownEvent(
                        kind="change",
                        fingerprint=best_fp,
                        previous_fingerprint=prev_fp,
                        label=(best.get("label") or "")[:255] or None,
                        previous_label=prev_label,
                        overall=float(overall) if overall is not None else None,
                        applied=applied,
                        error=apply_error,
                        detail=detail,
                    )
                )
            elif applied or apply_error:
                session.add(
                    CrownEvent(
                        kind="apply",
                        fingerprint=best_fp,
                        previous_fingerprint=result["live_fingerprint"] if not applied else None,
                        label=(best.get("label") or "")[:255] or None,
                        overall=float(overall) if overall is not None else None,
                        applied=applied,
                        error=apply_error,
                        detail=detail,
                    )
                )
    except Exception:  # noqa: BLE001 — a ledger write must never fail the check
        log.exception("Crown follower: could not record crown event")

    _state["last_result"] = result
    return result


# ── Status + statistics ───────────────────────────────────────────────────────────────


def status() -> dict:
    """The last check's result + when it ran (module state; None before the first check)."""
    last = _state.get("last_check") or 0.0
    return {
        "last_check_at": (
            datetime.fromtimestamp(last, tz=timezone.utc).isoformat() if last else None
        ),
        "last_result": _state.get("last_result"),
    }


def _as_utc(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; they're stored UTC (project convention)."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def stats(session, now: datetime | None = None) -> dict:
    """Crown-churn statistics from the ``CrownEvent`` ledger — "how often does the best
    profile change?".

    * ``total_changes`` / ``changes_24h`` / ``changes_7d`` / ``changes_30d`` — real crown
      changes (the first observation, ``previous_fingerprint=None``, is when tracking
      began, not a change).
    * ``mean_reign_hours`` / ``median_reign_hours`` — over *completed* reigns: the gaps
      between consecutive ledger timestamps (tracking start → first change → … → latest
      change). ``current_reign_hours`` is the still-open reign of the sitting crown.
    * ``changes_per_day`` — changes over the tracked window (capped at the last 30 days),
      per day. The obvious "will auto-follow thrash?" number.

    Changes are *observed at check time* (every ``interval_minutes``), so a flip that
    reverts entirely between two checks is invisible — this is a sampled statistic.
    """
    now = _as_utc(now or datetime.now(timezone.utc))
    rows = session.scalars(
        select(CrownEvent).where(CrownEvent.kind == "change").order_by(CrownEvent.id.asc())
    ).all()
    changes = [r for r in rows if r.previous_fingerprint is not None]

    def _within(hours: float) -> int:
        cutoff = now - timedelta(hours=hours)
        return sum(1 for r in changes if _as_utc(r.created_at) >= cutoff)

    tracked_since = _as_utc(rows[0].created_at) if rows else None
    last_change_at = _as_utc(changes[-1].created_at) if changes else None
    current_crown = rows[-1].fingerprint if rows else None
    current_label = rows[-1].label if rows else None

    # Completed reigns: gaps between consecutive ledger rows (first row = tracking start).
    times = [_as_utc(r.created_at) for r in rows]
    reigns = [
        (b - a).total_seconds() / 3600.0 for a, b in zip(times, times[1:]) if b > a
    ]
    reign_start = times[-1] if times else None
    current_reign_h = (
        (now - reign_start).total_seconds() / 3600.0 if reign_start else None
    )

    changes_30d = _within(24 * 30)
    per_day = None
    if tracked_since is not None:
        window_days = min((now - tracked_since).total_seconds() / 86400.0, 30.0)
        if window_days >= 1.0:
            per_day = round(changes_30d / window_days, 2)

    cutoff_30d = now - timedelta(days=30)
    distinct_30d = len(
        {r.fingerprint for r in changes if _as_utc(r.created_at) >= cutoff_30d}
    )

    def _round(x: float | None) -> float | None:
        return round(x, 1) if x is not None else None

    return {
        "tracked_since": tracked_since.isoformat() if tracked_since else None,
        "total_changes": len(changes),
        "changes_24h": _within(24),
        "changes_7d": _within(24 * 7),
        "changes_30d": changes_30d,
        "changes_per_day": per_day,
        "distinct_crowns_30d": distinct_30d,
        "last_change_at": last_change_at.isoformat() if last_change_at else None,
        "current_crown_fingerprint": current_crown,
        "current_crown_label": current_label,
        "current_reign_hours": _round(current_reign_h),
        "mean_reign_hours": _round(mean(reigns)) if reigns else None,
        "median_reign_hours": _round(median(reigns)) if reigns else None,
    }


def recent_events(session, limit: int = 20) -> list[dict]:
    """The newest ledger rows (changes + applies interleaved), newest first."""
    rows = session.scalars(
        select(CrownEvent).order_by(CrownEvent.id.desc()).limit(max(1, int(limit)))
    ).all()
    return [
        {
            "id": r.id,
            "created_at": _as_utc(r.created_at).isoformat() if r.created_at else None,
            "kind": r.kind,
            "fingerprint": r.fingerprint,
            "previous_fingerprint": r.previous_fingerprint,
            "label": r.label,
            "previous_label": r.previous_label,
            "overall": r.overall,
            "applied": r.applied,
            "error": r.error,
            "detail": r.detail,
        }
        for r in rows
    ]


__all__ = ["step", "check", "poke", "status", "stats", "recent_events"]
