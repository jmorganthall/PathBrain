"""Settings-vs-responsiveness correlation endpoints.

Groups completed runs by the firewall/SQM profile that was live when they ran,
and flags the most recent settings change when it moved the median SOPS beyond a
configurable threshold.
"""
from __future__ import annotations

from math import sqrt
from statistics import median, quantiles

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import profile_test as profile_test_mod
from ..config_store import get_config
from ..database import get_session
from ..logging_config import get_logger
from ..methodology import ensure_current_methodology
from ..metrics import all_metric_sources
from ..models import Run, RunStatus, Score
from ..providers import get_provider
from ..runner import MAX_ITERATIONS
from ..scoring import COMPLETION_METRIC_SOURCES
from ..settings_profile import diff_profiles, fingerprint, normalize, plan_apply, summarize
from ..trends import RunPoint, profile_relative

# The three headline axes (the temporal phases of a load) whose 0–100 scores define the
# Overall "corner" score under methodology speed-smoothness-v4; the crowned "best"
# profile is the confident one closest to the perfect (100, 100, 100) corner.
_CORNER_AXES = ("responsiveness", "smoothness", "speed")

# Non-metric numeric fields the /api/metrics catalog doesn't describe, exposed so the
# UI's chart-axis pickers + column selector can offer them (metric fields get their
# metadata from the catalog). higher_is_better drives the "↑/↓ better" hints.
_PROFILE_FIELDS = [
    {"key": "overall", "label": "Overall (corner)", "unit": "score", "higher_is_better": True, "group": "Scores"},
    {"key": "responsiveness", "label": "Responsiveness", "unit": "score", "higher_is_better": True, "group": "Scores"},
    {"key": "smoothness", "label": "Smoothness", "unit": "score", "higher_is_better": True, "group": "Scores"},
    {"key": "speed", "label": "Speed", "unit": "score", "higher_is_better": True, "group": "Scores"},
    {"key": "stability", "label": "Stability", "unit": "score", "higher_is_better": True, "group": "Scores"},
    {"key": "completion", "label": "Completion", "unit": "score", "higher_is_better": True, "group": "Scores"},
    {"key": "iterations", "label": "Iterations", "unit": "", "higher_is_better": True, "group": "Run stats"},
    {"key": "count", "label": "Runs", "unit": "", "higher_is_better": True, "group": "Run stats"},
    {"key": "relative_smoothness", "label": "vs typical", "unit": "", "higher_is_better": True, "group": "Run stats"},
]


def _corner_overall(scores: dict) -> float | None:
    """0–100 'closeness to the perfect corner' over the three headline axes
    (Responsiveness/Smoothness/Speed): 100 at all-100, 0 at all-0. Ranks identically to
    smallest distance to the corner, so the highest-``overall`` confident profile is
    'best'. Returns None unless every corner axis is present."""
    vals = [scores.get(a) for a in _CORNER_AXES]
    if any(v is None for v in vals):
        return None
    dist = sqrt(sum((100.0 - v) ** 2 for v in vals))
    return round(100.0 - dist / sqrt(len(_CORNER_AXES)), 1)

router = APIRouter()
log = get_logger("api.settings")


def _comparable(score: Score) -> bool:
    # A run is comparable once it has a Score under the current methodology that
    # isn't "incomparable" (i.e. its raw can supply the required metrics).
    return score.comparability != "incomparable"


def _min_runs(session: Session) -> int:
    return int((get_config(session).get("correlation", {}) or {}).get("min_runs", 5) or 5)


def _min_iterations(session: Session) -> int:
    """Total iterations a profile needs before it counts as confident (the unit of
    signal — a 15-iteration run is worth far more than a 1-iteration one)."""
    return int((get_config(session).get("correlation", {}) or {}).get("min_iterations", 15) or 15)


def _spread(vals: list[float]) -> dict:
    vals = sorted(vals)
    med = round(median(vals), 2)
    if len(vals) >= 2:
        q = quantiles(vals, n=4)
        p25, p75 = round(q[0], 2), round(q[2], 2)
    else:
        p25 = p75 = med
    return {
        "median": med,
        "p25": p25,
        "p75": p75,
        "min": round(min(vals), 2),
        "max": round(max(vals), 2),
    }


def _completed_runs_with_scores(session: Session):
    """Chronological (Run, Score) for completed runs with settings, scored under the
    current methodology."""
    methodology = ensure_current_methodology(session, get_config(session))
    return session.execute(
        select(Run, Score)
        .join(Score, Score.run_id == Run.id)
        # Eager-load each run's plugin results so per-profile metric medians (every
        # numeric value we collect, incl. display-only) can be aggregated without N+1.
        .options(selectinload(Run.results))
        .where(
            Run.status == RunStatus.COMPLETE,
            Run.settings_fingerprint.is_not(None),
            Score.methodology_version == methodology.version,
        )
        .order_by(Run.created_at)
    ).all()


@router.get("/settings/diagnostics")
def settings_diagnostics(session: Session = Depends(get_session)) -> dict:
    """Visibility into settings capture: how many runs are stamped, how many
    distinct fingerprints, and the most recent runs with their fingerprints.

    Lets us tell apart "old runs never captured" (lots of nulls) from "fingerprint
    changes every run" (lots of distinct fingerprints).
    """
    completed = session.scalars(
        select(Run).where(Run.status == RunStatus.COMPLETE).order_by(Run.created_at.desc())
    ).all()
    stamped = [r for r in completed if r.settings_fingerprint]
    distinct = {r.settings_fingerprint for r in stamped}
    # How many completed runs are comparable under the current methodology.
    methodology = ensure_current_methodology(session, get_config(session))
    with_latest = sum(
        1
        for score in session.scalars(
            select(Score)
            .join(Run, Run.id == Score.run_id)
            .where(
                Run.status == RunStatus.COMPLETE,
                Score.methodology_version == methodology.version,
            )
        )
        if _comparable(score)
    )
    recent = [
        {
            "id": r.id,
            "created_at": r.created_at.isoformat(),
            "label": r.label,
            "fingerprint": r.settings_fingerprint,
        }
        for r in completed[:15]
    ]
    return {
        "total_completed": len(completed),
        "stamped": len(stamped),
        "unstamped": len(completed) - len(stamped),
        "distinct_profiles": len(distinct),
        "with_latest_metrics": with_latest,
        "legacy_metrics": len(completed) - with_latest,
        "recent": recent,
    }


@router.post("/settings/backfill")
def backfill_settings(session: Session = Depends(get_session)) -> dict:
    """Stamp the *current* firewall settings onto completed runs that have none.

    Use when historical runs predate settings-capture (or ran while discovery was
    failing) AND the firewall hasn't changed since — they then aggregate into the
    current profile. Only touches runs with no captured settings.
    """
    provider = get_provider()
    try:
        normalized = normalize(provider.discover())
        fp = fingerprint(normalized)
    except Exception as exc:  # noqa: BLE001
        log.exception("Backfill discovery failed")
        raise HTTPException(
            status_code=502, detail=f"{provider.name} discovery failed: {type(exc).__name__}: {exc}"
        ) from exc

    runs = session.scalars(
        select(Run).where(Run.status == RunStatus.COMPLETE, Run.settings_fingerprint.is_(None))
    ).all()
    for run in runs:
        run.settings = normalized
        run.settings_fingerprint = fp
    session.commit()
    return {"updated": len(runs), "fingerprint": fp}


@router.get("/settings/profiles")
def settings_profiles(
    session: Session = Depends(get_session),
    complete_only: bool = Query(
        True, description="Only aggregate runs with the latest (paint) SOPS metrics."
    ),
    tz_offset: int = Query(
        0, description="Minutes to add to UTC for viewer-local time (day/hour baselines)."
    ),
) -> dict:
    """One row per distinct settings profile, with its SOPS distribution.

    By default only runs scored under the latest (paint-capturing) rubric are
    counted, so legacy runs with a thinner metric set don't inflate/skew a
    profile's SOPS. Set ``complete_only=false`` to include everything. Profiles
    with no qualifying runs drop out entirely.

    Each profile also carries ``relative_sops``: its SOPS *time-adjusted* against the
    day-of-week × hour-of-day baseline of this same population — "is this config
    performing above or below the historical norm for the times it actually ran".
    This is the fair comparator: it strips out the confound of a config happening to
    be sampled more during congested hours.

    Also returns ``best_diff``: how the best (top confident) profile differs from
    the next-ranked one — the at-a-glance "what changed and did it help" view.
    """
    min_runs = _min_runs(session)
    min_iterations = _min_iterations(session)
    min_samples = int((get_config(session).get("trends", {}) or {}).get("min_samples", 3) or 3)
    rows = _completed_runs_with_scores(session)
    # Config-blind baseline: every qualifying run, regardless of profile, defines
    # the time-of-day environment each profile's runs are judged against.
    baseline_points: list[RunPoint] = []
    groups: dict[str, dict] = {}
    metric_src = all_metric_sources()  # {logical_key: (plugin, source_key)} for every metric
    for run, score in rows:
        comparable = _comparable(score)
        if complete_only and not comparable:
            continue
        axes = (score.axis_scores or {}) if comparable else {}
        smooth, speed, comp_axis = axes.get("smoothness"), axes.get("speed"), axes.get("completion")
        # Smoothness is the primary ranking; the time baseline is built on it.
        point = RunPoint(created_at=run.created_at, values={"smoothness": smooth})
        baseline_points.append(point)
        g = groups.setdefault(
            run.settings_fingerprint,
            {
                "fingerprint": run.settings_fingerprint,
                "settings": run.settings,
                "smoothness": [],
                "speed": [],
                "points": [],
                "iterations": 0,
                "completion": [],
                "completion_iterations": 0,
                "completion_metrics": {m: [] for m in COMPLETION_METRIC_SOURCES},
                # Per-axis 0–100 score samples (speed/smoothness/stability/completion)…
                "axis_samples": {},
                # …and per-metric raw value samples (every numeric value we collect).
                "metric_samples": {},
                "first_seen": run.created_at,
                "last_seen": run.created_at,
            },
        )
        if smooth is not None:
            g["smoothness"].append(smooth)
        if speed is not None:
            g["speed"].append(speed)
        g["points"].append(point)
        # A run with more iterations is more data; track the total alongside runs.
        g["iterations"] += int(run.iterations or 1)
        if comp_axis is not None:
            g["completion"].append(comp_axis)
            g["completion_iterations"] += int(run.iterations or 1)
        mv = score.metric_values or {}
        for m in COMPLETION_METRIC_SOURCES:
            if mv.get(m) is not None:
                g["completion_metrics"][m].append(float(mv[m]))
        # All axis scores (0–100) for this run → per-axis samples.
        for axis_key, val in (axes or {}).items():
            if val is not None:
                g["axis_samples"].setdefault(axis_key, []).append(float(val))
        # Every metric's raw value for this run, from the plugin metric caches.
        results_by_plugin = {r.plugin: (r.metrics or {}) for r in run.results}
        for key, (plugin, source_key) in metric_src.items():
            val = results_by_plugin.get(plugin, {}).get(source_key)
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            g["metric_samples"].setdefault(key, []).append(float(val))
        g["settings"] = run.settings
        g["last_seen"] = run.created_at

    profiles = []
    for g in groups.values():
        count = len(g["smoothness"])
        if count == 0:
            continue  # nothing comparable to rank
        comp = g["completion"]
        # Per-axis medians (0–100) and the corner "overall" derived from them.
        scores = {axis: round(median(vals), 2) for axis, vals in g["axis_samples"].items() if vals}
        overall = _corner_overall(scores)
        # Per-metric medians — every numeric value we collect, for the chart + columns.
        metrics = {key: round(median(vals), 3) for key, vals in g["metric_samples"].items() if vals}
        rel = profile_relative(baseline_points, g["points"], "smoothness", tz_offset, min_samples)
        profiles.append(
            {
                "fingerprint": g["fingerprint"],
                "label": summarize(g["settings"]),
                "settings": g["settings"],
                "count": count,
                "iterations": g["iterations"],
                # Confidence is gated on total iterations (the unit of signal), not
                # run count.
                "confident": g["iterations"] >= min_iterations,
                "first_seen": g["first_seen"].isoformat(),
                "last_seen": g["last_seen"].isoformat(),
                # Primary ranking is Smoothness (top-level median/p25/p75/min/max).
                **_spread(g["smoothness"]),
                # Speed shown alongside (the other headline axis).
                "speed": _spread(g["speed"]) if g["speed"] else None,
                # Per-axis medians + the single corner "overall" (closeness to 100,100).
                "scores": scores,
                "overall": overall,
                # Every numeric value we collect, median over the profile's runs.
                "metrics": metrics,
                # Time-adjusted Smoothness: above/below the day×hour historical norm.
                "relative_sops": rel,
                # Completion axis, gated like SOPS: only confident with enough runs
                # that actually captured its metrics.
                "completion": (
                    {
                        "count": len(comp),
                        "iterations": g["completion_iterations"],
                        "confident": g["completion_iterations"] >= min_iterations,
                        **_spread(comp),
                    }
                    if comp
                    else None
                ),
                "completion_metrics": {
                    m: {"median": round(median(v), 1), "count": len(v)}
                    for m, v in g["completion_metrics"].items()
                    if v
                },
            }
        )
    # Rank by the corner "overall" (closeness to fastest+smoothest); profiles missing
    # it (no speed or smoothness yet) fall back to smoothness median, then sort last.
    profiles.sort(key=lambda p: (p["overall"] is not None, p["overall"] if p["overall"] is not None else p["median"]), reverse=True)

    # "Best" = the confident profile closest to the top-right corner (highest overall).
    best_fingerprint = next(
        (p["fingerprint"] for p in profiles if p["confident"] and p["overall"] is not None),
        None,
    )

    return {
        "profiles": profiles,
        "count": len(profiles),
        "min_runs": min_runs,
        "min_iterations": min_iterations,
        "complete_only": complete_only,
        "best_fingerprint": best_fingerprint,
        # Selectable non-metric numeric fields for the chart axes + column selector
        # (metric fields' metadata comes from /api/metrics).
        "fields": _PROFILE_FIELDS,
        "best_diff": _best_diff(profiles, best_fingerprint),
    }


def _best_diff(profiles: list[dict], best_fingerprint: str | None) -> dict | None:
    """Diff the best profile (closest to the top-right corner) against the next-ranked
    profile.

    Returns ``None`` until there are two profiles to compare. ``changes`` describe
    what the *best* profile did relative to the comparison one (e.g. CoDel target
    10ms → 5ms, direction "lower"), with the resulting SOPS delta.
    """
    best_idx = next(
        (i for i, p in enumerate(profiles) if p["fingerprint"] == best_fingerprint), None
    )
    if best_idx is None or best_idx + 1 >= len(profiles):
        return None
    best = profiles[best_idx]
    comparison = profiles[best_idx + 1]
    delta_abs = round(best["median"] - comparison["median"], 2)
    delta_pct = (
        round((delta_abs / comparison["median"]) * 100, 1) if comparison["median"] else None
    )

    def _comp_median(p: dict) -> float | None:
        c = p.get("completion")
        return c["median"] if c else None

    best_comp, comp_comp = _comp_median(best), _comp_median(comparison)
    completion_delta = (
        round(best_comp - comp_comp, 2)
        if best_comp is not None and comp_comp is not None
        else None
    )

    def _rel_median(p: dict) -> float | None:
        r = p.get("relative_sops")
        return r["delta_median"] if r else None

    best_rel, comp_rel = _rel_median(best), _rel_median(comparison)
    # Time-adjusted advantage: the gap once each profile's day/hour environment is
    # removed. Can differ from the raw delta if the two were sampled at different
    # times — that difference is exactly the confound this strips out.
    relative_delta = (
        round(best_rel - comp_rel, 2) if best_rel is not None and comp_rel is not None else None
    )
    return {
        "best": {
            "fingerprint": best["fingerprint"],
            "label": best["label"],
            "median": best["median"],
            "completion": best_comp,
            "relative_sops": best_rel,
            "confident": best["confident"],
        },
        "comparison": {
            "fingerprint": comparison["fingerprint"],
            "label": comparison["label"],
            "median": comparison["median"],
            "completion": comp_comp,
            "relative_sops": comp_rel,
            "confident": comparison["confident"],
        },
        "delta_abs": delta_abs,
        "delta_pct": delta_pct,
        # Completion can move opposite to SOPS — surfacing it here is the whole
        # point (feels-fast vs. raw-completion pulling apart).
        "completion_delta": completion_delta,
        "relative_delta": relative_delta,
        "changes": diff_profiles(comparison["settings"], best["settings"]),
    }


def _profile_settings(session: Session, fingerprint_: str) -> list[dict] | None:
    """The stored normalized settings for a profile (latest run that captured it)."""
    run = session.scalars(
        select(Run)
        .where(Run.settings_fingerprint == fingerprint_, Run.settings.is_not(None))
        .order_by(Run.created_at.desc())
    ).first()
    return run.settings if run else None


def _profile_iterations(session: Session, fingerprint_: str) -> int:
    """Total iterations a profile has accumulated across its *comparable* completed
    runs — the same count ``settings_profiles`` uses for the confidence flag."""
    methodology = ensure_current_methodology(session, get_config(session))
    rows = session.execute(
        select(Run, Score)
        .join(Score, Score.run_id == Run.id)
        .where(
            Run.status == RunStatus.COMPLETE,
            Run.settings_fingerprint == fingerprint_,
            Score.methodology_version == methodology.version,
        )
    ).all()
    return sum(int(run.iterations or 1) for run, score in rows if _comparable(score))


@router.post("/settings/apply-profile")
def apply_profile(
    body: dict = Body(...),
    session: Session = Depends(get_session),
) -> dict:
    """Write a stored settings profile to the firewall (the one-click apply).

    Body: ``{"fingerprint": "<12-hex>", "preview": bool}``. Discovers the live
    pipes, matches the profile's pipes by label, and applies every writable field
    that differs via ``provider.apply()`` (the only sanctioned firewall-write path).
    With ``preview: true`` it returns the planned field changes *without* writing —
    the UI uses this to show an exact-diff confirmation before committing.

    This is a one-way write (like Shotgun Sweep's apply-winner): to revert, apply a
    different profile. Fields already at the target value are skipped, so re-applying
    the current profile is a safe no-op.
    """
    fp = (body or {}).get("fingerprint")
    if not fp:
        raise HTTPException(status_code=400, detail="fingerprint is required")
    preview = bool((body or {}).get("preview", False))

    target = _profile_settings(session, fp)
    if not target:
        raise HTTPException(status_code=404, detail="No stored settings for that profile")

    provider = get_provider()
    try:
        live = provider.discover()
    except Exception as exc:  # noqa: BLE001
        log.exception("apply-profile discovery failed")
        raise HTTPException(
            status_code=502, detail=f"{provider.name} discovery failed: {type(exc).__name__}: {exc}"
        ) from exc

    changes, warnings = plan_apply(target, live)

    if preview:
        return {
            "preview": True,
            "fingerprint": fp,
            "label": summarize(target),
            "changes": changes,
            "warnings": warnings,
            "already_applied": not changes,
        }

    applied: list[dict] = []
    for ch in changes:
        try:
            provider.apply({"pipe_uuid": ch["pipe_uuid"], "param": ch["param"], "value": ch["value"]})
        except NotImplementedError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"The {provider.name} provider can't write changes.",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            log.exception("apply-profile write failed on %s after %s change(s)", ch["param"], len(applied))
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Applied {len(applied)} change(s), then failed on "
                    f"{ch['field_label']}: {type(exc).__name__}: {exc}. The firewall may be "
                    "partially changed — re-apply once the issue is resolved."
                ),
            ) from exc
        applied.append({"label": ch["label"], "field_label": ch["field_label"], "to": ch["to"]})

    # Best-effort: report the fingerprint the firewall is now on.
    resulting_fp = None
    try:
        resulting_fp = fingerprint(normalize(provider.discover()))
    except Exception:  # noqa: BLE001
        log.warning("apply-profile post-verify discovery failed", exc_info=True)

    log.info("Applied profile %s: %s change(s)", fp, len(applied))
    return {
        "ok": True,
        "fingerprint": fp,
        "label": summarize(target),
        "applied": applied,
        "warnings": warnings,
        "already_applied": not changes,
        "resulting_fingerprint": resulting_fp,
    }


@router.post("/settings/test-profile")
def test_profile(
    body: dict = Body(...),
    session: Session = Depends(get_session),
) -> dict:
    """Top a "limited data" profile up to the confidence minimum.

    Body: ``{"fingerprint": "<12-hex>"}``. Applies the profile to the firewall,
    runs exactly the iterations still needed to reach ``correlation.min_iterations``
    (capped at ``MAX_ITERATIONS``), then restores the pre-test settings. Returns the
    started test's id; poll ``GET /settings/test-profile/current`` for status. The
    run holds the coordination lock, so a test queues behind any other firewall
    operation.
    """
    fp = (body or {}).get("fingerprint")
    if not fp:
        raise HTTPException(status_code=400, detail="fingerprint is required")

    target = _profile_settings(session, fp)
    if not target:
        raise HTTPException(status_code=404, detail="No stored settings for that profile")

    min_iterations = _min_iterations(session)
    current_iters = _profile_iterations(session, fp)
    needed = min(MAX_ITERATIONS, max(0, min_iterations - current_iters))
    if needed <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"Profile already has {current_iters} iteration(s) (minimum {min_iterations}).",
        )

    try:
        test_id = profile_test_mod.start(fp, target, summarize(target), needed)
    except RuntimeError as exc:  # a test is already running
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("test-profile start failed")
        raise HTTPException(
            status_code=502, detail=f"Could not start the profile test: {type(exc).__name__}: {exc}"
        ) from exc

    log.info("Profile test %s started for %s: %s iteration(s)", test_id, fp, needed)
    return {
        "id": test_id,
        "fingerprint": fp,
        "iterations": needed,
        "current_iterations": current_iters,
        "min_iterations": min_iterations,
    }


@router.get("/settings/test-profile/current")
def current_profile_test() -> dict:
    """The most recent profile test, for status polling (``{test: {...} | null}``)."""
    return {"test": profile_test_mod.current()}


@router.get("/settings/impact")
def settings_impact(
    session: Session = Depends(get_session),
    complete_only: bool = Query(
        True, description="Only consider runs with the latest (paint) SOPS metrics."
    ),
) -> dict:
    """Compare the current settings profile to the one before the last change.

    Like ``/settings/profiles``, defaults to runs scored under the latest rubric so
    legacy data doesn't skew the before/after medians.
    """
    cfg = get_config(session).get("correlation", {}) or {}
    threshold = float(cfg.get("significant_change_pct", 5) or 5)
    min_runs = int(cfg.get("min_runs", 5) or 5)
    min_iterations = _min_iterations(session)
    rows = _completed_runs_with_scores(session)

    # Build contiguous segments of runs sharing a fingerprint (chronological).
    # Before/after medians are Smoothness (the headline this view ranks on).
    segments: list[dict] = []
    for run, score in rows:
        if complete_only and not _comparable(score):
            continue
        smooth = (score.axis_scores or {}).get("smoothness")
        if smooth is None:
            continue
        fp = run.settings_fingerprint
        if not segments or segments[-1]["fingerprint"] != fp:
            segments.append(
                {
                    "fingerprint": fp,
                    "settings": run.settings,
                    "sops": [],
                    "iterations": 0,
                    "changed_at": run.created_at,
                }
            )
        segments[-1]["sops"].append(smooth)
        segments[-1]["iterations"] += int(run.iterations or 1)
        segments[-1]["settings"] = run.settings

    base = {
        "changed": False,
        "threshold_pct": threshold,
        "min_runs": min_runs,
        "min_iterations": min_iterations,
    }
    if len(segments) < 2:
        return base

    prev, cur = segments[-2], segments[-1]
    before = round(median(prev["sops"]), 2)
    after = round(median(cur["sops"]), 2)
    delta_abs = round(after - before, 2)
    delta_pct = round((delta_abs / before) * 100, 1) if before else None
    # Don't make significance calls until both profiles have enough iterations.
    enough_data = prev["iterations"] >= min_iterations and cur["iterations"] >= min_iterations
    significant = enough_data and delta_pct is not None and abs(delta_pct) >= threshold
    return {
        "changed": True,
        "changed_at": cur["changed_at"].isoformat(),
        "threshold_pct": threshold,
        "min_runs": min_runs,
        "min_iterations": min_iterations,
        "enough_data": enough_data,
        "delta_abs": delta_abs,
        "delta_pct": delta_pct,
        "significant": significant,
        "before": {
            "label": summarize(prev["settings"]),
            "fingerprint": prev["fingerprint"],
            "median": before,
            "count": len(prev["sops"]),
            "iterations": prev["iterations"],
        },
        "after": {
            "label": summarize(cur["settings"]),
            "fingerprint": cur["fingerprint"],
            "median": after,
            "count": len(cur["sops"]),
            "iterations": cur["iterations"],
        },
    }
