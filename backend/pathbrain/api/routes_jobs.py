"""Unified "running jobs" feed for the top-right status dropdown.

Merges two sources into one list the UI can poll:

* in-process background jobs (``jobs.py``) — the re-grade / re-score / re-derive
  passes, with live progress + recent history;
* read-only **adapters** that synthesize a job entry from each existing tracker that
  already runs work on its own thread + DB row — benchmark runs, the Shotgun Sweep,
  profile tests, and experiments — so the dropdown shows *everything* happening, not
  just the score jobs.

The adapters don't change those subsystems; they just read state they already expose.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import baseline_test, challenger, current_test, duel, jobs, profile_test, refresh, sweep
from ..database import get_session
from ..models import Experiment, ExperimentStatus, Run, RunStatus, Sweep

router = APIRouter()


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _per_iteration_estimate(session: Session) -> float | None:
    """Avg per-iteration duration (ms) over recent completed runs, for ETAs."""
    rows = session.scalars(
        select(Run)
        .where(Run.status == RunStatus.COMPLETE, Run.per_iteration_ms.is_not(None))
        .order_by(Run.created_at.desc())
        .limit(5)
    ).all()
    vals = [r.per_iteration_ms for r in rows if r.per_iteration_ms]
    return sum(vals) / len(vals) if vals else None


def _fmt_eta(ms: float) -> str:
    secs = max(0, round(ms / 1000))
    if secs < 60:
        return f"~{secs}s left"
    return f"~{secs // 60}m {secs % 60:02d}s left"


def _as_dt(value) -> datetime | None:
    """A datetime from either a datetime or an ISO string — adapters carry both."""
    if value is None or isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _eta_ms(
    *,
    started_at=None,
    budget_s: float | None = None,
    remaining_units: float | None = None,
    per_unit_ms: float | None = None,
    current: float | None = None,
    total: float | None = None,
) -> tuple[float | None, str | None]:
    """``(milliseconds remaining, how we know)`` for any job — one rule for every kind.

    "How long until this finishes?" is the question a progress bar is standing in for and
    usually can't answer: 3/40 tells you nothing about whether to wait or walk away. Three
    ways to answer it, best first, because they are not equally trustworthy and the display
    says which one it used:

    * ``scheduled`` — the job is **time-boxed** (a duel window, a challenger race, "test
      current for 20 minutes"). Its finish time isn't estimated at all, it's a deadline.
    * ``measured`` — units of known cost remain: iterations left × the per-iteration time
      measured over recent completed runs. The unit is the same one the work is made of.
    * ``observed`` — nothing is known up front, so extrapolate this job's own rate:
      elapsed ÷ done × remaining. Needs one completed unit before it says anything, and
      it self-corrects as the job goes.

    ``None`` when a job genuinely can't be estimated (no deadline, no unit cost, no progress
    yet) — the display then says so, because a fabricated countdown is worse than no
    countdown: it is the one number a user would actually plan around.
    """
    now = datetime.now(timezone.utc)
    start = _as_dt(started_at)

    if budget_s and start is not None:
        left = (start + timedelta(seconds=float(budget_s)) - now).total_seconds() * 1000.0
        return max(0.0, left), "scheduled"

    if remaining_units is not None and per_unit_ms:
        return max(0.0, float(remaining_units) * float(per_unit_ms)), "measured"

    if start is not None and current and total and total > current:
        elapsed_ms = (now - start).total_seconds() * 1000.0
        if elapsed_ms > 0:
            return elapsed_ms / float(current) * (float(total) - float(current)), "observed"

    return None, None


def _with_eta(entry: dict, eta: tuple[float | None, str | None]) -> dict:
    """Attach the ETA to a job entry.

    Deliberately a **number of milliseconds remaining**, not a formatted string and not an
    absolute timestamp. A string can't be counted down; an absolute server timestamp would
    be read against the *browser's* clock, so any skew between the two lands straight in the
    number. A duration is skew-free: the client anchors it to its own clock the moment it
    arrives and ticks from there, and the next poll re-anchors it.
    """
    ms, basis = eta
    entry["eta_ms"] = None if ms is None else round(ms)
    entry["eta_basis"] = basis
    return entry


def _run_entry(r: Run, est: float | None, holder: str | None, parent_id: str | None) -> dict:
    """One active benchmark run as a job entry — either a standalone run (parent_id None) or a
    chunk nested under its broader job (parent_id = the group id)."""
    done, total = r.iterations_completed or 0, r.iterations or 1
    if r.status == RunStatus.RUNNING:
        message = f"iteration {min(done + 1, total)}/{total}"
    else:  # PENDING — queued behind the coordination lock; say what it's waiting on.
        message = f"queued — waiting for {holder}" if holder else "queued"
    eta = _eta_ms(
        started_at=r.started_at or r.created_at,
        remaining_units=max(0, total - done) if r.status == RunStatus.RUNNING else None,
        per_unit_ms=est,
        current=done,
        total=total,
    ) if r.status == RunStatus.RUNNING else (None, None)
    return _with_eta({
        "id": f"run-{r.id}",
        "kind": "run",
        "label": r.label or f"Benchmark run #{r.id}",
        "status": "running",
        "current": done,
        "total": total,
        "message": message,
        "error": None,
        "href": f"/runs/{r.id}",
        "parent_id": parent_id,
        # Cancelling a chunk marks it FAILED, which also stops its series (the driver breaks
        # on a failed chunk) — so this is both "cancel this chunk" and, for a chunk, the
        # mechanism behind a parent's "cancel the whole job".
        "cancel_url": f"/runs/{r.id}/cancel",
        "started_at": _iso(r.started_at or r.created_at),
        "finished_at": None,
    }, eta)


def _series_parent(session: Session, group: str, children: list[Run]) -> dict:
    """Synthesize a parent line for a manual run *series* (which has no DB row of its own):
    aggregate progress across all its chunks, and a cancel that stops the whole series."""
    total = next((c.job_group_total for c in children if c.job_group_total), None)
    # Sum completed iterations across ALL chunks of the group (incl. already-finished ones,
    # which aren't in `children` — those are only the active chunks), for true progress.
    done = session.execute(
        select(func.coalesce(func.sum(Run.iterations_completed), 0)).where(Run.job_group == group)
    ).scalar_one()
    n_chunks = session.execute(
        select(func.count(Run.id)).where(Run.job_group == group)
    ).scalar_one()
    label = next((c.label for c in children if c.label), None) or "Manual run"
    active_child = children[0].id if children else None
    msg = f"{done}/{total} iteration(s)" if total else f"{done} iteration(s)"
    started = children[0].started_at or children[0].created_at if children else None
    est = _per_iteration_estimate(session)
    eta = _eta_ms(
        started_at=started,
        remaining_units=max(0, (total or 0) - int(done)) if total else None,
        per_unit_ms=est,
        current=int(done),
        total=total,
    )
    return _with_eta({
        "id": group,
        "kind": "run_series",
        "label": label,
        "status": "running",
        "current": int(done),
        "total": total,
        "message": f"{msg} · {n_chunks} chunk(s)",
        "error": None,
        "href": None,
        "parent_id": None,
        # Cancelling the active chunk fails it, which stops the series (see _run_entry).
        "cancel_url": f"/runs/{active_child}/cancel" if active_child else None,
        "started_at": _iso(started),
        "finished_at": None,
    }, eta)


def _active_run_jobs(session: Session) -> list[dict]:
    from .. import coordinator

    runs = session.scalars(
        select(Run).where(Run.status.in_([RunStatus.RUNNING, RunStatus.PENDING])).order_by(Run.id)
    ).all()
    est = _per_iteration_estimate(session)
    holder = coordinator.owner()
    out: list[dict] = []
    # Group the active runs: standalone runs are top-level; chunked runs nest under the broader
    # job (an engine adapter parent for profile_test/current_test/baseline_test whose id matches
    # the group, or a synthesized parent for a manual "run-series").
    grouped: dict[str, list[Run]] = {}
    for r in runs:
        if r.job_group:
            grouped.setdefault(r.job_group, []).append(r)
        else:
            out.append(_run_entry(r, est, holder, parent_id=None))
    for group, children in grouped.items():
        for c in children:
            out.append(_run_entry(c, est, holder, parent_id=group))
        # Engine groups already emit their own parent (matching id); only the manual series
        # needs one synthesized here.
        if group.startswith("run-series-"):
            out.append(_series_parent(session, group, children))
    return out


def _active_sweep_job(session: Session) -> list[dict]:
    sweep_id = sweep.active_sweep_id()
    if sweep_id is None:
        return []
    sw = session.get(Sweep, sweep_id)
    if sw is None:
        return []
    done, total = sw.completed_variants or 0, sw.total_variants or 0
    eta = _eta_ms(started_at=sw.started_at or sw.created_at, current=done, total=total)
    return [
        _with_eta({
            "id": f"sweep-{sw.id}",
            "kind": "sweep",
            "label": "Shotgun sweep",
            "status": "running",
            "current": done,
            "total": total,
            "message": f"variant {min(done + 1, total)}/{total}" if total else "starting…",
            "error": None,
            "href": "/sweep",
            "parent_id": None,
            "cancel_url": f"/sweep/{sw.id}/cancel",
            "started_at": _iso(sw.started_at or sw.created_at),
            "finished_at": None,
        }, eta)
    ]


def _active_profile_test_job(session: Session) -> list[dict]:
    """The most recent profile test as a job entry — shown while running/pending AND for a
    short window after it finishes, so a fast failure (e.g. the firewall rejecting a field)
    stays visible with its error instead of blinking out of the dropdown."""
    t = profile_test.current()
    if not t:
        return []
    status = t.get("status")
    label = f"Test to minimum: {t.get('label') or t.get('fingerprint')}"
    if status in ("running", "pending"):
        # Progress lived only in the stage sentence ("part 1/1 (0/5 done)"), so the bar was
        # indeterminate and there was no ETA at all. The test's chunks carry
        # job_group="profile_test-<id>" and their own completed-iteration counts, which is
        # the same aggregation the manual-run series does — real progress, and a unit whose
        # cost we already measure.
        done = session.execute(
            select(func.coalesce(func.sum(Run.iterations_completed), 0))
            .where(Run.job_group == f"profile_test-{t['id']}")
        ).scalar_one()
        total = t.get("iterations")
        eta = _eta_ms(
            started_at=t.get("started_at") or t.get("created_at"),
            remaining_units=max(0, (total or 0) - int(done)) if total else None,
            per_unit_ms=_per_iteration_estimate(session),
            current=int(done),
            total=total,
        )
        return [
            _with_eta({
                "id": f"profile_test-{t['id']}",
                "kind": "profile_test",
                "label": label,
                "status": "running",
                "current": int(done),
                "total": total,
                # The live step readout (snapshot → apply → verify → benchmark → restore).
                "message": t.get("stage") or f"running {t.get('iterations')} iteration(s)",
                "error": None,
                "href": "/settings",
                "parent_id": None,
                "cancel_url": "/settings/test-profile/cancel",
                "started_at": t.get("started_at") or t.get("created_at"),
                "finished_at": None,
            }, eta)
        ]
    # Finished — keep it in the feed for a few minutes so the outcome is readable.
    if not _finished_recently(t.get("finished_at"), minutes=5):
        return []
    failed = status == "failed"
    cancelled = status == "cancelled"
    return [
        {
            "id": f"profile_test-{t['id']}",
            "kind": "profile_test",
            "label": label,
            "status": "failed" if failed else "succeeded",
            "current": None,
            "total": t.get("iterations"),
            "message": t.get("stage")
            or ("failed" if failed else "cancelled" if cancelled else "done — baseline restored"),
            "error": t.get("error") if failed else None,
            "href": "/settings",
            "parent_id": None,
            "cancel_url": None,
            "started_at": t.get("started_at") or t.get("created_at"),
            "finished_at": t.get("finished_at"),
        }
    ]


def _finished_recently(finished_at_iso: str | None, minutes: int) -> bool:
    """True if an ISO timestamp is within the last ``minutes`` (best-effort; False on parse
    failure so a bad value simply drops the entry rather than pinning it forever)."""
    if not finished_at_iso:
        return False
    try:
        ts = datetime.fromisoformat(finished_at_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts) <= timedelta(minutes=minutes)
    except (ValueError, TypeError):
        return False


def _baseline_per_iteration() -> float | None:
    """The baseline test's per-iteration cost is the same benchmark as any other run — but it
    is measured on the *unshaped* link, so it is not necessarily the same duration. Without a
    separate history to draw on, reuse the general estimate; the observed-rate fallback takes
    over the moment one iteration has actually completed, which is the more honest number."""
    return None


def _active_current_test_job() -> list[dict]:
    if not current_test.active():
        return []
    t = current_test.current()
    if not t or t.get("status") not in ("running", "pending"):
        return []
    mins = int((t.get("duration_s") or 0) // 60)
    collected = t.get("iterations_run") or 0
    eta = _eta_ms(
        started_at=t.get("started_at") or t.get("created_at"), budget_s=t.get("duration_s")
    )
    return [
        _with_eta({
            "id": f"current_test-{t['id']}",
            "kind": "current_test",
            "label": f"Test current: {t.get('label') or 'live profile'}",
            "status": "running",
            "current": collected,
            "total": None,
            "message": f"{mins} min on the live profile · {collected} iteration(s) collected",
            "error": None,
            "href": "/",
            "parent_id": None,
            "cancel_url": "/current/test/cancel",
            "started_at": t.get("started_at") or t.get("created_at"),
            "finished_at": None,
        }, eta)
    ]


def _active_baseline_test_job() -> list[dict]:
    """The most recent baseline (SQM off) test — shown while running/pending AND for a short
    window after it finishes, so a failure stays visible with its error and stage readout."""
    t = baseline_test.current()
    if not t:
        return []
    status = t.get("status")
    label = f"Baseline · SQM off ({t.get('trigger') or 'manual'})"
    if status in ("running", "pending"):
        done_iters = t.get("iterations_run") or 0
        total_iters = t.get("iterations")
        eta = _eta_ms(
            started_at=t.get("started_at") or t.get("created_at"),
            remaining_units=max(0, (total_iters or 0) - done_iters) if total_iters else None,
            per_unit_ms=_baseline_per_iteration(),
            current=done_iters,
            total=total_iters,
        )
        return [
            _with_eta({
                "id": f"baseline_test-{t['id']}",
                "kind": "baseline_test",
                "label": label,
                "status": "running",
                "current": t.get("iterations_run") or 0,
                "total": t.get("iterations"),
                "message": t.get("stage") or "running",
                "error": None,
                "href": "/baseline",
                "parent_id": None,
                "cancel_url": "/baseline/test/cancel",
                "started_at": t.get("started_at") or t.get("created_at"),
                "finished_at": None,
            }, eta)
        ]
    if not _finished_recently(t.get("finished_at"), minutes=5):
        return []
    failed = status == "failed"
    return [
        {
            "id": f"baseline_test-{t['id']}",
            "kind": "baseline_test",
            "label": label,
            "status": "failed" if failed else "succeeded",
            "current": t.get("iterations_run") or 0,
            "total": t.get("iterations"),
            "message": t.get("stage") or ("failed" if failed else "done — SQM restored"),
            "error": t.get("error") if failed else None,
            "href": "/baseline",
            "parent_id": None,
            "cancel_url": None,
            "started_at": t.get("started_at") or t.get("created_at"),
            "finished_at": t.get("finished_at"),
        }
    ]


def _experiment_window_close_ms(session: Session) -> float | None:
    """Milliseconds until the experimentation window closes, in the container's local time.

    An experiment has no unit count to burn down — it interleaves candidates for as long as
    it is allowed to — so the only truthful answer to "when is this done?" is the window's
    own closing hour, which is exactly when the engine restores the baseline. Window hours
    are local (the same clock ``scheduler`` gates on), and ``start > end`` means overnight.
    """
    from ..config_store import get_config

    window = ((get_config(session).get("experiment", {}) or {}).get("window", {}) or {})
    end_hour = window.get("end_hour")
    if end_hour is None:
        return None
    now = datetime.now().astimezone()
    close = now.replace(hour=int(end_hour) % 24, minute=0, second=0, microsecond=0)
    if close <= now:  # the window closes on the other side of midnight
        close += timedelta(days=1)
    return max(0.0, (close - now).total_seconds() * 1000.0)


def _active_experiment_job(session: Session) -> list[dict]:
    exp = session.scalars(
        select(Experiment)
        .where(Experiment.status == ExperimentStatus.RUNNING)
        .order_by(Experiment.id.desc())
    ).first()
    if exp is None:
        return []
    try:
        close_ms = _experiment_window_close_ms(session)
    except Exception:  # noqa: BLE001 — a config read must not break the jobs feed
        close_ms = None
    eta = (close_ms, "scheduled") if close_ms is not None else (None, None)
    return [
        _with_eta({
            "id": f"experiment-{exp.id}",
            "kind": "experiment",
            "label": f"Experiment: sweeping {exp.param}",
            "status": "running",
            "current": None,
            "total": None,
            "message": "interleaving candidates" + (" (dry-run)" if exp.dry_run else ""),
            "error": None,
            "href": "/experiments",
            "parent_id": None,
            "cancel_url": None,  # the experiment engine has no cancel endpoint
            "started_at": _iso(exp.created_at),
            "finished_at": None,
        }, eta)
    ]


def _active_challenger_job() -> list[dict]:
    if not challenger.active():
        return []
    r = challenger.current()
    if not r or r.get("status") not in ("running", "pending"):
        return []
    n_elim = len(r.get("eliminated") or [])
    leader = r.get("leader_label") or "…"
    n_refresh = r.get("incumbent_refreshes") or 0
    refresh_note = f" · {n_refresh} incumbent refresh{'es' if n_refresh != 1 else ''}" if n_refresh else ""
    eta = _eta_ms(
        started_at=r.get("started_at") or r.get("created_at"), budget_s=r.get("time_budget_s")
    )
    return [
        _with_eta({
            "id": f"challenger-{r['id']}",
            "kind": "challenger",
            "label": "Challenger race",
            "status": "running",
            "current": r.get("iterations_run") or 0,
            "total": None,
            "message": f"iter {r.get('iterations_run') or 0} · leader {leader} · {n_elim} eliminated{refresh_note}",
            "error": None,
            "href": "/settings",
            "parent_id": None,
            "cancel_url": "/settings/race/cancel",
            "started_at": r.get("started_at") or r.get("created_at"),
            "finished_at": None,
        }, eta)
    ]


def _active_refresh_job() -> list[dict]:
    if not refresh.active():
        return []
    r = refresh.current()
    if not r or r.get("status") not in ("running", "pending"):
        return []
    done, total = r.get("profiles_done") or 0, r.get("profiles_total") or 0
    cur = r.get("current_label")
    message = f"profile {min(done + 1, total)}/{total}" if total else "starting…"
    if cur:
        message += f" · {cur}"
    eta = _eta_ms(
        started_at=r.get("started_at") or r.get("created_at"), current=done, total=total
    )
    return [
        _with_eta({
            "id": f"refresh-{r['id']}",
            "kind": "refresh",
            "label": "Re-run all profiles",
            "status": "running",
            "current": done,
            "total": total,
            "message": message,
            "error": None,
            "href": "/settings",
            "parent_id": None,
            "cancel_url": "/settings/refresh/cancel",
            "started_at": r.get("started_at") or r.get("created_at"),
            "finished_at": None,
        }, eta)
    ]


def _active_duel_job() -> list[dict]:
    if not duel.active():
        return []
    d = duel.current()
    if not d or d.get("status") not in ("running", "pending"):
        return []
    n_verdicts = len(d.get("matchups") or [])
    eta = _eta_ms(
        started_at=d.get("started_at") or d.get("created_at"), budget_s=d.get("duration_s")
    )
    return [
        _with_eta({
            # id matches the chunks' job_group so they nest under this parent line.
            "id": f"duel-{d['id']}",
            "kind": "duel",
            "label": "Duel ladder",
            "status": "running",
            "current": d.get("iterations_run") or 0,
            "total": None,
            "message": d.get("stage") or f"{n_verdicts} verdict(s) so far",
            "error": None,
            "href": "/settings",
            "parent_id": None,
            "cancel_url": "/duel/cancel",
            "started_at": d.get("started_at") or d.get("created_at"),
            "finished_at": None,
        }, eta)
    ]


@router.get("/jobs")
def list_jobs(session: Session = Depends(get_session)) -> dict:
    """Every active + recently-finished background operation, for the jobs dropdown.

    Live adapter entries (runs/sweep/profile test/experiment/challenger race) come
    first, then the in-process score jobs (which include recent finished history).
    ``running`` is the count the UI badges.
    """
    adapters: list[dict] = []
    adapters += _active_run_jobs(session)
    adapters += _active_sweep_job(session)
    adapters += _active_profile_test_job(session)
    adapters += _active_current_test_job()
    adapters += _active_baseline_test_job()
    adapters += _active_experiment_job(session)
    adapters += _active_challenger_job()
    adapters += _active_refresh_job()
    adapters += _active_duel_job()

    # In-process score jobs (re-grade/rescore/rederive) are always top-level with no cancel.
    # They count rows, so their own observed rate is the only estimate available — and for a
    # pass over tens of thousands of runs it is the number that decides whether to wait.
    inproc = []
    for j in jobs.list_jobs():
        entry = {"parent_id": None, "cancel_url": None, **j}
        eta = (
            _eta_ms(started_at=j.get("started_at"), current=j.get("current"), total=j.get("total"))
            if j.get("status") == "running"
            else (None, None)
        )
        inproc.append(_with_eta(entry, eta))
    feed = adapters + inproc
    # One uniform shape for the whole feed: the dropdown renders a countdown for every entry,
    # so `eta_ms` must always be *present* (null where unknown or already finished) rather
    # than missing on whichever branch forgot it. Guaranteed here rather than trusted to each
    # adapter, because a missing key and a null one look identical until something reads it.
    for entry in feed:
        entry.setdefault("eta_ms", None)
        entry.setdefault("eta_basis", None)
        if entry["status"] != "running":
            entry["eta_ms"] = entry["eta_basis"] = None

    # The badge counts distinct top-level running jobs — a nested chunk shouldn't double-count
    # with its parent.
    running = sum(1 for j in feed if j["status"] == "running" and not j.get("parent_id"))
    return {"jobs": feed, "running": running}
