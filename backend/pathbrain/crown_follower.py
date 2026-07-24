"""Crown follower: keep the firewall on the crowned "best" profile as it changes.

The Settings-Impact crown (``compute_profiles`` → ``best_fingerprint``) is the confident
profile — total iterations ≥ ``correlation.min_iterations`` — with the highest Overall.
This module makes that verdict *actionable*: whenever a check runs (see the event-driven
design below) it will

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

**Event-driven, not polled.** The crown can only move when new data lands (a run
completes) or the grading changes (methodology / re-grade), so the follower doesn't sit
on a hot timer. The runner calls ``notify_run_complete`` as each run finishes (a pure
in-memory queue); on the next scheduler tick a **quick filter** (``_needs_full_check``)
decides whether that run could have moved the crown. Under the current ``weighted``
crown, profiles' Overalls are independent — a run changes exactly one profile — so the
filter recomputes just that profile's Overall (one indexed query, ``_profile_overall``)
and compares it against the cached crown/runner-up from the last full check. Only when
the filter can't rule movement out does the full ``compute_profiles`` verdict run
(which also records the ledger row and performs the apply). Percentile-corner
methodologies are field-relative (one run can move every profile's Overall), so there
the filter always escalates — still only on run completion, never on a clock. A slow
**backstop** full check (``crown_follow.interval_minutes``, default 6h) catches what
events can't see: re-grades finishing, external firewall edits between runs, config
changes. Quiet scheduler ticks cost zero I/O (an in-memory flag test).

Concurrency: checks run on the scheduler thread and take the coordination lock
non-blocking (``try_hold``) for the write — a busy pipeline defers the apply and the
follower retries shortly after (``RETRY_SECONDS``) rather than queueing behind a long
sweep. A crash mid-apply needs no reconciliation (nothing to restore), and re-applying
the crown is a no-op.
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
from .methodology import (
    ensure_current_methodology,
    overall_method,
    overall_metrics,
    overall_weights,
    weighted_score,
)
from .models import CrownEvent, Run, RunStatus, Score
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

# Module state: backstop bookkeeping, the last check's result for status display, and the
# quick-filter cache (the last full check's verdict — crown fp/Overall, runner-up Overall,
# and the methodology/config it was computed under).
_state: dict = {
    "last_full_check": 0.0,
    "backstop_s": None,  # cached backstop interval; None until the first config read
    "retry_at": None,    # soon-retry after a deferred/failed apply
    "last_result": None,
    "cache": None,
}
# Fingerprints of runs completed since the last evaluation (pure memory; the runner
# appends, the scheduler tick drains). Guarded by its own lock — never blocks a run.
_pending: list[str | None] = []
_pending_lock = threading.Lock()
# One check at a time (scheduler tick vs. a manual "sync now" from the API).
_check_lock = threading.Lock()

MIN_INTERVAL_MINUTES = 5
# How soon to retry a full check whose apply was deferred (coordinator busy) or failed —
# instead of waiting out the multi-hour backstop.
RETRY_SECONDS = 120


def _follow_config(session) -> dict:
    cfg = get_config(session).get("crown_follow", {}) or {}
    try:
        interval = float(cfg.get("interval_minutes", 360) or 360)
    except (TypeError, ValueError):
        interval = 360.0
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


def notify_run_complete(fingerprint: str | None) -> None:
    """Called by the runner as a run completes: queue a cheap crown re-evaluation for the
    next scheduler tick. Pure memory — never blocks or fails the finishing run."""
    with _pending_lock:
        _pending.append(fingerprint)


def _profile_overall(
    session, fp: str | None, version: str,
    crown_metrics: list[str], crown_required: list[str], weights: dict,
) -> tuple[float | None, int]:
    """``(overall, iterations)`` for **one** profile under the current *weighted* crown —
    the same median-subscore weighted average ``compute_profiles`` grades, computed over
    just this profile's comparable runs (one indexed query instead of the full field)."""
    rows = session.execute(
        select(Run.iterations, Score.subscores, Score.comparability)
        .join(Score, Score.run_id == Run.id)
        .where(
            Run.status == RunStatus.COMPLETE,
            Run.settings_fingerprint == fp,
            Score.methodology_version == version,
        )
    ).all()
    iters = 0
    samples: dict[str, list[float]] = {}
    for iterations, subscores, comparability in rows:
        if comparability == "incomparable":
            continue
        iters += int(iterations or 1)
        for m in crown_metrics:
            v = (subscores or {}).get(m)
            if v is not None:
                samples.setdefault(m, []).append(float(v))
    med = {m: round(median(vs), 2) for m, vs in samples.items() if vs}
    if any(med.get(m) is None for m in crown_required):
        return None, iters
    return weighted_score([(med.get(m), float(weights.get(m, 1.0))) for m in crown_metrics]), iters


def _needs_full_check(fp: str | None) -> tuple[bool, str]:
    """The cheap post-run filter: could the run that just completed on ``fp`` have moved
    the crown — or does the follower owe the firewall a write? Exact under a ``weighted``
    crown (profile Overalls are independent, so only ``fp``'s own standing changed);
    anything it can't rule out cheaply escalates to the full check. Ties escalate too
    (``<=``/``>=``), since the crown's exact-tie break (iterations, recency) needs the
    full field."""
    cache = _state.get("cache")
    if not cache:
        return True, "no cached standings yet"
    with session_scope() as session:
        config = get_config(session)
        methodology = ensure_current_methodology(session, config)
        min_iter = int((config.get("correlation", {}) or {}).get("min_iterations", 15) or 15)
        if (
            cache.get("methodology_version") != methodology.version
            or cache.get("min_iterations") != min_iter
        ):
            return True, "methodology or confidence bar changed"
        definition = methodology.definition or {}
        if overall_method(definition) != "weighted":
            # A percentile-corner crown is field-relative: one run re-ranks every profile,
            # so there is no sound incremental shortcut — always take the full verdict.
            return True, "field-relative (corner) crown"
        if fp is None:
            # A run that captured no settings joins no profile and can't move the crown.
            return False, "run has no settings fingerprint"
        crown_fp = cache.get("crown_fp")
        on_crown_fps = {f for f in (crown_fp, cache.get("crown_live_fp")) if f}
        if _follow_config(session)["enabled"] and crown_fp and fp not in on_crown_fps:
            # Following is armed yet a run measurably happened off-crown — the firewall
            # drifted or an engine raced other profiles; the full check re-applies.
            return True, "firewall was off the crown while following"
        crown_metrics, crown_required = overall_metrics(definition)
        weights = overall_weights(definition)
        overall, iters = _profile_overall(
            session, fp, methodology.version, crown_metrics, crown_required, weights
        )
        if fp in on_crown_fps:
            if overall is None:
                return True, "crown Overall no longer computable"
            cache["crown_overall"] = overall  # keep the cached bar fresh
            runner_up = cache.get("runner_up_overall")
            if runner_up is not None and overall <= runner_up:
                return True, "crown fell to the runner-up"
            return False, "crown still leads"
        if crown_fp is None:
            if overall is not None and iters >= min_iter:
                return True, "first confident profile (crown was vacant)"
            return False, "no confident crown yet"
        bar = cache.get("crown_overall")
        if overall is not None and iters >= min_iter and (bar is None or overall >= bar):
            return True, "profile reached the crown's Overall"
        return False, "still below the crown"


def step() -> bool:
    """Scheduler tick entry point. Quiet ticks cost zero I/O: work happens only when a
    run completed since the last tick (event queue), a deferred apply is due a retry, or
    the slow backstop interval elapsed. Completed runs go through the quick filter first;
    the full standings recompute runs only when the filter can't rule crown movement out.

    Returns True only when the firewall was written (so the scheduler yields the tick);
    everything else returns False and monitoring proceeds normally.
    """
    now = time.time()
    with _pending_lock:
        pending = list(dict.fromkeys(_pending))
        _pending.clear()
    backstop = _state.get("backstop_s")
    retry_at = _state.get("retry_at")
    due = (
        backstop is None
        or (now - _state["last_full_check"]) >= backstop
        or (retry_at is not None and now >= retry_at)
    )
    if not pending and not due:
        return False

    # We're doing work anyway — refresh the cached backstop interval from config.
    try:
        with session_scope() as session:
            cfg = _follow_config(session)
        _state["backstop_s"] = cfg["interval_minutes"] * 60.0
    except Exception:  # noqa: BLE001 — config read must never kill the scheduler
        log.debug("Crown follower: could not read config", exc_info=True)
        return False

    if pending and not due:
        try:
            reason = None
            for fp in pending:
                need, why = _needs_full_check(fp)
                if need:
                    reason = why
                    break
            if reason is None:
                log.debug(
                    "Crown follower: %d completed run(s), crown unaffected", len(pending)
                )
                return False
            log.info("Crown follower: full check — %s", reason)
        except Exception:  # noqa: BLE001 — a broken filter must degrade to the full check
            log.exception("Crown follower: quick check failed; falling back to full check")

    _state["last_full_check"] = now
    _state["retry_at"] = None
    try:
        result = check()
    except Exception:  # noqa: BLE001 — never let a check kill the scheduler loop
        log.exception("Crown follower check failed")
        return False
    # A deferred (pipeline busy) or failed apply retries soon, not hours away at the
    # next backstop.
    if (result.get("apply_skipped") or "").startswith("deferred") or (
        result.get("enabled") and result.get("error")
    ):
        _state["retry_at"] = now + RETRY_SECONDS
    return bool(result.get("applied"))


def poke() -> None:
    """Make the next scheduler tick run a full check (used when config just changed or a
    re-grade / methodology switch may have re-ranked the field without any run completing)."""
    _state["last_full_check"] = 0.0


def check() -> dict:
    """One full tracking (+ follow, when enabled) pass. Safe to call from any thread; also
    stamps the backstop clock, so a manual sync counts as a fresh full check."""
    with _check_lock:
        _state["last_full_check"] = time.time()
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
        meth_version = ensure_current_methodology(session, get_config(session)).version
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
        _cache_standings(field, meth_version, None, None)
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

    _cache_standings(field, meth_version, best, result)
    _state["last_result"] = result
    return result


def _cache_standings(field: dict, meth_version: str, best: dict | None, result: dict | None) -> None:
    """Refresh the quick-filter cache from a full check's verdict: the crown + runner-up
    Overalls the cheap post-run filter compares a single profile's recomputation against,
    stamped with the methodology/config they were computed under (a mismatch invalidates)."""
    best_fp = best.get("fingerprint") if best else None
    runner_up = None
    try:
        runner_up = max(
            (
                float(p["overall"])
                for p in field.get("profiles", [])
                if p.get("confident")
                and p.get("overall") is not None
                and p.get("fingerprint") != best_fp
            ),
            default=None,
        )
    except (TypeError, ValueError):  # malformed field (unit-test stubs) — cache without it
        runner_up = None
    _state["cache"] = {
        "methodology_version": meth_version,
        "min_iterations": int(field.get("min_iterations") or 0),
        "crown_fp": best_fp,
        "crown_overall": (
            float(best["overall"]) if best and best.get("overall") is not None else None
        ),
        "runner_up_overall": runner_up,
        # Where the firewall's echo actually hashes when it sits on the crown — a run
        # completed on this fingerprint is an on-crown run even if the hash differs from
        # the stored profile's (format-sensitive echoes).
        "crown_live_fp": (result or {}).get("live_fingerprint") if (result or {}).get("on_crown") else None,
    }


# ── Status + statistics ───────────────────────────────────────────────────────────────


def status() -> dict:
    """The last full check's result + when it ran (module state; None before the first)."""
    last = _state.get("last_full_check") or 0.0
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

    Changes are *observed at check time* (after completed runs that could move the crown,
    plus the backstop audit), so a flip that reverts entirely between two checks is
    invisible — this is a sampled statistic.
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


__all__ = ["step", "check", "poke", "notify_run_complete", "status", "stats", "recent_events"]
