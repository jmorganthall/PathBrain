"""Benchmark run orchestration.

A *run* executes every benchmark plugin against the current config. To reduce
per-run variability, a run can repeat the whole suite ``iterations`` times and
average each metric (keeping mean/stdev/min/max per metric). The averaged
metrics are scored into the Seat of Pants Score. Runs execute in a background
thread so the API returns immediately with a run id the UI polls; the run's
``iterations_completed`` is updated after each iteration for live progress.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean, median, pstdev
from time import perf_counter

from sqlalchemy import select

from .config_store import get_config
from .database import session_scope
from .logging_config import get_logger
from .models import BenchmarkResult, Run, RunStatus, ScoreResult
from .plugins import BenchmarkPlugin, PluginResult, iter_plugins
from .scoring import METRIC_SOURCES, compute_score

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
        "median": round(median(values), 3),
        "mean": round(mean(values), 3),
        "stdev": round(pstdev(values), 3) if n > 1 else 0.0,
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "n": n,
    }


def _plugin_metrics_from_values(metric_values: dict[str, float]) -> dict[str, dict]:
    """Map SOPS metric values back to a plugin->metrics dict for scoring."""
    out: dict[str, dict] = {}
    for metric, value in metric_values.items():
        src = METRIC_SOURCES.get(metric)
        if src and value is not None:
            plugin, key = src
            out.setdefault(plugin, {})[key] = value
    return out


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
            # Use the median as the central value (robust to outlier iterations).
            mean_metrics[key] = stats["median"]
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


def rescore_run(run, weights: dict, thresholds: dict, rubric_version: str | None) -> bool:
    """Re-grade an existing run from its stored raw measurements.

    Recomputes the headline SOPS from the stored median metric values and the
    confidence band from the stored per-iteration metrics, using the given
    (current) rubric. Mutates ``run.score`` in place; the caller commits. This is
    what keeps history comparable after the scoring rubric changes.
    """
    score = run.score
    if score is None:
        return False

    breakdown = compute_score(
        _plugin_metrics_from_values(score.metric_values or {}), weights, thresholds
    )

    # Rebuild per-iteration SOPS from the raw iteration metrics for the band.
    iters = 0
    for r in run.results:
        im = (r.details or {}).get("iteration_metrics") if r.details else None
        if im:
            iters = max(iters, len(im))
    per_iter: list[float] = []
    for i in range(iters):
        iter_metrics: dict[str, dict] = {}
        for r in run.results:
            im = (r.details or {}).get("iteration_metrics") if r.details else None
            if im and i < len(im) and im[i]:
                iter_metrics[r.plugin] = im[i]
        per_iter.append(compute_score(iter_metrics, weights, thresholds).sops)

    if per_iter:
        score.sops_stdev = round(pstdev(per_iter), 2) if len(per_iter) > 1 else 0.0
        score.sops_min = round(min(per_iter), 2)
        score.sops_max = round(max(per_iter), 2)

    score.sops = breakdown.sops
    score.subscores = breakdown.subscores
    score.weights_used = breakdown.weights_used
    score.metric_values = breakdown.metric_values
    score.rubric_version = rubric_version
    return True


def reconcile_interrupted_runs() -> int:
    """Mark runs left RUNNING/PENDING by a previous process as failed.

    Their executing thread is gone (e.g. the container was restarted), so they
    can never complete. Called once at startup.
    """
    with session_scope() as session:
        runs = session.scalars(
            select(Run).where(Run.status.in_([RunStatus.RUNNING, RunStatus.PENDING]))
        ).all()
        for run in runs:
            run.status = RunStatus.FAILED
            run.error = "Interrupted — service restarted while the run was in progress."
            run.finished_at = datetime.now(timezone.utc)
        if runs:
            log.warning("Reconciled %s interrupted run(s) to FAILED", len(runs))
        return len(runs)


def fail_stale_runs(timeout_minutes: float) -> int:
    """Fail runs that have been RUNNING/PENDING longer than ``timeout_minutes``.

    A watchdog for hung or orphaned jobs. Compares against ``started_at`` (or
    ``created_at`` for never-started runs), normalizing to naive UTC since SQLite
    drops tzinfo.
    """
    cutoff_s = max(timeout_minutes, 1.0) * 60.0
    now = datetime.utcnow()
    failed = 0
    with session_scope() as session:
        runs = session.scalars(
            select(Run).where(Run.status.in_([RunStatus.RUNNING, RunStatus.PENDING]))
        ).all()
        for run in runs:
            ref = run.started_at or run.created_at
            if ref is None:
                continue
            ref = ref.replace(tzinfo=None) if ref.tzinfo else ref
            if (now - ref).total_seconds() > cutoff_s:
                run.status = RunStatus.FAILED
                run.error = f"Timed out — exceeded {timeout_minutes:.0f} min watchdog limit."
                run.finished_at = datetime.now(timezone.utc)
                failed += 1
        if failed:
            log.warning("Watchdog failed %s stale run(s)", failed)
    return failed


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

            # Capture the firewall/SQM settings in effect, so this run's score is
            # attributable to a configuration profile. Best-effort: never fail the
            # run if discovery is unavailable.
            try:
                from .providers import get_provider
                from .settings_profile import fingerprint, normalize

                normalized = normalize(get_provider().discover())
                run.settings = normalized
                run.settings_fingerprint = fingerprint(normalized)
            except Exception:  # noqa: BLE001
                log.warning("Run %s: could not capture firewall settings", run_id, exc_info=True)
            session.commit()

            plugins: list[BenchmarkPlugin] = iter_plugins()
            per_plugin: dict[str, list[PluginResult]] = {p.name: [] for p in plugins}
            iteration_durations: list[float] = []
            weights = config.get("weights", {})
            thresholds = config.get("thresholds", {})

            # Score every iteration independently so we can report a robust
            # central SOPS and a confidence band, instead of a single noisy value.
            iteration_scores: list[float] = []
            iteration_metric_values: list[dict] = []

            for i in range(iterations):
                it_start = perf_counter()
                log.info("Run %s: iteration %s/%s", run_id, i + 1, iterations)
                iter_metrics: dict[str, dict] = {}
                for plugin in plugins:
                    section = config.get(plugin.name, {})
                    result = plugin.run(section)
                    per_plugin[plugin.name].append(result)
                    if result.success:
                        iter_metrics[plugin.name] = result.metrics
                    else:
                        log.warning(
                            "Run %s iter %s: plugin '%s' failed: %s",
                            run_id, i + 1, plugin.name, result.error,
                        )
                b = compute_score(iter_metrics, weights=weights, thresholds=thresholds)
                iteration_scores.append(b.sops)
                iteration_metric_values.append(b.metric_values)
                iteration_durations.append((perf_counter() - it_start) * 1000.0)
                run.iterations_completed = i + 1
                session.commit()  # surface progress to pollers

            # Per-plugin display aggregation (median central value + per-metric stats).
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

            # Robust headline: score the median of each metric across iterations.
            metric_keys: set[str] = set()
            for mv in iteration_metric_values:
                metric_keys.update(mv.keys())
            median_values: dict[str, float] = {}
            for k in metric_keys:
                vals = [mv[k] for mv in iteration_metric_values if mv.get(k) is not None]
                if vals:
                    median_values[k] = round(median(vals), 3)

            breakdown = compute_score(
                _plugin_metrics_from_values(median_values),
                weights=weights,
                thresholds=thresholds,
            )
            sops_stdev = round(pstdev(iteration_scores), 2) if len(iteration_scores) > 1 else 0.0
            sops_min = round(min(iteration_scores), 2) if iteration_scores else None
            sops_max = round(max(iteration_scores), 2) if iteration_scores else None

            session.add(
                ScoreResult(
                    run_id=run_id,
                    sops=breakdown.sops,
                    sops_stdev=sops_stdev,
                    sops_min=sops_min,
                    sops_max=sops_max,
                    subscores=breakdown.subscores,
                    weights_used=breakdown.weights_used,
                    metric_values=breakdown.metric_values,
                    rubric_version=config.get("rubric_version"),
                )
            )

            run.per_iteration_ms = (
                round(mean(iteration_durations), 3) if iteration_durations else None
            )
            run.status = RunStatus.COMPLETE
            run.finished_at = datetime.now(timezone.utc)
            session.commit()
            log.info(
                "Run %s complete: SOPS=%.2f ±%.2f (%s iteration(s))",
                run_id, breakdown.sops, sops_stdev, iterations,
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
