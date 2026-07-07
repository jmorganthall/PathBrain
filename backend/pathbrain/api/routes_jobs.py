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
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import baseline_test, challenger, current_test, jobs, profile_test, refresh, sweep
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


def _active_run_jobs(session: Session) -> list[dict]:
    from .. import coordinator

    runs = session.scalars(
        select(Run).where(Run.status.in_([RunStatus.RUNNING, RunStatus.PENDING]))
    ).all()
    est = _per_iteration_estimate(session)
    out = []
    for r in runs:
        done, total = r.iterations_completed or 0, r.iterations or 1
        is_running = r.status == RunStatus.RUNNING
        if is_running:
            message = f"iteration {min(done + 1, total)}/{total}"
            if est is not None:
                message += f" · {_fmt_eta(est * max(0, total - done))}"
        else:
            # Queued behind the coordination lock — tell the user what it's waiting on.
            holder = coordinator.owner()
            message = f"queued — waiting for {holder}" if holder else "queued"
        out.append(
            {
                "id": f"run-{r.id}",
                "kind": "run",
                "label": r.label or f"Benchmark run #{r.id}",
                "status": "running",
                "current": done,
                "total": total,
                "message": message,
                "error": None,
                "href": f"/runs/{r.id}",
                "started_at": _iso(r.started_at or r.created_at),
                "finished_at": None,
            }
        )
    return out


def _active_sweep_job(session: Session) -> list[dict]:
    sweep_id = sweep.active_sweep_id()
    if sweep_id is None:
        return []
    sw = session.get(Sweep, sweep_id)
    if sw is None:
        return []
    done, total = sw.completed_variants or 0, sw.total_variants or 0
    return [
        {
            "id": f"sweep-{sw.id}",
            "kind": "sweep",
            "label": "Shotgun sweep",
            "status": "running",
            "current": done,
            "total": total,
            "message": f"variant {min(done + 1, total)}/{total}" if total else "starting…",
            "error": None,
            "href": "/sweep",
            "started_at": _iso(sw.started_at or sw.created_at),
            "finished_at": None,
        }
    ]


def _active_profile_test_job() -> list[dict]:
    """The most recent profile test as a job entry — shown while running/pending AND for a
    short window after it finishes, so a fast failure (e.g. the firewall rejecting a field)
    stays visible with its error instead of blinking out of the dropdown."""
    t = profile_test.current()
    if not t:
        return []
    status = t.get("status")
    label = f"Test to minimum: {t.get('label') or t.get('fingerprint')}"
    if status in ("running", "pending"):
        return [
            {
                "id": f"profile_test-{t['id']}",
                "kind": "profile_test",
                "label": label,
                "status": "running",
                "current": None,
                "total": t.get("iterations"),
                # The live step readout (snapshot → apply → verify → benchmark → restore).
                "message": t.get("stage") or f"running {t.get('iterations')} iteration(s)",
                "error": None,
                "href": "/settings",
                "started_at": t.get("started_at") or t.get("created_at"),
                "finished_at": None,
            }
        ]
    # Finished — keep it in the feed for a few minutes so the outcome is readable.
    if not _finished_recently(t.get("finished_at"), minutes=5):
        return []
    failed = status == "failed"
    return [
        {
            "id": f"profile_test-{t['id']}",
            "kind": "profile_test",
            "label": label,
            "status": "failed" if failed else "succeeded",
            "current": None,
            "total": t.get("iterations"),
            "message": t.get("stage") or ("failed" if failed else "done — baseline restored"),
            "error": t.get("error") if failed else None,
            "href": "/settings",
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


def _active_current_test_job() -> list[dict]:
    if not current_test.active():
        return []
    t = current_test.current()
    if not t or t.get("status") not in ("running", "pending"):
        return []
    mins = int((t.get("duration_s") or 0) // 60)
    collected = t.get("iterations_run") or 0
    return [
        {
            "id": f"current_test-{t['id']}",
            "kind": "current_test",
            "label": f"Test current: {t.get('label') or 'live profile'}",
            "status": "running",
            "current": collected,
            "total": None,
            "message": f"{mins} min on the live profile · {collected} iteration(s) collected",
            "error": None,
            "href": "/",
            "started_at": t.get("started_at") or t.get("created_at"),
            "finished_at": None,
        }
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
        return [
            {
                "id": f"baseline_test-{t['id']}",
                "kind": "baseline_test",
                "label": label,
                "status": "running",
                "current": t.get("iterations_run") or 0,
                "total": t.get("iterations"),
                "message": t.get("stage") or "running",
                "error": None,
                "href": "/baseline",
                "started_at": t.get("started_at") or t.get("created_at"),
                "finished_at": None,
            }
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
            "started_at": t.get("started_at") or t.get("created_at"),
            "finished_at": t.get("finished_at"),
        }
    ]


def _active_experiment_job(session: Session) -> list[dict]:
    exp = session.scalars(
        select(Experiment)
        .where(Experiment.status == ExperimentStatus.RUNNING)
        .order_by(Experiment.id.desc())
    ).first()
    if exp is None:
        return []
    return [
        {
            "id": f"experiment-{exp.id}",
            "kind": "experiment",
            "label": f"Experiment: sweeping {exp.param}",
            "status": "running",
            "current": None,
            "total": None,
            "message": "interleaving candidates" + (" (dry-run)" if exp.dry_run else ""),
            "error": None,
            "href": "/experiments",
            "started_at": _iso(exp.created_at),
            "finished_at": None,
        }
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
    return [
        {
            "id": f"challenger-{r['id']}",
            "kind": "challenger",
            "label": "Challenger race",
            "status": "running",
            "current": r.get("iterations_run") or 0,
            "total": None,
            "message": f"iter {r.get('iterations_run') or 0} · leader {leader} · {n_elim} eliminated{refresh_note}",
            "error": None,
            "href": "/settings",
            "started_at": r.get("started_at") or r.get("created_at"),
            "finished_at": None,
        }
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
    return [
        {
            "id": f"refresh-{r['id']}",
            "kind": "refresh",
            "label": "Re-run all profiles",
            "status": "running",
            "current": done,
            "total": total,
            "message": message,
            "error": None,
            "href": "/settings",
            "started_at": r.get("started_at") or r.get("created_at"),
            "finished_at": None,
        }
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
    adapters += _active_profile_test_job()
    adapters += _active_current_test_job()
    adapters += _active_baseline_test_job()
    adapters += _active_experiment_job(session)
    adapters += _active_challenger_job()
    adapters += _active_refresh_job()

    feed = adapters + jobs.list_jobs()
    running = sum(1 for j in feed if j["status"] == "running")
    return {"jobs": feed, "running": running}
