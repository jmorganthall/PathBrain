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
from .raw_access import browser_url_observations, stored_iterations
from .scoring import (
    COMPLETION_METRIC_SOURCES,
    METRIC_SOURCES,
    compute_completion,
    compute_score,
)

log = get_logger("runner")

MAX_ITERATIONS = 500

# Above this many iterations, a single logical request is split into a series of
# runs of at most ``CHUNK_ITERATIONS`` each, so an interrupted long series still
# persists every completed chunk instead of losing the whole thing. Runs of
# ``CHUNK_ITERATIONS`` or fewer execute as a single run (the historical behaviour).
CHUNK_ITERATIONS = 5


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


def run_chunk(label: str | None, notes: str | None, iterations: int) -> tuple[int, bool, int]:
    """Create one run of ``iterations`` and execute it (blocking). Returns
    ``(run_id, ok, iterations_completed)`` where ``ok`` is True iff the run finished
    COMPLETE.

    The building block for a chunked series (manual large runs, the timed
    "test current" engine): the caller loops this under a held coordinator lock,
    so each chunk is persisted the moment it finishes. ``execute_run`` never
    raises — it records failures on the row — so ``ok`` is read back from status.
    """
    run_id = create_run(label=label, notes=notes, iterations=iterations)
    execute_run(run_id)
    with session_scope() as session:
        run = session.get(Run, run_id)
        completed = int(run.iterations_completed or 0) if run else 0
        ok = bool(run and run.status == RunStatus.COMPLETE)
    return run_id, ok, completed


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
        raws = stored_iterations(res.raw)
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


def _derivation_matches(a, b, *, rel: float = 1e-4, absr: float = 1e-6) -> bool:
    """Do a stored and a freshly re-derived metric value agree? Both missing → yes; one
    missing → no; numbers compared with a tiny tolerance (rounding across store/reload),
    everything else by equality."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= max(absr, rel * max(abs(float(a)), abs(float(b))))
    return a == b


def verify_run_derivation(run, artifact_base: str | None = None) -> dict:
    """**Read-only** integrity audit: re-derive every metric from the run's immutable raw and
    diff it against the value currently stored on the run's plugin results.

    This is the ground-truth test for "are we keeping the same data the same": a metric whose
    stored value doesn't reproduce from raw under the *current* derivation is a run carrying a
    **stale-formula value** — captured (or last derived) under an older ``DERIVATION_VERSION`` and
    never re-derived, so it's no longer like-for-like with fresh runs. Mutates nothing (unlike
    ``rederive_run``); it only reports. ``drift`` lists every metric that disagrees."""
    report: dict = {
        "run_id": run.id,
        "current_derivation": DERIVATION_VERSION,
        # The derivation the stored values were last produced under (None if never scored).
        "stored_derivation": getattr(run.score, "derivation_version", None) if run.score else None,
        "plugins": [],
        "drift": [],
        "checked": 0,
    }
    for res in run.results:
        raws = stored_iterations(res.raw)
        per_iter = [derive(res.plugin, r or {}, artifact_base) for r in raws]
        rederived = _median_values(per_iter) if per_iter else {}
        stored = res.metrics or {}
        rows = []
        for key in sorted(set(rederived) | set(stored)):
            sv, rv = stored.get(key), rederived.get(key)
            match = _derivation_matches(sv, rv)
            report["checked"] += 1
            delta = (
                round(float(rv) - float(sv), 4)
                if isinstance(sv, (int, float)) and isinstance(rv, (int, float))
                else None
            )
            row = {"key": key, "stored": sv, "rederived": rv, "match": match, "delta": delta}
            rows.append(row)
            if not match:
                report["drift"].append({"plugin": res.plugin, **row})
        report["plugins"].append({"plugin": res.plugin, "metrics": rows})
    report["consistent"] = not report["drift"]
    return report


def browser_collection_shape(runs: list) -> dict:
    """Summarize **what a cohort of runs actually collected** in their browser raw — the
    *ingredients*, not the derived metrics.

    The derivation audit proves the recipe (raw → metric) is unchanged; this exposes the thing it
    can't see: whether the *raw itself* is the same kind of measurement over time. Different URL
    set, LoAF added/dropped, or a shift in page composition (resource counts) all mean old and new
    runs aren't measuring the same thing — even when each one faithfully reproduces from its own
    raw. Returns the URL set, LoAF coverage + sources, and the median resource count per URL."""
    from collections import defaultdict

    url_runs: dict[str, int] = defaultdict(int)      # runs that loaded each URL
    url_resources: dict[str, list] = defaultdict(list)  # per-observation resource counts by URL
    obs_total = 0
    loaf_present = 0
    loaf_sources: set[str] = set()
    for run in runs:
        seen_urls: set[str] = set()
        for res in getattr(run, "results", None) or []:
            if res.plugin != "browser":
                continue
            for _i, url, obs in browser_url_observations(res.raw):
                obs_total += 1
                seen_urls.add(url)
                url_resources[url].append(len(obs.get("resources") or []))
                loaf = obs.get("loaf")
                src = loaf.get("source") if isinstance(loaf, dict) else None
                if src:
                    loaf_present += 1
                    loaf_sources.add(str(src))
        for u in seen_urls:
            url_runs[u] += 1
    return {
        "runs": len(runs),
        "urls": sorted(url_runs),
        "loaf_present_frac": round(loaf_present / obs_total, 3) if obs_total else 0.0,
        "loaf_sources": sorted(loaf_sources),
        "median_resources": {u: round(median(v), 1) for u, v in url_resources.items() if v},
    }


def compare_collection_shapes(old: dict, new: dict) -> dict:
    """Diff two cohorts' collection shapes into a plain verdict: which URLs appeared/disappeared,
    whether LoAF coverage flipped, and any URL whose page composition (median resource count)
    shifted materially. ``changed`` is the headline — did the raw ingredients drift old→new?"""
    old_urls, new_urls = set(old.get("urls") or []), set(new.get("urls") or [])
    added = sorted(new_urls - old_urls)
    removed = sorted(old_urls - new_urls)
    # LoAF coverage: a material flip (e.g. old runs predate LoAF capture) or a different source set.
    lo, ln = old.get("loaf_present_frac", 0.0), new.get("loaf_present_frac", 0.0)
    loaf_changed = abs(lo - ln) >= 0.25 or set(old.get("loaf_sources") or []) != set(new.get("loaf_sources") or [])
    # Page composition: for URLs in both cohorts, flag a >20% (and ≥2-resource) shift in the median.
    om, nm = old.get("median_resources") or {}, new.get("median_resources") or {}
    resource_shift = {}
    for u in sorted(old_urls & new_urls):
        a, b = om.get(u), nm.get(u)
        if a is None or b is None:
            continue
        if abs(a - b) >= 2 and abs(a - b) / max(a, b, 1) > 0.2:
            resource_shift[u] = {"old": a, "new": b}
    changed = bool(added or removed or loaf_changed or resource_shift)
    return {
        "urls_added": added,
        "urls_removed": removed,
        "loaf_changed": loaf_changed,
        "loaf_present": {"old": lo, "new": ln},
        "resource_shift": resource_shift,
        "changed": changed,
    }


def _iteration_plugin_metrics_from_raw(run, artifact_base: str | None) -> list[dict[str, dict]]:
    """Re-derive each iteration's ``plugin -> metrics`` from the run's stored raw."""
    by_plugin: dict[str, list[dict]] = {}
    n_iters = 0
    for res in run.results:
        raws = stored_iterations(res.raw)
        per_iter = [derive(res.plugin, r or {}, artifact_base) for r in raws]
        by_plugin[res.plugin] = per_iter
        n_iters = max(n_iters, len(per_iter))
    return [
        {p: pi[i] for p, pi in by_plugin.items() if i < len(pi) and pi[i]}
        for i in range(n_iters)
    ]


def score_metrics_under(session, run_id, run_methodology_version, methodology, iter_metrics):
    """Score per-iteration ``{plugin: metrics}`` under a methodology's frozen rubric,
    across every axis it defines, and upsert the (run × methodology) Score.

    Generic multi-axis: each axis is scored with ``compute_score`` over its own
    metric sources/weights/thresholds, so Speed/Smoothness/Stability/Completion (or
    any future axes) all fall out of the definition. ``is_at_measure`` is set when the
    methodology matches the run's capture-time version. Returns the Score, or ``None``
    when nothing was scorable. Caller commits.
    """
    from statistics import pstdev as _pstdev

    from .methodology import (
        axis_rubric,
        comparability,
        overall_from_definition,
        scored_axes,
        upsert_score,
    )

    definition = methodology.definition or {}
    axes = scored_axes(definition)
    if not axes or not iter_metrics:
        return None

    axis_scores: dict[str, float] = {}
    bands: dict[str, dict] = {}
    subscores: dict[str, float] = {}
    weights_used: dict[str, float] = {}
    metric_values: dict[str, float] = {}

    for axis in axes:
        sources, weights, thresholds = axis_rubric(definition, axis["key"])
        per_iter = [compute_score(im, weights, thresholds, sources) for im in iter_metrics]
        head = compute_score(
            _plugin_metrics_from_values(
                _median_values([b.metric_values for b in per_iter]), sources
            ),
            weights,
            thresholds,
            sources,
        )
        metric_values.update(head.metric_values)
        if not head.subscores:
            continue  # this axis captured none of its metrics on this run
        axis_scores[axis["key"]] = head.sops
        subscores.update(head.subscores)
        weights_used.update(head.weights_used)
        scores = [b.sops for b in per_iter if b.subscores]
        if scores:
            bands[axis["key"]] = {
                "stdev": round(_pstdev(scores), 2) if len(scores) > 1 else 0.0,
                "min": round(min(scores), 2),
                "max": round(max(scores), 2),
            }

    # First-class Overall: the methodology's headline roll-up (corner over the feel-trinity
    # subscores), computed once here so capture *and* re-grade persist it identically and
    # the settings/crown layer never has to recompute it. Stored alongside the axis scores
    # (it's a derived headline, not a scored axis, so it carries no rubric of its own).
    overall = overall_from_definition(definition, subscores)
    if overall is not None:
        axis_scores["overall"] = overall

    comp_tag, missing = comparability(definition, metric_values)
    return upsert_score(
        session,
        run_id,
        methodology.version,
        is_at_measure=(run_methodology_version == methodology.version),
        axis_scores=axis_scores,
        subscores=subscores,
        weights_used=weights_used,
        metric_values=metric_values,
        bands=bands,
        comparability=comp_tag,
        missing_metrics=missing or None,
    )


def _cached_iteration_metrics(session, run_id: int) -> list[dict[str, dict]] | None:
    """Per-iteration ``{plugin: metrics}`` from the cached ``details['iteration_metrics']``,
    read **without loading the large ``raw`` column** — the fast path for re-grade.

    Every run persists its per-iteration derived metrics in ``details`` (at collection and
    on every re-derive), so when a run's metrics are already current there's no need to
    re-derive from raw *or* pay the cost of deserializing the raw blobs. Returns ``None``
    if any result is missing the cache, so the caller falls back to a raw re-derivation."""
    rows = session.execute(
        select(BenchmarkResult.plugin, BenchmarkResult.details).where(
            BenchmarkResult.run_id == run_id
        )
    ).all()
    if not rows:
        return None
    by_plugin: dict[str, list[dict]] = {}
    n_iters = 0
    for plugin, details in rows:
        per_iter = (details or {}).get("iteration_metrics")
        if per_iter is None:
            return None  # incomplete cache → re-derive from raw instead
        by_plugin[plugin] = per_iter
        n_iters = max(n_iters, len(per_iter))
    return [
        {p: pi[i] for p, pi in by_plugin.items() if i < len(pi) and pi[i]}
        for i in range(n_iters)
    ]


def score_run_under(session, run, methodology, artifact_base: str | None = None):
    """Score a run from its preserved raw under a methodology, writing a Score row
    (the at-present record). Never mutates a different version's at-measure row.

    Scores every axis the methodology defines (Speed/Smoothness/…). **Fast path:** when the
    run's cached metrics were already derived under the current derivation, they're reused
    straight from ``details['iteration_metrics']`` — no ``derive()`` calls and no raw-blob
    load. This is the case right after a re-derive, and for any threshold-only (re-anchor)
    re-grade, where the derivation is unchanged. Only stale/never-derived runs pay the full
    raw re-derivation. Returns the Score, or ``None`` when the methodology has no recorded
    definition or the run has no derivable metrics."""
    if not (methodology.definition or {}).get("metrics"):
        return None  # pre-foundation methodology — definition not recorded
    iter_metrics: list[dict] | None = None
    # Reuse the cache only when it reflects the *current* derivation (so a metric added by a
    # newer derive-vN isn't silently missing). ``run.score`` is the legacy ScoreResult, whose
    # derivation_version a re-derive stamps to DERIVATION_VERSION; loading it touches no raw.
    if run.score is not None and run.score.derivation_version == DERIVATION_VERSION:
        iter_metrics = _cached_iteration_metrics(session, run.id)
    if not iter_metrics:
        iter_metrics = _iteration_plugin_metrics_from_raw(run, artifact_base)  # slow fallback
    if not iter_metrics:
        return None  # no metrics to interpret
    return score_metrics_under(session, run.id, run.methodology_version, methodology, iter_metrics)


# Commit the re-grade in batches of this many runs (each run is savepoint-isolated), so a
# long pass isn't bottlenecked on a per-run fsync while staying resumable.
_REGRADE_COMMIT_EVERY = 100


def score_history_under_current(session, progress=None) -> dict:
    """Score every completed run from raw under the current methodology (Phase 3
    re-grade). Writes new/refreshed Score rows; leaves other versions' at-measure
    rows untouched. Returns a comparability summary.

    Each run is committed as it's scored (not one all-or-nothing commit at the end)
    and wrapped in its own try/except, so the pass is **resumable and robust**: a
    slow run, a client/proxy timeout, or one run with malformed raw can't discard
    the progress already made — re-running simply continues. ``skipped`` counts runs
    whose raw can't be re-derived (e.g. captured before raw storage, or no browser
    raw for the required metric); ``errors`` counts runs that raised.

    ``progress`` is an optional ``callable(current, total, message)`` invoked after
    each run, so a background job can report live progress to the jobs feed.
    """
    from .config_store import get_config
    from .methodology import ensure_current_methodology

    methodology = ensure_current_methodology(session, get_config(session))
    session.commit()  # persist the methodology row before the (possibly long) loop
    artifact_base = os.path.abspath(get_settings().artifact_dir)
    run_ids = list(session.scalars(select(Run.id).where(Run.status == RunStatus.COMPLETE)))
    total = len(run_ids)
    counts = {"exact": 0, "partial": 0, "incomparable": 0, "scored": 0, "skipped": 0, "errors": 0}
    # Commit in batches instead of once per run: a per-run commit fsyncs on every one of
    # (potentially thousands of) runs, which dominated the wall-clock. Each run is wrapped in
    # a SAVEPOINT so a single malformed run rolls back only itself, not the whole batch — so
    # the pass stays resumable/robust while committing ~N× less often.
    for i, run_id in enumerate(run_ids):
        try:
            with session.begin_nested():  # per-run savepoint
                run = session.get(Run, run_id)
                score = score_run_under(session, run, methodology, artifact_base)
                if score is None:
                    counts["skipped"] += 1
                else:
                    counts["scored"] += 1
                    counts[score.comparability] = counts.get(score.comparability, 0) + 1
        except Exception:  # noqa: BLE001 — isolate a bad run; never abort the whole pass
            log.exception("Re-grade failed for run %s; skipping", run_id)
            counts["errors"] += 1
        if (i + 1) % _REGRADE_COMMIT_EVERY == 0:
            session.commit()
        if progress is not None:
            progress(i + 1, total, f"scored {counts['scored']}/{total}")
    session.commit()  # flush the final partial batch
    log.info("Re-graded %s run(s) under %s: %s", counts["scored"], methodology.version, counts)
    return {"methodology": methodology.version, "total": total, **counts}


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
    plugins: list[BenchmarkPlugin] = []
    try:
        with session_scope() as session:
            run = session.get(Run, run_id)
            if run is None:
                log.error("Run %s not found", run_id)
                return
            # A run can be cancelled while it's still queued behind the coordination
            # lock (status flipped to FAILED by /runs/{id}/cancel). Don't execute it —
            # otherwise a "cancelled" run would still run and write results (dirty data).
            if run.status != RunStatus.PENDING:
                log.info("Run %s no longer pending (%s) — skipping execution", run_id, run.status)
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

            plugins = iter_plugins()
            per_plugin: dict[str, list[PluginResult]] = {p.name: [] for p in plugins}
            # Per-plugin iteration count: a plugin's config section may cap how many of the
            # run's iterations it actually runs (e.g. browser.iterations < the cheap probes),
            # so the heavy browser samples fewer times. Headline metric medians use every
            # captured sample (skip-missing) so a capped plugin stays unbiased; only the
            # legacy SOPS confidence band is restricted to full-suite rounds (below).
            def _plugin_count(name: str) -> int:
                cap = (config.get(name, {}) or {}).get("iterations")
                if cap is None:
                    return iterations
                try:
                    return max(1, min(iterations, int(cap)))
                except (TypeError, ValueError):
                    return iterations

            plugin_counts = {p.name: _plugin_count(p.name) for p in plugins}
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
            # Per-iteration {plugin: metrics}, fed to the methodology-aware scorer for
            # the at-measure Score row (the new (run × methodology) record).
            iteration_plugin_metrics: list[dict] = []

            for i in range(iterations):
                it_start = perf_counter()
                log.info("Run %s: iteration %s/%s", run_id, i + 1, iterations)
                iter_metrics: dict[str, dict] = {}
                ran_full_suite = True
                for plugin in plugins:
                    if i >= plugin_counts[plugin.name]:
                        ran_full_suite = False  # this plugin opted out of this round
                        continue
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
                iteration_plugin_metrics.append(iter_metrics)
                b = compute_score(iter_metrics, weights=weights, thresholds=thresholds)
                # Only full-suite rounds feed the SOPS confidence band — a plugin running
                # fewer iterations would otherwise make the band heterogeneous (its metrics
                # still land in the headline median via skip-missing).
                if ran_full_suite:
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

            # Record the at-measure score in the (run × methodology) table under the
            # current methodology, scored across all its axes (Speed/Smoothness/…),
            # and stamp the run with the methodology it was interpreted under.
            from .methodology import ensure_current_methodology

            methodology = ensure_current_methodology(session, config)
            run.methodology_version = methodology.version
            score_metrics_under(
                session, run_id, methodology.version, methodology, iteration_plugin_metrics
            )

            run.per_iteration_ms = (
                round(mean(iteration_durations), 3) if iteration_durations else None
            )

            # Read-after integrity check: confirm the firewall settings under test
            # never changed mid-run. The start fingerprint was captured above; if a
            # re-read now yields a *different* one, something (another tuner, or a
            # direct OPNsense edit) moved the config while we were measuring, so what
            # we tested isn't what we thought — discard the run. Best-effort: only a
            # confirmed mismatch fails it (a failed re-read can't prove drift).
            start_fp = run.settings_fingerprint
            end_fp = None
            try:
                from .providers import get_provider
                from .settings_profile import fingerprint, normalize

                end_fp = fingerprint(normalize(get_provider().discover()))
            except Exception:  # noqa: BLE001 — can't prove drift; don't punish the run
                log.warning("Run %s: post-run settings re-read failed", run_id, exc_info=True)

            if start_fp and end_fp and start_fp != end_fp:
                run.status = RunStatus.FAILED
                run.error = (
                    f"Firewall settings changed mid-run ({start_fp} → {end_fp}); "
                    "measurement discarded."
                )
                run.finished_at = datetime.now(timezone.utc)
                session.commit()
                log.warning(
                    "Run %s failed integrity check: settings drifted %s → %s",
                    run_id, start_fp, end_fp,
                )
                return

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
    finally:
        # Release per-run plugin resources (e.g. the reused Chromium) so nothing leaks
        # between runs. Never raises.
        for plugin in plugins:
            try:
                plugin.teardown()
            except Exception:  # noqa: BLE001 — teardown must never break a run
                log.warning(
                    "Run %s: plugin '%s' teardown failed", run_id, plugin.name, exc_info=True
                )
