"""Settings-vs-responsiveness correlation endpoints.

Groups completed runs by the firewall/SQM profile that was live when they ran,
and flags the most recent settings change when it moved the median SOPS beyond a
configurable threshold.
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import median, quantiles

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer, selectinload

from .. import challenger as challenger_mod
from .. import profile_test as profile_test_mod
from .. import refresh as refresh_mod
from ..config_store import get_config, save_config
from ..database import get_session
from ..logging_config import get_logger
from ..methodology import (
    corner_score,
    ensure_current_methodology,
    overall_from_definition,
    overall_metrics,
    overall_method,
    overall_weights,
    weighted_score,
)
from ..metrics import METRIC_ROLES, ROLE_WEATHER, all_metric_sources
from ..models import BenchmarkResult, Run, RunStatus, Score
from ..providers import get_provider
from ..runner import MAX_ITERATIONS
from ..schemas import ApplySettings, TestSettings
from ..scoring import COMPLETION_METRIC_SOURCES
from .. import profile_names
from ..settings_profile import (
    _field_equal,
    _to_number,
    diff_profiles,
    environment_signature,
    fingerprint,
    normalize,
    plan_apply,
    summarize,
    unwritable_diffs,
)
from ..shaper_fields import SHAPER_FIELDS, SWEEPABLE_FIELDS, WRITABLE_FIELDS, coerce_value, field as shaper_field
from ..trends import RunPoint, _BaselineResolver, bucket_values, profile_relative
from ..weather import cohort_residuals, run_severities

# The three headline axes (the temporal phases of a load); their 0–100 scores still
# drive the per-axis display columns, but the **crown** no longer corners over them.
_CORNER_AXES = ("responsiveness", "smoothness", "speed")

# The crown corners over a small set of per-metric 0–100 subscores (perception-calibrated
# by the scoring engine, carried on every Score). The **authoritative** set is always the
# current methodology's ``overall`` spec (``methodology.overall_metrics`` — under v10,
# FCP × LCP × stall_energy). These module constants are ONLY a static FALLBACK for a
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
    {"key": "overall_recent", "label": "Overall (recent)", "unit": "score", "higher_is_better": True, "group": "Scores"},
    {"key": "weather_relative", "label": "Vs weather (Overall)", "unit": "", "higher_is_better": True, "group": "Scores"},
    {"key": "weather_severity", "label": "Weather severity", "unit": "pctl", "higher_is_better": False, "group": "Run stats"},
    {"key": "pct_vs_sqm_off", "label": "% vs SQM off", "unit": "%", "higher_is_better": True, "group": "Scores"},
    # weather_adjusted_overall (the setup-stripped decomposition) stays in the payload for API
    # consumers but is no longer offered as a column — the measured-weather cohort residual
    # (`weather_relative`) is the one surfaced "vs weather" reading.
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

# ── Weather-adjusted Overall (display-only, per-run, self-contained) ──────────────────
# A metric-based "vs weather" that doesn't infer conditions from other profiles' runs (the ±2h
# neighbour-pool `weather_overall` is contaminated by *which* profiles we ran). Instead it strips
# the connection-setup weather each run co-measures on its OWN socket: FCP/LCP are milestone sums
# that bake in dns+tcp+tls, so subtracting this run's nav setup phases leaves the part the profile
# actually influences. Cornered exactly like `overall` (same percentile space), so it ranks
# alongside it. Shape metrics (stall_energy) are post-first-byte and carried through unadjusted.
# **Informational only — never feeds crowning or elimination.**
_SETUP_WEATHER_PHASES = ("nav_dns", "nav_tcp", "nav_tls")   # the load's own connection-setup chain
_SETUP_ADJUSTED_METRICS = {"fcp", "lcp"}                      # milestone sums that bake in setup weather


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


def _metric_distribution(runs: list[dict], keys: list[str]) -> dict:
    """Per metric, the spread across a profile's **whole** run history (n/min/p25/median/p75/max).

    Computed from *all* runs before the per-profile sample cap, so the export always conveys a
    profile's variance even when ``run_samples`` is truncated to the latest N — the AI sees how
    reliably (not just how fast at best) a profile performs, without cherry-picking."""
    out: dict = {}
    for key in keys:
        vals = sorted(
            r["metrics"][key] for r in runs
            if isinstance(r["metrics"].get(key), (int, float)) and not isinstance(r["metrics"].get(key), bool)
        )
        if not vals:
            continue
        if len(vals) >= 2:
            qs = quantiles(vals, n=4, method="inclusive")  # [p25, p50, p75]
            p25, p75 = _round2(qs[0]), _round2(qs[2])
        else:
            p25 = p75 = _round2(vals[0])
        out[key] = {
            "n": len(vals),
            "min": _round2(vals[0]),
            "p25": p25,
            "median": _round2(median(vals)),
            "p75": p75,
            "max": _round2(vals[-1]),
        }
    return out


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
    """``(min_margin, sigma)`` for the tie-aware crown (config ``correlation``).

    ``min_margin`` is the absolute Overall-point floor a lead must clear (so a tie isn't
    broken by rounding when both estimates are pinned); ``sigma`` is how many **standard
    errors of the median** the gap must also exceed to count as a real lead — the
    significance threshold. A wider ``sigma`` demands more confidence. See ``_clearly_better``.

    ``sigma`` reads ``crown_tie_sigma`` (default 2.0 ≈ a ~2-SE separation). The pre-existing
    ``crown_tie_iqr_fraction`` is retired: it scaled the *raw* run-to-run IQR, which ignores
    sample size, so more runs never tightened a tie. The SE (IQR/√n) shrinks as runs accrue,
    so collecting data can actually break a tie."""
    corr = get_config(session).get("correlation", {}) or {}
    try:
        margin = float(corr.get("crown_tie_min_margin", 0.5))
    except (TypeError, ValueError):
        margin = 0.5
    try:
        sigma = float(corr.get("crown_tie_sigma", 2.0))
    except (TypeError, ValueError):
        sigma = 2.0
    return max(0.0, margin), max(0.0, sigma)


def _overall_iqr(p: dict) -> float:
    """Width of a profile's per-run Overall IQR (p75 − p25) — its run-to-run spread, i.e.
    how *steady* the felt experience is. ``inf`` when the band is unknown, so a profile
    with no measured spread never wins the "steadiest" tie-break on missing data."""
    lo, hi = p.get("overall_p25"), p.get("overall_p75")
    if lo is None or hi is None:
        return float("inf")
    return max(0.0, float(hi) - float(lo))


# ── Current form vs lifetime form (per profile, both directions) ────────────────────────
# The crown pools each profile's WHOLE history, so a heavily-sampled profile's Overall has
# mass inertia: fresh head-to-head data barely moves a 3000-iteration median. That's the
# right defense against hot streaks in a stationary world — but when the world changes, a
# crown can coast on the ghost of its past (current form worse than its record), and an
# undervalued profile can be held down by old bad data (current form better). This check
# compares each profile's recent-window median Overall against its prior history with the
# same IQR/√n significance machinery the crown ties use. Flag-and-steer only: "fading" /
# "rising" chips + a crown-level alert — the verdict itself stays the pooled measurements,
# and the recourse is re-measurement (race / re-run top-N), never re-weighting.
FORM_RECENT_RUNS = 15    # the "current form" window (runs)
FORM_MIN_PRIOR_RUNS = 15  # minimum prior history to compare against


def _median_se_of(values: list[float]) -> float:
    """SE of the median ≈ IQR/√n over a raw sample (same convention as ``_overall_se``)."""
    if len(values) < 2:
        return float("inf")
    q = quantiles(values, n=4)
    iqr = q[2] - q[0]
    return iqr / (len(values) ** 0.5)


def _profile_form(overalls_chronological: list[float], sigma: float) -> dict | None:
    """Compare a profile's recent form against its prior history, both directions.

    ``overalls_chronological`` is the profile's per-run Overall in run order. The last
    ``FORM_RECENT_RUNS`` runs are its *current form*; everything before is its *prior
    record*. The median difference is judged against ``sigma`` × the pooled SE of the two
    medians: significantly below → ``fading`` (its pooled Overall is propped by a past it
    no longer delivers); significantly above → ``rising`` (its pooled Overall understates
    what it does now). None when there isn't enough history for the split to mean anything.
    """
    n = len(overalls_chronological)
    if n < FORM_RECENT_RUNS + FORM_MIN_PRIOR_RUNS:
        return None
    recent = overalls_chronological[-FORM_RECENT_RUNS:]
    prior = overalls_chronological[:-FORM_RECENT_RUNS]
    med_recent, med_prior = median(recent), median(prior)
    pooled = (_finite(_median_se_of(recent)) ** 2 + _finite(_median_se_of(prior)) ** 2) ** 0.5
    threshold = sigma * pooled
    delta = med_recent - med_prior
    direction = "rising" if delta > threshold else ("fading" if delta < -threshold else "steady")
    return {
        "recent": round(med_recent, 2),
        "prior": round(med_prior, 2),
        "delta": round(delta, 2),
        "threshold": round(threshold, 2),
        "direction": direction,
        "recent_n": len(recent),
        "prior_n": len(prior),
    }


def _overall_se(p: dict) -> float:
    """Standard error of a profile's **median** Overall ≈ IQR/√n — how precisely we know the
    median, *not* how much individual runs bounce. Tightens as runs (``count``) accrue, so a
    heavily-sampled profile's median is treated as the confident estimate it is. ``inf`` when
    the spread or sample size is unknown (contributes 0 to the pooled SE via ``_finite``)."""
    iqr = _overall_iqr(p)
    n = int(p.get("count") or 0)
    if iqr == float("inf") or n < 1:
        return float("inf")
    return iqr / (n ** 0.5)


def _clearly_better(a: dict, b: dict, min_margin: float, sigma: float) -> bool:
    """Is profile ``a``'s Overall *clearly* above ``b``'s — a real lead, not run-to-run
    noise? True when ``a``'s median beats ``b``'s by more than BOTH ``min_margin`` (an
    absolute floor) AND ``sigma`` × the pooled **standard error of the two medians**
    (``√(SE_a² + SE_b²)``, the SE of their difference). Because SE = IQR/√n, the bar
    *shrinks as each profile accrues runs* — so two well-sampled profiles that are reliably
    ~1 point apart separate, while a thin or jittery pair stays a co-leader (statistical tie).
    This is the significance test: "highest median that actually stands apart wins."""
    am, bm = a.get("overall"), b.get("overall")
    if am is None or bm is None:
        return am is not None and bm is None  # a scored, b not → a wins by default
    gap = float(am) - float(bm)
    if gap <= 0:
        return False
    se_a, se_b = _finite(_overall_se(a)), _finite(_overall_se(b))
    pooled_se = (se_a ** 2 + se_b ** 2) ** 0.5  # SE of the difference of two medians
    return gap > max(min_margin, sigma * pooled_se)


def _finite(x: float) -> float:
    """An unknown/`inf` SE contributes 0 to the pooled spread — absent evidence of noise
    shouldn't *inflate* the gap a challenger must clear."""
    return 0.0 if x == float("inf") else x


def _select_crown(
    confident: list[dict],
    min_margin: float,
    sigma: float,
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
        if not _clearly_better(best, p, min_margin, sigma)
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
    """Chronological ``(Run, Score, results_by_plugin)`` for completed runs with settings,
    scored under the current methodology.

    ``results_by_plugin`` is each run's plugin metric cache (``{plugin: metrics}``),
    fetched as **plain rows in one bulk query** rather than eager-loaded ORM entities:
    materializing ~6 ``BenchmarkResult`` instances per run across all history (plus the
    relationship machinery) dominated the Settings-Impact load as runs piled up. The
    heavy immutable JSON blobs (``raw`` observations + per-target ``details``) are never
    selected at all, and ``Run.config_used`` (a full benchmark-config snapshot per run
    that no caller reads) is deferred for the same reason.
    """
    methodology = ensure_current_methodology(session, get_config(session))
    filters = (
        Run.status == RunStatus.COMPLETE,
        Run.settings_fingerprint.is_not(None),
        Score.methodology_version == methodology.version,
    )
    rows = session.execute(
        select(Run, Score)
        .join(Score, Score.run_id == Run.id)
        .options(defer(Run.config_used))
        .where(*filters)
        .order_by(Run.created_at)
    ).all()
    metrics_by_run: dict[int, dict[str, dict]] = {}
    for run_id, plugin, metrics in session.execute(
        select(BenchmarkResult.run_id, BenchmarkResult.plugin, BenchmarkResult.metrics)
        .join(Run, Run.id == BenchmarkResult.run_id)
        .join(Score, Score.run_id == Run.id)
        .where(*filters)
    ):
        metrics_by_run.setdefault(run_id, {})[plugin] = metrics or {}
    return [(run, score, metrics_by_run.get(run.id, {})) for run, score in rows]


@router.get("/settings/apply-warmup")
def apply_warmup(session: Session = Depends(get_session)) -> dict:
    """Read-only diagnostic: do the first chunks after a firewall apply measure low?

    Groups profile-test chunks (measured immediately after an apply) and current-test
    chunks (no firewall write — the control) by their parent job, centers each chunk's
    Overall on its own test's median, and aggregates by chunk position. A persistent
    negative first chunk after applies that the control doesn't show means a
    freshly-applied profile is penalized before it settles — which biases every quick
    test and the explore recommendation ledger against every candidate. Diagnosis only;
    no score changes.
    """
    from ..runner import apply_warmup_report

    return apply_warmup_report(session)


@router.get("/settings/profiles/{fingerprint}/verify-derivation")
def verify_profile_derivation(
    fingerprint: str,
    sample: int = Query(15, ge=1, le=100, description="Runs to check per cohort (oldest/newest)."),
    session: Session = Depends(get_session),
) -> dict:
    """Read-only data-integrity audit for a profile — the ground-truth answer to "are we keeping
    the same data the same?"

    Samples the profile's **oldest** and **newest** runs, re-derives each run's metrics from its
    immutable raw under the current derivation, and reports whether the stored values reproduce.
    If the *oldest* cohort drifts while the *newest* is clean, historical runs are carrying values
    computed under a formula that has since changed and were never re-derived — old and new are not
    like-for-like, and the fix is a full re-derive. Mutates nothing."""
    from ..config import get_settings
    from ..interpret import DERIVATION_VERSION
    from ..runner import (
        browser_collection_shape,
        compare_collection_shapes,
        verify_run_derivation as _verify,
    )

    art = get_settings().artifact_dir
    total = session.scalar(
        select(func.count()).select_from(Run).where(
            Run.status == RunStatus.COMPLETE, Run.settings_fingerprint == fingerprint
        )
    ) or 0
    if total == 0:
        raise HTTPException(status_code=404, detail=f"No completed runs for profile {fingerprint}")

    base = (
        select(Run)
        .where(Run.status == RunStatus.COMPLETE, Run.settings_fingerprint == fingerprint)
        .options(selectinload(Run.results))
    )
    oldest = list(session.scalars(base.order_by(Run.created_at.asc()).limit(sample)).all())
    seen = {r.id for r in oldest}
    newest = [r for r in session.scalars(base.order_by(Run.created_at.desc()).limit(sample)).all() if r.id not in seen]

    def _cohort(rows: list) -> dict:
        checked = drifting = 0
        drift_keys: dict[str, int] = {}
        for run in rows:
            if not run.results:
                continue
            rep = _verify(run, art)
            checked += 1
            if rep["drift"]:
                drifting += 1
                for d in rep["drift"]:
                    drift_keys[d["key"]] = drift_keys.get(d["key"], 0) + 1
        return {
            "checked": checked,
            "drifting": drifting,
            "consistent": drifting == 0,
            # Which metrics drifted, most-common first — the ones whose formula changed under them.
            "drift_metrics": sorted(drift_keys, key=lambda k: -drift_keys[k]),
        }

    old_c, new_c = _cohort(oldest), _cohort(newest)
    # Collection-shape comparison: did we collect the SAME raw ingredients old vs new? (URL set,
    # LoAF coverage, page composition). This catches the drift the derivation audit can't — a
    # faithful recipe applied to different ingredients still yields non-comparable metrics.
    old_shape, new_shape = browser_collection_shape(oldest), browser_collection_shape(newest)
    collection = compare_collection_shapes(old_shape, new_shape)
    collection["oldest"] = old_shape
    collection["newest"] = new_shape
    return {
        "fingerprint": fingerprint,
        "total_runs": total,
        "current_derivation": DERIVATION_VERSION,
        "oldest": old_c,
        "newest": new_c,
        # The headline: everything checked reproduces from raw → like-for-like preserved.
        "consistent": old_c["consistent"] and new_c["consistent"],
        # The tell the user is hunting: historical runs drift while fresh ones don't.
        "stale_history": (not old_c["consistent"]) and new_c["consistent"],
        # Did the raw INGREDIENTS change (URLs/LoAF/composition)? Even with a faithful recipe,
        # a change here means old and new runs aren't measuring the same thing.
        "collection": collection,
    }


@router.get("/settings/diagnostics")
def settings_diagnostics(session: Session = Depends(get_session)) -> dict:
    """Visibility into settings capture: how many runs are stamped, how many
    distinct fingerprints, and the most recent runs with their fingerprints.

    Lets us tell apart "old runs never captured" (lots of nulls) from "fingerprint
    changes every run" (lots of distinct fingerprints).
    """
    # Only the identity columns are needed (id/created_at/label/fingerprint) — not each run's
    # `settings` JSON. Pull just those so counting all history doesn't load every run's blob.
    completed = session.execute(
        select(Run.id, Run.created_at, Run.label, Run.settings_fingerprint)
        .where(Run.status == RunStatus.COMPLETE)
        .order_by(Run.created_at.desc())
    ).all()
    stamped = [r for r in completed if r.settings_fingerprint]
    distinct = {r.settings_fingerprint for r in stamped}
    # How many completed runs are comparable under the current methodology — a SQL COUNT on the
    # `comparability` column (only "incomparable" is excluded; NULL counts as comparable, matching
    # methodology.is_comparable), instead of materializing every Score row + its JSON to count.
    methodology = ensure_current_methodology(session, get_config(session))
    with_latest = session.scalar(
        select(func.count())
        .select_from(Score)
        .join(Run, Run.id == Score.run_id)
        .where(
            Run.status == RunStatus.COMPLETE,
            Score.methodology_version == methodology.version,
            Score.comparability.is_distinct_from("incomparable"),
        )
    ) or 0
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


@router.post("/settings/refingerprint")
def refingerprint_runs(
    session: Session = Depends(get_session),
) -> dict:
    """Recompute every run's ``settings_fingerprint`` from its *own* captured settings — a one-shot
    re-key of history under the current fingerprint scheme, preserving all the underlying data.

    Its effect today is to **collapse all "SQM off" runs into the single canonical SQM-off
    profile**: their fingerprints used to vary with the (inert) shaper field values the firewall
    echoes back while disabled, splintering the baseline into many one-off profiles. Ordinary
    shaped profiles hash identically under the new and old scheme, so they are left untouched — only
    SQM-off (and any other newly-collapsing) runs change their grouping key."""
    runs = session.scalars(
        select(Run).where(Run.status == RunStatus.COMPLETE, Run.settings.is_not(None))
    ).all()
    rekeyed = 0
    for run in runs:
        try:
            new_fp = fingerprint(run.settings or [])
        except Exception:  # noqa: BLE001 — one odd row must not abort the whole re-key
            continue
        if new_fp != run.settings_fingerprint:
            run.settings_fingerprint = new_fp
            rekeyed += 1
    session.commit()
    log.info("Re-fingerprinted %d of %d completed runs", rekeyed, len(runs))
    if rekeyed:
        # Re-keying regroups profiles, which can re-rank the field with no run completing —
        # wake the crown follower for a fresh full check.
        from .. import crown_follower

        crown_follower.poke()
    return {"scanned": len(runs), "rekeyed": rekeyed}


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
    # One live discovery per request, shared by the active-row fingerprint and the heirs'
    # reachability filter (two separate discover() round-trips used to stall the page).
    live = _discover_live_normalized()
    # The live profile, for flagging the active row only — it no longer influences the crown
    # (the crown follows the highest median Overall, whoever wins, by any margin).
    result["current_fingerprint"] = _current_fingerprint(live)
    # The crown's heirs (limited-data / stale profiles that could still dethrone it), the
    # effective per-metric thresholds (so the quadrant can flag a saturated axis), and the
    # methodology saturation report (metrics whose 'best' is too lenient to rank profiles).
    definition = ensure_current_methodology(session, get_config(session)).definition or {}
    result["heirs"] = _compute_heirs(result, session, live)
    result["metric_thresholds"] = _metric_thresholds(definition)
    result["saturation"] = _saturation_report(result["profiles"], definition)
    return result


@router.get("/settings/export/optimizer")
def optimizer_export(
    session: Session = Depends(get_session),
    runs_per_profile: int = Query(
        50, ge=1, le=1000, description="Cap the per-profile run samples (most recent first)."
    ),
    profile_limit: int | None = Query(
        None, ge=1, le=1000, description="Only the top-N profiles by Overall (default: all)."
    ),
) -> dict:
    """A single AI-ready JSON: every profile's **tunable shaper settings** → its **runs** →
    the **raw metrics used for scoring**, plus the methodology goal and the shaper field model.

    Purpose-built to hand to an LLM: it has the *levers* (which shaper params are writable +
    their sensible ranges), the *outcomes* (each run's raw fcp/lcp/total_stall and every other
    scored metric, in ms), and the *objective* (crown metrics + "lower is better" + the observed
    best/worst achieved so far) — everything needed to propose new, untested profiles likely to
    score higher. Profile-centric so settings↔performance patterns are explicit.
    """
    return build_optimizer_export(session, runs_per_profile, profile_limit)


@router.get("/settings/weather-sensitivity")
def weather_sensitivity(session: Session = Depends(get_session)) -> dict:
    """How much the *network weather* moves each crown metric — the metric-based read that a
    self-contained "vs weather" would be built on.

    For each co-measured weather covariate (probe instruments + the load's own connection-setup
    phases) × each crown metric, the Spearman ρ across per-run points — both pooled and
    **within-profile** (holding the profile fixed, the causal signal). Informational only; no
    scores change. Use it to confirm whether a weather adjustment is worth building and which
    covariates (the ``clean`` ones) to build it from."""
    methodology = ensure_current_methodology(session, get_config(session))
    defn = methodology.definition or {}
    crown_metrics, _ = overall_metrics(defn)
    metric_meta = {
        m["key"]: {
            "label": m.get("label"),
            "unit": m.get("unit"),
            "higher_is_better": bool(m.get("higher_is_better")),
        }
        for m in defn.get("metrics", []) if m.get("key")
    }
    return _weather_sensitivity(session, crown_metrics, metric_meta)


# --- Field ↔ outcome sensitivity ------------------------------------------------------
# A profile whose field value we correlate needs enough distinct points to mean anything.
SENSITIVITY_MIN_POINTS = 4       # need this many (field value, metric) pairs to correlate
SENSITIVITY_MIN_DISTINCT = 3     # ...spread over at least this many distinct field values
SENSITIVITY_TREND_RHO = 0.3      # |ρ| below this reads as "no clear relationship"


# Rank-correlation primitives now live in ``pathbrain.stats`` (shared with the
# campaign-drift check); aliased here so the call sites below stay unchanged.
from ..stats import pearson as _pearson  # noqa: E402
from ..stats import rank as _rank  # noqa: E402
from ..stats import spearman as _spearman  # noqa: E402


def _sensitivity_summary(pipe: str, field_label: str, metric_label: str, direction: str, effect: str) -> str:
    if direction == "none":
        return f"No clear relationship between {pipe} {field_label} and {metric_label}."
    verb = "rises" if direction == "increases" else "falls"
    tail = "improves the crown" if effect == "improves" else "worsens the crown"
    return f"As {pipe} {field_label} increases, {metric_label} {verb} — {tail}."


def _field_sensitivity(
    profiles_out: list[dict], crown_metrics: list[str], metric_meta: dict[str, dict]
) -> list[dict]:
    """Deterministic marginal relationships between the tunable levers and the crown metrics.

    For each writable shaper field (kept **per pipe label**, so the Download and Upload legs
    stay distinct) vs each crown metric **and the Overall itself**, the Spearman rank correlation
    across the exported profiles — one (field value, profile value) point per profile. This hands
    the model (and the UI) an explicit "as this field goes up, that goes up/down / improves/worsens"
    map instead of leaving it to eyeball the raw profile table. The Overall row matters most: a
    lever can move the Overall (the rank-corner we crown on) while barely correlating with any
    single raw metric, because the corner compounds small per-metric rank edges.

    These are **marginal, not partial** — profiles vary several fields at once, so a correlation
    can be confounded. They're directional evidence to reconcile against, not isolated effects.
    """
    # Correlate each lever against every crown metric AND against the Overall itself (the
    # percentile-rank corner we actually crown on). The Overall is often where the signal lives:
    # a profile wins by compounding small, sub-noise per-metric rank edges into a corner, so a
    # lever can move the Overall while barely correlating with any single raw metric.
    meta = dict(metric_meta)
    meta.setdefault("overall", {"label": "Overall", "higher_is_better": True})
    targets = list(crown_metrics) + ["overall"]
    # points[(pipe_label, field)][metric] = [(field_value, metric_value), …]
    points: dict[tuple[str, str], dict[str, list[tuple[float, float]]]] = {}
    for p in profiles_out:
        medians = p.get("metric_medians") or {}
        overall_val = p.get("overall")
        for pipe in (p.get("settings") or []):
            label = pipe.get("label") or "pipe"
            for fkey in WRITABLE_FIELDS:
                x = _to_number(fkey, pipe.get(fkey))
                if x is None:
                    continue
                for m in targets:
                    y = overall_val if m == "overall" else medians.get(m)
                    if isinstance(y, (int, float)) and not isinstance(y, bool):
                        points.setdefault((label, fkey), {}).setdefault(m, []).append((float(x), float(y)))

    out: list[dict] = []
    for (label, fkey), by_metric in points.items():
        fld = shaper_field(fkey)
        field_label = fld.label if fld else fkey
        for m, pts in by_metric.items():
            xs = [x for x, _ in pts]
            ys = [y for _, y in pts]
            if len(pts) < SENSITIVITY_MIN_POINTS or len(set(xs)) < SENSITIVITY_MIN_DISTINCT:
                continue
            rho = _spearman(xs, ys)
            if rho is None:
                continue
            higher_better = bool((meta.get(m) or {}).get("higher_is_better"))
            metric_label = (meta.get(m) or {}).get("label") or m
            if abs(rho) < SENSITIVITY_TREND_RHO:
                direction, effect = "none", "none"
            else:
                direction = "increases" if rho > 0 else "decreases"
                # Lower-is-better metric improving when it falls (rho<0) — XOR the metric's own
                # direction so a higher-is-better crown metric is handled too.
                effect = "improves" if ((rho < 0) != higher_better) else "worsens"
            out.append({
                "pipe": label,
                "field": fkey,
                "field_label": field_label,
                "metric": m,
                "metric_label": metric_label,
                "spearman": _round2(rho),
                "n": len(pts),
                "distinct_values": len(set(xs)),
                "metric_direction": direction,  # does the metric rise or fall as the field rises
                "effect": effect,               # improves / worsens the crown, given lower-is-better
                "summary": _sensitivity_summary(label, field_label, metric_label, direction, effect),
            })
    # Strongest (most confident) monotonic relationships first.
    out.sort(key=lambda r: -abs(r["spearman"] or 0.0))
    return out


# --- Metric-based "vs weather": how much do conditions move the crown metrics? ----------
# The current "vs weather" (trends.profile_weather_relative) infers conditions from the rolling
# median of *other profiles' runs* — contaminated by which profiles we happened to run. This is
# the first, informational step toward a self-contained replacement: use the weather signals each
# run *co-measures* (probe instruments + the load's own connection-setup phases) and quantify how
# much each crown metric actually responds to them. Deterministic, changes no scores; validates
# whether a weather adjustment is worth building and which covariates to build it from.
_WEATHER_SHAPED = {"download", "transfer", "nav_response"}   # the shaper moves these — never adjust with them
_WEATHER_NAV_SETUP = ["nav_dns", "nav_tcp", "nav_tls", "nav_request", "nav_response"]
WEATHER_WITHIN_MIN_POINTS = 5   # runs one profile needs before its within-profile ρ is trusted


def _weather_covariates() -> list[tuple[str, bool]]:
    """``(covariate_key, clean)`` — the ambient signals co-measured on every run. ``clean`` =
    profile-orthogonal (usable to weather-adjust); ``not clean`` = the shaper itself moves it
    (bandwidth caps download/transfer; nav_response is the SQM-facing delivery phase), so it's
    shown for transparency but must never adjust a metric or we'd subtract real profile effect.
    Two families: probe instruments (ledger role W — independent sockets) and the load's own
    connection-setup phases (role N, RTT-dominated path weather on the *same* socket as FCP/LCP,
    the causally cleaner proxy)."""
    keys = [k for k, r in METRIC_ROLES.items() if r == ROLE_WEATHER] + _WEATHER_NAV_SETUP
    seen: set[str] = set()
    out: list[tuple[str, bool]] = []
    for k in keys:
        if k in seen:
            continue
        seen.add(k)
        out.append((k, k not in _WEATHER_SHAPED))
    return out


def _weather_sensitivity(
    session: Session, crown_metrics: list[str], metric_meta: dict[str, dict]
) -> dict:
    """For each weather covariate × crown metric, the Spearman ρ across **per-run** points,
    computed two ways:

      * ``pooled`` — over every comparable run (marginal: mixes the weather effect with
        between-profile differences), and
      * ``within_profile`` — ρ computed *within each profile* (across its own runs, holding the
        profile fixed) then median-aggregated. This partials the profile out, so it's the
        causally meaningful "does weather move this metric" signal.

    A high within-profile |ρ| means the crown metric is weather-sensitive and worth adjusting for;
    ≈0 means it's already weather-robust (e.g. a separate-socket probe vs FCP, which ride
    different sockets) and an adjustment would do nothing. Covariates are tagged ``clean`` (usable
    to adjust) vs shaped (transparency only). Purely informational — no scores change."""
    metric_src = all_metric_sources()
    covariates = _weather_covariates()
    needed = set(crown_metrics) | {k for k, _ in covariates}

    # Per-run values (crown + covariates), grouped by profile for the within-profile pass. Same
    # source compute_profiles reads: the plugin metric cache, falling back to the re-graded
    # Score.metric_values when the cache predates a metric.
    runs_by_fp: dict[str, list[dict[str, float]]] = {}
    for run, score, results_by_plugin in _completed_runs_with_scores(session):
        if not _comparable(score):
            continue
        mv = score.metric_values or {}
        vals: dict[str, float] = {}
        for key in needed:
            plugin_src = metric_src.get(key)
            v = results_by_plugin.get(plugin_src[0], {}).get(plugin_src[1]) if plugin_src else None
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                v = mv.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals[key] = float(v)
        if vals:
            runs_by_fp.setdefault(run.settings_fingerprint, []).append(vals)

    all_runs = [v for runs in runs_by_fp.values() for v in runs]

    rows: list[dict] = []
    for cov, clean in covariates:
        cov_label = (metric_meta.get(cov) or {}).get("label") or cov
        for m in crown_metrics:
            m_meta = metric_meta.get(m) or {}
            pooled_pts = [(r[cov], r[m]) for r in all_runs if cov in r and m in r]
            pooled_rho = (
                _spearman([x for x, _ in pooled_pts], [y for _, y in pooled_pts])
                if len(pooled_pts) >= SENSITIVITY_MIN_POINTS else None
            )
            within: list[float] = []
            for runs in runs_by_fp.values():
                pts = [(r[cov], r[m]) for r in runs if cov in r and m in r]
                if len(pts) < WEATHER_WITHIN_MIN_POINTS or len({x for x, _ in pts}) < 2:
                    continue
                rho = _spearman([x for x, _ in pts], [y for _, y in pts])
                if rho is not None:
                    within.append(rho)
            within_rho = round(median(within), 3) if within else None
            if pooled_rho is None and within_rho is None:
                continue
            # Direction/sensitivity read off the causal (within-profile) ρ when we have it.
            ref = within_rho if within_rho is not None else pooled_rho
            if ref is None or abs(ref) < SENSITIVITY_TREND_RHO:
                direction, sensitive = "none", False
            else:
                direction, sensitive = ("increases" if ref > 0 else "decreases"), True
            rows.append({
                "covariate": cov,
                "covariate_label": cov_label,
                "clean": clean,                       # False = shaper-moved; never adjust with it
                "role": METRIC_ROLES.get(cov),
                "metric": m,
                "metric_label": m_meta.get("label") or m,
                "metric_higher_is_better": bool(m_meta.get("higher_is_better")),
                "pooled_spearman": _round2(pooled_rho),
                "pooled_n": len(pooled_pts),
                "within_profile_spearman": within_rho,
                "within_profile_profiles": len(within),
                "metric_direction": direction,        # does the metric rise/fall as weather rises
                "weather_sensitive": sensitive,       # |ρ| ≥ trend threshold → adjustment matters
            })
    # Strongest causal weather sensitivity first (within-profile ρ, else pooled).
    rows.sort(
        key=lambda r: -abs(
            r["within_profile_spearman"]
            if r["within_profile_spearman"] is not None
            else (r["pooled_spearman"] or 0.0)
        )
    )
    return {
        "crown_metrics": list(crown_metrics),
        "covariates": [
            {"key": k, "clean": c, "role": METRIC_ROLES.get(k),
             "label": (metric_meta.get(k) or {}).get("label") or k}
            for k, c in covariates
        ],
        "within_profile_min_points": WEATHER_WITHIN_MIN_POINTS,
        "trend_rho": SENSITIVITY_TREND_RHO,
        "runs_analyzed": len(all_runs),
        "profiles_analyzed": len(runs_by_fp),
        "rows": rows,
    }


# --- What the overperformers share -----------------------------------------------------
# A monotone rank correlation (above) can't see a *sweet spot* — a lever value the best
# profiles cluster on while both extremes are worse — or an interaction. This contrast
# answers "which settings do the top-Overall profiles share?" directly, catching those.
SIGNATURE_TOP_FRACTION = 0.25    # the top quartile by Overall = "the overperformers"
SIGNATURE_MIN_PROFILES = 8       # need this many scored profiles to split top/rest at all
SIGNATURE_MIN_GROUP = 3          # ...and this many on each side of the split
SIGNATURE_SHIFT_STRONG = 0.5     # |top−rest median| ≥ this many field-IQRs = a clear higher/lower
SIGNATURE_CONCENTRATION = 0.5    # top IQR ≤ half the field IQR = the top group agrees on a value


def _cliffs_delta(a: list[float], b: list[float]) -> float | None:
    """Cliff's delta / rank-biserial: P(a>b) − P(a<b) ∈ [−1,1]. Scale-free 'do group a's values
    tend to sit above group b's?' — robust to outliers and to different field scales."""
    if not a or not b:
        return None
    gt = sum(1 for x in a for y in b if x > y)
    lt = sum(1 for x in a for y in b if x < y)
    return (gt - lt) / (len(a) * len(b))


def _lever_signature(profiles_out: list[dict]) -> dict:
    """For each writable lever (per pipe label): the value the **top-Overall** profiles share vs
    the field's full range — the settings the overperformers have in common.

    Complements ``_field_sensitivity``: correlation asks "does raising this lever monotonically
    help?"; this asks "what do the winners run?", which also catches a **sweet spot** in the
    middle (top group concentrates on a value both extremes miss) and combination effects a
    single-lever correlation is blind to. Deterministic; the same data the correlation uses.
    """
    ranked = [
        p for p in profiles_out
        if isinstance(p.get("overall"), (int, float)) and not isinstance(p.get("overall"), bool)
    ]
    ranked.sort(key=lambda p: p["overall"], reverse=True)
    n = len(ranked)
    if n < SIGNATURE_MIN_PROFILES:
        return {"available": False, "reason": f"need ≥{SIGNATURE_MIN_PROFILES} scored profiles, have {n}", "levers": []}
    top_k = max(SIGNATURE_MIN_GROUP, round(n * SIGNATURE_TOP_FRACTION))
    top_k = min(top_k, n - SIGNATURE_MIN_GROUP)  # keep ≥ MIN_GROUP in the rest, too
    top_fps = {id(p) for p in ranked[:top_k]}

    # Per (pipe_label, field): the numeric values in the top group and the rest.
    top_by: dict[tuple[str, str], list[float]] = {}
    rest_by: dict[tuple[str, str], list[float]] = {}
    for p in ranked:
        bucket = top_by if id(p) in top_fps else rest_by
        for pipe in (p.get("settings") or []):
            label = pipe.get("label") or "pipe"
            for fkey in WRITABLE_FIELDS:
                v = _to_number(fkey, pipe.get(fkey))
                if v is not None:
                    bucket.setdefault((label, fkey), []).append(float(v))

    levers: list[dict] = []
    for key in set(top_by) | set(rest_by):
        label, fkey = key
        top_vals = top_by.get(key, [])
        rest_vals = rest_by.get(key, [])
        all_vals = top_vals + rest_vals
        if len(top_vals) < SIGNATURE_MIN_GROUP or len(rest_vals) < SIGNATURE_MIN_GROUP:
            continue
        if len(set(all_vals)) < 2:  # constant field — nothing to share
            continue
        top_s, rest_s, all_s = _spread(top_vals), _spread(rest_vals), _spread(all_vals)
        field_iqr = all_s["p75"] - all_s["p25"] or (all_s["max"] - all_s["min"])
        if not field_iqr:
            continue
        shift = (top_s["median"] - rest_s["median"]) / field_iqr
        top_iqr = top_s["p75"] - top_s["p25"]
        concentration = max(0.0, 1.0 - (top_iqr / field_iqr))
        cliff = _cliffs_delta(top_vals, rest_vals)
        fld = shaper_field(fkey)
        field_label = fld.label if fld else fkey

        if abs(shift) >= SIGNATURE_SHIFT_STRONG:
            pattern = "higher" if shift > 0 else "lower"
        elif concentration >= SIGNATURE_CONCENTRATION:
            pattern = "sweet_spot"
        else:
            pattern = "none"
        levers.append({
            "pipe": label,
            "field": fkey,
            "field_label": field_label,
            "pattern": pattern,             # higher / lower / sweet_spot / none
            "top_value": top_s["median"],   # the value the overperformers share
            "top_range": [top_s["p25"], top_s["p75"]],
            "field_range": [all_s["min"], all_s["max"]],
            "field_median": all_s["median"],
            "shift": _round2(shift),                 # signed, in field-IQR units
            "concentration": _round2(concentration),  # 0..1, higher = top group agrees more
            "cliffs_delta": _round2(cliff),
            "top_n": len(top_vals),
            "rest_n": len(rest_vals),
            "summary": _signature_summary(label, field_label, pattern, top_s, all_s),
        })
    # Most distinctive levers first: a clear higher/lower or a tight shared value.
    levers.sort(key=lambda l: -max(abs(l["shift"] or 0.0), l["concentration"] or 0.0))
    return {
        "available": True,
        "top_profiles": top_k,
        "rest_profiles": n - top_k,
        "top_fraction": SIGNATURE_TOP_FRACTION,
        "levers": levers,
    }


# --- Where to collect more data (active experiment design) -----------------------------
# The most valuable AI output isn't always a finished profile — it's "this lever looks
# promising but you haven't measured enough to trust it; go collect data HERE." A signal is
# actionable only once it's resolved, so flag promising-but-undersampled levers.
COVERAGE_STRONG_RHO = 0.3        # |ρ vs Overall| ≥ this = a clear trend worth resolving
COVERAGE_SUGGESTIVE_RHO = 0.2    # ...≥ this = suggestive, worth a confirming sweep
COVERAGE_MIN_DISTINCT = 4        # fewer measured values than this = under-sampled


def _coverage_values(fkey: str, lo: float, hi: float, measured: set[float], k: int = 4) -> list[int]:
    """Up to ``k`` evenly-spaced integer values in (lo, hi] not already measured — the points
    that would resolve a lever's effect."""
    if lo is None or hi is None or hi <= lo:
        return []
    out: list[int] = []
    for i in range(1, k + 1):
        v = int(round(lo + (hi - lo) * i / k))
        if v not in measured and v not in out:
            out.append(v)
    return out


def _coverage_gaps(profiles_out: list[dict], field_sensitivity: list[dict], lever_signature: dict) -> list[dict]:
    """Levers with a promising-but-under-resolved signal — where collecting data beats guessing.

    A lever qualifies when it shows a directional signal (a top-profile pattern, or a suggestive
    ρ against the Overall) yet is under-sampled (few distinct measured values, or the favored
    direction runs off the edge of what's been measured). For each, recommend the concrete values
    to measure next — so the model can 'kick back' a data request instead of a speculative profile.
    """
    vals_by: dict[tuple[str, str], list[float]] = {}
    for p in profiles_out:
        for pipe in (p.get("settings") or []):
            label = pipe.get("label") or "pipe"
            for fkey in WRITABLE_FIELDS:
                v = _to_number(fkey, pipe.get(fkey))
                if v is not None:
                    vals_by.setdefault((label, fkey), []).append(float(v))

    overall_rho = {
        (r["pipe"], r["field"]): r.get("spearman")
        for r in field_sensitivity if r.get("metric") == "overall"
    }
    sig_by = {(l["pipe"], l["field"]): l for l in (lever_signature.get("levers") or [])}

    gaps: list[dict] = []
    for (label, fkey), vals in vals_by.items():
        distinct = sorted(set(vals))
        n_distinct = len(distinct)
        if n_distinct < 2:
            continue  # a constant lever carries no signal to resolve
        mmin, mmax = distinct[0], distinct[-1]
        rho = overall_rho.get((label, fkey))
        sig = sig_by.get((label, fkey)) or {}
        pattern = sig.get("pattern")

        has_pattern = pattern in ("higher", "lower", "sweet_spot")
        suggestive = rho is not None and abs(rho) >= COVERAGE_SUGGESTIVE_RHO
        if not (has_pattern or suggestive):
            continue

        # Which direction looks better (pattern wins; else infer from ρ sign — Overall is
        # higher-is-better, so ρ>0 means raising the lever helps).
        if pattern in ("higher", "lower"):
            better = pattern
        elif rho is not None and abs(rho) >= COVERAGE_SUGGESTIVE_RHO:
            better = "higher" if rho > 0 else "lower"
        else:
            better = None  # sweet_spot / just needs resolution

        fld = shaper_field(fkey)
        field_label = fld.label if fld else fkey
        sweepable = bool(fld and fld.sweepable)
        sd = (fld.sweep_default if fld else None) or {}
        sweep_min, sweep_max = sd.get("min"), sd.get("max")
        measured = set(distinct)

        # Recommend values in the favored direction, else fill the interior to resolve the trend.
        if better == "lower" and sweep_min is not None and mmin > sweep_min:
            action = "extend_lower"
            suggested = _coverage_values(fkey, sweep_min, mmin, measured)
            rationale = (f"Top profiles favor lower {field_label}; the lowest you've measured is "
                         f"{mmin:g}. Measure below it to find the floor.")
        elif better == "higher" and sweep_max is not None and mmax < sweep_max:
            action = "extend_higher"
            suggested = _coverage_values(fkey, mmax, sweep_max, measured)
            rationale = (f"Top profiles favor higher {field_label}; the highest you've measured is "
                         f"{mmax:g}. Measure above it to find the ceiling.")
        elif n_distinct < COVERAGE_MIN_DISTINCT:
            action = "resolve"
            suggested = _coverage_values(fkey, mmin, mmax, measured)
            rationale = (f"{field_label} shows a signal but only {n_distinct} distinct value(s) "
                         f"measured ({mmin:g}–{mmax:g}) — too few to trust. Add interior values.")
        else:
            continue  # already well-sampled in the useful direction

        if not suggested:
            continue
        strength = max(abs(rho or 0.0), abs(sig.get("shift") or 0.0), sig.get("concentration") or 0.0)
        gaps.append({
            "pipe": label,
            "field": fkey,
            "field_label": field_label,
            "distinct_values": n_distinct,
            "measured_range": [mmin, mmax],
            "overall_rho": _round2(rho) if rho is not None else None,
            "pattern": pattern,
            "sweepable": sweepable,          # can the Shotgun Sweep run this directly?
            "action": action,                # extend_lower / extend_higher / resolve
            "suggested_values": suggested,
            "rationale": rationale,
            # Promising signal × how under-sampled it is (fewer values ⇒ more to gain).
            "priority": _round2(strength * (0.5 + 1.0 / n_distinct)),
        })
    gaps.sort(key=lambda g: -(g["priority"] or 0.0))
    return gaps


def _signature_summary(pipe: str, field_label: str, pattern: str, top_s: dict, all_s: dict) -> str:
    if pattern == "none":
        return f"Top profiles show no distinctive {pipe} {field_label} — it ranges as widely as the rest."
    lo, hi = top_s["p25"], top_s["p75"]
    rng = f"{top_s['median']:g} ({lo:g}–{hi:g})" if lo != hi else f"{top_s['median']:g}"
    field_rng = f"{all_s['min']:g}–{all_s['max']:g}"
    if pattern == "higher":
        return f"Top profiles run {pipe} {field_label} HIGHER — ~{rng} vs the {field_rng} field range."
    if pattern == "lower":
        return f"Top profiles run {pipe} {field_label} LOWER — ~{rng} vs the {field_rng} field range."
    return f"Top profiles concentrate {pipe} {field_label} at ~{rng} — a shared sweet spot within the {field_rng} field range."


def build_optimizer_export(
    session: Session, runs_per_profile: int = 50, profile_limit: int | None = None
) -> dict:
    """Assemble the AI-ready optimizer export (see ``optimizer_export``). Factored out so the
    AI-suggestion flow (``ai.suggest``) sends exactly what the export endpoint returns.
    ``profile_limit`` keeps only the top-N profiles by Overall (they're already ranked), to
    bound the payload the model has to read."""
    methodology = ensure_current_methodology(session, get_config(session))
    defn = methodology.definition or {}
    crown_metrics, crown_required = overall_metrics(defn)
    metric_src = all_metric_sources()  # {logical_key: (plugin, source_key)}
    scored_keys = [m["key"] for m in defn.get("metrics", []) if m.get("key") in metric_src]

    # Per-run raw scoring metrics, grouped by profile (comparable, current-methodology runs).
    runs_by_fp: dict[str, list[dict]] = {}
    for run, score, results_by_plugin in _completed_runs_with_scores(session):
        if not _comparable(score):
            continue
        metrics: dict[str, float] = {}
        for key in scored_keys:
            plugin, source_key = metric_src[key]
            val = results_by_plugin.get(plugin, {}).get(source_key)
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                metrics[key] = val
        runs_by_fp.setdefault(run.settings_fingerprint, []).append({
            "run_id": run.id,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "iterations": int(run.iterations or 1),
            "metrics": metrics,
        })
    # Per-metric spread across the *whole* run history (before the sample cap) — so variance is
    # always conveyed even when run_samples is truncated to the latest N.
    dist_by_fp: dict[str, dict] = {
        fp: _metric_distribution(runs, scored_keys) for fp, runs in runs_by_fp.items()
    }
    for fp, runs in runs_by_fp.items():
        runs.sort(key=lambda r: r["created_at"] or "", reverse=True)
        del runs[runs_per_profile:]

    result = compute_profiles(session, complete_only=True)
    # Profiles are already ranked by Overall (best first); keep the top-N when a limit is set.
    ranked = result["profiles"]
    selected = ranked[:profile_limit] if profile_limit else ranked
    profiles_out = []
    for p in selected:
        fp = p["fingerprint"]
        samples = runs_by_fp.get(fp, [])
        profiles_out.append({
            "fingerprint": fp,
            "label": p["label"],
            # FULL profile details — the complete shaper config per pipe (bandwidth, quantum,
            # limit, target, interval, ecn, flows, queues, scheduler): both the tunable levers
            # an AI would vary and the identity fields that define the profile.
            "settings": p["settings"],
            "runs": p["count"],
            "iterations": p["iterations"],
            "confident": p["confident"],
            "first_seen": p.get("first_seen"),
            "last_seen": p.get("last_seen"),
            # SCORING data — how the profile performed. Percentile-normalized Overall (+ IQR),
            # per-crown-metric percentile, the graded axis scores (responsiveness/smoothness/
            # speed/…), and the raw median of every scored metric (ms). Higher percentile /
            # score, lower ms = better.
            "overall": p["overall"],
            "overall_iqr": {"p25": p.get("overall_p25"), "p75": p.get("overall_p75")},
            "crown_percentiles": p.get("crown_norm") or {},
            "axis_scores": p.get("scores") or {},
            "metric_medians": p.get("metrics") or {},
            # Per-metric spread over ALL of this profile's runs (n/min/p25/median/p75/max) —
            # the full variance, independent of the run_samples cap. Lower ms = better.
            "metric_distribution": dist_by_fp.get(fp, {}),
            # The raw scoring metrics per run (most recent first, capped to runs_per_profile).
            "run_samples": samples,
            "run_samples_truncated": len(samples) < p["count"],
        })

    metric_meta = {
        m["key"]: {
            "label": m.get("label"),
            "unit": m.get("unit"),
            "higher_is_better": bool(m.get("higher_is_better")),
            "is_crown_metric": m["key"] in crown_metrics,
        }
        for m in defn.get("metrics", []) if m.get("key")
    }
    # Precomputed settings→outcome relationships (marginal Spearman ρ per writable field × crown
    # metric) so the model reasons over an explicit "this up → that down" map, not just raw rows.
    field_sensitivity = _field_sensitivity(profiles_out, crown_metrics, metric_meta)
    # What the top-Overall profiles share (top-vs-rest per lever) — catches a sweet spot or a
    # combination that a monotone correlation can't see.
    lever_signature = _lever_signature(profiles_out)
    # Where a promising signal is under-sampled — the levers worth collecting MORE data on
    # (so the model can 'kick back' a data request instead of a speculative profile).
    coverage_gaps = _coverage_gaps(profiles_out, field_sensitivity, lever_signature)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runs_per_profile_limit": runs_per_profile,
        "profile_count": len(profiles_out),        # profiles included in this export
        "profiles_available": len(ranked),         # total profiles that could be exported
        "profile_limit": profile_limit,            # None = all; else top-N by Overall
        "methodology": {
            "version": methodology.version,
            "crown_metrics": crown_metrics,
            # What "better" means, spelled out for the model.
            "objective": (
                "Minimize each crown metric (all are times in ms; lower is better). Overall is "
                "the corner over each crown metric's percentile within the measured field "
                "(magnitude-blind rank), 0–100, higher = better. Suggest shaper settings likely "
                "to reduce the crown metrics below the best observed so far."
            ),
            # The best/worst raw actually achieved per crown metric — the frontier to beat.
            "observed_range": result.get("crown_field") or {},
            "metrics": metric_meta,
        },
        # The shaper field model: which params are writable (an AI may only suggest changes to
        # these — apply() can't write the rest), plus sensible ranges, so proposals are valid.
        "shaper_model": {
            "writable_fields": list(WRITABLE_FIELDS),
            "sweepable_fields": list(SWEEPABLE_FIELDS),
            # The shaper has a SEPARATE pipe per direction. Each profile's `settings` is a list
            # of pipes (typically a Download and an Upload pipe, by `label`); every pipe has its
            # OWN tunable quantum/target/interval/ecn/limit/flows and its own bandwidth (in that
            # pipe's `download_bandwidth` field regardless of direction — `upload_bandwidth` is
            # unused/null). Upload shaping matters as much as download — tune both pipes.
            "pipes_note": (
                "Each profile has one pipe per direction (see each pipe's 'label', e.g. Download "
                "and Upload). Tune BOTH: every pipe has independent quantum/target/interval/ecn/"
                "limit/flows and its own bandwidth (the pipe's 'download_bandwidth' field is that "
                "pipe's bandwidth for either direction; 'upload_bandwidth' is unused). Upload "
                "shaping affects responsiveness under load as much as download."
            ),
            # Per field: its kind + the EXACT value format the firewall expects on input, with a
            # live example pulled from a real pipe — so the model returns values in the firewall's
            # own format (e.g. target "5ms" not 5, quantum 3000 not "3000") instead of a variant we
            # then have to coerce.
            "fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "kind": f.kind,
                    "unit": f.unit,
                    "writable": f.writable,
                    "sweepable": f.sweepable,
                    "suggested_range": f.sweep_default,
                    "value_format": _field_format_hint(f),
                    "example": _field_example(f.key, selected),
                }
                for f in SHAPER_FIELDS
            ],
        },
        # Deterministic interpretation layer: how each lever moves each crown metric across the
        # measured field. Computed here (not left to the model to eyeball) so it's trustworthy
        # and chartable regardless of the AI. See `_field_sensitivity`.
        "analysis": {
            "note": (
                "field_sensitivity is a deterministic marginal rank correlation (Spearman ρ) over "
                "the exported profiles: each row is one writable field on one pipe vs one crown "
                "metric OR vs the Overall itself (metric='overall', the rank-corner we crown on) — "
                "'metric_direction' says whether it rises or falls as the field rises, 'effect' "
                "whether that improves or worsens the crown. The 'overall' rows matter most: a lever "
                "can move the Overall while barely correlating with any single raw metric. MARGINAL, "
                "not partial: profiles vary several fields at once, so a relationship can be "
                "confounded. Use it as directional evidence, not an isolated causal effect. "
                "ρ∈[-1,1]; |ρ|≥0.3 is reported as a trend, below that as 'none'."
            ),
            "field_sensitivity": field_sensitivity,
            "top_profile_signature_note": (
                "top_profile_signature answers a DIFFERENT question than the correlations: for each "
                "lever, what the top-Overall profiles (top quartile) run vs the whole field. Use it "
                "when correlations are flat — a lever can show ρ≈0 yet the winners still cluster on a "
                "specific value ('sweet_spot', both extremes worse) or run it systematically "
                "higher/lower. 'pattern' is higher/lower/sweet_spot/none; 'top_value'+'top_range' is "
                "what they share; 'field_range' is the full spread. Prefer proposals that match the "
                "top profiles' shared values on the distinctive levers."
            ),
            "top_profile_signature": lever_signature,
            "coverage_gaps_note": (
                "coverage_gaps flags levers with a PROMISING but UNDER-SAMPLED signal — a "
                "directional pattern or suggestive ρ, but too few distinct values measured (or the "
                "favored direction runs off the edge of what's been tested). For these, the right "
                "move is NOT a finished profile — it's a data request: measure the "
                "`suggested_values` (`sweepable`=true means the Shotgun Sweep can run them directly). "
                "More data beats a guess. Return these as `data_requests`, ranked by `priority`, and "
                "prefer them over speculative suggestions when the signal isn't yet trustworthy."
            ),
            "coverage_gaps": coverage_gaps,
        },
        "profiles": profiles_out,
    }


def _field_format_hint(f) -> str:
    """A one-line description of the exact input format a shaper field expects, so the AI can
    match the firewall's own representation rather than a look-alike."""
    if f.kind == "bool":
        return "boolean (true/false)"
    if f.unit:  # target / interval — the firewall keys these by the bare number
        return f'integer in {f.unit} (bare number, unquoted — e.g. 5, NOT "5{f.unit}")'
    if f.kind == "int":
        return "integer (no units, unquoted)"
    if f.key in ("download_bandwidth", "upload_bandwidth"):
        return 'bandwidth string like "100Mbit" / "1Gbit"'
    return "string"


def _field_example(key: str, profiles: list[dict]):
    """A real value for ``key`` taken from the first profile pipe that has one — a concrete
    template the AI can copy the exact format of."""
    for p in profiles:
        for pipe in (p.get("settings") or []):
            v = pipe.get(key)
            if v is not None:
                return v
    return None


def _discover_live_normalized() -> list[dict] | None:
    """The live firewall settings, normalized (None if discovery fails).

    One best-effort discovery shared by everything a request needs it for — the
    profiles endpoint used to discover twice per page load (once for the active-row
    fingerprint, once for the heirs' reachability filter), each a fresh HTTPS
    round-trip with its own TLS handshake and up to the provider timeout; on a slow
    or unreachable firewall that alone stalled the Settings-Impact page for tens of
    seconds. Discover once, derive both from the result.
    """
    try:
        return normalize(get_provider().discover())
    except Exception:  # noqa: BLE001 — best-effort; callers degrade gracefully
        log.debug("Live settings discovery failed", exc_info=True)
        return None


def _current_fingerprint(live: list[dict] | None = None) -> str | None:
    """Fingerprint of the live firewall settings right now (None if discovery fails).
    Pass ``live`` (a pre-discovered normalized config) to avoid a fresh discovery."""
    if live is None:
        live = _discover_live_normalized()
    try:
        return fingerprint(live) if live is not None else None
    except Exception:  # noqa: BLE001 — best-effort; the UI just won't flag an active row
        log.debug("Could not fingerprint current settings for active-profile flag", exc_info=True)
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
    thresholds the score actually uses (a version may re-anchor a metric's 'best', e.g.
    fcp→150ms), NOT the catalog defaults. Lets the quadrant flag an axis as **saturated**: when every
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


def _compute_heirs(result: dict, session: Session, live: list[dict] | None = None) -> dict:
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
    min_iterations = result.get("min_iterations") or _min_iterations(session)
    stale_minutes = challenger_mod._contender_stale_minutes(session)
    limit = _heir_count(session)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Live environment signature for the reachability filter (best-effort; if discovery
    # fails we don't filter, same as the race start-check). ``live`` is the caller's
    # already-discovered normalized config (shared with the active-row fingerprint) so
    # this doesn't cost a second firewall round-trip per page load.
    if live is None:
        live = _discover_live_normalized()
    reachable_env = None
    try:
        if live is not None:
            reachable_env = environment_signature(live)
    except Exception:  # noqa: BLE001 — best-effort
        log.debug("Heirs: could not derive live environment signature", exc_info=True)

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
                "name": p.get("name"),
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
    """Aggregate completed runs into per-profile rows ranked by the crown corner
    Overall, with the crowned ``best_fingerprint``. Shared by the ``/settings/profiles``
    endpoint and the challenger race (``challenger.py``) so both rank profiles with
    identical logic. Each profile carries ``axis_spreads`` ({axis: {median,p25,p75,n}})
    for the display columns and ``crown_spreads`` (same shape, keyed by the methodology's
    resolved crown metrics — ``overall_metrics``, not the module fallback ``CROWN_METRICS``)
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
    # Keys needed per-run for the weather-adjusted crown: the crown metrics + the setup phases we
    # subtract from fcp/lcp. Captured alongside metric_samples in the run loop.
    _crown_adj_keys = set(crown_metrics) | set(_SETUP_WEATHER_PHASES)
    # Clean (profile-orthogonal) weather covariates — the inputs to each run's measured
    # weather severity for the "vs weather" cohort residual. Shaped covariates are excluded
    # by construction (adjusting with a shaper-moved signal would subtract profile effect).
    _clean_covs = [k for k, clean in _weather_covariates() if clean]
    _capture_keys = _crown_adj_keys | set(_clean_covs)
    # Per-run (fingerprint, overall, covariate readings) for the weather cohort pass.
    weather_runs: list[tuple[str, float, dict[str, float]]] = []
    # ── Recent-evidence window (the "Overall (recent)" COLUMN, not the verdict) ──────
    # A per-profile window over its most recent ``crown_window_iterations`` iterations
    # (walking runs newest-first until the window fills). This briefly WAS the verdict
    # basis, and live data showed why it can't be: ~100 iterations span only a few days —
    # essentially one weather regime per profile — so windowed rankings compared different
    # profiles' different weather head-on and thrashed. The verdict is back on the all-time
    # pool (weather averages out; stable); the window now feeds only the informational
    # ``overall_recent`` column — the drift lens that shows what each profile's Overall
    # would be on current evidence, alongside the vs-weather conditions lens and the
    # fading/rising form chips. 0 disables the column.
    try:
        crown_window = int(
            (get_config(session).get("correlation", {}) or {}).get("crown_window_iterations", 100)
            or 0
        )
    except (TypeError, ValueError):
        crown_window = 100
    in_window: set[int] | None = None
    if crown_window > 0:
        in_window = set()
        _runs_by_fp: dict[str, list] = {}
        for _run, _score, _res in rows:
            _runs_by_fp.setdefault(_run.settings_fingerprint, []).append(_run)
        for _fp_runs in _runs_by_fp.values():
            acc = 0
            for _run in reversed(_fp_runs):  # rows are chronological → newest first
                in_window.add(_run.id)
                acc += int(_run.iterations or 1)
                if acc >= crown_window:
                    break

    # Config-blind baseline: every qualifying run, regardless of profile, defines
    # the time-of-day environment each profile's runs are judged against.
    baseline_points: list[RunPoint] = []
    groups: dict[str, dict] = {}
    metric_src = all_metric_sources()  # {logical_key: (plugin, source_key)} for every metric
    for run, score, results_by_plugin in rows:
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
            # Live fallback for a Score predating the persisted Overall — derived the SAME way the
            # methodology grades it (corner or weighted), so the fallback matches the verdict.
            run_overall = overall_from_definition(methodology.definition or {}, crown_sub)
        # Time baseline carries both smoothness and the per-run Overall, so we can read
        # each profile's "vs typical" (day×hour-adjusted) for the Overall too.
        point_values = {"smoothness": smooth}
        if run_overall is not None:
            point_values["overall"] = run_overall
        # Fingerprint tags the point so the contemporaneous "network weather" baseline can
        # exclude a profile from its own baseline (the day×hour baseline ignores it).
        point = RunPoint(
            created_at=run.created_at, values=point_values, fingerprint=run.settings_fingerprint
        )
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
                # Windowed (most-recent) crown subscores + iteration count → `overall_recent`.
                "recent_subscores": {},
                "recent_iterations": 0,
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
                # …plus the per-run weather-adjusted crown raw (setup-stripped fcp/lcp, raw
                # stall_energy) for the display-only `weather_adjusted_overall`.
                "crown_adj_samples": {},
                "first_seen": run.created_at,
                "last_seen": run.created_at,
            },
        )
        # All-time aggregation (the verdict basis — weather averages out over the pool);
        # runs inside the recent window ALSO feed the windowed crown subscores that
        # become the informational `overall_recent` column.
        windowed = in_window is not None and run.id in in_window
        g["points"].append(point)
        mv = score.metric_values or {}
        if smooth is not None:
            g["smoothness"].append(smooth)
        if speed is not None:
            g["speed"].append(speed)
        # A run with more iterations is more data; track the total alongside runs.
        g["iterations"] += int(run.iterations or 1)
        if comp_axis is not None:
            g["completion"].append(comp_axis)
            g["completion_iterations"] += int(run.iterations or 1)
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
        if windowed:
            g["recent_iterations"] += int(run.iterations or 1)
            for metric in crown_metrics:
                val = crown_sub.get(metric)
                if val is not None:
                    g["recent_subscores"].setdefault(metric, []).append(float(val))
        # Every metric's raw value for this run, from the plugin metric caches
        # (``results_by_plugin``, bulk-fetched by _completed_runs_with_scores), falling back
        # to the current-methodology Score's derived metric_values (keyed by logical key) when
        # the plugin cache predates a metric. A re-grade re-derives from raw into the Score but
        # does not rewrite BenchmarkResult.metrics, so a run captured before a metric existed
        # (e.g. stall_time, added in v8) carries it only on the re-graded Score — sourcing it
        # here lets re-graded history feed the crown normalization + columns, not just fresh
        # runs. (Same source the completion metrics already read from above.)
        run_vals: dict[str, float] = {}  # this run's values for the weather-adjust keys
        for key, (plugin, source_key) in metric_src.items():
            val = results_by_plugin.get(plugin, {}).get(source_key)
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                val = mv.get(key)
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            g["metric_samples"].setdefault(key, []).append(float(val))
            if key in _capture_keys:
                run_vals[key] = float(val)
        # Weather-adjusted crown raw for this run: strip the connection-setup weather (this run's
        # own nav dns+tcp+tls) from the paint milestones; carry shape metrics through unadjusted.
        # A run without the nav waterfall can't be setup-adjusted, so it's left out of that
        # metric's adjusted samples (never fabricated).
        setup = sum(run_vals[p] for p in _SETUP_WEATHER_PHASES if p in run_vals)
        has_setup = any(p in run_vals for p in _SETUP_WEATHER_PHASES)
        for m in crown_metrics:
            if m not in run_vals:
                continue
            if m in _SETUP_ADJUSTED_METRICS:
                if has_setup:
                    g["crown_adj_samples"].setdefault(m, []).append(max(0.0, run_vals[m] - setup))
            else:
                g["crown_adj_samples"].setdefault(m, []).append(run_vals[m])
        # Feed the measured-weather cohort pass: this run's outcome + its own covariate
        # readings (the conditions it faced), for the "vs weather" residual below.
        if run_overall is not None:
            covs = {k: run_vals[k] for k in _clean_covs if k in run_vals}
            weather_runs.append((run.settings_fingerprint, float(run_overall), covs))
        g["settings"] = run.settings
        g["last_seen"] = run.created_at

    # Precompute the day×hour bucketing of the whole field ONCE per metric, and wrap each in a
    # shared _BaselineResolver. The time-adjusted "vs typical" reading below is computed for
    # every profile against this same shared baseline; bucketing is O(all runs), and the
    # fallback-ladder pool + median used to be recomputed per *run* — together they made the
    # endpoint quadratic in history (the reason Settings-Impact took minutes as runs piled up).
    # Build both once here; every profile reuses them.
    smoothness_buckets = bucket_values(baseline_points, "smoothness", tz_offset)
    overall_buckets = bucket_values(baseline_points, "overall", tz_offset)
    smoothness_resolver = _BaselineResolver(smoothness_buckets, min_samples)
    overall_resolver = _BaselineResolver(overall_buckets, min_samples)

    # Measured-weather cohort residuals ("wins above the weather"): severity from each run's
    # own clean covariate readings, cohort = other profiles' runs in the same severity band.
    # Flag-and-steer only — never a crown input (see pathbrain.weather).
    _w_sev = run_severities([covs for _, _, covs in weather_runs], _clean_covs)
    weather_rel_by_fp, weather_sev_by_fp = cohort_residuals(
        [fp for fp, _, _ in weather_runs], [ov for _, ov, _ in weather_runs], _w_sev
    )
    # Tie/significance parameters — shared by the crown-tie machinery below and the
    # per-profile current-form check inside the loop.
    tie_margin, tie_sigma = _crown_tie_params(session)

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
        # Median weather-adjusted crown raw (setup-stripped fcp/lcp, raw stall_energy) — cornered
        # against the field in the normalize pass into the display-only weather_adjusted_overall.
        crown_adj = {m: round(median(vals), 3) for m, vals in g["crown_adj_samples"].items() if vals}
        # Overall + its IQR are computed after the loop (they need the field's best/worst to
        # normalize); placeholders here, filled in the normalize pass.
        overall = overall_p25_val = overall_p75_val = None
        rel = profile_relative(
            baseline_points, g["points"], "smoothness", tz_offset, min_samples,
            resolver=smoothness_resolver,
        )
        # Time-adjusted Overall ("vs typical"): how this profile scored vs the day×hour norm.
        # Kept as an informational signal (display + a hook for smarter heir-hunting), not a
        # crown input — the crown is highest Overall, full stop.
        rel_overall = profile_relative(
            baseline_points, g["points"], "overall", tz_offset, min_samples,
            resolver=overall_resolver,
        )
        profiles.append(
            {
                "fingerprint": g["fingerprint"],
                # `label` stays the technical settings summary (what the profile IS);
                # `name` is its call sign (what to CALL it). Both travel together so a
                # view can lead with the memorable one and keep the precise one at hand.
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
                # Median weather-adjusted crown raw (display-only) → weather_adjusted_overall below.
                "crown_adj": crown_adj,
                "weather_adjusted_overall": None,
                # Optimistic ceiling (field-normalized) — filled in the normalize pass; drives
                # the heirs card + challenger race.
                "optimistic": None,
                # The single corner "overall" — closeness to the fastest-on-all-crown-metrics
                # corner over the raw measurements. This IS the crown basis: highest wins.
                "overall": overall,
                # Time-adjusted ("vs typical") Overall — informational, not a crown input.
                # Kept in the payload for the best-diff comparison; no longer a surfaced column
                # (superseded by the measured-weather `weather_relative` below).
                "relative_overall": rel_overall,
                # "Wins above the weather": this profile's per-run Overall vs the median of
                # OTHER profiles' runs in the same measured-weather severity band, aggregated.
                # {delta_median, p25, p75, count, coverage} — flag-and-steer only, never a
                # crown input. `weather_severity` is the median conditions (0–100 percentile,
                # higher = harsher weather) this profile has been measured under — a
                # sampling-fairness readout on its own.
                "weather_relative": weather_rel_by_fp.get(g["fingerprint"]),
                "weather_severity": weather_sev_by_fp.get(g["fingerprint"]),
                # Filled after ranking: residual standing far above raw standing → flagged.
                "weather_beater": False,
                # "Overall (recent)": the crown grade recomputed over ONLY the most recent
                # crown_window_iterations — the drift lens (what this profile's Overall
                # would be on current evidence). Filled in the normalize pass; None under a
                # non-weighted methodology, with the window disabled, or when the recent
                # window can't supply every required crown subscore.
                "overall_recent": None,
                "recent_iterations": g["recent_iterations"],
                # Median crown subscores over the recent window (the inputs to overall_recent).
                "recent_scores": {
                    m: round(median(vals), 2)
                    for m, vals in g["recent_subscores"].items()
                    if vals
                },
                # Current form vs prior record (recent-window median Overall vs the rest of
                # its history, significance-gated both directions): "fading" = its pooled
                # Overall is propped by a past it no longer delivers; "rising" = its pooled
                # Overall understates what it does now. Flag-and-steer, never a crown input.
                "form": _profile_form(
                    [
                        pt.values["overall"]
                        for pt in g["points"]
                        if pt.values.get("overall") is not None
                    ],
                    tie_sigma,
                ),
                # "% vs SQM off" (filled after the normalize pass, once every Overall is final).
                "pct_vs_sqm_off": None,
                "is_sqm_off": False,
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
    # The crown's combine rule is a methodology property: ``weighted`` ranks on a magnitude-aware
    # weighted average of the perception-calibrated subscores (v15+); otherwise the field-percentile
    # corner. Both read the crown metric set + weights from the definition, so a methodology change
    # re-wires the ranking with no edit here.
    crown_method = overall_method(methodology.definition or {})
    crown_weights = overall_weights(methodology.definition or {})
    for p in profiles:
        res = _normalized_crown(
            p.get("metrics") or {}, p.get("crown_raw") or {}, crown_field, crown_higher,
            crown_metrics, crown_required,
        )
        # Per-metric percentile standings always come from the normalized pass (the display columns).
        p["crown_norm"] = res["norm"]
        if crown_method == "weighted":
            # Weighted average of the calibrated subscore median / quartiles (higher = better, so
            # p75 is the optimistic ceiling). Magnitude-aware + low-noise → the field separates.
            cs = p.get("crown_scores") or {}
            sp = p.get("crown_spreads") or {}
            if any(cs.get(m) is None for m in crown_required):
                # Missing a required crown subscore → no Overall, same quarantine as the corner path.
                p["overall"] = p["overall_p25"] = p["overall_p75"] = p["optimistic"] = None
            else:
                def _w(pick) -> float | None:
                    return weighted_score(
                        [(pick(m), float(crown_weights.get(m, 1.0))) for m in crown_metrics]
                    )

                p["overall"] = _w(lambda m: cs.get(m))
                p["overall_p25"] = _w(lambda m: (sp.get(m) or {}).get("p25"))
                p["overall_p75"] = _w(lambda m: (sp.get(m) or {}).get("p75"))
                p["optimistic"] = _w(lambda m: (sp.get(m) or {}).get("p75"))
            # "Overall (recent)" — the same weighted grade over ONLY the recent window's
            # subscore medians. Informational drift lens; never the crown. Requires every
            # required crown subscore inside the window (no fabricated numbers).
            rs = p.get("recent_scores") or {}
            if rs and not any(rs.get(m) is None for m in crown_required):
                p["overall_recent"] = weighted_score(
                    [(rs.get(m), float(crown_weights.get(m, 1.0))) for m in crown_metrics]
                )
        else:
            p["overall"] = res["overall"]
            p["overall_p25"] = res["p25"]
            p["overall_p75"] = res["p75"]
            p["optimistic"] = res["optimistic"]

    # Display-only weather-adjusted Overall: the SAME percentile-corner as `overall`, but over the
    # setup-stripped crown raw (fcp/lcp minus each run's own connection-setup weather). Its own
    # field so the percentile scale reflects the adjusted values. Never touches crowning.
    adj_field = {
        m: [p["crown_adj"][m] for p in profiles if (p.get("crown_adj") or {}).get(m) is not None]
        for m in crown_metrics
    }
    for p in profiles:
        adj = p.get("crown_adj") or {}
        adj_norm = {
            m: _percentile_norm(adj.get(m), adj_field.get(m), bool(crown_higher.get(m)))
            for m in crown_metrics if adj_field.get(m)
        }
        p["weather_adjusted_overall"] = _crown_corner(adj_norm, crown_metrics, crown_required)

    # ── "% vs SQM off" ──────────────────────────────────────────────────────────────
    # How much each profile's Overall beats (or trails) running with SQM *off* — the honest
    # baseline for what shaping is buying. It's computed straight from the same field-normalized
    # Overall the crown ranks on, so it's wired into the scoring methodology: change the crown
    # metrics (a new methodology) → Overall re-ranks → this % re-derives with it, no separate
    # knob to keep in sync. The bar is the best Overall among measured "SQM off" profiles (any
    # pipe with shaping disabled — see settings_profile.fingerprint). Positive = this profile is
    # that % better than the unshaped link; negative = worse. None when there's no baseline yet
    # or the profile has no Overall.
    def _is_sqm_off(p: dict) -> bool:
        return any((pipe or {}).get("enabled") is False for pipe in (p.get("settings") or []))

    sqm_off_overalls = [p["overall"] for p in profiles if _is_sqm_off(p) and p["overall"] is not None]
    baseline_overall = max(sqm_off_overalls) if sqm_off_overalls else None
    for p in profiles:
        p["is_sqm_off"] = _is_sqm_off(p)
        if baseline_overall and baseline_overall > 0 and p["overall"] is not None:
            p["pct_vs_sqm_off"] = round((p["overall"] - baseline_overall) / baseline_overall * 100, 1)
        else:
            p["pct_vs_sqm_off"] = None

    # Rank the table by the raw-normalized corner "overall"; profiles missing it (no crown
    # metrics captured yet) fall back to smoothness median, sort last.
    # Call signs, in one query for the whole field (see profile_names.names_for).
    _names = profile_names.names_for(session, [p["fingerprint"] for p in profiles])
    for p in profiles:
        p["name"] = _names.get(p["fingerprint"]) or p["fingerprint"][:8]

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
    confident = [p for p in profiles if p["confident"] and p["overall"] is not None]
    best, co_leaders = _select_crown(confident, tie_margin, tie_sigma)
    best_fingerprint = best["fingerprint"] if best else None
    # Co-leaders within noise of the crown (excluding the crown itself) — an informational
    # "this was close" flag, not a re-ranking. Empty when the crown stands clearly apart.
    crown_co_leaders = [fp for fp in co_leaders if fp != best_fingerprint]

    # Crown-lead-vs-noise readout: the actual numbers behind "is #1 a real lead?" — the crown's
    # Overall + its standard error, the gap to the runner-up, and the significance threshold
    # (max(floor, σ · pooled SE)). Lets the UI show measured signal-vs-noise instead of an adjective.
    crown_confidence = None
    if best is not None:
        b_se = _finite(_overall_se(best))
        runner = max(
            (p for p in confident if p["fingerprint"] != best_fingerprint and p.get("overall") is not None),
            key=lambda p: float(p["overall"]),
            default=None,
        )
        crown_confidence = {
            "overall": round(float(best["overall"]), 2),
            "overall_se": round(b_se, 3),
            "runner_up_overall": None,
            "gap_to_runner_up": None,
            "noise_threshold": None,
            "sigma": tie_sigma,
            "clear_lead": True,
            "co_leader_count": len(crown_co_leaders),
            "confident_count": len(confident),
        }
        if runner is not None:
            gap = float(best["overall"]) - float(runner["overall"])
            pooled_se = (b_se ** 2 + _finite(_overall_se(runner)) ** 2) ** 0.5
            thresh = max(tie_margin, tie_sigma * pooled_se)
            crown_confidence.update({
                "runner_up_overall": round(float(runner["overall"]), 2),
                "gap_to_runner_up": round(gap, 3),
                "noise_threshold": round(thresh, 3),
                # A real lead only when the gap clears the significance bar — otherwise it's a tie.
                "clear_lead": gap > thresh,
            })

    # Custom crown: an *exploratory* second take on "best" that corners over a caller-chosen
    # set of betterments (per-metric subscores) instead of the canonical feel trinity. It's
    # a live lens over the same persisted subscores — no re-grade, no methodology change —
    # so the user can ask "which profile wins if I only care about THESE?". The canonical
    # ``best_fingerprint`` is untouched; this is a parallel, simpler argmax of the custom
    # corner among confident profiles (no Thompson — it's a what-if view, not the verdict).
    custom_best_fingerprint = _apply_custom_crown(profiles, custom_crown_metrics)

    # The ghost-crown check: when the CROWN's current form significantly trails its own
    # prior record, its pooled Overall — the bar every challenger must clear — is propped
    # by history it no longer delivers. Surface it so the user re-measures (race / re-run
    # top-N); a rising crown needs no alarm (its bar is merely understated).
    crown_fading = None
    if best is not None and (best.get("form") or {}).get("direction") == "fading":
        crown_fading = {
            "fingerprint": best["fingerprint"],
            "label": best["label"],
            "name": best.get("name"),
            **best["form"],
        }

    # ── Weather-beater flags + crown-suspect (flag-and-steer; the crown stays raw) ──
    # A profile whose "vs weather" residual standing is far above its raw Overall standing
    # delivered average outcomes in conditions where the field delivered below-average ones —
    # "there may be something here". And if the residual ranking's top profile isn't the raw
    # crown, the crown may be weather-confounded: the answer is to RACE them (contemporaneous
    # head-to-head raw data), never to re-rank on the model.
    weather_crown_suspect = None
    ranked_weather = [
        p for p in profiles
        if (p.get("weather_relative") or {}).get("delta_median") is not None
        and (p["weather_relative"].get("coverage") or 0) >= 0.5
    ]
    if len(ranked_weather) >= 3:
        by_residual = sorted(
            ranked_weather, key=lambda p: p["weather_relative"]["delta_median"], reverse=True
        )
        raw_ranked = [p for p in profiles if p.get("overall") is not None]
        raw_pos = {p["fingerprint"]: i for i, p in enumerate(raw_ranked)}
        n_res, n_raw = len(by_residual), max(1, len(raw_ranked))
        for i, p in enumerate(by_residual):
            res_pct = 1 - i / max(1, n_res - 1) if n_res > 1 else 1.0  # 1 = best residual
            rp = raw_pos.get(p["fingerprint"])
            raw_pct = 1 - rp / max(1, n_raw - 1) if rp is not None and n_raw > 1 else 0.0
            # Top-quartile residual standing while sitting in the bottom half raw, with a
            # genuinely positive residual → flagged.
            if res_pct >= 0.75 and raw_pct <= 0.5 and p["weather_relative"]["delta_median"] > 0:
                p["weather_beater"] = True
        top = by_residual[0]
        if (
            best_fingerprint
            and top["fingerprint"] != best_fingerprint
            and top["weather_relative"]["delta_median"] > 0
        ):
            weather_crown_suspect = {
                "fingerprint": top["fingerprint"],
                "label": top["label"],
                "name": top.get("name"),
                "delta_median": top["weather_relative"]["delta_median"],
                "coverage": top["weather_relative"]["coverage"],
            }

    return {
        "profiles": profiles,
        "count": len(profiles),
        "min_runs": min_runs,
        "min_iterations": min_iterations,
        "complete_only": complete_only,
        # The "SQM off" baseline Overall the "% vs SQM off" column is measured against (best
        # Overall among measured SQM-off profiles), or null if no baseline test has run yet.
        "sqm_off_overall": baseline_overall,
        "best_fingerprint": best_fingerprint,
        # Fingerprints statistically tied with the crown (co-leaders) — the crown's median
        # lead over these is within run-to-run noise, so the UI flags them as a tie instead
        # of implying the crown is decisively better. Empty when the crown stands apart.
        "co_leaders": crown_co_leaders,
        # Crown-lead-vs-noise: the measured signal-to-noise behind the #1 verdict — Overall + SE,
        # gap to runner-up, the significance threshold (σ·pooled-SE), and whether the lead clears it.
        "crown_confidence": crown_confidence,
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
        # The residual ranking's top profile when it differs from the raw crown — the
        # "crown may be weather-confounded; race these" signal (None when they agree).
        "weather_crown_suspect": weather_crown_suspect,
        # The ghost-crown signal: the crown's current form significantly trails its own
        # prior record, so the bar challengers race against may be stale (None when steady).
        "crown_fading": crown_fading,
        # The recent-evidence window the informational `overall_recent` column is computed
        # over (iterations per profile; 0 = column disabled). The VERDICT pools all time.
        "crown_window_iterations": crown_window,
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
    10ms → 5ms, direction "lower"), with the resulting **Overall** delta.
    """
    best_idx = next(
        (i for i, p in enumerate(profiles) if p["fingerprint"] == best_fingerprint), None
    )
    if best_idx is None or best_idx + 1 >= len(profiles):
        return None
    best = profiles[best_idx]
    comparison = profiles[best_idx + 1]
    # The gap is measured on the **Overall** (the field-normalized crown corner we rank on),
    # not the legacy Smoothness median. Either profile's Overall can be None (no crown data),
    # so the delta is best-effort.
    best_ov, comp_ov = best.get("overall"), comparison.get("overall")
    delta_abs = (
        round(best_ov - comp_ov, 2) if best_ov is not None and comp_ov is not None else None
    )
    delta_pct = (
        round((delta_abs / comp_ov) * 100, 1) if delta_abs is not None and comp_ov else None
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
        r = p.get("relative_overall")
        return r["delta_median"] if r else None

    best_rel, comp_rel = _rel_median(best), _rel_median(comparison)
    # Time-adjusted advantage: the Overall gap once each profile's day/hour environment is
    # removed. Can differ from the raw delta if the two were sampled at different
    # times — that difference is exactly the confound this strips out.
    relative_delta = (
        round(best_rel - comp_rel, 2) if best_rel is not None and comp_rel is not None else None
    )
    return {
        "best": {
            "fingerprint": best["fingerprint"],
            "label": best["label"],
            "name": best.get("name"),
            "overall": best_ov,
            "completion": best_comp,
            "relative_overall": best_rel,
            "confident": best["confident"],
        },
        "comparison": {
            "fingerprint": comparison["fingerprint"],
            "label": comparison["label"],
            "name": comparison.get("name"),
            "overall": comp_ov,
            "completion": comp_comp,
            "relative_overall": comp_rel,
            "confident": comparison["confident"],
        },
        "delta_abs": delta_abs,
        "delta_pct": delta_pct,
        # Completion can move opposite to the Overall — surfacing it here is the whole
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

    applied = _write_changes(provider, changes)

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


def _write_changes(provider, changes: list[dict]) -> list[dict]:
    """Apply a planned change list to the firewall via ``provider.apply()`` (the only sanctioned
    write path), returning the applied summaries. Raises ``HTTPException`` on the first failure —
    reporting how many writes already landed so a partial apply is visible. Shared by the
    apply-profile and apply-settings endpoints."""
    applied: list[dict] = []
    for ch in changes:
        try:
            provider.apply({"pipe_uuid": ch["pipe_uuid"], "param": ch["param"], "value": ch["value"]})
        except NotImplementedError as exc:
            raise HTTPException(
                status_code=400, detail=f"The {provider.name} provider can't write changes.",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            log.exception("apply write failed on %s after %s change(s)", ch["param"], len(applied))
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Applied {len(applied)} change(s), then failed on "
                    f"{ch['field_label']}: {type(exc).__name__}: {exc}. The firewall may be "
                    "partially changed — re-apply once the issue is resolved."
                ),
            ) from exc
        applied.append({"label": ch["label"], "field_label": ch["field_label"], "to": ch["to"]})
    return applied


@router.post("/settings/apply-settings")
def apply_settings(
    payload: ApplySettings,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
) -> dict:
    """Write an **arbitrary** set of shaper settings (e.g. an AI suggestion) to the firewall
    **permanently** — the same one-way apply as ``apply-profile``, but for settings that aren't a
    stored profile yet. Overlays only *writable* fields onto the live profile (so it's always
    reachable), then:

    * ``preview: true`` → returns the exact planned field writes without touching the firewall,
      for the confirm dialog (the same ``ApplyProfileResult`` shape the profile apply uses).
    * commit → applies via ``provider.apply()`` and, with ``run_benchmark`` (default true), kicks a
      single-iteration benchmark on the new settings in the background.

    Like apply-profile this is a one-way write (to revert, apply another profile); unlike
    ``test-settings`` it does **not** restore a baseline. Rejects a no-op / unreachable change."""
    provider = get_provider()
    try:
        live = provider.discover()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"{provider.name} discovery failed: {type(exc).__name__}: {exc}"
        ) from exc

    live_norm = normalize(live)
    target = _apply_writable_overrides(live_norm, payload.settings)
    if environment_signature(target) != environment_signature(live_norm):
        raise HTTPException(
            status_code=400, detail="Unreachable: the settings change a non-writable field."
        )
    changes, warnings = plan_apply(target, live)
    label = payload.label or summarize(target)
    target_fp = fingerprint(target)

    if payload.preview:
        return {
            "preview": True,
            "fingerprint": target_fp,
            "label": label,
            "changes": changes,
            "warnings": warnings,
            "already_applied": not changes,
        }
    if not changes:
        raise HTTPException(
            status_code=400, detail="Those settings match the current profile — nothing to change."
        )

    applied = _write_changes(provider, changes)
    resulting_fp = None
    try:
        resulting_fp = fingerprint(normalize(provider.discover()))
    except Exception:  # noqa: BLE001
        log.warning("apply-settings post-verify discovery failed", exc_info=True)

    run_id = None
    if payload.run_benchmark:
        from ..runner import create_run
        from .routes_run import _locked_execute

        run_id = create_run(
            label=f"apply · {label}", notes=f"Benchmark after applying settings {target_fp}", iterations=1,
        )
        background.add_task(_locked_execute, run_id)

    log.info("Applied settings %s: %s change(s)%s", target_fp, len(applied),
             f"; benchmark run {run_id}" if run_id else "")
    return {
        "ok": True,
        "fingerprint": target_fp,
        "label": label,
        "applied": applied,
        "warnings": warnings,
        "already_applied": False,
        "resulting_fingerprint": resulting_fp,
        "run_id": run_id,
    }


@router.post("/settings/test-profile")
def test_profile(
    body: dict = Body(...),
    session: Session = Depends(get_session),
) -> dict:
    """Benchmark a stored profile: apply it, run it, restore the previous settings.

    Body: ``{"fingerprint": "<12-hex>", "iterations": <int|null>}``. ``iterations`` is the
    same contract as ``start_settings_test`` — **omitted means top up to the confidence
    minimum** (the long "settle the question" answer, refused when the profile is already
    confident, since there is nothing to top up), and an **explicit count runs exactly that
    many** whatever the profile already has (the short "how is it doing right now?" answer,
    which a confident profile is precisely the kind you want to re-measure). Both are capped
    at ``MAX_ITERATIONS``.

    Returns the started test's id; poll ``GET /settings/test-profile/current`` for status.
    The run holds the coordination lock, so a test queues behind any other firewall
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
    requested = (body or {}).get("iterations")
    if requested is not None:
        try:
            needed = max(1, min(MAX_ITERATIONS, int(requested)))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="iterations must be a number") from None
    else:
        needed = min(MAX_ITERATIONS, max(0, min_iterations - current_iters))
        if needed <= 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Profile already has {current_iters} iteration(s) (minimum "
                    f"{min_iterations}). Pass an explicit iteration count to re-measure it."
                ),
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
        # Which of the two lengths ran, so the caller can say "quick test" vs "topping up"
        # without re-deriving it from the counts.
        "mode": "exact" if requested is not None else "top_up",
    }


def _apply_writable_overrides(live_norm: list[dict], suggested) -> list[dict]:
    """Build a full normalized profile from the live one, overriding ONLY writable fields with a
    suggestion — so the result is always reachable (non-writable topology stays as live).
    ``suggested`` is a list of per-pipe objects (each with a ``label`` matching a live pipe) or a
    single flat dict applied to every pipe."""
    by_label: dict = {}
    flat: dict = {}
    if isinstance(suggested, list):
        for s in suggested:
            if isinstance(s, dict):
                by_label[s.get("label")] = s
    elif isinstance(suggested, dict):
        flat = suggested
    out: list[dict] = []
    for pipe in live_norm:
        p = dict(pipe)
        override = by_label.get(pipe.get("label")) or flat
        for f in WRITABLE_FIELDS:
            if not isinstance(override, dict) or override.get(f) is None:
                continue
            # Overriding a field with the value it already holds is not a change — and
            # re-writing it in a different notation invents a new profile identity out of
            # nothing. ``coerce_value`` canonicalizes to the firewall's wire form ("5ms" -> 5),
            # which for an UNCHANGED field means the target differs from live textually while
            # being identical semantically: ``plan_apply`` plans no write (it compares
            # numerically), the firewall keeps reporting "5ms", and ``fingerprint(target)``
            # names a profile that will never exist. Anything measured then files under the
            # firewall's real fingerprint while the caller holds the invented one — the
            # "tests ran but the profile has no history" bug. So leave live's own
            # representation in place unless the value genuinely differs.
            if _field_equal(f, p.get(f), override[f]):
                continue
            p[f] = coerce_value(f, override[f])
        out.append(p)
    return out


def start_settings_test(
    session: Session,
    settings,
    label: str | None,
    iterations: int | None = None,
) -> dict:
    """Apply an arbitrary set of writable overrides onto the live profile and benchmark it.

    The shared body behind ``POST /settings/test-settings`` and the Explore page's
    "test this recommendation" — one validate → apply → benchmark → restore path, so a
    caller cannot invent a second set of reachability rules.

    ``iterations`` is the number of iterations to run: ``None`` means *top up to the
    confidence minimum* (the long, "settle the question" answer), an explicit count means
    run exactly that many (the short, "did this go anywhere?" answer). Both are capped at
    ``MAX_ITERATIONS`` and floored at one — a top-up on an already-confident profile still
    collects a fresh reading rather than refusing.

    Returns ``{id, fingerprint, iterations, label, existing_iterations}``; raises
    ``HTTPException`` for a no-op, an unreachable change, or a busy pipeline.
    """
    try:
        live = get_provider().discover()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not read the live firewall: {exc}") from exc
    live_norm = normalize(live)
    target = _apply_writable_overrides(live_norm, settings)
    if environment_signature(target) != environment_signature(live_norm):
        raise HTTPException(
            status_code=400, detail="Unreachable: the suggestion changes a non-writable field."
        )

    # Trim the target to what the firewall can actually be driven to. ``plan_apply`` silently
    # drops a field on a pipe it can't write (no live match, no uuid) — the apply "succeeds",
    # the post-apply verify re-plans and again sees nothing left, and the firewall settles on a
    # DIFFERENT profile, which the benchmark then measures and is filed under. Leaving those
    # fields in the target means the fingerprint returned here names a profile that will never
    # exist: the runs land under one key while the caller records another, and the profile
    # reads as having no history. Reverting them makes this fingerprint honest by construction,
    # and the caller is told what was dropped instead of it happening behind the count.
    skipped = unwritable_diffs(target, live)
    warnings: list[str] = []
    if skipped:
        by_label = {p.get("label"): p for p in live_norm}
        for drop in skipped:
            pipe = next((p for p in target if p.get("label") == drop["label"]), None)
            source = by_label.get(drop["label"])
            if pipe is not None and source is not None:
                pipe[drop["field"]] = source.get(drop["field"])
            warnings.append(
                f"{drop['label']}·{drop['field_label']} left at {drop.get('from')} "
                f"(wanted {drop.get('to')}) — {drop['reason']}"
            )
        log.info("Test-settings: reverted %s unappliable field(s): %s", len(skipped), "; ".join(warnings))

    target_fp = fingerprint(target)
    if target_fp == fingerprint(live_norm):
        detail = "Those settings match the current profile — nothing to change."
        if warnings:
            detail = (
                "Nothing left to change: every requested field is on a pipe the firewall "
                "cannot write — " + "; ".join(warnings)
            )
        raise HTTPException(status_code=400, detail=detail)
    existing = _profile_iterations(session, target_fp)
    if iterations is None:
        wanted = max(1, _min_iterations(session) - existing)
    else:
        wanted = max(1, int(iterations))
    wanted = min(MAX_ITERATIONS, wanted)
    try:
        test_id = profile_test_mod.start(target_fp, target, label or "AI suggestion", wanted)
    except RuntimeError as exc:  # another firewall/benchmark session already running
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not start the test: {exc}") from exc
    log.info("Test-settings %s started (fp %s, %s iteration(s))", test_id, target_fp, wanted)
    return {
        "id": test_id,
        "fingerprint": target_fp,
        "iterations": wanted,
        "label": label,
        "existing_iterations": existing,
        # Requested fields the firewall cannot write, reverted so the fingerprint above is real.
        "warnings": warnings,
    }


@router.post("/settings/test-settings")
def test_settings(payload: TestSettings, session: Session = Depends(get_session)) -> dict:
    """Apply an *arbitrary* set of shaper settings (e.g. an AI suggestion) onto the live profile —
    overriding only writable fields — then benchmark it and restore the baseline (a normal profile
    test under the coordinator lock). ``iterations`` runs exactly that many; omitted, it tops the
    profile up to the confidence minimum. Rejects a no-op or a change that touches a non-writable
    field up front, so we never apply something unreachable."""
    return start_settings_test(session, payload.settings, payload.label, payload.iterations)


@router.get("/settings/test-profile/current")
def current_profile_test() -> dict:
    """The most recent profile test, for status polling (``{test: {...} | null}``)."""
    return {"test": profile_test_mod.current()}


@router.post("/settings/test-profile/cancel")
def cancel_profile_test() -> dict:
    """Ask the running profile test ("test to minimum") to stop after its current chunk.
    The baseline is still restored. Returns whether one was active."""
    cancelled = profile_test_mod.cancel()
    return {"cancelled": cancelled, "status": (profile_test_mod.current() or {}).get("status")}


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
    top: int | None = Query(
        None, description="Re-run only the top-N profiles (winner-first by prior methodology)."
    ),
    rank_by: str | None = Query(
        None, description="Methodology version to rank by (defaults to the prior methodology)."
    ),
    session: Session = Depends(get_session),
) -> dict:
    """Preview a 'Re-run profiles' batch: how many profiles, total iterations, and an
    estimated duration (from recent runs' per-iteration timing) — so the UI can show
    'N profiles × M iterations ≈ ~T' before committing. With ``top`` set, previews a
    winner-first subset ranked by ``rank_by`` (or the prior methodology)."""
    return refresh_mod.preview(session, iterations, top=top, rank_by=rank_by)


@router.post("/settings/refresh")
def start_refresh(body: dict = Body(...), session: Session = Depends(get_session)) -> dict:
    """Start a 'Re-run profiles' batch: apply each stored profile, run ``iterations``
    benchmarks on it, and restore the baseline at the end (see ``refresh.py``).

    Body: ``{"iterations": <number>, "top"?: <N>, "rank_by"?: <version>}``. With ``top`` set,
    only the top-N profiles are re-run, **winner-first** by their Overall under ``rank_by`` (or
    the prior methodology) — fresh data for the best performers first after a methodology
    publish. Returns the refresh id; poll ``GET /settings/refresh`` for status.
    """
    iterations = int((body or {}).get("iterations") or 0)
    if iterations <= 0:
        raise HTTPException(status_code=400, detail="iterations must be > 0")
    top_raw = (body or {}).get("top")
    top = int(top_raw) if top_raw not in (None, "") else None
    if top is not None and top <= 0:
        raise HTTPException(status_code=400, detail="top must be > 0 when provided")
    rank_by = (body or {}).get("rank_by") or None
    try:
        refresh_id = refresh_mod.start(iterations, top=top, rank_by=rank_by)
    except RuntimeError as exc:  # already running, or no profiles
        # "already running" is a conflict; "no profiles" is a bad request.
        status = 409 if "already running" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("refresh start failed")
        raise HTTPException(
            status_code=502, detail=f"Could not start the refresh: {type(exc).__name__}: {exc}"
        ) from exc
    return {"id": refresh_id, "iterations": iterations, "top": top}


@router.get("/settings/refresh")
def current_refresh() -> dict:
    """The most recent profile refresh, for status polling (``{refresh: {...} | null}``)."""
    return {"refresh": refresh_mod.current()}


@router.post("/settings/refresh/cancel")
def cancel_refresh() -> dict:
    """Ask the running refresh to stop after the current profile (baseline is restored)."""
    return {"cancelled": refresh_mod.cancel()}


# ── Crown follower ("Follow best") ────────────────────────────────────────────────────


@router.put("/settings/profiles/{fingerprint}/name")
def rename_profile(fingerprint: str, body: dict = Body(...)) -> dict:
    """Give a profile a call sign of your own ("Living Room Fix", "Old Reliable").

    Names are auto-assigned on first sight; this overrides one. Uniqueness is enforced —
    a call sign that names two profiles is worse than no call sign at all.
    """
    try:
        name = profile_names.rename(fingerprint, str((body or {}).get("name") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"fingerprint": fingerprint, "name": name}


@router.get("/settings/crowns")
def crowns(session: Session = Depends(get_session)) -> dict:
    """**Both verdicts side by side** — the pooled crown and the duel champion.

    PathBrain names a best profile two ways, and they answer different questions: the
    pooled crown is the observational argmax over all history ("who has the best record
    across every condition we've measured"), the duel champion is the controlled-trial
    winner ("who beat whom, same weather, head to head"). They can disagree, and that
    disagreement is information — so the Dashboard shows both rather than picking one.
    Only the ``governing`` one is what automation acts on (the crowning policy).

    Deliberately **cheap**: the pooled crown is read from the crown-churn ledger (one
    indexed row) instead of recomputing ``compute_profiles`` on every dashboard load, and
    the duel side reads the matchup ledger. Neither triggers a scoring pass.
    """
    from .. import crown_follower, crowning
    from .. import duel as duel_mod

    rematch_days = int((get_config(session).get("duel", {}) or {}).get("rematch_days", 7) or 7)
    pooled = crown_follower.current_crown(session)
    resolution = crowning.resolve(session, pooled_best_fp=(pooled or {}).get("fingerprint"))

    # The duel side: the ladder's champion plus its record in the ring. `latest_champion`
    # returns None once a verdict ages past the rematch window, so a stale champion is
    # still shown — labelled expired — rather than silently vanishing from the dashboard.
    table = duel_mod.standings()
    champion = table.get("champion")
    fresh = duel_mod.latest_champion(session, max_age_days=rematch_days)
    duel_out = None
    if champion:
        record = next(
            (r for r in table.get("standings", []) if r["fingerprint"] == champion["fingerprint"]),
            None,
        )
        duel_out = {
            **champion,
            "fresh": fresh is not None,
            "freshness_days": rematch_days,
            "wins": (record or {}).get("wins", 0),
            "losses": (record or {}).get("losses", 0),
            "draws": (record or {}).get("draws", 0),
            "matchups": (record or {}).get("matchups", 0),
            "beaten": (record or {}).get("beaten", []),
        }

    # Both verdicts on ONE scale, same vintage: the ledger's ``overall`` on the pooled side
    # was recorded at crowning time (possibly days ago), and the duel side had no Overall at
    # all — so the card couldn't compare the two profiles on the number that actually
    # crowns. ``profile_overalls`` reads each profile's LIVE pooled Overall in one batched
    # indexed query (no ``compute_profiles`` pass, keeping this endpoint cheap), and the
    # delta is signed from the champion's side: positive = the duel champion also measures
    # higher on the pooled record.
    overall_delta = None
    both = [fp for fp in ((pooled or {}).get("fingerprint"), (duel_out or {}).get("fingerprint")) if fp]
    if both:
        try:
            methodology = ensure_current_methodology(session, get_config(session))
            crown_metrics, crown_required = overall_metrics(methodology.definition or {})
            weights = overall_weights(methodology.definition or {})
            live = crown_follower.profile_overalls(
                session, both, methodology.version, crown_metrics, crown_required, weights
            )
            for side in (pooled, duel_out):
                if side and side.get("fingerprint") in live:
                    overall_now, iters = live[side["fingerprint"]]
                    side["overall_now"] = None if overall_now is None else round(overall_now, 2)
                    side["overall_iterations"] = iters
            p_now = (pooled or {}).get("overall_now")
            d_now = (duel_out or {}).get("overall_now")
            if p_now is not None and d_now is not None:
                overall_delta = round(d_now - p_now, 2)
        except Exception:  # noqa: BLE001 — a scoring hiccup must not blank the card
            log.debug("Two-crowns: could not compute live Overalls", exc_info=True)

    # Both verdicts are read by name — the whole point of the card is telling two
    # profiles apart at a glance, which "q1514 t5ms" vs "q1514 t10ms" defeats.
    call_signs = profile_names.names_for(
        session,
        [fp for fp in ((pooled or {}).get("fingerprint"), (duel_out or {}).get("fingerprint")) if fp],
    )
    if pooled:
        pooled["name"] = call_signs.get(pooled["fingerprint"])
    if duel_out:
        duel_out["name"] = call_signs.get(duel_out["fingerprint"])

    last = crown_follower.status().get("last_result") or {}
    return {
        "policy": resolution["policy"],
        "pooled": pooled,
        "duel": duel_out,
        # Which verdict automation currently follows, and whether it fell back.
        "governing": {
            "source": resolution["source"],
            "fingerprint": resolution["fingerprint"],
            "detail": resolution["detail"],
        },
        # Champion's live pooled Overall minus the crown's — how far apart the two
        # verdicts sit on the one scale they share. None until both have a live Overall.
        "overall_delta": overall_delta,
        # True when both verdicts name the same profile — the strongest signal available:
        # the observational field and the head-to-head trial agree.
        "agree": bool(
            pooled
            and duel_out
            and pooled.get("fingerprint") == duel_out.get("fingerprint")
        ),
        "follow_enabled": bool(
            (get_config(session).get("crown_follow", {}) or {}).get("enabled", False)
        ),
        # Whether the firewall was on the governing crown at the follower's last check.
        "on_crown": last.get("on_crown"),
        "checked_at": crown_follower.status().get("last_check_at"),
    }


@router.get("/settings/crown-follow")
def crown_follow_status(session: Session = Depends(get_session)) -> dict:
    """The crown follower's config, last-check status, crown-churn statistics, and the
    newest ledger events — everything the top-bar "Follow best" switch + popover shows.
    Read-only; the churn ledger accrues whether or not following is enabled."""
    from .. import crown_follower

    from .. import crowning
    from .. import duel as duel_mod

    cfg = get_config(session).get("crown_follow", {}) or {}
    rematch_days = int((get_config(session).get("duel", {}) or {}).get("rematch_days", 7) or 7)

    champion = duel_mod.latest_champion(session, max_age_days=rematch_days)
    follow_status = crown_follower.status()
    churn = crown_follower.stats(session)
    events = crown_follower.recent_events(session, limit=20)

    # Call signs, resolved by fingerprint in one query for everything the popover shows.
    # The stored ``label`` on a crown event is a full settings summary — *"Download:
    # 880Mbit q7313 t7 i45 ecn | Upload: 880Mbit q450 t3 i60 ecn"* — so a ledger of crown
    # changes read as two of those with an arrow between them, in a 340px popover, on a
    # phone. It says what changed and never says **who**, which is exactly what call signs
    # exist for and what every other view already leads with. Resolved by fingerprint (not
    # frozen into the row) so a rename lands everywhere at once, and best-effort: naming
    # can never be why the popover fails.
    _named = _crown_follow_names(
        session,
        [
            (follow_status.get("last_result") or {}).get("crown_fingerprint"),
            (follow_status.get("last_result") or {}).get("governing_fingerprint"),
            churn.get("current_crown_fingerprint"),
            (champion or {}).get("fingerprint"),
            *[e.get("fingerprint") for e in events],
            *[e.get("previous_fingerprint") for e in events],
        ],
    )
    last_result = follow_status.get("last_result")
    if isinstance(last_result, dict):
        last_result["crown_name"] = _named.get(last_result.get("crown_fingerprint"))
        last_result["governing_name"] = _named.get(last_result.get("governing_fingerprint"))
    churn["current_crown_name"] = _named.get(churn.get("current_crown_fingerprint"))
    if champion:
        champion["name"] = _named.get(champion.get("fingerprint")) or champion.get("label")
    for e in events:
        e["name"] = _named.get(e.get("fingerprint"))
        e["previous_name"] = _named.get(e.get("previous_fingerprint"))

    return {
        "config": {
            "enabled": bool(cfg.get("enabled", False)),
            "interval_minutes": float(cfg.get("interval_minutes", 360) or 360),
            # The first-class crowning policy: which verdict the follower acts on.
            "policy": crowning.active_policy(session),
        },
        "policies": list(crowning.POLICIES),
        # The duel ladder's latest fresh champion (or null) — shown beside the pooled
        # crown so the popover can display both verdicts whatever the policy.
        "duel_champion": champion,
        "status": follow_status,
        "stats": churn,
        "events": events,
    }


def _crown_follow_names(session: Session, fingerprints: list[str | None]) -> dict[str, str]:
    """Call signs for a mixed bag of (possibly None, possibly repeated) fingerprints."""
    wanted = sorted({fp for fp in fingerprints if fp})
    if not wanted:
        return {}
    try:
        return profile_names.names_for(session, wanted)
    except Exception:  # noqa: BLE001 — a cosmetic lookup can't break the status read
        log.debug("Crown-follow status: could not resolve call signs", exc_info=True)
        return {}


@router.post("/settings/crown-follow")
def crown_follow_update(
    body: dict = Body(...), session: Session = Depends(get_session)
) -> dict:
    """Update the crown follower config (the "Follow best" switch): ``{enabled?,
    interval_minutes?}``. Saved to the DB-backed config; a change pokes the follower so
    the next scheduler tick (≤15s) re-checks instead of waiting out the interval."""
    from .. import crown_follower

    updates: dict = {}
    if "enabled" in (body or {}):
        updates["enabled"] = bool(body["enabled"])
    if "interval_minutes" in (body or {}):
        try:
            interval = float(body["interval_minutes"])
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="interval_minutes must be a number"
            ) from None
        if interval < crown_follower.MIN_INTERVAL_MINUTES:
            raise HTTPException(
                status_code=400,
                detail=f"interval_minutes must be ≥ {crown_follower.MIN_INTERVAL_MINUTES}",
            )
        updates["interval_minutes"] = interval
    if "policy" in (body or {}):
        from .. import crowning

        policy = str(body["policy"] or "").lower()
        if policy not in crowning.POLICIES:
            raise HTTPException(
                status_code=400, detail=f"policy must be one of {list(crowning.POLICIES)}"
            )
        updates["policy"] = policy
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")

    effective = save_config(session, {"crown_follow": updates})
    crown_follower.poke()
    cfg = effective.get("crown_follow", {}) or {}
    log.info("Crown follow config updated: %s", updates)
    return {
        "config": {
            "enabled": bool(cfg.get("enabled", False)),
            "interval_minutes": float(cfg.get("interval_minutes", 360) or 360),
            "policy": str(cfg.get("policy", "pooled") or "pooled"),
        }
    }


@router.post("/settings/crown-follow/sync")
def crown_follow_sync() -> dict:
    """Run a crown check **now** (the popover's "sync now"): records any crown change and,
    when following is enabled, applies the crown immediately. Returns the check result;
    if another firewall session holds the lock the apply is reported as deferred, not
    queued (the next interval retries)."""
    from .. import crown_follower

    return {"result": crown_follower.check()}


@router.get("/settings/impact")
def settings_impact(
    session: Session = Depends(get_session),
    complete_only: bool = Query(
        True, description="Only consider runs comparable under the current methodology."
    ),
) -> dict:
    """Compare the current settings profile to the one before the last change.

    Like ``/settings/profiles``, defaults to runs scored under the latest rubric so
    legacy data doesn't skew the before/after medians. The before/after medians are the
    **Overall** (the headline this view ranks on), read from each run's persisted
    ``axis_scores['overall']``.
    """
    cfg = get_config(session).get("correlation", {}) or {}
    threshold = float(cfg.get("significant_change_pct", 5) or 5)
    min_runs = int(cfg.get("min_runs", 5) or 5)
    min_iterations = _min_iterations(session)
    # This view only needs each run's Overall + fingerprint/settings/iterations — NOT its plugin
    # results or the Score's other JSON blobs. Pull just those columns (no `selectinload(results)`,
    # so we don't load + JSON-decode every BenchmarkResult across all history to compute a
    # before/after median of the last two segments).
    methodology = ensure_current_methodology(session, get_config(session))
    rows = session.execute(
        select(
            Run.settings_fingerprint, Run.settings, Run.iterations, Run.created_at,
            Score.axis_scores, Score.comparability,
        )
        .join(Score, Score.run_id == Run.id)
        .where(
            Run.status == RunStatus.COMPLETE,
            Run.settings_fingerprint.is_not(None),
            Score.methodology_version == methodology.version,
        )
        .order_by(Run.created_at)
    ).all()

    # Build contiguous segments of runs sharing a fingerprint (chronological). Before/after
    # medians are the Overall (the crown roll-up persisted per run), not the legacy Smoothness.
    segments: list[dict] = []
    for fp, settings, iterations, created_at, axis_scores, comparability in rows:
        # Same rule as methodology.is_comparable, on the bare column: only "incomparable" is excluded.
        if complete_only and comparability == "incomparable":
            continue
        overall = (axis_scores or {}).get("overall")
        if overall is None:
            continue
        if not segments or segments[-1]["fingerprint"] != fp:
            segments.append(
                {
                    "fingerprint": fp,
                    "settings": settings,
                    "scores": [],
                    "iterations": 0,
                    "changed_at": created_at,
                }
            )
        segments[-1]["scores"].append(overall)
        segments[-1]["iterations"] += int(iterations or 1)
        segments[-1]["settings"] = settings

    base = {
        "changed": False,
        "threshold_pct": threshold,
        "min_runs": min_runs,
        "min_iterations": min_iterations,
    }
    if len(segments) < 2:
        return base

    prev, cur = segments[-2], segments[-1]
    before = round(median(prev["scores"]), 2)
    after = round(median(cur["scores"]), 2)
    delta_abs = round(after - before, 2)
    delta_pct = round((delta_abs / before) * 100, 1) if before else None
    # Don't make significance calls until both profiles have enough iterations.
    enough_data = prev["iterations"] >= min_iterations and cur["iterations"] >= min_iterations
    significant = enough_data and delta_pct is not None and abs(delta_pct) >= threshold
    # Call signs for the two sides: "Tall Garland → Sincere Kite" is the sentence a human
    # reads; the settings summaries stay beside them for the reader who wants the numbers.
    names = profile_names.names_for(session, [prev["fingerprint"], cur["fingerprint"]])
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
            "name": names.get(prev["fingerprint"]),
            "fingerprint": prev["fingerprint"],
            "median": before,
            "count": len(prev["scores"]),
            "iterations": prev["iterations"],
        },
        "after": {
            "label": summarize(cur["settings"]),
            "name": names.get(cur["fingerprint"]),
            "fingerprint": cur["fingerprint"],
            "median": after,
            "count": len(cur["scores"]),
            "iterations": cur["iterations"],
        },
    }
