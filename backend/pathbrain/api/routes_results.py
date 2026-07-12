"""Results endpoints: fetch a run's full detail (metrics + score)."""
from __future__ import annotations

from collections import Counter
from statistics import mean, median

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config_store import get_config
from ..database import get_session
from ..methodology import ensure_current_methodology
from ..interpret.smoothness import longest_void_diagnostic
from ..metrics import has_latest_metrics
from ..raw_access import browser_url_observations
from ..models import BenchmarkResult, Run, RunStatus, Score, ScoreResult
from ..schemas import BenchmarkResultOut, RunBaselineOut, RunDetail, ScoreOut
from ..settings_profile import summarize

router = APIRouter()

# How many recent runs to average into a baseline. Keeps the comparison anchored
# to recent typical behavior rather than a profile's entire history.
BASELINE_RUN_LIMIT = 50


def _serialize_score(score: ScoreResult | None) -> ScoreOut | None:
    if score is None:
        return None
    out = ScoreOut.model_validate(score)
    out.legacy = not has_latest_metrics(score.metric_values)
    return out


def _current_overall(session: Session, run_id: int) -> float | None:
    """The run's Overall under the *current* methodology (``axis_scores['overall']``),
    or None when the run isn't comparable / not yet graded under it. This is the
    first-class headline figure the gauge shows, decoupled from the legacy SOPS."""
    methodology = ensure_current_methodology(session, get_config(session))
    score = session.scalars(
        select(Score).where(
            Score.run_id == run_id,
            Score.methodology_version == methodology.version,
        )
    ).first()
    if score is None or score.comparability == "incomparable":
        return None
    return (score.axis_scores or {}).get("overall")


def _pause_diagnostics(run: Run) -> list[dict] | None:
    """Per-URL "where's the pause?" diagnostic from the browser result's raw observations —
    the single longest void's location + phase + network/render attribution.

    Walks the stored browser raw via ``browser_url_observations`` (the single reader of the
    ``iterations`` → ``urls`` nesting) and aggregates per URL, keeping the **worst (longest) void**
    across iterations as the representative pause. Best-effort: any URL whose raw can't produce a
    void is skipped; None when there's no usable browser raw at all."""
    browser = next((r for r in run.results if r.plugin == "browser"), None)
    by_url: dict[str, dict] = {}
    for _i, url, u in browser_url_observations(getattr(browser, "raw", None)):
        try:
            diag = longest_void_diagnostic(u.get("nav"), u.get("resources"), u.get("paint"), u.get("loaf"))
        except Exception:  # noqa: BLE001 — a diagnostic must never break the run detail page
            diag = None
        if diag is None:
            continue
        prev = by_url.get(url)
        if prev is None or diag["duration_ms"] > prev["duration_ms"]:
            by_url[url] = diag
    out = [{"url": url, **d} for url, d in by_url.items()]
    return out or None


def _serialize_run(run: Run, overall: float | None = None) -> RunDetail:
    return RunDetail(
        id=run.id,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        status=run.status.value if hasattr(run.status, "value") else str(run.status),
        label=run.label,
        notes=run.notes,
        error=run.error,
        iterations=run.iterations,
        iterations_completed=run.iterations_completed,
        per_iteration_ms=run.per_iteration_ms,
        settings_fingerprint=run.settings_fingerprint,
        settings=run.settings,
        config_used=run.config_used,
        results=[BenchmarkResultOut.model_validate(r) for r in run.results],
        score=_serialize_score(run.score),
        overall=overall,
        pause_diagnostics=_pause_diagnostics(run),
    )


@router.get("/results/latest", response_model=RunDetail)
def latest_result(session: Session = Depends(get_session)) -> RunDetail:
    run = session.scalars(
        select(Run)
        .where(Run.status == RunStatus.COMPLETE)
        .order_by(Run.created_at.desc())
        .limit(1)
    ).first()
    if run is None:
        raise HTTPException(status_code=404, detail="No completed runs yet")
    return _serialize_run(run, _current_overall(session, run.id))


@router.get("/results/{run_id}", response_model=RunDetail)
def get_result(run_id: int, session: Session = Depends(get_session)) -> RunDetail:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return _serialize_run(run, _current_overall(session, run.id))


# Cap the raw reads for a profile's pause roll-up — the browser raw is heavy, and the median over
# the most recent N runs is representative without loading a profile's entire history.
PROFILE_PAUSE_RUN_LIMIT = 40


@router.get("/results/profile/{fingerprint}/pauses")
def profile_pause_rollup(fingerprint: str, session: Session = Depends(get_session)) -> dict:
    """Aggregate the per-URL "where's the pause?" diagnostic across a profile's recent runs — the
    profile-level roll-up of the run-detail card. For each URL it reports the typical (median)
    longest void, the dominant phase (where it falls: pre_fcp / fcp_lcp / lcp_load / post_load), and
    the network-vs-render attribution split — so a profile shows WHERE its pauses concentrate and
    WHAT causes them, not just a single run. Reads at most ``PROFILE_PAUSE_RUN_LIMIT`` recent runs."""
    runs = session.scalars(
        select(Run)
        .where(Run.status == RunStatus.COMPLETE, Run.settings_fingerprint == fingerprint)
        .order_by(Run.created_at.desc())
        .limit(PROFILE_PAUSE_RUN_LIMIT)
        .options(selectinload(Run.results))
    ).all()
    by_url: dict[str, dict] = {}
    used = 0
    for run in runs:
        diags = _pause_diagnostics(run)  # per-URL worst void for this run (already iteration-aware)
        if not diags:
            continue
        used += 1
        for d in diags:
            b = by_url.setdefault(d["url"], {"durations": [], "phases": [], "attrs": []})
            b["durations"].append(d["duration_ms"])
            b["phases"].append(d["phase"])
            if d.get("attribution"):
                b["attrs"].append(d["attribution"])
    urls: list[dict] = []
    for url, b in by_url.items():
        durs, phases, attrs = b["durations"], b["phases"], b["attrs"]
        phase = Counter(phases).most_common(1)[0][0]
        net = sum(1 for a in attrs if a == "network")
        rnd = sum(1 for a in attrs if a == "render")
        urls.append(
            {
                "url": url,
                "runs": len(durs),
                "median_void_ms": round(median(durs), 1),
                "phase": phase,
                "phase_fraction": round(phases.count(phase) / len(phases), 2),
                # Dominant cause + the network/render split, so "is this profile's pause shapeable?"
                # is answerable at a glance (network = SQM-movable; render = client CPU it can't move).
                "attribution": Counter(attrs).most_common(1)[0][0] if attrs else None,
                "network_fraction": round(net / len(attrs), 2) if attrs else None,
                "render_fraction": round(rnd / len(attrs), 2) if attrs else None,
            }
        )
    urls.sort(key=lambda x: -x["median_void_ms"])
    return {"fingerprint": fingerprint, "runs": used, "run_cap": PROFILE_PAUSE_RUN_LIMIT, "urls": urls}


def _average_metrics(
    session: Session, runs: list[Run], exclude_run_id: int
) -> tuple[dict, int]:
    """Mean of each numeric plugin metric across ``runs`` (excluding one run).

    Returns ``(metrics, run_count)`` where ``metrics`` maps plugin -> {key: mean}
    and ``run_count`` is the number of runs that contributed at least one value.

    Reads only ``(run_id, plugin, metrics)`` in a single query rather than walking
    ``run.results`` (which lazy-loads each full result row — including the large,
    unused ``raw``/``details`` JSON blobs — one query per run).
    """
    run_ids = [r.id for r in runs if r.id != exclude_run_id]
    if not run_ids:
        return {}, 0
    rows = session.execute(
        select(BenchmarkResult.run_id, BenchmarkResult.plugin, BenchmarkResult.metrics).where(
            BenchmarkResult.run_id.in_(run_ids)
        )
    ).all()
    samples: dict[str, dict[str, list[float]]] = {}
    contributing: set[int] = set()
    for rid, plugin, res_metrics in rows:
        for key, value in (res_metrics or {}).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            samples.setdefault(plugin, {}).setdefault(key, []).append(float(value))
            contributing.add(rid)
    metrics = {
        plugin: {key: round(mean(vals), 3) for key, vals in keyed.items() if vals}
        for plugin, keyed in samples.items()
    }
    return metrics, len(contributing)


@router.get("/results/{run_id}/baseline", response_model=RunBaselineOut)
def get_result_baseline(
    run_id: int, session: Session = Depends(get_session)
) -> RunBaselineOut:
    """Average plugin metrics for the *best-scoring* settings profile, for comparison.

    The useful question on a run isn't "how does this compare to its own profile"
    (that's circular) but "how far is it from the best configuration I've found".
    So the baseline is the settings profile with the highest median SOPS: each
    metric arrow then shows whether this run beats — or trails — the best profile.

    Confident profiles (>= ``correlation.min_runs`` runs) are preferred when any
    exist, so a single fluky run can't define "best". When no profiles with
    captured settings exist, falls back to the most recent completed runs. The
    viewed run is always excluded so it isn't compared against itself.
    """
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    min_runs = int((get_config(session).get("correlation", {}) or {}).get("min_runs", 5) or 5)

    # Group completed, scored runs by settings profile.
    rows = session.execute(
        select(Run, ScoreResult.sops)
        .join(ScoreResult, ScoreResult.run_id == Run.id)
        .where(Run.status == RunStatus.COMPLETE, Run.settings_fingerprint.is_not(None))
        .order_by(Run.created_at.desc())
    ).all()
    groups: dict[str, dict] = {}
    for r, sops in rows:
        g = groups.setdefault(
            r.settings_fingerprint, {"runs": [], "sops": [], "settings": r.settings}
        )
        if len(g["runs"]) < BASELINE_RUN_LIMIT:
            g["runs"].append(r)
        g["sops"].append(sops)

    # Prefer confident profiles when we have any; otherwise consider them all.
    confident = {fp: g for fp, g in groups.items() if len(g["sops"]) >= min_runs}
    candidates = confident or groups

    best_fp: str | None = None
    best_median: float | None = None
    for fp, g in candidates.items():
        med = median(g["sops"])
        if best_median is None or med > best_median:
            best_fp, best_median = fp, med

    if best_fp is not None:
        best = groups[best_fp]
        metrics, count = _average_metrics(session, best["runs"], exclude_run_id=run_id)
        if count > 0:
            return RunBaselineOut(
                run_id=run_id,
                scope="best_profile",
                profile_fingerprint=best_fp,
                profile_label=summarize(best["settings"]),
                profile_median_sops=round(best_median, 2),
                is_best_profile=run.settings_fingerprint == best_fp,
                run_count=count,
                metrics=metrics,
            )

    # Fallback: no usable profile (settings never captured, or the best profile
    # only contains this run) — compare against recent completed runs instead.
    recent = list(
        session.scalars(
            select(Run)
            .where(Run.status == RunStatus.COMPLETE, Run.id != run_id)
            .order_by(Run.created_at.desc())
            .limit(BASELINE_RUN_LIMIT)
        ).all()
    )
    metrics, count = _average_metrics(session, recent, exclude_run_id=run_id)
    return RunBaselineOut(
        run_id=run_id,
        scope="all",
        profile_fingerprint=None,
        profile_label=None,
        profile_median_sops=None,
        is_best_profile=False,
        run_count=count,
        metrics=metrics,
    )
