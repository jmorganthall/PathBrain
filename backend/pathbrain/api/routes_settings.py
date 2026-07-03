"""Settings-vs-responsiveness correlation endpoints.

Groups completed runs by the firewall/SQM profile that was live when they ran,
and flags the most recent settings change when it moved the median SOPS beyond a
configurable threshold.
"""
from __future__ import annotations

from statistics import median, quantiles

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, defer, selectinload

from .. import challenger as challenger_mod
from .. import profile_test as profile_test_mod
from .. import refresh as refresh_mod
from ..config_store import get_config
from ..database import get_session
from ..logging_config import get_logger
from ..methodology import corner_score, ensure_current_methodology, overall_metrics
from ..metrics import all_metric_sources
from ..models import BenchmarkResult, Run, RunStatus, Score
from ..providers import get_provider
from ..runner import MAX_ITERATIONS
from ..scoring import COMPLETION_METRIC_SOURCES
from ..settings_profile import (
    diff_profiles,
    environment_signature,
    fingerprint,
    normalize,
    plan_apply,
    summarize,
)
from ..trends import RunPoint, profile_relative

# The three headline axes (the temporal phases of a load); their 0–100 scores still
# drive the per-axis display columns, but the **crown** no longer corners over them.
_CORNER_AXES = ("responsiveness", "smoothness", "speed")

# The crown corners over a small set of per-metric 0–100 subscores (perception-calibrated
# by the scoring engine, carried on every Score). The **authoritative** set is always the
# current methodology's ``overall`` spec (``methodology.overall_metrics`` — under v7,
# FCP × LCP × total_stall). These module constants are ONLY a static FALLBACK for a
# methodology that has no overall spec at all (pre-v5); they intentionally don't track the
# current crown. Everything that corners — the live ``_crown_corner`` fallback,
# ``crown_spreads``, ``optimistic_overall``, and the challenger race — reads the
# methodology-resolved set, so the crown always follows the methodology and never drifts.
CROWN_METRICS = ("fcp", "total_stall", "load_event")
CROWN_REQUIRED = ("fcp", "total_stall", "load_event")

# Non-metric numeric fields the /api/metrics catalog doesn't describe, exposed so the
# UI's chart-axis pickers + column selector can offer them (metric fields get their
# metadata from the catalog). higher_is_better drives the "↑/↓ better" hints.
_PROFILE_FIELDS = [
    {"key": "overall", "label": "Overall (feel)", "unit": "score", "higher_is_better": True, "group": "Scores"},
    {"key": "custom_overall", "label": "Overall (custom)", "unit": "score", "higher_is_better": True, "group": "Scores"},
    {"key": "responsiveness", "label": "Responsiveness", "unit": "score", "higher_is_better": True, "group": "Scores"},
    {"key": "smoothness", "label": "Smoothness", "unit": "score", "higher_is_better": True, "group": "Scores"},
    {"key": "speed", "label": "Speed", "unit": "score", "higher_is_better": True, "group": "Scores"},
    {"key": "stability", "label": "Stability", "unit": "score", "higher_is_better": True, "group": "Scores"},
    {"key": "completion", "label": "Completion", "unit": "score", "higher_is_better": True, "group": "Scores"},
    {"key": "iterations", "label": "Iterations", "unit": "", "higher_is_better": True, "group": "Run stats"},
    {"key": "count", "label": "Runs", "unit": "", "higher_is_better": True, "group": "Run stats"},
    {"key": "relative_smoothness", "label": "vs typical", "unit": "", "higher_is_better": True, "group": "Run stats"},
]


def _crown_corner(
    subscores: dict | None,
    metrics: tuple | list = CROWN_METRICS,
    required: tuple | list = CROWN_REQUIRED,
) -> float | None:
    """Live fallback for the methodology's Overall: corner over the crown metric subscores,
    requiring ``required`` and folding in the rest when present. ``metrics``/``required``
    come from the methodology's ``overall`` spec (``overall_metrics``) so this mirrors
    ``overall_from_definition`` exactly — it's only used for a Score that predates the
    persisted Overall (fixtures / not-yet-re-graded)."""
    sub = subscores or {}
    if any(sub.get(k) is None for k in required):
        return None
    return corner_score([sub.get(k) for k in metrics if sub.get(k) is not None])


# Optimism margin (points) given to a corner axis with too few samples to have a
# spread — the benefit of the doubt that keeps a 1-shot challenger in the race.
RACE_OPTIMISM_MARGIN = 5.0


# ── Raw-measurement crown ───────────────────────────────────────────────────────────
# The Overall/crown is the corner over each crown metric's **raw measurement**, mapped to
# 0–100 by its **percentile within the field's distribution** — NOT the methodology's
# perception grade, and NOT a min/max rescale. Percentile (rank) normalization gives every
# metric an identical, uniform spread by construction, so **no single metric can dominate**
# the corner (the failure mode of min/max, where one fast/slow outlier compresses a metric
# and total_stall — spread more evenly — steamrolls FCP/LCP). The scale comes from the
# measurements' *ranking*, so re-grading a metric can't move the crown; it stays monotonic in
# the raw values, so the crown-metric columns still explain the ranking. Trade-off: it's
# magnitude-blind (a 1 ms edge and a 200 ms edge both mean "one rank better").

def _percentile_norm(value: float | None, field: list[float], higher: bool) -> float | None:
    """Map a raw measurement to a 0–100 **percentile** within ``field`` (all profiles' median
    raw for this metric) — the fraction of the field this value is at least as good as, with
    half credit for ties (mid-rank empirical CDF). Direction-aware: for lower-is-better, a
    smaller value beats a larger one. None for a missing value / empty field; 100 for a
    single-profile field. Uniform by construction, so each metric contributes equal spread."""
    if value is None or not field:
        return None
    n = len(field)
    if n == 1:
        return 100.0
    worse = sum(1 for x in field if (x < value if higher else x > value))
    equal = sum(1 for x in field if x == value)
    return round(100.0 * (worse + 0.5 * equal) / n, 2)


def _crown_field_values(profiles: list[dict], metrics) -> dict:
    """Per crown metric, the list of all profiles' median raw values — the distribution each
    profile is percentile-ranked against."""
    field: dict = {}
    for m in metrics:
        field[m] = [
            p["metrics"][m] for p in profiles if (p.get("metrics") or {}).get(m) is not None
        ]
    return field


def _round2(x: float | None) -> float | None:
    return round(x, 2) if x is not None else None


def _normalized_crown(
    median_raw: dict, raw_spreads: dict, field: dict, higher: dict,
    metrics, required, margin: float = RACE_OPTIMISM_MARGIN,
) -> dict:
    """Field-normalized crown corners for one profile, all in the same 0–100 percentile space
    (no grading): the point ``overall`` (corner over each metric's percentile median), the IQR
    ``p25``/``p75`` (corner over the percentile of the pessimistic/optimistic raw quartile, so
    it brackets ``overall``), and the ``optimistic`` ceiling (optimistic quartile percentile,
    or the median percentile + a small margin for a thin <2-sample metric — the heir/race
    benefit of the doubt). Also returns the per-metric ``norm`` medians for display. Missing a
    required metric → that corner is None."""
    def norm(m, raw):
        f = field.get(m)
        return _percentile_norm(raw, f, bool(higher.get(m))) if f else None

    crown_norm, p25n, p75n, optn = {}, {}, {}, {}
    for m in metrics:
        nmed = norm(m, (median_raw or {}).get(m))
        crown_norm[m] = _round2(nmed)
        sp = raw_spreads.get(m) or {}
        n = sp.get("n") or 0
        # Optimistic raw = the good-side quartile (low for lower-is-better, high otherwise);
        # pessimistic = the other side. Normalization orients both so optimistic → higher score.
        good_raw = sp.get("p75") if higher.get(m) else sp.get("p25")
        bad_raw = sp.get("p25") if higher.get(m) else sp.get("p75")
        p75n[m] = _round2(norm(m, good_raw) if good_raw is not None else nmed)
        p25n[m] = _round2(norm(m, bad_raw) if bad_raw is not None else nmed)
        if n >= 2 and good_raw is not None:
            optn[m] = _round2(norm(m, good_raw))
        elif nmed is not None:
            optn[m] = _round2(min(100.0, nmed + margin))
        else:
            optn[m] = None
    return {
        "overall": _crown_corner(crown_norm, metrics, required),
        "p25": _crown_corner(p25n, metrics, required),
        "p75": _crown_corner(p75n, metrics, required),
        "optimistic": _crown_corner(optn, metrics, required),
        "norm": {m: v for m, v in crown_norm.items() if v is not None},
    }


router = APIRouter()
log = get_logger("api.settings")


def _comparable(score: Score) -> bool:
    # A run is comparable once it has a Score under the current methodology that
    # isn't "incomparable" (i.e. its raw can supply the required metrics). Delegates to
    # the single central predicate so every view filters identically.
    from ..methodology import is_comparable

    return is_comparable(score)


def _min_runs(session: Session) -> int:
    return int((get_config(session).get("correlation", {}) or {}).get("min_runs", 5) or 5)


def _min_iterations(session: Session) -> int:
    """Total iterations a profile needs before it counts as confident (the unit of
    signal — a 15-iteration run is worth far more than a 1-iteration one)."""
    return int((get_config(session).get("correlation", {}) or {}).get("min_iterations", 15) or 15)


def _crown_tie_params(session: Session) -> tuple[float, float]:
    """``(min_margin, iqr_fraction)`` for the tie-aware crown (config ``correlation``).

    ``min_margin`` is the absolute Overall-point floor a challenger must clear (so a
    tie isn't broken by rounding when both bands are ~0); ``iqr_fraction`` is how much
    of the two profiles' averaged Overall IQR the median gap must also exceed (a wider,
    noisier band demands a wider gap). See ``_clearly_better``."""
    corr = get_config(session).get("correlation", {}) or {}
    try:
        margin = float(corr.get("crown_tie_min_margin", 0.5))
    except (TypeError, ValueError):
        margin = 0.5
    try:
        frac = float(corr.get("crown_tie_iqr_fraction", 0.5))
    except (TypeError, ValueError):
        frac = 0.5
    return max(0.0, margin), max(0.0, frac)


def _overall_iqr(p: dict) -> float:
    """Width of a profile's per-run Overall IQR (p75 − p25) — its run-to-run spread, i.e.
    how *steady* the felt experience is. ``inf`` when the band is unknown, so a profile
    with no measured spread never wins the "steadiest" tie-break on missing data."""
    lo, hi = p.get("overall_p25"), p.get("overall_p75")
    if lo is None or hi is None:
        return float("inf")
    return max(0.0, float(hi) - float(lo))


def _clearly_better(a: dict, b: dict, min_margin: float, iqr_fraction: float) -> bool:
    """Is profile ``a``'s Overall *clearly* above ``b``'s — a real lead, not run-to-run
    noise? True when ``a``'s median beats ``b``'s by more than BOTH ``min_margin`` (an
    absolute floor) AND ``iqr_fraction`` × the two profiles' averaged Overall IQR (so a
    jitterier pair needs a wider gap to separate). This is what turns "highest median
    wins" into "highest median that actually stands apart wins": profiles that don't
    clear the bar are co-leaders (a statistical tie), decided on steadiness instead."""
    am, bm = a.get("overall"), b.get("overall")
    if am is None or bm is None:
        return am is not None and bm is None  # a scored, b not → a wins by default
    gap = float(am) - float(bm)
    if gap <= 0:
        return False
    pooled_iqr = (_finite(_overall_iqr(a)) + _finite(_overall_iqr(b))) / 2.0
    return gap > max(min_margin, iqr_fraction * pooled_iqr)


def _finite(x: float) -> float:
    """An unknown/`inf` IQR contributes 0 to the pooled spread — absent evidence of
    noise shouldn't *inflate* the gap a challenger must clear."""
    return 0.0 if x == float("inf") else x


def _select_crown(
    confident: list[dict],
    min_margin: float,
    iqr_fraction: float,
) -> tuple[dict | None, list[str]]:
    """Pick the crown from the confident profiles. Returns
    ``(best_profile, co_leader_fingerprints)``.

    The crown is the **highest median Overall**, full stop — the profile that wins, wins,
    even by an infinitesimal margin. No stickiness/hysteresis and no steadiness override
    enter the *verdict*: a marginally-higher median is still a higher median, and the crown
    follows it deterministically (ties on the exact median break toward the more-measured,
    then most-recently-seen profile).

    The ``co_leaders`` — every confident profile the crown can't ``_clearly_better`` (i.e.
    within run-to-run noise of it, including the crown itself) — are still returned, but
    **purely as information**: the UI flags them as "tied" so a photo finish reads as one,
    without ever changing *who* is crowned. This keeps the IQR "how close is this really?"
    signal while letting the actual winner take the crown.

    Pure (no DB) so it's unit-testable in isolation, like ``rank_challengers``."""
    scored = [p for p in confident if p.get("overall") is not None]
    if not scored:
        return None, []
    # The winner is simply the highest median Overall (deterministic tie-break on exact
    # equality: more iterations, then most recent). A hair of a lead still wins.
    best = max(
        scored,
        key=lambda p: (float(p["overall"]), int(p.get("iterations") or 0), p.get("last_seen") or ""),
    )
    # Informational only: who is statistically indistinguishable from the crown.
    co_fps = [
        p["fingerprint"]
        for p in scored
        if not _clearly_better(best, p, min_margin, iqr_fraction)
    ]
    return best, co_fps


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
        # Defer the heavy immutable JSON blobs (raw observations + per-target details):
        # the aggregation only reads ``metrics``/``plugin``, so loading + JSON-decoding
        # the raw payload of every browser result across all history was pure waste.
        .options(selectinload(Run.results).options(defer(BenchmarkResult.raw), defer(BenchmarkResult.details)))
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
    crown_metrics: str | None = Query(
        None,
        description=(
            "Optional comma-separated subscore keys (e.g. 'fcp,inp') for a custom crown: "
            "a live corner over the chosen betterments, returned per-profile as "
            "'custom_overall' + a 'custom_best_fingerprint'. Canonical Overall is unchanged."
        ),
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

    Also returns ``current_fingerprint``: the profile the firewall is on *right now*
    (best-effort live discovery), so the UI can flag the active row.
    """
    custom = [m.strip() for m in (crown_metrics or "").split(",") if m.strip()] or None
    result = compute_profiles(
        session, complete_only=complete_only, tz_offset=tz_offset, custom_crown_metrics=custom
    )
    # The live profile, for flagging the active row only — it no longer influences the crown
    # (the crown follows the highest median Overall, whoever wins, by any margin).
    result["current_fingerprint"] = _current_fingerprint()
    # The crown's heirs (limited-data / stale profiles that could still dethrone it), the
    # effective per-metric thresholds (so the quadrant can flag a saturated axis), and the
    # methodology saturation report (metrics whose 'best' is too lenient to rank profiles).
    definition = ensure_current_methodology(session, get_config(session)).definition or {}
    result["heirs"] = _compute_heirs(result, session)
    result["metric_thresholds"] = _metric_thresholds(definition)
    result["saturation"] = _saturation_report(result["profiles"], definition)
    return result


def _current_fingerprint() -> str | None:
    """Fingerprint of the live firewall settings right now (None if discovery fails)."""
    try:
        return fingerprint(normalize(get_provider().discover()))
    except Exception:  # noqa: BLE001 — best-effort; the UI just won't flag an active row
        log.debug("Could not discover current settings for active-profile flag", exc_info=True)
        return None


def _heir_count(session: Session) -> int:
    """How many heirs to surface on the crown card (config ``challenger.heir_count``,
    default 5)."""
    val = (get_config(session).get("challenger", {}) or {}).get("heir_count", 5)
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return 5


def _metric_thresholds(definition: dict) -> dict[str, dict]:
    """Per-metric *effective* best/worst/direction under the current methodology — the
    thresholds the score actually uses (v6 re-anchors fcp→150ms, load_event→800ms, …),
    NOT the catalog defaults. Lets the quadrant flag an axis as **saturated**: when every
    profile already sits past 'best', the raw spread the user is reading carries no score
    signal (the crown isn't decided there). Keyed by metric key."""
    out: dict[str, dict] = {}
    for m in (definition or {}).get("metrics", []):
        if m.get("best") is None or m.get("worst") is None:
            continue
        out[m["key"]] = {
            "best": m["best"],
            "worst": m["worst"],
            "higher_is_better": bool(m.get("higher_is_better")),
        }
    return out


# A scored metric that pins this share of profiles at ~100 (their value already clears the
# 'best' threshold) can no longer rank them — so the threshold is too lenient to crown the
# fastest. Flag it for a methodology re-anchor. Need a few profiles before judging.
SATURATION_FLAG_FRACTION = 0.5
SATURATION_MIN_PROFILES = 3


def _saturation_report(profiles: list[dict], definition: dict) -> list[dict]:
    """Per scored metric with a **non-zero** ``best``: the share of profiles whose median
    already clears 'best' (so the metric scores ~100 and can't separate them). Flags any
    metric saturating more than ``SATURATION_FLAG_FRACTION`` of profiles — a sign the
    threshold is too lenient to crown the fastest profile — and suggests re-anchoring
    'best' to the fastest value actually measured (so that profile scores 100 and the rest
    rank below it). ``best``=0 metrics (e.g. total_stall) are skipped: saturating at the
    physical floor is genuinely optimal, not a miscalibration."""
    report: list[dict] = []
    for m in (definition or {}).get("metrics", []):
        key, best = m.get("key"), m.get("best")
        # Only scored metrics (axis set) with a non-zero, finite 'best' can be re-anchored.
        if m.get("axis") is None or not best:
            continue
        higher = bool(m.get("higher_is_better"))
        vals = [
            p["metrics"][key]
            for p in profiles
            if key in (p.get("metrics") or {}) and p["metrics"][key] is not None
        ]
        if len(vals) < SATURATION_MIN_PROFILES:
            continue
        saturated = [v for v in vals if (v >= best if higher else v <= best)]
        frac = len(saturated) / len(vals)
        flagged = frac > SATURATION_FLAG_FRACTION
        # Re-anchor to the fastest (best-performing) profile measured: max for higher-is-
        # better, min for lower-is-better. None when not flagged or degenerate (all equal).
        suggested = None
        if flagged:
            anchor = max(vals) if higher else min(vals)
            if anchor != best:
                suggested = round(anchor, 1)
        report.append(
            {
                "key": key,
                "label": m.get("label") or key,
                "unit": m.get("unit") or "",
                "best": best,
                "saturated_fraction": round(frac, 3),
                "profiles": len(vals),
                "flagged": flagged,
                "suggested_best": suggested,
                "higher_is_better": higher,
            }
        )
    return report


def _compute_heirs(result: dict, session: Session) -> dict:
    """The crown's **heirs**: limited-data or stale-confident profiles whose *optimistic
    ceiling* can still clear the reigning crown's Overall — "run these and one may dethrone
    the crown".

    The ceiling is ``optimistic_overall`` — the crown corner over each metric's p75 upper
    estimate, the very number the challenger race uses to keep/eliminate a contender — so
    the heirs list can't drift from the race or the persisted Overall. The pool is exactly
    the profiles the crown *excludes*: not-yet-confident (under the iteration minimum) or
    confident-but-stale (newest run older than ``challenger.contender_stale_minutes``). A
    profile is an heir unless even its optimistic best case can't reach the crown. Bootstrap
    (no crown yet) → every non-confident profile is an heir.

    Only profiles **reachable** from the live environment are listed — an heir is something
    you could actually race, and the race can't apply a profile whose non-writable fields
    (scheduler/queues/upload bandwidth) differ from the current config. So this matches the
    race's contender set instead of dangling profiles it would refuse.

    Returns ``{items, total, limit, crown_overall}``: ``total`` is every qualifying heir
    (drives the "N could beat your crown" badge), ``items`` the top ``limit`` by ceiling-
    above-crown. Profiles that never produced a comparable run have no ceiling to rank by
    and aren't here — the Race button's bootstrap path still picks them up."""
    from datetime import datetime, timezone

    profiles = result.get("profiles", [])
    best_fp = result.get("best_fingerprint")
    crown_metrics = result.get("overall_metrics") or list(CROWN_METRICS)
    crown_required = result.get("overall_required") or list(CROWN_REQUIRED)
    min_iterations = result.get("min_iterations") or _min_iterations(session)
    stale_minutes = challenger_mod._contender_stale_minutes(session)
    limit = _heir_count(session)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Live environment signature for the reachability filter (best-effort; if discovery
    # fails we don't filter, same as the race start-check).
    reachable_env = None
    try:
        reachable_env = environment_signature(normalize(get_provider().discover()))
    except Exception:  # noqa: BLE001 — best-effort
        log.debug("Heirs: live discovery failed; not filtering by reachability", exc_info=True)

    crown = next((p for p in profiles if p["fingerprint"] == best_fp), None)
    crown_overall = crown["overall"] if crown else None

    heirs: list[dict] = []
    for p in profiles:
        if p["fingerprint"] == best_fp:
            continue
        # Skip profiles the race could never apply (non-writable fields differ from live).
        if reachable_env is not None and environment_signature(
            p.get("settings") or []
        ) != reachable_env:
            continue
        confident = bool(p.get("confident"))
        stale = confident and challenger_mod._incumbent_stale(
            p.get("last_seen"), stale_minutes, now
        )
        # Heir pool: not-yet-confident (limited data) or confident-but-stale only.
        if confident and not stale:
            continue
        opt = p.get("optimistic")  # field-normalized ceiling, computed in compute_profiles
        # Qualify unless even the optimistic ceiling can't reach the crown. With no crown
        # (bootstrap) or no ceiling estimate yet, keep it — we can't rule it out.
        if crown_overall is not None and opt is not None and opt <= crown_overall:
            continue
        margin = (
            round(opt - crown_overall, 1)
            if (opt is not None and crown_overall is not None)
            else None
        )
        heirs.append(
            {
                "fingerprint": p["fingerprint"],
                "label": p["label"],
                "reason": "stale" if stale else ("limited-data" if opt is not None else "untested"),
                "optimistic": opt,
                "margin": margin,
                "overall": p.get("overall"),
                "iterations": p.get("iterations"),
                "iterations_to_min": max(0, min_iterations - int(p.get("iterations") or 0)),
                "confident": confident,
                "last_seen": p.get("last_seen"),
            }
        )

    # Order to mirror the race's sampling priority (challenger.rank_challengers): confront the
    # biggest known threat first, then refresh nearby stale incumbents, then fill in unknowns —
    # so the top heir on the card is the first profile a race would actually run.
    #   tier 0 — limited-data with a known ceiling: highest optimistic ceiling first
    #   tier 1 — stale confident: closest to the crown first (smallest |Overall − crown|)
    #   tier 2 — untested (no ceiling estimate yet): listed last
    def _heir_key(h: dict) -> tuple:
        if h["reason"] == "stale":
            closeness = abs((h.get("overall") or 0.0) - (crown_overall or 0.0))
            return (1, closeness, 0.0)
        if h.get("optimistic") is not None:
            return (0, 0.0, -h["optimistic"])  # biggest threat (highest ceiling) first
        return (2, 0.0, 0.0)

    heirs.sort(key=_heir_key)
    return {
        "items": heirs[:limit],
        "total": len(heirs),
        "limit": limit,
        "crown_overall": crown_overall,
    }


def compute_profiles(
    session: Session,
    complete_only: bool = True,
    tz_offset: int = 0,
    custom_crown_metrics: list[str] | None = None,
) -> dict:
    """Aggregate completed runs into per-profile rows ranked by the feel-trinity corner
    Overall, with the crowned ``best_fingerprint``. Shared by the ``/settings/profiles``
    endpoint and the challenger race (``challenger.py``) so both rank profiles with
    identical logic. Each profile carries ``axis_spreads`` ({axis: {median,p25,p75,n}})
    for the display columns and ``crown_spreads`` (same shape, keyed by ``CROWN_METRICS``)
    so a caller can compute an ``optimistic_overall`` for not-yet-confident profiles."""
    min_runs = _min_runs(session)
    min_iterations = _min_iterations(session)
    min_samples = int((get_config(session).get("trends", {}) or {}).get("min_samples", 3) or 3)
    rows = _completed_runs_with_scores(session)
    # The crown metric set, from the current methodology's `overall` spec — the single
    # source of truth shared by the persisted Overall, the live fallback, crown_spreads,
    # optimistic_overall, and the challenger race (fallback to the module default for a
    # pre-v5 methodology with no overall spec).
    methodology = ensure_current_methodology(session, get_config(session))
    crown_metrics, crown_required = overall_metrics(methodology.definition or {})
    if not crown_metrics:
        crown_metrics, crown_required = list(CROWN_METRICS), list(CROWN_REQUIRED)
    # Per crown metric: is higher raw better? Drives which end of the field is "best" when
    # normalizing the raw measurement (the crown's scale). Read from the methodology's metric
    # defs (all current crown metrics are lower-is-better).
    _defn_metrics = {m.get("key"): m for m in (methodology.definition or {}).get("metrics", [])}
    crown_higher = {m: bool((_defn_metrics.get(m) or {}).get("higher_is_better")) for m in crown_metrics}
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
        # Per-metric 0–100 subscores carried on the Score (perception-calibrated by the
        # methodology's thresholds) — the building blocks for both the canonical Overall
        # and any custom-crown corner the caller asks for.
        crown_sub = (score.subscores or {}) if comparable else {}
        # This run's Overall: the methodology's first-class value persisted at scoring time
        # (``axis_scores['overall']``); fall back to the live feel-trinity corner for a
        # Score that predates it (fixtures / not-yet-re-graded).
        run_overall = axes.get("overall")
        if run_overall is None:
            run_overall = _crown_corner(crown_sub, crown_metrics, crown_required)
        # Time baseline carries both smoothness and the per-run Overall, so we can read
        # each profile's "vs typical" (day×hour-adjusted) for the Overall too.
        point_values = {"smoothness": smooth}
        if run_overall is not None:
            point_values["overall"] = run_overall
        point = RunPoint(created_at=run.created_at, values=point_values)
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
                # …per-metric raw value samples (every numeric value we collect)…
                "metric_samples": {},
                # …and per-metric 0–100 subscore samples (every scored metric), so the
                # canonical crown and any custom corner share one set of building blocks.
                "subscore_samples": {},
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
        # All axis scores (0–100) for this run → per-axis samples (display columns).
        # ``overall`` is a derived headline, not an axis, so it never becomes a column.
        for axis_key, val in (axes or {}).items():
            if val is not None and axis_key != "overall":
                g["axis_samples"].setdefault(axis_key, []).append(float(val))
        # Every per-metric subscore (0–100) for this run → the crown's corner inputs and
        # the menu of "betterments" a custom crown can corner over.
        for metric, val in crown_sub.items():
            if val is not None:
                g["subscore_samples"].setdefault(metric, []).append(float(val))
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
        # Per-axis spread + sample count (display columns).
        axis_spreads = {
            axis: {**_spread(vals), "n": len(vals)}
            for axis, vals in g["axis_samples"].items() if vals
        }
        # Per-metric subscore medians (every scored metric) — the menu of "betterments".
        # ``crown_scores`` powers display/charting and the custom-crown corner; the
        # trinity subset (``crown_spreads``) drives the challenger's ``optimistic_overall``.
        subscore_medians = {m: round(median(vals), 2) for m, vals in g["subscore_samples"].items() if vals}
        crown_scores = subscore_medians  # graded subscores — kept for the custom-crown lens only
        crown_spreads = {
            m: {**_spread(vals), "n": len(vals)}
            for m, vals in g["subscore_samples"].items() if vals and m in crown_metrics
        }
        # Per-metric medians — every numeric value we collect, for the chart + columns.
        metrics = {key: round(median(vals), 3) for key, vals in g["metric_samples"].items() if vals}
        # Raw spread (p25/p75/n) of each crown metric — the inputs to the field-normalized
        # crown, computed once the whole field is known (second pass below). The crown scores
        # the *raw measurements*, not the methodology grade.
        crown_raw = {
            m: {**_spread(vals), "n": len(vals)}
            for m, vals in g["metric_samples"].items() if vals and m in crown_metrics
        }
        # Overall + its IQR are computed after the loop (they need the field's best/worst to
        # normalize); placeholders here, filled in the normalize pass.
        overall = overall_p25_val = overall_p75_val = None
        rel = profile_relative(baseline_points, g["points"], "smoothness", tz_offset, min_samples)
        # Time-adjusted Overall ("vs typical"): how this profile scored vs the day×hour norm.
        # Kept as an informational signal (display + a hook for smarter heir-hunting), not a
        # crown input — the crown is highest Overall, full stop.
        rel_overall = profile_relative(baseline_points, g["points"], "overall", tz_offset, min_samples)
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
                # Per-axis medians (display) + the feel-trinity subscore medians/spreads.
                "scores": scores,
                "axis_spreads": axis_spreads,
                "crown_scores": crown_scores,
                "crown_spreads": crown_spreads,
                # Raw spread of each crown metric (for the normalize pass) + the normalized
                # 0–100 medians (filled below) that the crown actually corners over.
                "crown_raw": crown_raw,
                "crown_norm": {},
                # Optimistic ceiling (field-normalized) — filled in the normalize pass; drives
                # the heirs card + challenger race.
                "optimistic": None,
                # The single corner "overall" — closeness to the fastest-on-all-crown-metrics
                # corner over the raw measurements. This IS the crown basis: highest wins.
                "overall": overall,
                # Time-adjusted ("vs typical") Overall — informational, not a crown input.
                "relative_overall": rel_overall,
                # Overall IQR (corner over each crown metric's p25/p75) — brackets Overall.
                "overall_p25": overall_p25_val,
                "overall_p75": overall_p75_val,
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
    # ── Normalize pass: field percentile-normalized raw crown ───────────────────────
    # Now that the whole field is built, map each crown metric's raw measurement to its
    # percentile within the field's distribution, then corner. Percentile (rank) normalization
    # gives every metric equal, uniform spread, so no one metric can dominate the corner. The
    # scale comes from the measurements' ranking, not any methodology threshold, so re-grading
    # can't move the crown. Fills each profile's overall / IQR / optimistic / normalized values.
    crown_field = _crown_field_values(profiles, crown_metrics)
    for p in profiles:
        res = _normalized_crown(
            p.get("metrics") or {}, p.get("crown_raw") or {}, crown_field, crown_higher,
            crown_metrics, crown_required,
        )
        p["overall"] = res["overall"]
        p["overall_p25"] = res["p25"]
        p["overall_p75"] = res["p75"]
        p["optimistic"] = res["optimistic"]
        p["crown_norm"] = res["norm"]

    # Rank the table by the raw-normalized corner "overall"; profiles missing it (no crown
    # metrics captured yet) fall back to smoothness median, sort last.
    profiles.sort(key=lambda p: (p["overall"] is not None, p["overall"] if p["overall"] is not None else p["median"]), reverse=True)

    # "Best" = the crown: the confident profile (total iterations ≥ the minimum) with the
    # highest median Overall — the profile that wins, wins, even by an infinitesimal margin.
    # No stickiness/hysteresis and no steadiness override enter the verdict: a marginally
    # higher median is still a higher median, and the crown follows it deterministically.
    #
    # The IQR still buys us something — but only as *information*: ``co_leaders`` lists the
    # profiles statistically indistinguishable from the crown (within run-to-run noise), so
    # the UI can flag a photo finish as "tied" without ever changing *who* is crowned. The
    # challenger race reads best_fingerprint's Overall as its bar, unchanged.
    #
    # Finding *challengers* that could overtake the crown is a separate, smarter job: the
    # "Heirs to the crown" card and the challenger race rank under-sampled / stale profiles
    # by their *optimistic ceiling* (``optimistic_overall``) against the crown's Overall, to
    # decide where to spend iterations to confirm or deny an heir. That hunt is untouched.
    tie_margin, tie_fraction = _crown_tie_params(session)
    confident = [p for p in profiles if p["confident"] and p["overall"] is not None]
    best, co_leaders = _select_crown(confident, tie_margin, tie_fraction)
    best_fingerprint = best["fingerprint"] if best else None
    # Co-leaders within noise of the crown (excluding the crown itself) — an informational
    # "this was close" flag, not a re-ranking. Empty when the crown stands clearly apart.
    crown_co_leaders = [fp for fp in co_leaders if fp != best_fingerprint]

    # Custom crown: an *exploratory* second take on "best" that corners over a caller-chosen
    # set of betterments (per-metric subscores) instead of the canonical feel trinity. It's
    # a live lens over the same persisted subscores — no re-grade, no methodology change —
    # so the user can ask "which profile wins if I only care about THESE?". The canonical
    # ``best_fingerprint`` is untouched; this is a parallel, simpler argmax of the custom
    # corner among confident profiles (no Thompson — it's a what-if view, not the verdict).
    custom_best_fingerprint = _apply_custom_crown(profiles, custom_crown_metrics)

    return {
        "profiles": profiles,
        "count": len(profiles),
        "min_runs": min_runs,
        "min_iterations": min_iterations,
        "complete_only": complete_only,
        "best_fingerprint": best_fingerprint,
        # Fingerprints statistically tied with the crown (co-leaders) — the crown's median
        # lead over these is within run-to-run noise, so the UI flags them as a tie instead
        # of implying the crown is decisively better. Empty when the crown stands apart.
        "co_leaders": crown_co_leaders,
        # The methodology's canonical crown metric set (source of truth for the corner) —
        # the challenger race reads these so its optimistic estimate matches the persisted
        # Overall exactly.
        "overall_metrics": crown_metrics,
        "overall_required": crown_required,
        # The field distribution per crown metric — the ranking the crown percentile-normalizes
        # over (for transparency: this is what re-measuring, not re-grading, moves). We surface
        # the observed best/worst/count; the full percentile scale is derived from the field.
        "crown_field": {
            m: {
                "best": (min(v) if v else None) if not crown_higher.get(m) else (max(v) if v else None),
                "worst": (max(v) if v else None) if not crown_higher.get(m) else (min(v) if v else None),
                "n": len(v),
            }
            for m, v in crown_field.items()
        },
        # Echo the custom-crown selection (None when not requested) + its winner.
        "crown_metrics": list(custom_crown_metrics) if custom_crown_metrics else None,
        "custom_best_fingerprint": custom_best_fingerprint,
        # Selectable non-metric numeric fields for the chart axes + column selector
        # (metric fields' metadata comes from /api/metrics).
        "fields": _PROFILE_FIELDS,
        "best_diff": _best_diff(profiles, best_fingerprint),
    }


def _apply_custom_crown(profiles: list[dict], metrics: list[str] | None) -> str | None:
    """Set each profile's ``custom_overall`` (corner over the chosen metric subscores) and
    return the confident winner. ``metrics`` are subscore keys (e.g. ``["fcp", "inp"]``);
    the corner is an *intersection*, so a profile missing any chosen metric gets ``None``
    (it can't be placed on this custom corner). No-op returning ``None`` when no metrics
    are requested. The winner is the highest custom corner among confident profiles."""
    if not metrics:
        for p in profiles:
            p["custom_overall"] = None
        return None
    best_fp, best_val = None, None
    for p in profiles:
        cs = p.get("crown_scores") or {}
        vals = [cs.get(m) for m in metrics]
        custom = corner_score(vals) if all(v is not None for v in vals) else None
        p["custom_overall"] = custom
        if custom is not None and p.get("confident") and (best_val is None or custom > best_val):
            best_fp, best_val = p["fingerprint"], custom
    return best_fp


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
    background: BackgroundTasks,
    body: dict = Body(...),
    session: Session = Depends(get_session),
) -> dict:
    """Write a stored settings profile to the firewall (the one-click apply).

    Body: ``{"fingerprint": "<12-hex>", "preview": bool, "run_benchmark": bool}``.
    Discovers the live pipes, matches the profile's pipes by label, and applies every
    writable field that differs via ``provider.apply()`` (the only sanctioned
    firewall-write path). With ``preview: true`` it returns the planned field changes
    *without* writing — the UI uses this to show an exact-diff confirmation before
    committing. With ``run_benchmark`` (default **true**) it kicks a single-iteration
    benchmark on the just-applied profile in the background (returned as ``run_id``), so
    a one-click apply immediately measures the new settings.

    This is a one-way write (like Shotgun Sweep's apply-winner): to revert, apply a
    different profile. Fields already at the target value are skipped, so re-applying
    the current profile is a safe no-op.
    """
    fp = (body or {}).get("fingerprint")
    if not fp:
        raise HTTPException(status_code=400, detail="fingerprint is required")
    preview = bool((body or {}).get("preview", False))
    run_benchmark = bool((body or {}).get("run_benchmark", True))

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

    # Optionally measure the just-applied profile: a single-iteration benchmark, kicked in
    # the background under the coordination lock (so it queues behind any other firewall
    # session and shows in the jobs dropdown). Apply is a one-way write — the benchmark
    # just records how the new settings perform; it doesn't revert anything.
    run_id = None
    if run_benchmark:
        from ..runner import create_run
        from .routes_run import _locked_execute

        run_id = create_run(
            label=f"apply · {summarize(target)}",
            notes=f"Benchmark after applying profile {fp}",
            iterations=1,
        )
        background.add_task(_locked_execute, run_id)

    log.info("Applied profile %s: %s change(s)%s", fp, len(applied),
             f"; benchmark run {run_id}" if run_id else "")
    return {
        "ok": True,
        "fingerprint": fp,
        "label": summarize(target),
        "applied": applied,
        "warnings": warnings,
        "already_applied": not changes,
        "resulting_fingerprint": resulting_fp,
        "run_id": run_id,
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


def _contending_challengers(session: Session) -> tuple[str | None, list[str]]:
    """``(best_fingerprint, [contender fingerprints])`` for the race — via the same
    augmented field + ranking the race loop uses, so the start check matches the loop
    exactly. Contenders span no-data profiles (no current-methodology data), under-min
    profiles that can still beat the bar, and stale confident profiles. ``best_fingerprint``
    may be None (bootstrap: race to establish a best)."""
    from datetime import datetime, timezone

    field = challenger_mod._field(session)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale_min = challenger_mod._contender_stale_minutes(session)
    # Match the loop: only count contenders reachable from the live environment (apply()
    # can't drive scheduler/queues/upload bandwidth), so the button doesn't offer a race
    # whose only contenders can never be applied.
    reachable_env = None
    try:
        reachable_env = environment_signature(normalize(get_provider().discover()))
    except Exception:  # noqa: BLE001 — best-effort; without it we just don't pre-filter
        log.debug("Could not discover live settings for reachability filter", exc_info=True)
    best_fp, _bar, _leader, contenders, _newly = challenger_mod.rank_challengers(
        field, {}, now=now, stale_minutes=stale_min, reachable_env=reachable_env
    )
    return best_fp, [p["fingerprint"] for p, _ in contenders]


@router.post("/settings/race")
def start_race(body: dict = Body(...), session: Session = Depends(get_session)) -> dict:
    """Start a challenger race: adaptively measure the profiles we can't currently trust
    against the winner — profiles with no current-methodology data, under-minimum profiles
    that could still overtake the best, and stale confident profiles — one iteration at a
    time within a time budget (see ``challenger.py``).

    Body: ``{"time_budget_minutes": <number>, "auto_promote": <bool>}``. Runs even with no
    confident best yet (bootstrap, e.g. right after a methodology change). Returns the race
    id; poll ``GET /settings/race`` for status.
    """
    minutes = float((body or {}).get("time_budget_minutes") or 0)
    if minutes <= 0:
        raise HTTPException(status_code=400, detail="time_budget_minutes must be > 0")
    auto_promote = bool((body or {}).get("auto_promote", False))

    _best_fp, contenders = _contending_challengers(session)
    if not contenders:
        raise HTTPException(
            status_code=400,
            detail=(
                "Nothing to race — every profile is either already confident/current or "
                "unreachable from the live environment (its scheduler/queues/upload bandwidth "
                "differ from the current config, which apply() can't change)."
            ),
        )

    try:
        race_id = challenger_mod.start(int(minutes * 60), auto_promote)
    except RuntimeError as exc:  # a race is already running
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("race start failed")
        raise HTTPException(
            status_code=502, detail=f"Could not start the race: {type(exc).__name__}: {exc}"
        ) from exc

    return {"id": race_id, "contenders": len(contenders), "auto_promote": auto_promote}


@router.get("/settings/race")
def current_race() -> dict:
    """The most recent challenger race, for status polling (``{race: {...} | null}``)."""
    return {"race": challenger_mod.current()}


@router.post("/settings/race/cancel")
def cancel_race() -> dict:
    """Ask the running race to stop after its current iteration (baseline is restored)."""
    return {"cancelled": challenger_mod.cancel()}


@router.get("/settings/refresh/preview")
def refresh_preview(
    iterations: int = Query(..., description="Benchmark iterations to run per profile."),
    session: Session = Depends(get_session),
) -> dict:
    """Preview a 'Re-run all profiles' batch: how many profiles, total iterations, and an
    estimated duration (from recent runs' per-iteration timing) — so the UI can show
    'N profiles × M iterations ≈ ~T' before committing."""
    return refresh_mod.preview(session, iterations)


@router.post("/settings/refresh")
def start_refresh(body: dict = Body(...), session: Session = Depends(get_session)) -> dict:
    """Start a 'Re-run all profiles' batch: apply each stored profile, run ``iterations``
    benchmarks on it, and restore the baseline at the end (see ``refresh.py``).

    Body: ``{"iterations": <number>}``. Returns the refresh id; poll
    ``GET /settings/refresh`` for status.
    """
    iterations = int((body or {}).get("iterations") or 0)
    if iterations <= 0:
        raise HTTPException(status_code=400, detail="iterations must be > 0")
    try:
        refresh_id = refresh_mod.start(iterations)
    except RuntimeError as exc:  # already running, or no profiles
        # "already running" is a conflict; "no profiles" is a bad request.
        status = 409 if "already running" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("refresh start failed")
        raise HTTPException(
            status_code=502, detail=f"Could not start the refresh: {type(exc).__name__}: {exc}"
        ) from exc
    return {"id": refresh_id, "iterations": iterations}


@router.get("/settings/refresh")
def current_refresh() -> dict:
    """The most recent profile refresh, for status polling (``{refresh: {...} | null}``)."""
    return {"refresh": refresh_mod.current()}


@router.post("/settings/refresh/cancel")
def cancel_refresh() -> dict:
    """Ask the running refresh to stop after the current profile (baseline is restored)."""
    return {"cancelled": refresh_mod.cancel()}


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
