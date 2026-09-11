"""The universal "add a job" queue: one way for every user-triggered session to start.

PathBrain runs **one** firewall/benchmark session at a time — that exclusion is what makes
a measurement mean anything, and ``coordinator`` enforces it. What was never universal was
what happens when you press a button while the pipeline is busy, and there were three
different answers depending only on which button you pressed:

* **A refusal.** Six engines guarded their own ``start()`` with an "already running" check
  that became an HTTP 409, so the button dead-ended. The check protected nothing the
  coordinator wasn't already protecting — it just fired earlier, and hardest exactly when
  queueing is what a person wants.
* **A silent queue.** Start a *different* kind of session and it worked: the engine's thread
  simply blocked on ``coordinator.hold`` until its turn. Correct, and invisible — the toast
  said the session had begun, nothing happened for hours, and that reads identically to a
  broken button.
* **An immediate start**, when the pipeline happened to be free.

Three behaviours for one intent is not a policy, it is an accident of which module a button
landed in. This module makes it one: **submitting a job always succeeds**, and the caller is
always told which of the two things happened — it started, or it is Nth in line behind
something named.

The design is deliberately small. It does *not* re-implement each engine's persistence,
because their rows already exist and differ for good reasons (a sweep is a grid, a duel is a
window, a refresh is a list of profiles). It sits in *front* of them: a submitted job either
starts right now — calling the engine's own ``start()`` synchronously, so the caller still
gets the real session id back exactly as before — or is held as a **ticket** until the
pipeline is free, at which point a single dispatcher calls that same ``start()``.

Two consequences worth stating rather than discovering:

* The queue **survives a restart**. It began in memory, on the argument that a queued job
  has by definition applied nothing and so has nothing to resume — true, and beside the
  point. The person who queued twelve bets for the night pressed the button once, and a
  container recreate (Watchtower pulling the image a merged PR just published, an OOM kill,
  a compose restart) emptied the line with nothing on screen to say so: reported as
  *"cancelled a duel job and all my queued jobs were cancelled too — even the non-duel
  ones"*, because the restart happened to follow the cancel, and every queueing layer
  dropped its work at once (tickets gone, pending profile tests marked cancelled, pending
  manual runs marked failed). So a ticket is now written to ``queued_jobs`` with a JSON
  ``spec`` that its kind's registered **starter** can rebuild the call from, and
  :func:`restore` re-queues the pending rows at startup — inside ``RESUME_WINDOW_HOURS``,
  because a queue from last week is not a queue, it is a surprise. The same window governs
  ``profile_test``'s pending rows and pending manual runs, so all three layers answer a
  restart the same way. Resuming happens **after** every engine's reconcile has restored
  the firewall, never before: a resumed job snapshots its baseline when it starts, and that
  baseline must be the real one.
* A queued job's **validation moves with it**. "No contenders to race" or "no stored
  profiles to re-run" used to be an error the HTTP caller saw immediately; for a queued job
  it can only be discovered when its turn comes. So the failure is recorded on the ticket and
  surfaced in the jobs feed rather than being lost.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import coordinator, firewall_guard
from .logging_config import get_logger

log = get_logger("job_queue")

#: How often the dispatcher re-checks whether the pipeline has freed up. The coordinator
#: offers no notification to wait on, and a session runs for minutes at the very least, so
#: polling is both the simple answer and an accurate one.
POLL_S = 1.0

#: A started job gets this long to actually take the pipeline before the dispatcher moves
#: on. Engines start a thread and return, so ``active()`` is true within milliseconds — this
#: only covers the handoff, and must stay short: it is time the *next* queued job spends
#: waiting on a job that has, on this evidence, not started at all.
START_GRACE_S = 5.0

#: How long a queued job stays worth resuming after a restart. A ticket younger than this
#: is re-queued exactly as submitted; an older one is marked ``expired`` with the reason,
#: because silently starting last week's session on a firewall nobody is watching is the
#: surprise the in-memory design was originally avoiding. One number for every layer —
#: tickets, pending profile tests, pending manual runs — so a restart treats them alike.
RESUME_WINDOW_HOURS = 24.0
#: Settled rows (started/failed/cancelled/expired) older than this are pruned on restore.
PRUNE_AFTER_DAYS = 7


@dataclass
class Ticket:
    """One submitted job: what it is, and what it is waiting for."""

    id: int
    kind: str
    label: str
    submitted_at: datetime
    # The engine's own ``start()``, already bound to its arguments. Held in memory rather
    # than serialized: the queue does not outlive the process, so there is nothing to
    # rehydrate, and a closure keeps every engine's signature its own business.
    start: Callable[[], Any] = field(repr=False)
    state: str = "pending"          # pending → started | failed | cancelled
    result: Any = None
    error: str | None = None
    started_at: datetime | None = None
    # The JSON-serializable arguments the kind's starter rebuilds the call from; a ticket
    # with a spec is written to ``queued_jobs`` (``row_id``) and survives a restart.
    spec: dict | None = None
    row_id: int | None = None
    resumed: bool = False


@dataclass
class Submission:
    """What happened to a submit: it ran, or it is waiting."""

    started: bool
    result: Any = None
    ticket: Ticket | None = None
    queue_position: int | None = None
    queue_ahead: int = 0
    blocked_by: str | None = None

    def placement(self) -> dict:
        """The uniform queue-placement block every start endpoint returns.

        One shape for every button, so a client never has to know which engine it pressed
        to find out whether anything is going to happen.
        """
        return {
            "queued": not self.started,
            "ticket_id": self.ticket.id if self.ticket else None,
            "queue_position": self.queue_position,
            "queue_ahead": self.queue_ahead,
            "blocked_by": self.blocked_by,
        }


_lock = threading.RLock()
_queue: list[Ticket] = []
_seq = 0
_dispatcher: threading.Thread | None = None
#: ``kind -> active()``. Registered by the API layer so this module never imports the
#: engines (several of them import scoring, which imports the API — a cycle).
_engines: dict[str, Callable[[], bool]] = {}
#: ``kind -> starter(spec)``: the one definition of how a kind's session is started from a
#: serializable spec. A route submits a spec rather than a closure, so the job that runs
#: now and the job that runs after a restart are the same call.
_starters: dict[str, Callable[[dict], Any]] = {}
#: What the last :func:`restore` did, for ``status()`` — so a feed can say the queue came
#: back after a restart rather than leaving the reader to wonder why it is not empty.
_restored: dict | None = None
#: Extra "what is waiting" sources. Two engines queue *themselves* rather than through a
#: ticket, because their caller needs a real row id back synchronously — a profile test's
#: fingerprint is recorded in the recommendation ledger before it runs, and a manual run's
#: id is what the dashboard polls. They still belong in one list of what is waiting, so
#: they contribute their own pending rows here.
_pending_sources: list[Callable[[], list[dict]]] = []
#: Recently finished tickets, kept only so a job that failed its validation an hour after
#: being submitted still has somewhere to say so.
_recent: list[Ticket] = []
_RECENT_MAX = 20


def register(kind: str, is_active: Callable[[], bool]) -> None:
    """Teach the queue how to tell whether a ``kind`` of session is still running."""
    with _lock:
        _engines[kind] = is_active


def register_starter(kind: str, start: Callable[[dict], Any]) -> None:
    """Teach the queue how to start a ``kind`` of session from its serializable spec."""
    with _lock:
        _starters[kind] = start


def _starter_for(kind: str) -> Callable[[dict], Any] | None:
    with _lock:
        fn = _starters.get(kind)
    if fn is None and not _engines:
        # Nothing registered yet (a request before the lifespan ran, or a test client that
        # never runs it): the registration is idempotent and imports lazily, so do it now.
        register_engines()
        with _lock:
            fn = _starters.get(kind)
    return fn


def register_pending_source(source: Callable[[], list[dict]]) -> None:
    """Add a self-queueing engine's waiting work to the one "what is waiting" list."""
    with _lock:
        _pending_sources.append(source)


def register_engines() -> None:
    """Register every engine that owns the pipeline. Called once at startup.

    Done here rather than at each engine's import so this module imports none of them:
    several pull in scoring, which pulls in the API layer, which imports this — a cycle.
    Manual runs are deliberately absent: a run has no module-level singleton (every run is
    its own row) and its "am I active?" question is answered by the coordinator itself.
    """
    from . import baseline_test, challenger, current_test, duel, profile_test, refresh, sweep

    register("sweep", sweep.active)
    register("race", challenger.active)
    register("refresh", refresh.active)
    register("profile_test", profile_test.active)
    register("current_test", current_test.active)
    register("baseline_test", baseline_test.active)
    register("duel", duel.active)

    # How each kind starts from its spec — the single definition, used for a job that starts
    # now and for one rebuilt from a row after a restart. Looked up at call time, so a test
    # that patches an engine's ``start`` is honoured.
    register_starter(
        "sweep",
        lambda s: sweep.start(
            s.get("spec") or {}, int(s["iterations"]), float(s.get("dwell_s") or 0.0),
            bool(s.get("dry_run", False)), s.get("pipe_uuid") or None,
        ),
    )
    register_starter(
        "race", lambda s: challenger.start(int(s["time_budget_s"]), bool(s.get("auto_promote", False)))
    )
    register_starter(
        "refresh",
        lambda s: refresh.start(
            int(s["iterations"]), top=s.get("top"), rank_by=s.get("rank_by"), fingerprints=s.get("fingerprints"),
        ),
    )
    register_starter("current_test", lambda s: current_test.start(float(s["minutes"])))
    register_starter(
        "baseline_test",
        lambda s: baseline_test.start(
            int(s["iterations"]), int(s.get("settle_seconds") or 0), trigger=s.get("trigger") or "manual"
        ),
    )
    register_starter(
        "duel", lambda s: duel.start(s.get("duration_minutes"), trigger=s.get("trigger") or "manual",
                                     contenders=s.get("contenders"),
                                     campaign_id=s.get("campaign_id"),
                                     base_fingerprint=s.get("base_fingerprint"))
    )

    # The two self-queueing engines: they own row-level identity their callers need back
    # synchronously, so they queue themselves — but they still report into the one list.
    register_pending_source(_profile_test_pending)
    register_pending_source(_run_pending)


def _profile_test_pending() -> list[dict]:
    from . import profile_test

    return [
        {
            "ticket_id": None,
            "kind": "profile_test",
            "id": t["id"],
            "label": t.get("label") or t.get("fingerprint"),
            "queue_position": t.get("queue_position"),
            "submitted_at": t.get("created_at"),
            "state": "pending",
        }
        for t in profile_test.active_tests()
        if t.get("status") == "pending"
    ]


def _run_pending() -> list[dict]:
    """Manual runs that exist but have not started measuring — they are holding for the lock."""
    from sqlalchemy import select as _select

    from .database import session_scope
    from .models import Run, RunStatus

    with session_scope() as session:
        rows = session.scalars(
            _select(Run).where(Run.status == RunStatus.PENDING).order_by(Run.id)
        ).all()
        return [
            {
                "ticket_id": None,
                "kind": "run",
                "id": r.id,
                "label": r.label or f"run #{r.id}",
                "queue_position": None,
                "submitted_at": r.created_at.isoformat() if r.created_at else None,
                "state": "pending",
            }
            for r in rows
        ]


def kind_active(kind: str) -> bool:
    """Is a session of exactly this kind already running?

    Deliberately **per kind**, not "is any engine active". Cross-kind exclusion is the
    coordinator's job and it does it properly, with a lease and stale-holder eviction; a
    module-level ``active()`` flag has neither. Gating the whole queue on "no engine
    anywhere reports itself active" would mean one engine that died with its flag set stalls
    every job forever, with nothing to evict it — strictly worse than the refusals this
    replaces. So this answers only the question an engine's flag can actually answer:
    would starting *this* kind collide with itself.
    """
    with _lock:
        is_active = _engines.get(kind)
    if is_active is None:
        return False
    try:
        return bool(is_active())
    except Exception:  # noqa: BLE001 — a broken probe must not wedge the queue
        log.debug("job_queue: %s active() raised", kind, exc_info=True)
        return False


def can_start_now(kind: str | None = None) -> bool:
    """Would a job submitted right now begin immediately?"""
    with _lock:
        if _queue:
            return False
    if coordinator.busy():
        return False
    return not (kind and kind_active(kind))


def blocked_by() -> str | None:
    """What is in the way, in words — ``None`` when nothing is."""
    return coordinator.describe(coordinator.owner())


def submit(
    kind: str, label: str, start: Callable[[], Any] | None = None, *, spec: dict | None = None
) -> Submission:
    """Start a job, or queue it. Never refuses because something else is running.

    When the pipeline is free the engine's own ``start()`` is called **synchronously**, so
    the caller gets the real session id back and every existing response shape is preserved;
    exceptions it raises propagate exactly as they always did. Otherwise the job becomes a
    ticket and the dispatcher starts it when its turn comes.

    Pass a ``spec`` — the JSON-serializable arguments — and the kind's registered starter
    makes the call; a queued ticket with a spec is written to disk and comes back after a
    restart (see :func:`restore`). A bare ``start`` closure is still accepted (tests, ad hoc
    callers) and queues in memory only.
    """
    global _seq
    # A session whose work is switching profiles cannot run while the guard refuses writes.
    # This is a genuinely bad request — the job would apply nothing — not "the pipeline is
    # busy", so it is the one refusal this contract allows (see ``firewall_guard.blocked_reason``).
    blocked = firewall_guard.blocked_reason(kind)
    if blocked:
        log.warning("Job %s (%s) refused: %s", kind, label, blocked)
        raise ValueError(blocked)
    if start is None:
        starter = _starter_for(kind)
        if starter is None:
            raise ValueError(f"No starter registered for '{kind}' jobs")
        args = dict(spec or {})
        start = lambda: starter(args)  # noqa: E731 — bound once, the one call for now or later
    with _lock:
        if not _queue and not coordinator.busy() and not kind_active(kind):
            log.info("Job %s (%s) starting immediately", kind, label)
            return Submission(started=True, result=start())

        _seq += 1
        ticket = Ticket(
            id=_seq,
            kind=kind,
            label=label,
            submitted_at=datetime.now(timezone.utc),
            start=start,
            spec=spec,
        )
        if spec is not None:
            _persist(ticket)  # under the lock, so a cancel can never race the row into being
        _queue.append(ticket)
        position = len(_queue)
        _ensure_dispatcher()

    holder = blocked_by()
    log.info(
        "Job %s (%s) queued at position %s%s",
        kind, label, position, f" behind {holder}" if holder else "",
    )
    return Submission(
        started=False,
        ticket=ticket,
        queue_position=position,
        # Everything it waits on: the session holding the pipeline plus the tickets ahead.
        queue_ahead=(position - 1) + (1 if holder else 0),
        blocked_by=holder,
    )


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _persist(ticket: Ticket) -> None:
    """Write a queued ticket to ``queued_jobs``. Best-effort: the disk copy is what survives
    a restart, and failing to write it must never turn a queue into a refusal."""
    from .database import session_scope
    from .models import QueuedJob

    try:
        with session_scope() as session:
            row = QueuedJob(
                kind=ticket.kind, label=ticket.label, spec=ticket.spec,
                submitted_at=ticket.submitted_at, state="pending", resumed=ticket.resumed,
            )
            session.add(row)
            session.flush()
            ticket.row_id = row.id
    except Exception:  # noqa: BLE001
        log.warning("job_queue: could not persist ticket %s (%s)", ticket.kind, ticket.label, exc_info=True)


def _settle(ticket: Ticket) -> None:
    """Record a ticket's final state on its row, so a restart never resurrects it."""
    if ticket.row_id is None:
        return
    from .database import session_scope
    from .models import QueuedJob

    try:
        with session_scope() as session:
            row = session.get(QueuedJob, ticket.row_id)
            if row is None:
                return
            row.state = ticket.state
            row.error = ticket.error
            row.started_at = ticket.started_at
            row.finished_at = datetime.now(timezone.utc)
    except Exception:  # noqa: BLE001
        log.debug("job_queue: could not settle ticket row %s", ticket.row_id, exc_info=True)


def restore(*, now: datetime | None = None) -> dict:
    """Re-queue the tickets a previous process left pending. Called once at startup, and
    only after every engine's reconcile has restored the firewall.

    A pending row younger than ``RESUME_WINDOW_HOURS`` with a registered starter comes back
    as a ticket exactly as submitted (marked ``resumed`` so the feed can say so), ahead of
    anything submitted since — it was asked for first. Anything older, or of a kind nothing
    can start, is marked ``expired`` with the reason rather than deleted: the row is the
    only record that the button was ever pressed. Settled rows past ``PRUNE_AFTER_DAYS``
    are dropped. Returns ``{tickets, expired, at}``; also kept for ``status()``.
    """
    global _restored, _seq
    from sqlalchemy import delete as _delete, select as _select

    from .database import session_scope
    from .models import QueuedJob

    now = now or datetime.now(timezone.utc)
    tickets: list[Ticket] = []
    expired = 0
    with _lock:
        # Rows this process already holds as live tickets are not orphans. Startup never has
        # any, but this keeps a second restore from duplicating the queue it just built.
        live_rows = {t.row_id for t in _queue if t.row_id is not None}
    with session_scope() as session:
        rows = session.scalars(
            _select(QueuedJob).where(QueuedJob.state == "pending").order_by(QueuedJob.id)
        ).all()
        for row in rows:
            if row.id in live_rows:
                continue
            submitted = _as_utc(row.submitted_at) or now
            age_h = (now - submitted).total_seconds() / 3600.0
            starter = _starter_for(row.kind)
            if age_h > RESUME_WINDOW_HOURS:
                row.state = "expired"
                row.error = (
                    f"Not resumed — queued {age_h:.0f} h ago, before a restart; older than the "
                    f"{RESUME_WINDOW_HOURS:g} h resume window."
                )
                row.finished_at = now
                expired += 1
                continue
            if starter is None:
                row.state = "expired"
                row.error = f"Not resumed — nothing registered can start '{row.kind}' jobs."
                row.finished_at = now
                expired += 1
                continue
            row.resumed = True
            args = dict(row.spec or {})
            with _lock:
                _seq += 1
                seq = _seq
            tickets.append(
                Ticket(
                    id=seq, kind=row.kind, label=row.label, submitted_at=submitted,
                    start=lambda st=starter, a=args: st(a), spec=row.spec, row_id=row.id, resumed=True,
                )
            )
        cutoff = now - timedelta(days=PRUNE_AFTER_DAYS)
        session.execute(
            _delete(QueuedJob).where(QueuedJob.state != "pending", QueuedJob.finished_at < cutoff.replace(tzinfo=None)),
            # No in-session evaluation: the rows settled above carry aware timestamps and the
            # stored column is naive UTC; the database compares them correctly, the evaluator
            # cannot.
            execution_options={"synchronize_session": False},
        )
    with _lock:
        _queue[0:0] = tickets  # ahead of anything submitted since this process started
        if _queue:
            _ensure_dispatcher()
    _restored = {"at": now.isoformat(), "tickets": len(tickets), "expired": expired}
    if tickets or expired:
        log.warning(
            "Job queue restored after a restart: %s ticket(s) re-queued, %s expired",
            len(tickets), expired,
        )
    return dict(_restored)


def note_restored(**counts: int) -> None:
    """Add what the other queueing layers resumed (profile tests, manual runs) to the
    restore record, so ``status()`` tells the whole story in one place."""
    global _restored
    base = dict(_restored or {"at": datetime.now(timezone.utc).isoformat(), "tickets": 0, "expired": 0})
    base.update({k: int(v) for k, v in counts.items()})
    _restored = base


def _ensure_dispatcher() -> None:
    """Make sure something is draining the queue. Caller holds ``_lock``."""
    global _dispatcher
    if _dispatcher is not None and _dispatcher.is_alive():
        return
    _dispatcher = threading.Thread(target=_drain, name="pathbrain-job-queue", daemon=True)
    _dispatcher.start()


def _drain() -> None:
    """Start queued jobs one at a time, oldest first, as the pipeline frees up.

    Claiming the next ticket and deciding to retire both happen under ``_lock``, so a
    ticket submitted by a request is either seen by this dispatcher or arrives after it has
    cleared itself — in which case that request starts a new one. There is no third case,
    which is what stops the queue stranding with nobody draining it.
    """
    global _dispatcher
    while True:
        with _lock:
            if not _queue:
                _dispatcher = None
                return
            head = _queue[0]
            ready = not coordinator.busy() and not kind_active(head.kind)
            ticket = head if ready else None
            if ticket is not None:
                _queue.pop(0)
        if ticket is None:
            time.sleep(POLL_S)
            continue

        ticket.started_at = datetime.now(timezone.utc)
        # The guard may have tripped while this ticket waited — a queued job's validation
        # runs at its turn, and "can the firewall be written?" is part of it.
        blocked = firewall_guard.blocked_reason(ticket.kind)
        try:
            if blocked:
                raise ValueError(blocked)
            ticket.result = ticket.start()
            ticket.state = "started"
            log.info("Queued job %s (%s) started", ticket.kind, ticket.label)
        except Exception as exc:  # noqa: BLE001
            # Its validation only runs now — "nothing to race", "no stored profiles". The
            # HTTP caller is long gone, so the ticket carries the reason instead.
            ticket.state = "failed"
            ticket.error = f"{type(exc).__name__}: {exc}"
            log.warning("Queued job %s (%s) failed to start: %s", ticket.kind, ticket.label, exc)
        _settle(ticket)
        _remember(ticket)

        if ticket.state == "started":
            _wait_until_running(ticket.kind)


def _wait_until_running(kind: str) -> None:
    """Give a just-started engine a moment to actually take the pipeline.

    Without this the dispatcher could start the next job in the gap between an engine's
    ``start()`` returning and its thread taking the lock — two sessions in flight, which is
    the one thing the whole pipeline is built to prevent. Bounded, so a kind whose
    ``active()`` was never registered costs a pause rather than the queue.
    """
    deadline = time.monotonic() + START_GRACE_S
    while time.monotonic() < deadline:
        if coordinator.busy() or kind_active(kind):
            return
        time.sleep(0.05)
    log.warning("Queued job %s did not take the pipeline within the grace period", kind)


def _remember(ticket: Ticket) -> None:
    with _lock:
        _recent.append(ticket)
        del _recent[:-_RECENT_MAX]


def cancel(ticket_id: int) -> bool:
    """Drop a queued job before it starts. Returns True if it was still waiting.

    Free by construction: a pending ticket has applied nothing and written nothing, so it
    simply leaves the line. A job that has already *started* is the engine's own to cancel —
    it owns the baseline it has to restore.
    """
    with _lock:
        for i, ticket in enumerate(_queue):
            if ticket.id == ticket_id:
                ticket.state = "cancelled"
                _queue.pop(i)
                _settle(ticket)
                _remember(ticket)
                log.info("Queued job %s (%s) cancelled before starting", ticket.kind, ticket.label)
                return True
    return False


def pending() -> list[dict]:
    """Everything waiting, in the order it will run."""
    with _lock:
        tickets = list(_queue)
    return [
        {
            "ticket_id": t.id,
            "kind": t.kind,
            "label": t.label,
            "queue_position": i,
            "submitted_at": t.submitted_at.isoformat(),
            "state": t.state,
            "resumed": t.resumed,
        }
        for i, t in enumerate(tickets, start=1)
    ]


def recent_failures() -> list[dict]:
    """Queued jobs that failed when their turn finally came — the errors nobody was there
    to receive."""
    with _lock:
        tickets = [t for t in _recent if t.state == "failed"]
    return [
        {
            "ticket_id": t.id,
            "kind": t.kind,
            "label": t.label,
            "error": t.error,
            "started_at": t.started_at.isoformat() if t.started_at else None,
        }
        for t in tickets
    ]


def all_pending() -> list[dict]:
    """Everything waiting to run, across both queueing layers.

    Ordering between layers is best-effort: a ticket waits on the dispatcher while a
    self-queued session waits on the coordinator directly, and the coordinator's lock is
    documented as unfair. Within a layer the order is exact. This is a deliberate trade —
    strict global FIFO would mean taking the row id away from the two callers that need it
    synchronously — and it costs at most a swap between two things that were both about to
    run anyway.
    """
    out = list(pending())
    with _lock:
        sources = list(_pending_sources)
    for source in sources:
        try:
            out.extend(source())
        except Exception:  # noqa: BLE001 — a listing must never fail the status read
            log.debug("job_queue: a pending source raised", exc_info=True)
    return out


def status() -> dict:
    """What the pipeline is doing and who is waiting — one read for every "can I start?" UI."""
    waiting = all_pending()
    return {
        # The coordinator is the authority on "is the pipeline in use" — it is the thing
        # that actually serializes sessions, and unlike a module flag it has a lease and an
        # eviction path when a holder dies.
        "busy": coordinator.busy(),
        "blocked_by": blocked_by(),
        "owner": coordinator.owner(),
        "held_for_s": coordinator.held_for(),
        "queue_depth": len(waiting),
        "pending": waiting,
        "recent_failures": recent_failures(),
        # Set once per process by the startup restore: what came back after a restart.
        "restored": dict(_restored) if _restored else None,
    }


def _reset_for_tests() -> None:
    global _seq, _dispatcher, _restored
    with _lock:
        _queue.clear()
        _recent.clear()
        _engines.clear()
        _starters.clear()
        _pending_sources.clear()
        _seq = 0
        _dispatcher = None
        _restored = None


__all__ = [
    "submit", "cancel", "pending", "all_pending", "status", "register", "kind_active",
    "register_pending_source", "register_engines", "register_starter", "restore",
    "note_restored", "can_start_now", "Submission", "RESUME_WINDOW_HOURS",
]
