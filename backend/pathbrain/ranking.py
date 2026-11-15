"""The profile ranking: the pooled field's per-profile rows, the percentile crown and the
tie-aware crown selection behind ``/settings/profiles``, the challenger race and every
engine that reads ``compute_profiles``.

Moved verbatim from ``api/routes_settings.py`` (S16, S17). The routes keep the HTTP
handlers, the field cache and its single-flight (``compute_profiles``), and import every
name here back, so callers and patches of ``routes_settings.<name>`` keep working for the
code that stayed there. Read-only: nothing here writes the firewall.
"""
from __future__ import annotations

from statistics import median, quantiles

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from . import host_adjust as host_adjust_mod
from .config_store import get_config
from .methodology import corner_score, ensure_current_methodology, latest_score, overall_from_definition, overall_method, overall_weights, ranking_crown, weighted_score
from .metrics import METRIC_ROLES, ROLE_WEATHER, all_metric_sources
from .models import BenchmarkResult, Run, RunStatus, Score
from .scoring import COMPLETION_METRIC_SOURCES
from .settings_overrides import effective_fingerprint, overlay as overlay_settings
from . import profile_names
from .settings_profile import diff_profiles, is_sqm_off_profile, summarize
from .trends import RunPoint, _BaselineResolver, bucket_values, profile_relative
from .weather import cohort_residuals, run_severities


def _crown_corner(
    subscores: dict | None,
    metrics: tuple | list,
    required: tuple | list,
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


def _comparable(score: Score) -> bool:
    # A run is comparable once it has a Score under the current methodology that
    # isn't "incomparable" (i.e. its raw can supply the required metrics). Delegates to
    # the single central predicate so every view filters identically.
    from .methodology import is_comparable

    return is_comparable(score)


def _min_runs(session: Session) -> int:
    return int((get_config(session).get("correlation", {}) or {}).get("min_runs", 5) or 5)


def _min_iterations(session: Session) -> int:
    """Total iterations a profile needs before it counts as confident (the unit of
    signal — a 15-iteration run is worth far more than a 1-iteration one)."""
    return int((get_config(session).get("correlation", {}) or {}).get("min_iterations", 15) or 15)


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
    from .profile_aggregates import median_se

    # A profile graded by the shrinkage pass carries its SE already — the same IQR/√runs,
    # floored for a thin profile at the field's per-run noise over √runs (`shrinkage.
    # profile_se`), so one run's IQR of zero never reads as a noiseless median here either.
    if p.get("overall_se") is not None:
        return float(p["overall_se"])
    iqr = _overall_iqr(p)
    if iqr == float("inf"):
        return float("inf")
    # ``count`` is the RUN count — the quartiles are over per-run Overalls — and the one
    # arithmetic lives in ``profile_aggregates.median_se`` so the verdict card and the fused
    # ranking read the same number for the same profile.
    se = median_se(iqr, int(p.get("count") or 0))
    return float("inf") if se is None else se


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
    follows it deterministically. The Overall is carried at full precision, so an exact tie
    is a genuine coincidence rather than two readings rounded onto one decimal; when it does
    happen it breaks toward the higher **shrunk** reading (`overall_shrunk`, the posterior
    under the field prior — the profile whose reading rests on more evidence), then the
    more-measured, then the most-recently-seen profile.

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
    # equality: the higher posterior, then more iterations, then most recent). A hair of a
    # lead still wins.
    def _key(p: dict) -> tuple:
        shrunk = p.get("overall_shrunk")
        return (
            float(p["overall"]),
            float(shrunk) if shrunk is not None else float("-inf"),
            int(p.get("iterations") or 0),
            p.get("last_seen") or "",
        )

    best = max(scored, key=_key)
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
        effective_fingerprint().is_not(None),  # a run's own fingerprint, or its override (S22)
        Score.methodology_version == methodology.version,
        latest_score(),
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
    # Callers read run.settings / run.settings_fingerprint: give them the effective values
    # (override if present, else the run's own) as loaded state, never written back.
    overlay_settings(session, [run for run, _ in rows])
    return [(run, score, metrics_by_run.get(run.id, {})) for run, score in rows]


# --- Metric-based "vs weather": how much do conditions move the crown metrics? ----------
# The current "vs weather" (trends.profile_weather_relative) infers conditions from the rolling
# median of *other profiles' runs* — contaminated by which profiles we happened to run. This is
# the first, informational step toward a self-contained replacement: use the weather signals each
# run *co-measures* (probe instruments + the load's own connection-setup phases) and quantify how
# much each crown metric actually responds to them. Deterministic, changes no scores; validates
# whether a weather adjustment is worth building and which covariates to build it from.
_WEATHER_SHAPED = {"download", "transfer", "nav_response"}   # the shaper moves these — never adjust with them


_WEATHER_NAV_SETUP = ["nav_dns", "nav_tcp", "nav_tls", "nav_request", "nav_response"]


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


def _compute_profiles_uncached(
    session: Session,
    complete_only: bool = True,
    tz_offset: int = 0,
    custom_crown_metrics: list[str] | None = None,
    include_weather: bool = True,
) -> dict:
    """Aggregate completed runs into per-profile rows ranked by the crown corner
    Overall, with the crowned ``best_fingerprint``. Shared by the ``/settings/profiles``
    endpoint and the challenger race (``challenger.py``) so both rank profiles with
    identical logic. Each profile carries ``axis_spreads`` ({axis: {median,p25,p75,n}})
    for the display columns and ``crown_spreads`` (same shape, keyed by the methodology's
    resolved crown metrics — ``methodology.ranking_crown``)
    so a caller can compute an ``optimistic_overall`` for not-yet-confident profiles."""
    min_runs = _min_runs(session)
    min_iterations = _min_iterations(session)
    min_samples = int((get_config(session).get("trends", {}) or {}).get("min_samples", 3) or 3)
    rows = _completed_runs_with_scores(session)
    # The crown metric set, from the current methodology — the single source of truth
    # shared by the persisted Overall, the live fallback, crown_spreads, optimistic_overall,
    # and the challenger race (a definition with no `overall` spec ranks by the legacy crown
    # the methodology names: `methodology.ranking_crown`).
    methodology = ensure_current_methodology(session, get_config(session))
    crown_metrics, crown_required = ranking_crown(methodology.definition or {})
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
    # The pooled host adjustment (host_adjust.py, R9): the ring's β — fitted on same-profile
    # legs minutes apart, so it prices only the machine's own speed — read against the field's
    # reference machine, so a profile measured through the host's nightly slow window is priced
    # beside its raw Overall. A reading, never a crown input; a failed fit is no reading.
    host_model = None
    host_vectors: list[list[float]] = []
    try:
        if bool((get_config(session).get("duel") or {}).get("host_adjust", True)):
            host_model = host_adjust_mod.model_for(session, methodology.version)
    except Exception:  # noqa: BLE001 — a reading, never a failed field
        host_model = None
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
                # …and (per-run Overall, log host vector) pairs for the pooled host adjustment.
                "host_runs": [],
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
        if host_model is not None and host_model.active and run_overall is not None:
            host_vec = host_adjust_mod._log_vector((run.instrument_health or {}).get("readings"))
            if host_vec is not None:
                host_vectors.append(host_vec)
                g["host_runs"].append((float(run_overall), host_vec))
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
    # `include_weather=False` skips it outright. It is the single most expensive pass in
    # this function (~30% of it: a percentile rank of every run against every covariate's
    # whole-history distribution, then a cohort median per severity band) and it feeds
    # exactly two display-only fields. The engines that call `compute_profiles` for the
    # *field* — the duel, the challenger race, the crown follower, explore — read neither,
    # so they were paying for it on every call.
    if include_weather:
        _w_sev = run_severities([covs for _, _, covs in weather_runs], _clean_covs)
        weather_rel_by_fp, weather_sev_by_fp = cohort_residuals(
            [fp for fp, _, _ in weather_runs], [ov for _, ov, _ in weather_runs], _w_sev
        )
    else:
        weather_rel_by_fp, weather_sev_by_fp = {}, {}
    # Tie/significance parameters — shared by the crown-tie machinery below and the
    # per-profile current-form check inside the loop.
    tie_margin, tie_sigma = _crown_tie_params(session)

    # The field's reference machine for the pooled host adjustment: the mean log host reading
    # over every comparable run with readings (None without any).
    host_reference = host_adjust_mod.pooled_reference(host_vectors) if host_vectors else None

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
        # Host-adjusted Overall: each run's Overall less what the machine's speed on that run
        # was worth against the reference machine (β · Δlog host readings), medianed over the
        # runs that carry readings. `host_adjustment` is the shift it made to this profile.
        host_adj_overall = host_adjustment = None
        host_adj_n = 0
        if host_reference is not None and g["host_runs"]:
            adjusted, raw_same = [], []
            for run_ov, vec in g["host_runs"]:
                adj = host_adjust_mod.pooled_adjustment(host_model, vec, host_reference)
                if adj is None:
                    continue
                adjusted.append(run_ov - adj)
                raw_same.append(run_ov)
            if adjusted:
                host_adj_n = len(adjusted)
                host_adj_overall = round(median(adjusted), 2)
                host_adjustment = round(median(adjusted) - median(raw_same), 2)
        profiles.append(
            {
                "fingerprint": g["fingerprint"],
                # `label` stays the technical settings summary (what the profile IS);
                # `name` is its call sign (what to CALL it). Both travel together so a
                # view can lead with the memorable one and keep the precise one at hand.
                "label": summarize(g["settings"]),
                "settings": g["settings"],
                "count": count,
                # The pooled host adjustment (display only): this profile's Overall with the
                # measuring machine's own speed priced out, how many of its runs carried the
                # readings, and the shift (adjusted − raw over those runs).
                "overall_host_adjusted": host_adj_overall,
                "host_adjusted_runs": host_adj_n,
                "host_adjustment": host_adjustment,
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
        return is_sqm_off_profile(p.get("fingerprint"), p.get("settings"))

    sqm_off_overalls = [p["overall"] for p in profiles if _is_sqm_off(p) and p["overall"] is not None]
    baseline_overall = max(sqm_off_overalls) if sqm_off_overalls else None
    for p in profiles:
        p["is_sqm_off"] = _is_sqm_off(p)
        if baseline_overall and baseline_overall > 0 and p["overall"] is not None:
            p["pct_vs_sqm_off"] = round((p["overall"] - baseline_overall) / baseline_overall * 100, 1)
        else:
            p["pct_vs_sqm_off"] = None

    # ── Shrinkage pass: the field as a prior ────────────────────────────────────────
    # Under the weighted crown each profile's Overall is its own median, so the field can be
    # read as a prior and every reading shrunk toward it by exactly as much as its own noise
    # warrants (`shrinkage`). The crown below is still the argmax of the RAW median — that
    # rule is untouched — but `optimistic`, the number that admits a thin profile to the
    # ring, the race and the heirs card, becomes the posterior CEILING: a one-run profile's
    # p75 was its own point estimate, so admission was a coin flip on the run that happened
    # to land high. The unshaped baseline is left out of the prior (a different regime, and
    # it would only widen τ); it is still graded against the prior like everyone else.
    shrinkage_info: dict | None = None
    if crown_method == "weighted":
        from .shrinkage import fit as _fit_prior, grade as _shrink_grade

        prior = _fit_prior([
            (p["overall"], _overall_iqr(p), p.get("count"))
            for p in profiles if p.get("overall") is not None and not p["is_sqm_off"]
        ])
        for p in profiles:
            iqr = _overall_iqr(p)
            g = _shrink_grade(prior, p.get("overall"), None if iqr == float("inf") else iqr, p.get("count"))
            # The p75 ceiling the gate used to read, kept beside the posterior one for the record.
            p["optimistic_p75"] = p.get("optimistic")
            if g is None:
                p["overall_shrunk"] = p["overall_shrunk_se"] = p["overall_se"] = None
                continue
            p["overall_shrunk"], p["overall_shrunk_se"] = g["shrunk"], g["shrunk_se"]
            p["overall_se"] = g["se"]
            p["optimistic"] = g["ceiling"]
        shrinkage_info = prior.as_dict()

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

    # The pooled host adjustment's own readout, and the one question it exists to ask: does
    # the host-adjusted argmax name a different profile than the raw crown? If it does the
    # crown may owe its rank to a faster machine — the case for a RACE (back-to-back legs on
    # the same machine), never a re-crown: `host_crown_suspect` is flag-and-steer exactly as
    # `weather_crown_suspect` is.
    host_adjust_info = None
    host_crown_suspect = None
    if host_model is not None:
        host_adjust_info = {
            **host_model.describe(),
            "runs_with_readings": len(host_vectors),
            "reference": host_adjust_mod.describe_reference(host_reference),
            "crown_raw": best_fingerprint,
            "crown_adjusted": None,
        }
        adjusted_field = [p for p in confident if p.get("overall_host_adjusted") is not None]
        if host_reference is not None and adjusted_field and best_fingerprint:
            top = max(adjusted_field, key=lambda p: (p["overall_host_adjusted"], p["iterations"]))
            host_adjust_info["crown_adjusted"] = top["fingerprint"]
            crown_p = next((p for p in confident if p["fingerprint"] == best_fingerprint), None)
            if top["fingerprint"] != best_fingerprint and crown_p is not None:
                crown_adj = crown_p.get("overall_host_adjusted")
                host_crown_suspect = {
                    "fingerprint": top["fingerprint"],
                    "label": top["label"],
                    "name": top.get("name"),
                    "overall": top.get("overall"),
                    "overall_host_adjusted": top["overall_host_adjusted"],
                    "host_adjustment": top.get("host_adjustment"),
                    "crown_fingerprint": best_fingerprint,
                    "crown_label": crown_p["label"],
                    "crown_name": crown_p.get("name"),
                    "crown_overall": crown_p.get("overall"),
                    "crown_overall_host_adjusted": crown_adj,
                    "crown_host_adjustment": crown_p.get("host_adjustment"),
                    "why": (
                        f"With the measuring machine's own speed priced out, {top['label']} reads "
                        f"{top['overall_host_adjusted']:.2f} against the crown's "
                        f"{crown_adj if crown_adj is None else format(crown_adj, '.2f')} — the crown may "
                        f"owe its rank to a faster machine during its runs. A head-to-head race on one "
                        f"machine settles it; nothing is re-ranked."
                    ),
                }

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
        # The field-as-prior behind `overall_shrunk` and the posterior `optimistic` ceiling:
        # centre, between-profile τ, pooled per-run noise and the basis of each (`shrinkage.py`).
        # None under a field-relative (corner) crown, where a profile has no reading of its own.
        "shrinkage": shrinkage_info,
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
        # The pooled host adjustment (host_adjust.py): the ring's β, the field's reference
        # machine, how many runs carried readings, and the raw vs host-adjusted argmax. Null
        # when the adjustment is switched off (`duel.host_adjust`).
        "host_adjust": host_adjust_info,
        # The host-adjusted argmax when it differs from the raw crown — "the crown may owe its
        # rank to a faster machine; race these" (None when they agree or nothing is adjusted).
        "host_crown_suspect": host_crown_suspect,
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
