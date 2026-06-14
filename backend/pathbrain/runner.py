"""Benchmark run orchestration.

A *run* executes every benchmark plugin against the current config, stores raw
results, then computes and stores the Seat of Pants Score. Runs execute in a
background thread so the API can return immediately with a run id the UI polls.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .config_store import get_config
from .database import session_scope
from .logging_config import get_logger
from .models import BenchmarkResult, Run, RunStatus, ScoreResult
from .plugins import iter_plugins
from .scoring import compute_score

log = get_logger("runner")


def create_run(label: str | None = None, notes: str | None = None) -> int:
    """Create a pending run row and return its id."""
    with session_scope() as session:
        config = get_config(session)
        run = Run(label=label, notes=notes, status=RunStatus.PENDING, config_used=config)
        session.add(run)
        session.flush()
        return run.id


def execute_run(run_id: int) -> None:
    """Execute all plugins for ``run_id``, store results and score.

    Designed to be safe to call from a background task: it manages its own
    session and never raises out (failures are recorded on the run).
    """
    log.info("Run %s starting", run_id)
    try:
        with session_scope() as session:
            run = session.get(Run, run_id)
            if run is None:
                log.error("Run %s not found", run_id)
                return
            run.status = RunStatus.RUNNING
            run.started_at = datetime.now(timezone.utc)
            config = run.config_used or get_config(session)
            session.commit()

            plugin_metrics: dict[str, dict] = {}
            for plugin in iter_plugins():
                section = config.get(plugin.name, {})
                log.info("Run %s: executing plugin '%s'", run_id, plugin.name)
                result = plugin.run(section)
                session.add(
                    BenchmarkResult(
                        run_id=run_id,
                        plugin=result.plugin,
                        success=result.success,
                        error=result.error,
                        duration_ms=result.duration_ms,
                        metrics=result.metrics,
                        details=result.details,
                    )
                )
                if result.success:
                    plugin_metrics[result.plugin] = result.metrics
                else:
                    log.warning("Run %s: plugin '%s' failed: %s", run_id, plugin.name, result.error)

            breakdown = compute_score(
                plugin_metrics,
                weights=config.get("weights", {}),
                thresholds=config.get("thresholds", {}),
            )
            session.add(
                ScoreResult(
                    run_id=run_id,
                    sops=breakdown.sops,
                    subscores=breakdown.subscores,
                    weights_used=breakdown.weights_used,
                    metric_values=breakdown.metric_values,
                )
            )

            run.status = RunStatus.COMPLETE
            run.finished_at = datetime.now(timezone.utc)
            session.commit()
            log.info("Run %s complete: SOPS=%.2f", run_id, breakdown.sops)
    except Exception as exc:  # noqa: BLE001 — never let a background task crash silently
        log.exception("Run %s failed", run_id)
        with session_scope() as session:
            run = session.get(Run, run_id)
            if run is not None:
                run.status = RunStatus.FAILED
                run.error = f"{type(exc).__name__}: {exc}"
                run.finished_at = datetime.now(timezone.utc)
                session.commit()
