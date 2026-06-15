"""Benchmark run orchestration.

A *run* executes every benchmark plugin against the current config. To reduce
per-run variability, a run can repeat the whole suite ``iterations`` times and
average each metric (keeping mean/stdev/min/max per metric). The averaged
metrics are scored into the Seat of Pants Score. Runs execute in a background
thread so the API returns immediately with a run id the UI polls; the run's
``iterations_completed`` is updated after each iteration for live progress.
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean, pstdev
from time import perf_counter

from .config_store import get_config
from .database import session_scope
from .logging_config import get_logger
from .models import BenchmarkResult, Run, RunStatus, ScoreResult
from .plugins import BenchmarkPlugin, PluginResult, iter_plugins
from .scoring import compute_score

log = get_logger("runner")

MAX_ITERATIONS = 20


def create_run(
    label: str | None = None,
    notes: str | None = None,
    iterations: int | None = None,
) -> int:
    """Create a pending run row and return its id."""
    with session_scope() as session:
        config = get_config(session)
        iters = iterations if iterations else int(config.get("iterations", 1) or 1)
        iters = max(1, min(iters, MAX_ITERATIONS))
        run = Run(
            label=label,
            notes=notes,
            status=RunStatus.PENDING,
            config_used=config,
            iterations=iters,
        )
        session.add(run)
        session.flush()
        return run.id


def _metric_stats(values: list[float]) -> dict:
    n = len(values)
    return {
        "mean": round(mean(values), 3),
        "stdev": round(pstdev(values), 3) if n > 1 else 0.0,
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "n": n,
    }


def _aggregate(results: list[PluginResult]) -> dict:
    """Average a plugin's results across iterations.

    Returns a dict with ``success``, averaged ``metrics`` (mean per key),
    ``details`` (last successful iteration's details enriched with per-metric
    stats and per-iteration values), ``duration_ms`` (mean) and ``error``.
    """
    successes = [r for r in results if r.success]
    if not successes:
        error = next((r.error for r in results if r.error), "all iterations failed")
        return {
            "success": False,
            "metrics": {},
            "details": {"iterations": len(results), "samples": 0},
            "duration_ms": None,
            "error": error,
        }

    keys: set[str] = set()
    for r in successes:
        keys.update(r.metrics.keys())

    mean_metrics: dict[str, float | None] = {}
    metric_stats: dict[str, dict] = {}
    for key in sorted(keys):
        values = [
            float(r.metrics[key])
            for r in successes
            if r.metrics.get(key) is not None
        ]
        if values:
            stats = _metric_stats(values)
            metric_stats[key] = stats
            mean_metrics[key] = stats["mean"]
        else:
            mean_metrics[key] = None

    durations = [r.duration_ms for r in successes if r.duration_ms is not None]

    # Start from the most recent successful iteration's details so plugin-specific
    # payloads (e.g. the browser engine's per-URL screenshots) remain available,
    # then layer the aggregation summary on top.
    details = dict(successes[-1].details or {})
    details["iterations"] = len(results)
    details["samples"] = len(successes)
    details["metric_stats"] = metric_stats
    details["iteration_metrics"] = [r.metrics for r in results]

    return {
        "success": True,
        "metrics": mean_metrics,
        "details": details,
        "duration_ms": round(mean(durations), 3) if durations else None,
        "error": None,
    }


def execute_run(run_id: int) -> None:
    """Execute all plugins for ``run_id`` across iterations, store + score.

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
            iterations = run.iterations or 1
            session.commit()

            plugins: list[BenchmarkPlugin] = iter_plugins()
            per_plugin: dict[str, list[PluginResult]] = {p.name: [] for p in plugins}
            iteration_durations: list[float] = []

            for i in range(iterations):
                it_start = perf_counter()
                log.info("Run %s: iteration %s/%s", run_id, i + 1, iterations)
                for plugin in plugins:
                    section = config.get(plugin.name, {})
                    result = plugin.run(section)
                    per_plugin[plugin.name].append(result)
                    if not result.success:
                        log.warning(
                            "Run %s iter %s: plugin '%s' failed: %s",
                            run_id, i + 1, plugin.name, result.error,
                        )
                iteration_durations.append((perf_counter() - it_start) * 1000.0)
                run.iterations_completed = i + 1
                session.commit()  # surface progress to pollers

            plugin_metrics: dict[str, dict] = {}
            for plugin in plugins:
                agg = _aggregate(per_plugin[plugin.name])
                session.add(
                    BenchmarkResult(
                        run_id=run_id,
                        plugin=plugin.name,
                        success=agg["success"],
                        error=agg["error"],
                        duration_ms=agg["duration_ms"],
                        metrics=agg["metrics"],
                        details=agg["details"],
                    )
                )
                if agg["success"]:
                    plugin_metrics[plugin.name] = {
                        k: v for k, v in agg["metrics"].items() if v is not None
                    }

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

            run.per_iteration_ms = (
                round(mean(iteration_durations), 3) if iteration_durations else None
            )
            run.status = RunStatus.COMPLETE
            run.finished_at = datetime.now(timezone.utc)
            session.commit()
            log.info(
                "Run %s complete: SOPS=%.2f (%s iteration(s))",
                run_id, breakdown.sops, iterations,
            )
    except Exception as exc:  # noqa: BLE001 — never let a background task crash silently
        log.exception("Run %s failed", run_id)
        with session_scope() as session:
            run = session.get(Run, run_id)
            if run is not None:
                run.status = RunStatus.FAILED
                run.error = f"{type(exc).__name__}: {exc}"
                run.finished_at = datetime.now(timezone.utc)
                session.commit()
