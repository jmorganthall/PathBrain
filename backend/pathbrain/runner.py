"""Benchmark run orchestration.

A *run* executes every benchmark plugin against the current config. To reduce
per-run variability, a run can repeat the whole suite ``iterations`` times and
average each metric (keeping mean/stdev/min/max per metric). The averaged
metrics are scored into the Seat of Pants Score. Runs execute in a background
thread so the API returns immediately with a run id the UI polls; the run's
``iterations_completed`` is updated after each iteration for live progress.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from statistics import mean, median, pstdev
from time import perf_counter

from sqlalchemy import select

from .config import get_settings
from .config_store import get_config
from .database import session_scope
from .interpret import DERIVATION_VERSION, derive
from .logging_config import get_logger
from .models import BenchmarkResult, Run, RunStatus, ScoreResult
from .plugins import BenchmarkPlugin, PluginResult, iter_plugins
from .scoring import (
    COMPLETION_METRIC_SOURCES,
    METRIC_SOURCES,
    compute_completion,
    compute_score,
)

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


def _plugin_metrics_from_values(
    metric_values: dict[str, float],
    metric_sources: dict[str, tuple[str, str]] = METRIC_SOURCES,
) -> dict[str, dict]:
    """Map axis metric values back to a plugin->metrics dict for scoring."""
    out: dict[str, dict] = {}
    for metric, value in metric_values.items():
        src = metric_sources.get(metric)
        if src and value is not None:
            plugin, key = src
            out.setdefault(plugin, {})[key] = value
    return out


def _median_values(per_iteration_values: list[dict]) -> dict[str, float]:
    """Median of each metric across iterations (skipping missing samples)."""
    keys: set[str] = set()
    for mv in per_iteration_values:
        keys.update(mv.keys())
    out: dict[str, float] = {}
    for k in keys:
        vals = [mv[k] for mv in per_iteration_values if mv.get(k) is not None]
        if vals:
            out[k] = round(median(vals), 3)
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


def _iteration_metrics(run) -> list[dict[str, dict]]:
    """Reconstruct per-iteration ``plugin -> metrics`` from stored raw results."""
    iters = 0
    for r in run.results:
        im = (r.details or {}).get("iteration_metrics") if r.details else None
        if im:
            iters = max(iters, len(im))
    out: list[dict[str, dict]] = []
    for i in range(iters):
        iter_metrics: dict[str, dict] = {}
        for r in run.results:
            im = (r.details or {}).get("iteration_metrics") if r.details else None
            if im and i < len(im) and im[i]:
                iter_metrics[r.plugin] = im[i]
        out.append(iter_metrics)
    return out


def rescore_run(
    run,
    weights: dict,
    thresholds: dict,
    rubric_version: str | None,
    completion_weights: dict | None = None,
    completion_thresholds: dict | None = None,
) -> bool:
    """Re-grade an existing run from its stored raw measurements.

    Recomputes the headline SOPS (perception-led) *and* the Completion score from
    the stored metric values, plus each axis's confidence band from the stored
    per-iteration metrics, using the given (current) rubric. Mutates ``run.score``
    in place; the caller commits. Keeps history comparable after a rubric change.

    The stored metric values are merged across both axes' slots before re-mapping,
    so runs from before the SOPS/Completion split (whose infra metrics live in the
    old SOPS ``metric_values``) still backfill both axes.
    """
    score = run.score
    if score is None:
        return False

    merged_values = {**(score.completion_metric_values or {}), **(score.metric_values or {})}
    breakdown = compute_score(
        _plugin_metrics_from_values(merged_values, METRIC_SOURCES), weights, thresholds
    )
    iter_metrics_list = _iteration_metrics(run)

    per_iter = [compute_score(m, weights, thresholds).sops for m in iter_metrics_list]
    if per_iter:
        score.sops_stdev = round(pstdev(per_iter), 2) if len(per_iter) > 1 else 0.0
        score.sops_min = round(min(per_iter), 2)
        score.sops_max = round(max(per_iter), 2)

    score.sops = breakdown.sops
    score.subscores = breakdown.subscores
    score.weights_used = breakdown.weights_used
    score.metric_values = breakdown.metric_values
    score.rubric_version = rubric_version

    # Completion axis. Recompute from the same stored values; leave NULL if none
    # of its metrics were captured.
    if completion_weights is not None and completion_thresholds is not None:
        cb = compute_completion(
            _plugin_metrics_from_values(merged_values, COMPLETION_METRIC_SOURCES),
            completion_weights,
            completion_thresholds,
        )
        if cb.subscores:
            c_iter = [
                compute_completion(m, completion_weights, completion_thresholds)
                for m in iter_metrics_list
            ]
            c_scores = [b.sops for b in c_iter if b.subscores]
            score.completion = cb.sops
            score.completion_subscores = cb.subscores
            score.completion_weights_used = cb.weights_used
            score.completion_metric_values = cb.metric_values
            if c_scores:
                score.completion_stdev = (
                    round(pstdev(c_scores), 2) if len(c_scores) > 1 else 0.0
                )
                score.completion_min = round(min(c_scores), 2)
                score.completion_max = round(max(c_scores), 2)
    return True


def rederive_run(
    run,
    weights: dict,
    thresholds: dict,
    rubric_version: str | None,
    completion_weights: dict | None = None,
    completion_thresholds: dict | None = None,
    artifact_base: str | None = None,
) -> bool:
    """Re-derive a run's metrics from its stored *raw* observations, then re-score.

    Unlike :func:`rescore_run` (which re-grades the cached metric scalars under a new
    rubric), this re-runs the whole interpretation: raw → derived metrics → score.
    Use it after a derivation formula changes or a new metric is added, so history
    reflects it without re-collecting. Runs whose raw lacks a signal (e.g. legacy
    runs with no filmstrip) simply don't gain that metric.
    """
    if run.score is None:
        return False

    derived_by_plugin: dict[str, list[dict]] = {}
    n_iters = 0
    for res in run.results:
        raws = (res.raw or {}).get("iterations") or []
        per_iter = [derive(res.plugin, r or {}, artifact_base) for r in raws]
        derived_by_plugin[res.plugin] = per_iter
        n_iters = max(n_iters, len(per_iter))
        # Refresh the per-plugin cache + per-iteration metrics from the re-derivation.
        res.metrics = _median_values(per_iter) if per_iter else {}
        details = dict(res.details or {})
        details["iteration_metrics"] = per_iter
        res.details = details

    iter_metrics_list = [
        {p: pi[i] for p, pi in derived_by_plugin.items() if i < len(pi) and pi[i]}
        for i in range(n_iters)
    ]
    # Refresh the cached scalar metric values (median across iterations) on both
    # axes, then let rescore_run produce the headline + bands from them.
    if iter_metrics_list:
        run.score.metric_values = _median_values(
            [compute_score(im, weights, thresholds).metric_values for im in iter_metrics_list]
        )
        run.score.completion_metric_values = (
            _median_values(
                [
                    compute_completion(im, completion_weights or {}, completion_thresholds or {}).metric_values
                    for im in iter_metrics_list
                ]
            )
            or None
        )
    run.score.derivation_version = DERIVATION_VERSION
    return rescore_run(
        run, weights, thresholds, rubric_version, completion_weights, completion_thresholds
    )


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
            artifact_base = os.path.abspath(get_settings().artifact_dir)
            iteration_durations: list[float] = []
            weights = config.get("weights", {})
            thresholds = config.get("thresholds", {})
            completion_weights = config.get("completion_weights", {})
            completion_thresholds = config.get("completion_thresholds", {})

            # Score every iteration independently so we can report a robust
            # central value and a confidence band, instead of a single noisy
            # value — for both the SOPS (human-feel) and Completion axes.
            iteration_scores: list[float] = []
            iteration_metric_values: list[dict] = []
            completion_scores: list[float] = []
            completion_metric_values: list[dict] = []

            for i in range(iterations):
                it_start = perf_counter()
                log.info("Run %s: iteration %s/%s", run_id, i + 1, iterations)
                iter_metrics: dict[str, dict] = {}
                for plugin in plugins:
                    section = config.get(plugin.name, {})
                    result = plugin.run(section)
                    per_plugin[plugin.name].append(result)
                    if result.success:
                        # Interpret raw → scoreable metrics (the cache); raw is kept
                        # as the source of truth so this can be re-derived later.
                        result.metrics = derive(plugin.name, result.raw, artifact_base)
                        iter_metrics[plugin.name] = result.metrics
                    else:
                        log.warning(
                            "Run %s iter %s: plugin '%s' failed: %s",
                            run_id, i + 1, plugin.name, result.error,
                        )
                b = compute_score(iter_metrics, weights=weights, thresholds=thresholds)
                iteration_scores.append(b.sops)
                iteration_metric_values.append(b.metric_values)
                cb = compute_completion(iter_metrics, completion_weights, completion_thresholds)
                if cb.subscores:  # only when completion metrics were captured
                    completion_scores.append(cb.sops)
                completion_metric_values.append(cb.metric_values)
                iteration_durations.append((perf_counter() - it_start) * 1000.0)
                run.iterations_completed = i + 1
                session.commit()  # surface progress to pollers

            # Per-plugin display aggregation (median central value + per-metric stats).
            # Store the raw observations per iteration as the immutable source of truth.
            for plugin in plugins:
                results = per_plugin[plugin.name]
                agg = _aggregate(results)
                session.add(
                    BenchmarkResult(
                        run_id=run_id,
                        plugin=plugin.name,
                        success=agg["success"],
                        error=agg["error"],
                        duration_ms=agg["duration_ms"],
                        metrics=agg["metrics"],
                        details=agg["details"],
                        raw={"iterations": [r.raw for r in results]},
                    )
                )

            # Robust headline: score the median of each metric across iterations.
            breakdown = compute_score(
                _plugin_metrics_from_values(_median_values(iteration_metric_values)),
                weights=weights,
                thresholds=thresholds,
            )
            sops_stdev = round(pstdev(iteration_scores), 2) if len(iteration_scores) > 1 else 0.0
            sops_min = round(min(iteration_scores), 2) if iteration_scores else None
            sops_max = round(max(iteration_scores), 2) if iteration_scores else None

            # Completion headline — separate axis. NULL when no completion metrics
            # were captured this run.
            c_breakdown = compute_completion(
                _plugin_metrics_from_values(
                    _median_values(completion_metric_values), COMPLETION_METRIC_SOURCES
                ),
                completion_weights,
                completion_thresholds,
            )
            has_completion = bool(c_breakdown.subscores)
            comp_stdev = (
                round(pstdev(completion_scores), 2) if len(completion_scores) > 1 else 0.0
            ) if completion_scores else None

            score_result = ScoreResult(
                    run_id=run_id,
                    sops=breakdown.sops,
                    sops_stdev=sops_stdev,
                    sops_min=sops_min,
                    sops_max=sops_max,
                    subscores=breakdown.subscores,
                    weights_used=breakdown.weights_used,
                    metric_values=breakdown.metric_values,
                    rubric_version=config.get("rubric_version"),
                    derivation_version=DERIVATION_VERSION,
                    completion=c_breakdown.sops if has_completion else None,
                    completion_stdev=comp_stdev if has_completion else None,
                    completion_min=(
                        round(min(completion_scores), 2) if completion_scores else None
                    ),
                    completion_max=(
                        round(max(completion_scores), 2) if completion_scores else None
                    ),
                    completion_subscores=c_breakdown.subscores if has_completion else None,
                    completion_weights_used=c_breakdown.weights_used if has_completion else None,
                    completion_metric_values=c_breakdown.metric_values if has_completion else None,
            )
            session.add(score_result)

            # Record the at-measure score in the (run × methodology) table and stamp
            # the run with the methodology it was interpreted under at capture.
            from .methodology import ensure_current_methodology, record_at_measure

            methodology = ensure_current_methodology(session, config)
            record_at_measure(session, run, score_result, methodology.version)

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
