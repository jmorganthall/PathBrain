"""The firewall guard: every write is counted, paced, budgeted, and refusable.

Written after the incident in which the duel ladder's writes — one ``setPipe`` plus a full
shaper reconfigure per differing field, on every leg, retried two seconds after a timeout —
coincided with OPNsense reboots and a WAN that dropped several times a day. Nothing in
PathBrain could have caught it, because the firewall write path was the one part of the
platform with no instrument on it: every metric a run produces is measured, versioned and
audited, and the reconfigure rate was recorded nowhere. This module is that instrument,
plus the invariants that stop a regression on the write path from reaching the network.

Four rules, enforced here rather than remembered per engine (the ``ResilientProvider`` in
``session_runtime`` routes every write through them, and ``get_provider()`` returns nothing
else):

1. **Every write is on a ledger** (``FirewallWrite``): when, which engine held the pipeline,
   which pipe and field, how many reconfigures it cost, how long the firewall took, and
   whether it succeeded, was verified after a timeout, failed, or was refused. "What did
   PathBrain do in the five minutes before the drop?" is one query.
2. **Hands-off is a persistent state** (``FirewallGuardState``): while it is set, every write
   is refused and recorded as refused — a baseline restore included, because a restore is a
   write into a firewall that may be mid-boot, which is exactly what hurt. It is set by an
   outage (any ``FirewallUnavailable``), by the write budget, by a new build (a container
   that comes up on a different ``git_sha`` than the one last armed runs read-only until a
   person arms it: every deploy is a canary hour on the household's own monitoring), or by
   hand. It is cleared only by hand (``arm``).
3. **Writes are paced and budgeted**: a minimum gap between reconfigures (the guard waits
   it out) and a per-hour cap that trips hands-off — a session stops, the network does not.
4. **A write that times out is never reissued.** The wrapper re-reads the firewall and
   checks whether the write took; if it cannot tell, the firewall is treated as gone and
   hands-off trips. Two shaper reloads overlapping inside OPNsense was the new behaviour
   that the retry policy introduced, and this is the rule that removes it.

Read-only diagnostics are best-effort; the refusals are not. A guard that cannot read its
own state refuses to write.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .database import session_scope
from .logging_config import get_logger
from .models import FirewallGuardState, FirewallWrite

log = get_logger("firewall_guard")

#: Defaults for ``config.firewall`` (``config_store`` carries the same values; these are the
#: fallback when the config cannot be read, and are deliberately the cautious side).
DEFAULTS = {
    "min_reconfigure_gap_s": 15.0,
    "max_reconfigures_per_hour": 60,
    "cooldown_after_outage_s": 300.0,
    "arm_required_after_deploy": True,
}
#: The longest the guard will hold a write to honour the minimum gap before giving up on
#: pacing and refusing instead — a session should never sit on the pipeline for minutes
#: because of a mis-set gap.
MAX_GAP_WAIT_S = 120.0
LEDGER_LIMIT = 50

_lock = threading.Lock()


class FirewallHandsOff(RuntimeError):
    """A write was refused: the guard is hands-off, cooling down after an outage, or over
    budget. ``reason`` is the sentence a session card shows."""

    def __init__(self, reason: str, *, kind: str = "hands_off") -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _build_sha() -> str:
    try:
        from .config import get_settings

        return (get_settings().git_sha or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def config() -> dict:
    """The ``firewall`` config section, defaults filled in. Never raises."""
    out = dict(DEFAULTS)
    try:
        from .config_store import get_config

        with session_scope() as s:
            section = (get_config(s) or {}).get("firewall") or {}
        for k in DEFAULTS:
            if k in section and section[k] is not None:
                out[k] = section[k]
    except Exception:  # noqa: BLE001 — defaults are the cautious side
        log.debug("firewall_guard: config unreadable, using defaults", exc_info=True)
    return out


def _state_row(s) -> FirewallGuardState:
    row = s.get(FirewallGuardState, 1)
    if row is None:
        row = FirewallGuardState(id=1, hands_off=False, refused_count=0)
        s.add(row)
        s.flush()
    return row


def _snapshot(row: FirewallGuardState) -> dict:
    return {
        "hands_off": bool(row.hands_off),
        "reason": row.reason,
        "kind": row.kind,
        "tripped_at": _as_utc(row.tripped_at).isoformat() if row.tripped_at else None,
        "tripped_by": row.tripped_by,
        "armed_sha": row.armed_sha,
        "armed_at": _as_utc(row.armed_at).isoformat() if row.armed_at else None,
        "last_contact_at": _as_utc(row.last_contact_at).isoformat() if row.last_contact_at else None,
        "unreachable_since": _as_utc(row.unreachable_since).isoformat() if row.unreachable_since else None,
        "reachable_since": _as_utc(row.reachable_since).isoformat() if row.reachable_since else None,
        "refused_count": int(row.refused_count or 0),
        "last_refusal": row.last_refusal,
    }


def state() -> dict:
    with session_scope() as s:
        return _snapshot(_state_row(s))


def trip(reason: str, *, by: str = "guard", kind: str = "hands_off") -> dict:
    """Set hands-off. A guard already hands-off keeps its first reason (the cause), but a
    new outage still stamps ``unreachable_since`` so the cooldown is measured from it."""
    with _lock, session_scope() as s:
        row = _state_row(s)
        if not row.hands_off:
            row.hands_off = True
            row.reason = reason
            row.kind = kind
            row.tripped_at = _now()
            row.tripped_by = by
            log.error("FIREWALL HANDS-OFF (%s): %s", by, reason)
        else:
            log.warning("firewall_guard: already hands-off (%s); new trigger from %s: %s", row.reason, by, reason)
        return _snapshot(row)


def arm(*, by: str = "user") -> dict:
    """Clear hands-off and stamp the running build as the armed one. The only way back."""
    sha = _build_sha()
    with _lock, session_scope() as s:
        row = _state_row(s)
        row.hands_off = False
        row.reason = None
        row.kind = None
        row.tripped_at = None
        row.tripped_by = None
        row.armed_sha = sha or row.armed_sha
        row.armed_at = _now()
        log.warning("Firewall writes ARMED by %s on build %s", by, (sha or "unknown")[:12])
        return _snapshot(row)


def hands_off(reason: str, *, by: str = "user") -> dict:
    return trip(reason or "set by hand", by=by, kind="manual")


#: Session kinds whose work *is* switching the firewall's profile. A session of one of
#: these kinds started while writes are refused cannot do its job — it can only measure
#: whatever profile the firewall happens to be sitting on, fail its legs and stop — so it
#: is refused at the door rather than allowed to burn the pipeline discovering that. The
#: kinds left out are the ones that still mean something read-only: ``current_test``
#: measures the live profile and never writes, and a manual run is a measurement.
WRITING_KINDS = frozenset({"sweep", "race", "refresh", "baseline_test", "duel", "profile_test",
                           "write_probe"})


def blocked_reason(kind: str | None = None) -> str | None:
    """Why a profile-switching session must not start, or None if it may.

    The guard's enforcement is at the write itself, which is the right place for it: it
    cannot be forgotten and it catches a write from anywhere. But refusing writes one at a
    time is a poor way to stop a *session* whose every leg is a write — it starts, measures
    the profile it was already on, is refused on the first leg that needs a change, and
    aborts three legs later, having spent the pipeline and produced nothing. So the kinds
    in :data:`WRITING_KINDS` ask this first and decline in a sentence naming the remedy.
    """
    if kind is not None and kind not in WRITING_KINDS:
        return None
    st = state()
    if not st.get("hands_off"):
        return None
    why = st.get("reason") or "writes are refused"
    return (
        f"The firewall guard is hands-off — {why} A session that switches profiles cannot "
        "run until writes are armed: it would measure whichever profile the firewall is "
        "already on and fail every leg that needs a change. Arm writes from the top bar "
        "once the network is known good."
    )


def startup_check() -> dict:
    """A new build never writes the firewall until a person arms it. Compares the running
    ``git_sha`` with the one last armed; a dev build with no sha is left alone (there is no
    identity to compare, and a test run must not start hands-off)."""
    cfg = config()
    sha = _build_sha()
    with session_scope() as s:
        row = _state_row(s)
        armed = row.armed_sha or ""
        already = bool(row.hands_off)
    if not cfg.get("arm_required_after_deploy", True) or not sha:
        return state()
    if sha != armed and not already:
        return trip(
            f"new build {sha[:7]} — firewall writes stay off until you arm them from the top bar "
            f"(last armed build: {armed[:7] or 'never'}). Measurements run; nothing is applied or restored.",
            by="deploy", kind="deploy",
        )
    return state()


#: A successful contact is stamped to the database at most this often; the guard reads the
#: firewall several times per leg and a row write per read would be pure amplification.
#: An outage, and the first success after one, are always written.
CONTACT_STAMP_S = 60.0
_last_ok_stamp = 0.0
_outage_pending = False


def note_contact(ok: bool, error: str | None = None) -> None:
    """Record that the firewall answered (or did not). An outage trips hands-off; the
    return marks ``reachable_since`` so the cooldown can be measured."""
    global _last_ok_stamp, _outage_pending
    if ok:
        mono = time.monotonic()
        with _lock:
            if not _outage_pending and mono - _last_ok_stamp < CONTACT_STAMP_S:
                return
            _last_ok_stamp = mono
            pending = _outage_pending
            _outage_pending = False
        with _lock, session_scope() as s:
            row = _state_row(s)
            now = _now()
            row.last_contact_at = now
            if row.unreachable_since is not None or pending:
                row.reachable_since = now
                row.unreachable_since = None
                log.warning("firewall_guard: firewall reachable again; writes cool down for %ss",
                            config().get("cooldown_after_outage_s"))
        return
    with _lock:
        _outage_pending = True
    with _lock, session_scope() as s:
        row = _state_row(s)
        if row.unreachable_since is None:
            row.unreachable_since = _now()
    trip(f"the firewall stopped answering: {error or 'unreachable'}", by="outage", kind="outage")


def _owner() -> str | None:
    try:
        from . import coordinator

        return coordinator.status().get("owner")
    except Exception:  # noqa: BLE001
        return None


def record(op: str, *, changes: list[dict] | None, reconfigures: int, outcome: str,
           error: str | None = None, latency_ms: float | None = None) -> None:
    """One ledger row. Never raises — the ledger must not be why a write fails."""
    try:
        first = (changes or [{}])[0] if changes else {}
        fields = sorted({str(c.get("param") or c.get("field") or "") for c in (changes or []) if c})
        with session_scope() as s:
            s.add(FirewallWrite(
                op=op,
                owner=_owner(),
                pipe_uuid=str(first.get("pipe_uuid") or "")[:64] or None,
                field=",".join(f for f in fields if f)[:120] or None,
                value=str(first.get("value"))[:64] if first and "value" in first else None,
                changes=changes,
                reconfigures=int(reconfigures),
                outcome=outcome,
                error=(error or None),
                latency_ms=latency_ms,
                git_sha=(_build_sha() or None),
            ))
    except Exception:  # noqa: BLE001
        log.debug("firewall_guard: ledger write failed", exc_info=True)


def reconfigures_since(since: datetime) -> int:
    with session_scope() as s:
        n = s.execute(
            select(func.coalesce(func.sum(FirewallWrite.reconfigures), 0)).where(
                FirewallWrite.at >= since, FirewallWrite.outcome.in_(("ok", "verified"))
            )
        ).scalar_one()
        return int(n or 0)


def last_reconfigure_at() -> datetime | None:
    with session_scope() as s:
        at = s.execute(
            select(func.max(FirewallWrite.at)).where(
                FirewallWrite.reconfigures > 0, FirewallWrite.outcome.in_(("ok", "verified"))
            )
        ).scalar_one()
        return _as_utc(at)


def _refuse(op: str, changes: list[dict] | None, reconfigures: int, reason: str, kind: str) -> None:
    with _lock, session_scope() as s:
        row = _state_row(s)
        row.refused_count = int(row.refused_count or 0) + 1
        row.last_refusal = f"{op}: {reason}"
    record(op, changes=changes, reconfigures=reconfigures, outcome="refused", error=reason)
    log.warning("firewall_guard: REFUSED %s (%s): %s", op, kind, reason)
    raise FirewallHandsOff(reason, kind=kind)


def before_write(op: str, changes: list[dict] | None = None, *, reconfigures: int = 1,
                 sleep=time.sleep) -> None:
    """The gate every write passes. Raises :class:`FirewallHandsOff` (recorded as a refusal)
    when hands-off is set, the post-outage cooldown has not elapsed, or the write would
    exceed the hourly budget (which also trips hands-off). Waits out the minimum gap."""
    cfg = config()
    st = state()
    if st["hands_off"]:
        _refuse(op, changes, reconfigures, f"hands-off: {st['reason']}", st.get("kind") or "hands_off")
    cooldown = float(cfg.get("cooldown_after_outage_s") or 0)
    if st["reachable_since"] and cooldown > 0:
        since = datetime.fromisoformat(st["reachable_since"])
        left = cooldown - (_now() - since).total_seconds()
        if left > 0:
            _refuse(op, changes, reconfigures,
                    f"the firewall came back {int(cooldown - left)}s ago; writes resume after a "
                    f"{int(cooldown)}s cooldown ({int(left)}s left)", "cooldown")
    cap = int(cfg.get("max_reconfigures_per_hour") or 0)
    if cap > 0 and reconfigures > 0:
        used = reconfigures_since(_now() - timedelta(hours=1))
        if used + reconfigures > cap:
            reason = (f"write budget exceeded: {used} reconfigures in the last hour, the cap is {cap} "
                      f"(firewall.max_reconfigures_per_hour). Every engine is stopped until you arm again.")
            trip(reason, by="budget", kind="budget")
            _refuse(op, changes, reconfigures, reason, "budget")
    gap = float(cfg.get("min_reconfigure_gap_s") or 0)
    if gap > 0 and reconfigures > 0:
        last = last_reconfigure_at()
        if last is not None:
            wait = gap - (_now() - last).total_seconds()
            if wait > 0:
                if wait > MAX_GAP_WAIT_S:
                    _refuse(op, changes, reconfigures,
                            f"minimum gap between reconfigures is {gap:.0f}s and the next slot is {wait:.0f}s away", "gap")
                log.info("firewall_guard: pacing %s — waiting %.1fs for the %.0fs reconfigure gap", op, wait, gap)
                sleep(wait)


def recent_writes(limit: int = LEDGER_LIMIT) -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(select(FirewallWrite).order_by(FirewallWrite.id.desc()).limit(limit)).all()
        return [{
            "id": r.id,
            "at": _as_utc(r.at).isoformat() if r.at else None,
            "op": r.op,
            "owner": r.owner,
            "pipe_uuid": r.pipe_uuid,
            "field": r.field,
            "value": r.value,
            "changes": r.changes,
            "reconfigures": r.reconfigures,
            "outcome": r.outcome,
            "error": r.error,
            "latency_ms": r.latency_ms,
            "git_sha": r.git_sha,
        } for r in rows]


def summary() -> dict:
    """The guard on one read: state, budget and the last hour — what the pipeline health
    endpoint and the top-bar chip render."""
    cfg = config()
    now = _now()
    hour = reconfigures_since(now - timedelta(hours=1))
    day = reconfigures_since(now - timedelta(hours=24))
    last = last_reconfigure_at()
    with session_scope() as s:
        refused_hour = int(s.execute(
            select(func.count(FirewallWrite.id)).where(
                FirewallWrite.at >= now - timedelta(hours=1), FirewallWrite.outcome == "refused"
            )
        ).scalar_one() or 0)
    return {
        **state(),
        "build_sha": _build_sha() or None,
        "config": cfg,
        "reconfigures_last_hour": hour,
        "reconfigures_last_24h": day,
        "refused_last_hour": refused_hour,
        "last_reconfigure_at": last.isoformat() if last else None,
    }


__all__ = [
    "DEFAULTS", "FirewallHandsOff", "WRITING_KINDS", "arm", "before_write", "blocked_reason",
    "config", "hands_off", "note_contact", "recent_writes", "record", "startup_check", "state",
    "summary", "trip",
]
