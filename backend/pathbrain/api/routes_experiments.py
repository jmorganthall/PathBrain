"""Experiment engine endpoints.

Experiment parameters (window, candidates, enable/dry-run) are edited via
``/api/config`` under the ``experiment`` key; the engine auto-starts within the
window when armed. These endpoints expose status, history, and a manual abort.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config_store import get_config
from ..database import get_session
from ..experiment import abort_active, in_window
from ..models import Experiment, ExperimentStatus

router = APIRouter()


def _experiment_summary(exp: Experiment) -> dict:
    return {
        "id": exp.id,
        "created_at": exp.created_at.isoformat(),
        "finished_at": exp.finished_at.isoformat() if exp.finished_at else None,
        "status": exp.status.value if hasattr(exp.status, "value") else str(exp.status),
        "param": exp.param,
        "candidates": exp.candidates,
        "dry_run": exp.dry_run,
        "baseline_value": exp.baseline_value,
        "trial_count": len(exp.trials),
        "result": exp.result,
    }


@router.get("/experiments")
def list_experiments(session: Session = Depends(get_session)) -> dict:
    cfg = get_config(session).get("experiment", {}) or {}
    rows = session.scalars(
        select(Experiment).order_by(Experiment.created_at.desc()).limit(50)
    ).all()
    active = next((r for r in rows if r.status == ExperimentStatus.RUNNING), None)
    return {
        "status": {
            "enabled": bool(cfg.get("enabled")),
            "dry_run": bool(cfg.get("dry_run", True)),
            "auto_promote": bool(cfg.get("auto_promote")),
            "in_window": in_window(cfg.get("window", {}) or {}),
            "window": cfg.get("window", {}),
            "param": cfg.get("param"),
            "candidates": cfg.get("candidates", []),
            "active_experiment_id": active.id if active else None,
        },
        "experiments": [_experiment_summary(e) for e in rows],
    }


@router.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: int, session: Session = Depends(get_session)) -> dict:
    exp = session.get(Experiment, experiment_id)
    if exp is None:
        return {"error": "not found"}
    summary = _experiment_summary(exp)
    summary["trials"] = [
        {
            "id": t.id,
            "created_at": t.created_at.isoformat(),
            "value": t.value,
            "sops": t.sops,
            "run_id": t.run_id,
            "applied": t.applied,
        }
        for t in exp.trials
    ]
    summary["baseline_settings"] = exp.baseline_settings
    return summary


@router.post("/experiments/abort")
def abort_experiment() -> dict:
    """Stop the running experiment immediately and restore the baseline config."""
    aborted = abort_active()
    return {"aborted": aborted}
