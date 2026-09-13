"""The firewall write ledger — and the one refusal that survives it.

Every write PathBrain makes is recorded here, and exactly one class of write is refused:
a change naming a shaper field the registry does not mark writable. That is the whole
module now, and the shrinking is the point.

**What this used to be, and why it is gone.** After an incident in which OPNsense rebooted
and the WAN dropped several times a day while the duel ladder was running, this module grew
a safety valve: a minimum gap between reconfigures, an hourly reconfigure budget, a
post-outage cooldown, a gate that made every new build start read-only until a person armed
it, and a persistent *hands-off* state that any of those could trip and only a human could
clear. It was built on the theory that PathBrain's **rate** of writing was the hazard.

That theory was wrong, and the ledger — the one piece of this module that was actually an
instrument — is what disproved it. Twenty rows, no exceptions: **every** write carrying
``flows`` timed out the 30 s call and took the box off the network for 30-35 s, and **every**
write in the same hours that did not carry it was clean and sub-second (a bare shaper reload
420/449/492 ms, ``quantum`` + reload 521 ms, ``limit`` + reload 498 ms). The hazard was never
how often PathBrain wrote. It was one field, in a handful of writes, rebuilding the dummynet
flow table while the link ran through it. ``flows`` is non-writable now (``shaper_fields``),
which removes the cause.

So the valve was a rate limit on the wrong variable, and it was not free. It refused writes
that were never going to hurt; it stopped whole sessions at the door; a deploy left the
household's own monitoring unable to apply anything until somebody noticed a chip in the top
bar; and every one of those refusals cost a night of measurement to prevent an outage that
the offending field, not the write count, was causing. **PathBrain is ready to write, as it
was before.** What was needed was diligence about *what* gets injected into a production
firewall — not a throttle on how often.

Three rules remain, and each earned its place in that incident rather than being assumed:

1. **Every write is on a ledger** (``FirewallWrite``): when, which engine held the pipeline,
   which pipe and field, how many reconfigures it cost, how long the firewall took, and
   whether it succeeded, was verified after a timeout, failed, or was refused. "What did
   PathBrain do in the five minutes before the drop?" is one query — and it is the query
   that found the real cause. It writes nothing and stops nothing.
2. **A field the registry does not mark writable is never written.** ``shaper_fields`` is
   where that decision lives and ``plan_apply`` already honours it, so no engine can plan
   such a change — but a hand-built change list, a route, or a spec queued before the
   registry changed can still reach the provider, and the field that made this rule
   necessary costs an outage, not a wasted call. So the last thing between a change list and
   the firewall checks it too. This is a rule about **what**, and it is the one the evidence
   supports. The pipe on/off toggle (``param: "enabled"``) is not a shaper field at all and
   is unaffected; it is its own documented write path.
3. **A write that times out is never reissued** (enforced in ``session_runtime``, beside the
   call it governs). The wrapper re-reads the firewall and either finds the write took
   (``verified``) or reports the firewall gone. Two shaper reloads overlapping inside
   OPNsense was a real new behaviour that a retry policy introduced, and unlike the budget it
   is a claim about correctness, not about frequency.

Read-only diagnostics are best-effort; the refusal is not.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from .database import session_scope
from .logging_config import get_logger
from .models import FirewallWrite
from .shaper_fields import FIELD_LABELS, WRITABLE_FIELDS, field as shaper_field

log = get_logger("firewall_guard")

LEDGER_LIMIT = 50

_lock = threading.Lock()


class FirewallWriteRefused(RuntimeError):
    """A write was refused because it named a field PathBrain never writes.

    The only refusal left. It is **permanent**: there is no state to clear and no button
    that makes it go away, which is why ``describe_failure`` says so plainly rather than
    pointing at a remedy. (This was ``FirewallHandsOff`` while the guard also refused for
    reasons that *did* lift — a budget, a cooldown, an un-armed build. Those are gone, and a
    name that implied they might come back would be the wrong name.)
    """

    def __init__(self, reason: str, *, kind: str = "protected_field") -> None:
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


def _owner() -> str | None:
    try:
        from . import coordinator

        return coordinator.status().get("owner")
    except Exception:  # noqa: BLE001
        return None


def record(op: str, *, changes: list[dict] | None, reconfigures: int, outcome: str,
           error: str | None = None, latency_ms: float | None = None) -> int | None:
    """One ledger row; returns its id (None if the ledger could not be written).

    The id is what lets ``link_watch`` come back a few seconds later and attach what this
    write cost the household. Never raises — the ledger must not be why a write fails.
    """
    try:
        first = (changes or [{}])[0] if changes else {}
        fields = sorted({str(c.get("param") or c.get("field") or "") for c in (changes or []) if c})
        with session_scope() as s:
            row = FirewallWrite(
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
            )
            s.add(row)
            s.flush()
            return int(row.id)
    except Exception:  # noqa: BLE001
        log.debug("firewall_guard: ledger write failed", exc_info=True)
        return None


def reconfigures_since(since: datetime) -> int:
    """How many reconfigures landed since ``since`` — a **reading**, not a limit.

    Nothing consults this to decide whether a write may happen; the health endpoint and the
    Firewall page render it so a person can see the rate PathBrain is actually writing at.
    """
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


def protected_params(changes: list[dict] | None) -> list[str]:
    """The params in ``changes`` that are shaper fields the registry does not mark writable.

    A param the registry has never heard of is *not* protected: the pipe on/off toggle
    writes ``param: "enabled"``, which is a separate, documented write path and not a shaper
    parameter at all. Only a field PathBrain declares — and declares unwritable — is refused,
    so the registry stays the one place the decision is made.
    """
    seen: list[str] = []
    for ch in changes or []:
        key = str((ch or {}).get("param") or "")
        if key and key not in WRITABLE_FIELDS and shaper_field(key) is not None and key not in seen:
            seen.append(key)
    return seen


def before_write(op: str, changes: list[dict] | None = None, *, reconfigures: int = 1) -> None:
    """The gate every write passes. One rule: nothing may name a non-writable shaper field.

    Raises :class:`FirewallWriteRefused` (recorded on the ledger as a refusal) and otherwise
    returns immediately — there is no pacing, no budget and no state to consult, so this
    costs a dictionary scan and never sleeps. A write that is allowed is allowed *now*.
    """
    protected = protected_params(changes)
    if not protected:
        return
    names = ", ".join(FIELD_LABELS.get(k, k) for k in protected)
    reason = (
        f"{names} is captured but never written — PathBrain does not change it "
        f"({'/'.join(protected)} is not a writable shaper field). Nothing was applied."
    )
    with _lock:
        record(op, changes=changes, reconfigures=reconfigures, outcome="refused", error=reason)
    log.warning("firewall_guard: REFUSED %s (protected_field): %s", op, reason)
    raise FirewallWriteRefused(reason)


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
            "gap_ms": r.gap_ms,
            "box_gap_ms": r.box_gap_ms,
            "through_gap_ms": r.through_gap_ms,
            "watch": r.watch,
            "git_sha": r.git_sha,
        } for r in rows]


def summary() -> dict:
    """The write path on one read: the rate, and what the last hour looked like.

    Every field here is descriptive. There is no state, no cap and nothing to arm — the
    health endpoint and the Firewall page render this so the write rate is *visible*, which
    is what was missing when it mattered, rather than *limited*, which is what did not help.
    """
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
        "build_sha": _build_sha() or None,
        "reconfigures_last_hour": hour,
        "reconfigures_last_24h": day,
        "refused_last_hour": refused_hour,
        "last_reconfigure_at": last.isoformat() if last else None,
    }


__all__ = [
    "FirewallWriteRefused", "before_write", "last_reconfigure_at", "protected_params",
    "reconfigures_since", "recent_writes", "record", "summary",
]
