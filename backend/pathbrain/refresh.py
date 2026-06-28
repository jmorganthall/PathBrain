"""Profile refresh: re-run every stored profile for a chosen number of iterations.

The batch sibling of ``profile_test`` ("Test to minimum"). Publishing a methodology
that adds a crown metric quarantines historical runs whose raw can't supply it
(``methodology.comparability`` → ``incomparable``); those profiles then have no
comparable data under the current methodology. This session gives them fresh data:

1. Snapshot the live firewall settings (the baseline to restore).
2. For each stored profile: apply it for real, read it back to confirm it was reached,
   and run one benchmark with the **caller-chosen** number of iterations (the caller
   decides how much fresh data to collect per profile — not auto-forced to the minimum).
3. **Always** restore the pre-refresh baseline at the end (and on crash-restart, via
   ``reconcile_interrupted_refreshes``).

Like ``profile_test``/``challenger`` it runs in its own thread and holds the
coordination lock for the whole session, so it never overlaps a sweep, an experiment,
or a monitoring/manual run. Each benchmark adds the read-before/after integrity
guarantee (see ``runner``). One profile failing to apply doesn't abort the batch — it's
logged and the refresh moves on.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from statistics import median

from sqlalchemy import select

from . import coordinator
from .database import session_scope
from .logging_config import get_logger
from .models import ProfileRefresh, ProfileRefreshStatus, Run, RunStatus
from .profile_test import _apply_all
from .providers import get_provider
from .runner import MAX_ITERATIONS, create_run, execute_run
from .settings_profile import fingerprint, normalize, plan_apply, summarize

log = get_logger("refresh")

# One refresh at a time. Module state coordinates with the driver thread, carries the
# work-list (settings aren't all stored on the row), and the cooperative cancel flag.
_state: dict = {"active": False, "id": None, "thread": None, "cancel": False, "plan": None}


def active() -> bool:
    return bool(_state.get("active"))


def cancel() -> bool:
    """Request the running refresh stop after the current profile. False if none."""
    if not active():
        return False
    _state["cancel"] = True
    log.info("Profile refresh %s: cancel requested", _state.get("id"))
    return True


def list_profiles(session) -> list[dict]:
    """Every distinct stored profile (newest settings per fingerprint), as
    ``[{fingerprint, settings, label}]`` — the candidates a refresh re-runs."""
    rows = session.execute(
        select(Run.settings_fingerprint, Run.settings)
        .where(
            Run.status == RunStatus.COMPLETE,
            Run.settings_fingerprint.is_not(None),
            Run.settings.is_not(None),
        )
        .order_by(Run.created_at.desc())
    ).all()
    latest: dict[str, list] = {}
    for fp, settings in rows:
        latest.setdefault(fp, settings)  # desc order → first seen is the newest settings
    return [
        {"fingerprint": fp, "settings": settings, "label": summarize(settings)}
        for fp, settings in latest.items()
    ]


# Rough fixed overhead per profile (apply + read-back verify + final restore), added to
# the benchmark time so the estimate isn't optimistic. Seconds.
_PER_PROFILE_OVERHEAD_S = 3.0


def _median_iteration_ms(session) -> float | None:
    """Median wall-clock per benchmark iteration, from recent completed runs — the basis
    for the time estimate. ``None`` when no run has recorded a per-iteration timing yet."""
    rows = session.scalars(
        select(Run.per_iteration_ms)
        .where(Run.status == RunStatus.COMPLETE, Run.per_iteration_ms.is_not(None))
        .order_by(Run.created_at.desc())
        .limit(50)
    ).all()
    vals = [float(v) for v in rows if v]
    return median(vals) if vals else None


def preview(session, iterations: int) -> dict:
    """What a refresh would do + how long it'd take: profile count, total iterations, and
    an estimated duration (median per-iteration time × total iterations + per-profile
    apply/restore overhead). ``estimated_seconds`` is None when there's no timing history
    to base it on."""
    iters = max(1, min(MAX_ITERATIONS, int(iterations)))
    n_profiles = len(list_profiles(session))
    per_ms = _median_iteration_ms(session)
    total_iterations = n_profiles * iters
    estimated = None
    if per_ms is not None:
        estimated = round(total_iterations * (per_ms / 1000.0) + n_profiles * _PER_PROFILE_OVERHEAD_S)
    return {
        "profiles": n_profiles,
        "iterations": iters,
        "total_iterations": total_iterations,
        "per_iteration_ms": round(per_ms, 1) if per_ms is not None else None,
        "estimated_seconds": estimated,
    }


def start(iterations: int) -> int:
    """Launch a profile refresh that runs ``iterations`` benchmarks on every stored
    profile. Returns the ``ProfileRefresh`` id.

    Raises ``RuntimeError`` if one is already running, or if there are no stored
    profiles. ``iterations`` is clamped to ``1..MAX_ITERATIONS``. The baseline is
    snapshotted inside the driver (under the lock) so it reflects the true pre-refresh
    state."""
    if active():
        raise RuntimeError("A profile refresh is already running.")
    iters = max(1, min(MAX_ITERATIONS, int(iterations)))
    with session_scope() as session:
        profiles = list_profiles(session)
        if not profiles:
            raise RuntimeError("No stored profiles to refresh.")
        plan = [{**p, "needed": iters} for p in profiles]
        row = ProfileRefresh(status=ProfileRefreshStatus.PENDING, profiles_total=len(plan))
        session.add(row)
        session.flush()
        rid = row.id

    _state.update({"active": True, "id": rid, "cancel": False, "plan": plan})
    thread = threading.Thread(target=_drive, args=(rid,), name="pathbrain-refresh", daemon=True)
    _state["thread"] = thread
    thread.start()
    log.info("Profile refresh %s started: %s profile(s) × %s iteration(s)", rid, len(plan), iters)
    return rid


def _apply_profile(provider, target_settings: list[dict], target_fp: str) -> None:
    """Apply a stored profile and read it back to confirm we reached it."""
    changes, _warnings = plan_apply(target_settings, provider.discover())
    _apply_all(provider, changes)
    reached = fingerprint(normalize(provider.discover()))
    if reached != target_fp:
        raise RuntimeError(f"Could not reach profile (got {reached}, wanted {target_fp}).")


def _drive(refresh_id: int) -> None:
    provider = get_provider()
    plan = _state.get("plan") or []
    final_status = ProfileRefreshStatus.COMPLETE
    err: str | None = None
    baseline: list[dict] = []
    failures: list[str] = []
    try:
        # Hold the coordination lock for the whole session (apply → benchmark → … →
        # restore). Queues behind any in-progress firewall/benchmark session.
        with coordinator.hold(f"refresh#{refresh_id}"):
            baseline = normalize(provider.discover())
            with session_scope() as session:
                row = session.get(ProfileRefresh, refresh_id)
                row.status = ProfileRefreshStatus.RUNNING
                row.started_at = datetime.now(timezone.utc)
                row.baseline = baseline
            iterations_run = 0
            done = 0
            try:
                for item in plan:
                    if _state.get("cancel"):
                        final_status = ProfileRefreshStatus.CANCELLED
                        break
                    fp, settings = item["fingerprint"], item["settings"]
                    label, needed = item["label"], item["needed"]
                    with session_scope() as session:
                        row = session.get(ProfileRefresh, refresh_id)
                        row.current_fingerprint = fp
                        row.current_label = label
                    try:
                        _apply_profile(provider, settings, fp)
                        run_id = create_run(
                            label=f"refresh · {label}",
                            notes=f"Profile refresh #{refresh_id}: {needed} fresh iteration(s) of {fp}",
                            iterations=needed,
                        )
                        execute_run(run_id)  # blocking; its own read-before/after integrity applies
                        iterations_run += needed
                    except Exception as exc:  # noqa: BLE001 — one bad profile shouldn't abort the batch
                        log.exception("Profile refresh %s: profile %s failed", refresh_id, fp)
                        failures.append(f"{label}: {type(exc).__name__}: {exc}")
                    done += 1
                    with session_scope() as session:
                        row = session.get(ProfileRefresh, refresh_id)
                        row.profiles_done = done
                        row.iterations_run = iterations_run
            finally:
                # Always restore the pre-refresh baseline.
                try:
                    restore, _ = plan_apply(baseline, provider.discover())
                    _apply_all(provider, restore)
                    log.info("Profile refresh %s: restored baseline", refresh_id)
                except Exception:  # noqa: BLE001 — never raise out of cleanup
                    log.exception("Profile refresh %s: baseline restore failed", refresh_id)
    except Exception as exc:  # noqa: BLE001 — record + (best-effort) restore, never crash the thread
        log.exception("Profile refresh %s failed", refresh_id)
        final_status = ProfileRefreshStatus.FAILED
        err = f"{type(exc).__name__}: {exc}"
        try:
            if baseline:
                restore, _ = plan_apply(baseline, get_provider().discover())
                _apply_all(get_provider(), restore)
        except Exception:  # noqa: BLE001
            log.exception("Profile refresh %s: restore after failure failed", refresh_id)
    finally:
        if failures and err is None:
            err = f"{len(failures)} profile(s) could not be refreshed: " + "; ".join(failures)
        with session_scope() as session:
            row = session.get(ProfileRefresh, refresh_id)
            if row is not None:
                row.status = final_status
                row.error = err
                row.current_fingerprint = None
                row.current_label = None
                row.finished_at = datetime.now(timezone.utc)
        _state.update({"active": False, "id": None, "cancel": False, "plan": None})
        log.info("Profile refresh %s finished: %s", refresh_id, final_status.value)


def _serialize(row: ProfileRefresh) -> dict:
    return {
        "id": row.id,
        "status": row.status.value if hasattr(row.status, "value") else str(row.status),
        "profiles_total": row.profiles_total or 0,
        "profiles_done": row.profiles_done or 0,
        "iterations_run": row.iterations_run or 0,
        "current_fingerprint": row.current_fingerprint,
        "current_label": row.current_label,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        # Best-effort label of whatever currently holds the lock (for a queued refresh).
        "lock_owner": coordinator.owner(),
    }


def current() -> dict | None:
    """The most recent profile refresh (for status polling), or None."""
    with session_scope() as session:
        row = session.scalars(select(ProfileRefresh).order_by(ProfileRefresh.id.desc())).first()
        return _serialize(row) if row else None


def reconcile_interrupted_refreshes() -> int:
    """Restore the baseline for any refresh left RUNNING by a previous process.

    Called once at startup, like ``challenger.reconcile_interrupted_challenges``. The
    driving thread is gone, so the firewall may be stranded on a refreshed profile —
    set it back to the snapshotted baseline.
    """
    provider = None
    restored = 0
    with session_scope() as session:
        rows = session.scalars(
            select(ProfileRefresh).where(
                ProfileRefresh.status.in_(
                    [ProfileRefreshStatus.RUNNING, ProfileRefreshStatus.PENDING]
                )
            )
        ).all()
        for row in rows:
            baseline = row.baseline or []
            if baseline:
                try:
                    provider = provider or get_provider()
                    changes, _ = plan_apply(baseline, provider.discover())
                    _apply_all(provider, changes)
                except Exception:  # noqa: BLE001
                    log.exception("Profile refresh %s: restore on reconcile failed", row.id)
            row.status = ProfileRefreshStatus.FAILED
            row.error = "Interrupted — service restarted mid-refresh; baseline restored (best-effort)."
            row.finished_at = datetime.now(timezone.utc)
            restored += 1
    if restored:
        log.warning("Reconciled %s interrupted profile refresh(es); baseline restored", restored)
    return restored


__all__ = [
    "start", "active", "cancel", "current", "preview", "list_profiles",
    "reconcile_interrupted_refreshes",
]
