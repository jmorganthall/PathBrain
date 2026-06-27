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

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs, profile_test, sweep
from ..database import get_session
from ..models import Experiment, ExperimentStatus, Run, RunStatus, Sweep

router = APIRouter()


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _active_run_jobs(session: Session) -> list[dict]:
    runs = session.scalars(
        select(Run).where(Run.status.in_([RunStatus.RUNNING, RunStatus.PENDING]))
    ).all()
    out = []
    for r in runs:
        done, total = r.iterations_completed or 0, r.iterations or 1
        out.append(
            {
                "id": f"run-{r.id}",
                "kind": "run",
                "label": r.label or f"Benchmark run #{r.id}",
                "status": "running",
                "current": done,
                "total": total,
                "message": f"iteration {min(done + 1, total)}/{total}",
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
    if not profile_test.active():
        return []
    t = profile_test.current()
    if not t or t.get("status") not in ("running", "pending"):
        return []
    return [
        {
            "id": f"profile_test-{t['id']}",
            "kind": "profile_test",
            "label": f"Test to minimum: {t.get('label') or t.get('fingerprint')}",
            "status": "running",
            "current": None,
            "total": t.get("iterations"),
            "message": f"running {t.get('iterations')} iteration(s), then restoring",
            "error": None,
            "href": "/settings",
            "started_at": t.get("started_at") or t.get("created_at"),
            "finished_at": None,
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


@router.get("/jobs")
def list_jobs(session: Session = Depends(get_session)) -> dict:
    """Every active + recently-finished background operation, for the jobs dropdown.

    Live adapter entries (runs/sweep/profile test/experiment) come first, then the
    in-process score jobs (which include recent finished history). ``running`` is the
    count the UI badges.
    """
    adapters: list[dict] = []
    adapters += _active_run_jobs(session)
    adapters += _active_sweep_job(session)
    adapters += _active_profile_test_job()
    adapters += _active_experiment_job(session)

    feed = adapters + jobs.list_jobs()
    running = sum(1 for j in feed if j["status"] == "running")
    return {"jobs": feed, "running": running}
