"""Settings-vs-responsiveness correlation endpoints.

Groups completed runs by the firewall/SQM profile that was live when they ran,
and flags the most recent settings change when it moved the median SOPS beyond a
configurable threshold.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from statistics import median, quantiles

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer, selectinload

from .. import challenger as challenger_mod
from .. import crowning
from .. import host_adjust as host_adjust_mod
from .. import profile_test as profile_test_mod
from .. import refresh as refresh_mod
from .. import why as why_mod
from ..config_store import get_config, save_config
from ..database import get_session
from .. import job_queue
from ..logging_config import get_logger
from ..methodology import (
    latest_score,
    corner_score,
    ensure_current_methodology,
    overall_from_definition,
    overall_metrics,
    overall_method,
    overall_weights,
    ranking_crown,
    weighted_score,
)
from ..metrics import METRIC_ROLES, ROLE_WEATHER, all_metric_sources
from ..ranking import (  # noqa: F401 — moved to `ranking` (S16, S17); re-exported for callers and patches
    FORM_MIN_PRIOR_RUNS,
    FORM_RECENT_RUNS,
    RACE_OPTIMISM_MARGIN,
    _PROFILE_FIELDS,
    _SETUP_ADJUSTED_METRICS,
    _SETUP_WEATHER_PHASES,
    _WEATHER_NAV_SETUP,
    _WEATHER_SHAPED,
    _apply_custom_crown,
    _best_diff,
    _clearly_better,
    _comparable,
    _completed_runs_with_scores,
    _compute_profiles_uncached,
    _crown_corner,
    _crown_field_values,
    _crown_tie_params,
    _finite,
    _median_se_of,
    _min_iterations,
    _min_runs,
    _normalized_crown,
    _overall_iqr,
    _overall_se,
    _percentile_norm,
    _profile_form,
    _round2,
    _select_crown,
    _spread,
    _weather_covariates,
)
from ..settings_overrides import effective_fingerprint, effective_settings, overrides_for, record as record_override
from ..models import BenchmarkResult, Run, RunStatus, Score
from ..firewall_write import apply_changes, normalize_changes
from ..providers import get_provider
from ..runner import MAX_ITERATIONS
from ..schemas import ApplySettings, TestSettings
from ..scoring import COMPLETION_METRIC_SOURCES
from .. import profile_names
from ..settings_profile import (
    _field_equal,
    _to_number,
    describe_unreachable,
    diff_profiles,
    environment_signature,
    fingerprint,
    is_sqm_off_profile,
    normalize,
    plan_apply,
    summarize,
    unreachable_fields,
    unwritable_diffs,
)
from ..shaper_fields import SHAPER_FIELDS, SWEEPABLE_FIELDS, WRITABLE_FIELDS, coerce_value, field as shaper_field
from ..trends import RunPoint, _BaselineResolver, bucket_values, profile_relative
from ..weather import cohort_residuals, run_severities

# The three headline axes (the temporal phases of a load); their 0–100 scores still
# drive the per-axis display columns, but the **crown** no longer corners over them.
_CORNER_AXES = ("responsiveness", "smoothness", "speed")

# The crown corners over a small set of per-metric 0–100 subscores (perception-calibrated
# by the scoring engine, carried on every Score). The set is always read from the current
# methodology (``methodology.ranking_crown``: its ``overall`` spec, or for a definition with
# none, the legacy crown it names). Everything that corners — ``_crown_corner``,
# ``crown_spreads``, ``optimistic_overall``, and the challenger race — reads that set, so
# the crown always follows the methodology and this module names no crown leg.


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


router = APIRouter()
log = get_logger("api.settings")


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


@router.get("/settings/profiles/{fingerprint}/why")
def profile_why(
    fingerprint: str,
    vs: str | None = Query(None, description="Fingerprint to compare against (default: SQM off, else the crown)."),
    limit: int = Query(0, ge=0, le=why_mod.SITE_RUN_MAX, description="Newest runs a side for the per-site pass (0, the default, skips it — it re-derives every page of every run from stored raw)."),
    session: Session = Depends(get_session),
) -> dict:
    """**Where a win lives**: what the Overall gap between this profile and a reference is made
    of — per crown leg (exact under the weighted crown), per navigation phase, and per site,
    every delta with its noise bar. Read-only; see ``why.explain``."""
    try:
        return why_mod.explain(session, fingerprint, vs=vs, site_run_limit=limit)
    except why_mod.NoData as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/settings/profiles/{fingerprint}/conditions")
def profile_conditions(fingerprint: str, session: Session = Depends(get_session)) -> dict:
    """When this profile was measured (hour of day) and whether the machine was slow while it
    was — a profile measured mostly in the host's slow hours wears that in its standing.
    Read-only; see ``instrument_health.profile_conditions``."""
    from .. import instrument_health

    return instrument_health.profile_conditions(session, fingerprint)


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
            Run.status == RunStatus.COMPLETE, effective_fingerprint() == fingerprint
        )
    ) or 0
    if total == 0:
        raise HTTPException(status_code=404, detail=f"No completed runs for profile {fingerprint}")

    base = (
        select(Run)
        .where(Run.status == RunStatus.COMPLETE, effective_fingerprint() == fingerprint)
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
        select(Run.id, Run.created_at, Run.label, effective_fingerprint().label("settings_fingerprint"))
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
            latest_score(),
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
    """Attribute the *current* firewall settings to completed runs that have none.

    Use when historical runs predate settings-capture (or ran while discovery was
    failing) AND the firewall hasn't changed since — they then aggregate into the
    current profile. Only touches runs with no settings, captured or overridden.

    The run rows are never changed (S22): each run gets two append-only
    ``settings_overrides`` rows (settings and fingerprint, source ``backfill``), which the
    ranking and profile readers apply through :mod:`pathbrain.settings_overrides`.
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

    run_ids = session.scalars(
        select(Run.id).where(Run.status == RunStatus.COMPLETE, effective_fingerprint().is_(None))
    ).all()
    reason = "settings-less run attributed to the firewall profile current at backfill"
    for run_id in run_ids:
        record_override(session, run_id, "settings", normalized, source="backfill", reason=reason)
        record_override(session, run_id, "settings_fingerprint", fp, source="backfill", reason=reason)
    session.commit()
    if run_ids:
        # The field cache's stamp counts runs and scores, which an override moves neither of.
        invalidate_profiles_cache()
    return {"updated": len(run_ids), "fingerprint": fp}


@router.post("/settings/refingerprint")
def refingerprint_runs(
    session: Session = Depends(get_session),
) -> dict:
    """Recompute every run's ``settings_fingerprint`` from its *own* captured settings — a one-shot
    re-key of history under the current fingerprint scheme, preserving all the underlying data.

    Two collapses, both idempotent. **"SQM off" runs fold into the single canonical SQM-off
    profile**: their fingerprints used to vary with the (inert) shaper field values the firewall
    echoes back while disabled, splintering the baseline into many one-off profiles. And
    **spelling variants fold onto the firewall's own form** (`shaper_fields.canonical_value`):
    a run whose stored settings say ``target: "5ms"`` or ``5`` re-keys onto the profile the
    firewall reports as ``"5"``. A run recorded off a real OPNsense ``discover()`` is already in
    that form and is left untouched — on a live install this re-keys nothing but SQM-off
    history; on a mock-backed dev database it also folds the mock's ``"5ms"`` spellings.

    **Overridden runs (S22)** are re-keyed from their *effective* settings, and the new key is
    appended as a ``settings_fingerprint`` override (source ``refingerprint``) — their row is
    never written, so the correction history stays complete and the row keeps what was
    captured. A run with no override is re-keyed in place as before: its row *is* its record."""
    runs = session.scalars(
        select(Run).where(Run.status == RunStatus.COMPLETE, effective_settings().is_not(None))
    ).all()
    found = overrides_for(session, [r.id for r in runs])
    rekeyed = 0
    for run in runs:
        over = found.get(run.id)
        settings = over.get("settings", run.settings) if over else run.settings
        current_fp = over.get("settings_fingerprint", run.settings_fingerprint) if over else run.settings_fingerprint
        try:
            new_fp = fingerprint(settings or [])
        except Exception:  # noqa: BLE001 — one odd row must not abort the whole re-key
            continue
        if new_fp == current_fp:
            continue
        if over:
            record_override(session, run.id, "settings_fingerprint", new_fp, source="refingerprint",
                            reason=f"re-keyed from {current_fp!r} under the current fingerprint scheme")
        else:
            run.settings_fingerprint = new_fp
        rekeyed += 1
    session.commit()
    log.info("Re-fingerprinted %d of %d completed runs", rekeyed, len(runs))
    if rekeyed:
        # Re-keying regroups profiles, which can re-rank the field with no run completing —
        # wake the crown follower for a fresh full check.
        from .. import crown_follower

        # …and drop the memoized field. This is the one mutation the cache stamp cannot
        # see: re-keying rewrites `settings_fingerprint` IN PLACE, so the run and score
        # counts and their max ids are all unchanged while the grouping underneath has
        # moved. Every other path (new runs, re-grades) mints rows and is caught by the
        # stamp; this one has to say so.
        invalidate_profiles_cache()
        crown_follower.poke()
    return {"scanned": len(runs), "rekeyed": rekeyed}


@router.get("/settings/profiles")
def settings_profiles(
    response: Response,
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
    # One verification of the per-profile rollups for the whole read: the heirs, the seeded
    # field and the fused ranking below each ask for the same aggregates, and re-verifying
    # them per ask was most of this endpoint's time on the NAS.
    from .. import profile_aggregates

    timer = _PhaseTimer()
    with profile_aggregates.request_memo():
        result = _settings_profiles(session, complete_only, tz_offset, crown_metrics, timer)
    # Where the time went, per phase, as a Server-Timing header (visible in the browser's
    # network panel or a fetch) and one log line. Read-only diagnostics: the payload is
    # unchanged.
    response.headers["Server-Timing"] = timer.header()
    log.info("GET /settings/profiles: %s", timer.summary())
    return result


class _PhaseTimer:
    """Wall-clock per phase of one Profiles read, in milliseconds."""

    def __init__(self) -> None:
        self._start = self._last = time.perf_counter()
        self.phases: list[tuple[str, float]] = []

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        self.phases.append((name, (now - self._last) * 1000.0))
        self._last = now

    def _all(self) -> list[tuple[str, float]]:
        return [*self.phases, ("total", (time.perf_counter() - self._start) * 1000.0)]

    def header(self) -> str:
        return ", ".join(f"{name};dur={ms:.0f}" for name, ms in self._all())

    def summary(self) -> str:
        return " ".join(f"{name}={ms:.0f}ms" for name, ms in self._all())


def _settings_profiles(
    session: Session, complete_only: bool, tz_offset: int, crown_metrics: str | None,
    timer: _PhaseTimer | None = None,
) -> dict:
    mark = timer.mark if timer is not None else (lambda _name: None)
    custom = [m.strip() for m in (crown_metrics or "").split(",") if m.strip()] or None
    result = compute_profiles(
        session, complete_only=complete_only, tz_offset=tz_offset, custom_crown_metrics=custom
    )
    mark("field")
    # One live discovery per request, shared by the active-row fingerprint and the heirs'
    # reachability filter (two separate discover() round-trips used to stall the page).
    live = _discover_live_normalized()
    mark("discover")
    # The live profile, for flagging the active row only — it no longer influences the crown
    # (the crown follows the highest median Overall, whoever wins, by any margin).
    result["current_fingerprint"] = _current_fingerprint(live)
    # The crown's heirs (limited-data / stale profiles that could still dethrone it), the
    # effective per-metric thresholds (so the quadrant can flag a saturated axis), and the
    # methodology saturation report (metrics whose 'best' is too lenient to rank profiles).
    definition = ensure_current_methodology(session, get_config(session)).definition or {}
    # With no crown under the current methodology (right after a publish — a new crown
    # metric, a changed site list) the heirs are computed over a field SEEDED from the
    # prior version's standings, so the card says which profiles to measure first rather
    # than going blank. The seed is a copy: the memoized field never carries it, and the
    # standings below stay the current version's own (nothing seeded is scored).
    seeded = _seeded_field(session, result)
    mark("seed")
    result["heirs"] = _compute_heirs(seeded, session, live)
    mark("heirs")
    result["seeded"] = _seed_summary(session, seeded)
    result["metric_thresholds"] = _metric_thresholds(definition)
    result["saturation"] = _saturation_report(result["profiles"], definition)
    # Outliers — profiles whose crown-metric medians (or Overall) sit far outside the field.
    # Computed here, once, so "Hide outliers" (a view choice) and "Re-run outliers" (an
    # evidence choice) both read one flag instead of each forming its own opinion.
    result["outliers"] = _outlier_report(
        result["profiles"], result.get("overall_metrics") or [], definition,
        int(result.get("min_iterations") or 0),
    )

    # ── The primary ordering: the ring where it has measured, pooled where it hasn't ────
    # Applied HERE, at the one seam every reader of this endpoint shares, rather than in
    # each surface — five surfaces blending two scales privately is how the verdicts drift
    # apart. `best_fingerprint` stays the POOLED crown on purpose: the duel's own
    # matchmaking reads it as the independent opinion, and re-pointing it at the ring would
    # make the ladder choose who gets checked against the ladder.
    mark("extras")
    ranked = crowning.rank_field(session, result)
    mark("rank")
    by_fp = ranked["by_fingerprint"]
    for profile in result["profiles"]:
        entry = by_fp.get(profile["fingerprint"]) or {}
        profile["verdict_source"] = entry.get("source")
        profile["primary_rank"] = entry.get("position")
        profile["ring_rating"] = entry.get("ring_rating")
        profile["ring_rounds"] = entry.get("ring_rounds")
        profile["primary_score"] = entry.get("score")
        profile["primary_score_source"] = entry.get("score_source")
    order = {fp: i for i, fp in enumerate(ranked["order"])}
    result["profiles"].sort(key=lambda p: order.get(p["fingerprint"], len(order)))
    result["ranking"] = ranked["ranking"]
    # The field's best profile under the primary ordering, kept BESIDE the pooled crown
    # rather than overwriting it, so a caller always knows which verdict it is reading.
    result["primary_best_fingerprint"] = ranked["best_fingerprint"]
    result["primary_best_source"] = ranked["best_source"]
    result["ring_rated_count"] = ranked["ring_rated"]
    result["seeded_count"] = ranked["seeded"]
    result["score_sources"] = ranked.get("score_sources")
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
    covariates (the ``clean`` ones) to build it from.

    The response also carries ``variance`` — the **variance decomposition** (see
    ``_weather_variance``): per crown metric + the per-run Overall, how much of the
    within-profile run-to-run variance the clean covariates jointly explain. The ρ table
    says which covariate moves which metric; the decomposition answers the budget question
    ("how much of the noise is measurable weather at all?") whose complement is the
    measured justification for the duel's paired design.

    **Memoized on the field stamp** for the same reason `compute_profiles` is: this is a
    pure-Python pass over every comparable run (the rank correlations were already heavy;
    the decomposition's least-squares adds an O(runs·covariates²) accumulation per
    outcome), it holds the GIL for its whole duration, and it is a pure function of the
    runs + scores + methodology — exactly what `_field_stamp` is the cheap identity of. A
    hit costs two indexed aggregates; a miss computes once per new data."""
    global _weather_memo
    stamp = _field_stamp(session)
    memo = _weather_memo
    if memo is not None and memo[0] == stamp:
        return memo[1]
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
    result = _weather_sensitivity(session, crown_metrics, metric_meta)
    # Two callers racing a cold cache both compute — wasteful once, and far better than one
    # blocking the other for the whole pass (the compute_profiles memo makes the same call).
    _weather_memo = (stamp, result)
    return result


#: ``(field stamp, response)`` of the last weather-sensitivity compute — see the endpoint.
_weather_memo: tuple[tuple, dict] | None = None


# --- Field ↔ outcome sensitivity ------------------------------------------------------
# A profile whose field value we correlate needs enough distinct points to mean anything.
SENSITIVITY_MIN_POINTS = 4       # need this many (field value, metric) pairs to correlate
SENSITIVITY_MIN_DISTINCT = 3     # ...spread over at least this many distinct field values
SENSITIVITY_TREND_RHO = 0.3      # |ρ| below this reads as "no clear relationship"


# Rank-correlation primitives now live in ``pathbrain.stats`` (shared with the
# campaign-drift check); aliased here so the call sites below stay unchanged.
from ..stats import ols_residual_ss as _ols_residual_ss  # noqa: E402
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


WEATHER_WITHIN_MIN_POINTS = 5   # runs one profile needs before its within-profile ρ is trusted


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
            # The per-run graded Overall rides along for the variance decomposition: the
            # crown metrics are what pairing protects, but the Overall is what verdicts
            # actually read, so "how much of the noise is measurable weather?" has to be
            # answerable for it too.
            ov = (score.axis_scores or {}).get("overall")
            if isinstance(ov, (int, float)) and not isinstance(ov, bool):
                vals["overall"] = float(ov)
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
        "variance": _weather_variance(runs_by_fp, crown_metrics, metric_meta),
    }


# --- Weather variance decomposition ----------------------------------------------------
# The per-pair ρ table above says WHICH covariates move WHICH crown metrics. This answers
# the budget question that actually decides architecture: of the run-to-run noise a profile
# shows, how much could the clean covariates jointly account for at all? That number is the
# ceiling on any covariate-based weather adjustment — and its complement is the measured
# justification for the duel's paired design, which controls for the weather no probe sees.
WEATHER_R2_MIN_RUNS = 30        # fewer complete runs than this and an R² is a coin toss
WEATHER_R2_MIN_PER_PROFILE = 2  # one run demeans to zero — it can't witness within-profile variance
WEATHER_R2_COV_COVERAGE = 0.8   # a covariate absent from >20% of runs is dropped, not allowed to gut n


def _weather_variance(
    runs_by_fp: dict[str, list[dict[str, float]]],
    crown_metrics: list[str],
    metric_meta: dict[str, dict],
) -> dict:
    """How much of each outcome's **within-profile** run-to-run variance the clean
    covariates jointly explain — the empirical answer to "can we measure the weather?".

    Per outcome (each crown metric + the per-run Overall): demean the outcome and every
    clean covariate within each profile — the pure-arithmetic fixed effect, so a covariate
    that only differs *between* profiles (i.e. the profile itself wearing a weather
    costume) explains exactly nothing — then fit one joint least-squares over the pooled
    demeaned runs and report the adjusted R². Degrees of freedom charge for both the
    per-profile means and the covariates, so a pile of useless covariates reads ≈0
    instead of creeping upward with k.

    Read it as a **ceiling, not a promise**: it is linear and in-sample, so it is the
    most a covariate-based weather adjustment could remove (optimistically, for a linear
    adjuster; an underestimate only if the true relation is strongly non-monotonic in a
    way rank-correlation on the card above would also miss). The complement is conditions
    no instrument sees plus irreducible run noise — the share only a paired back-to-back
    comparison controls for, which is the duel's whole justification stated as a number.

    ``within_sd`` / ``residual_sd`` translate the share into the unit people actually
    reason in: the ± noise a profile's runs show before and after the best linear use of
    the covariates. ``residual_sd`` can read *worse* than ``within_sd`` when the
    covariates are useless — that's the honest out-of-sample price of fitting them, not a
    bug. Purely informational; no scores change.
    """
    clean = [k for k, is_clean in _weather_covariates() if is_clean]
    meta = dict(metric_meta)
    meta.setdefault("overall", {"label": "Overall", "unit": "pts"})
    outcomes = list(crown_metrics) + ["overall"]
    # One pass over the runs builds every outcome's pool and its covariate coverage —
    # this function can be handed six-figure run counts, so nothing below re-scans them.
    pools: dict[str, list[tuple[str, dict[str, float]]]] = {m: [] for m in outcomes}
    coverage: dict[str, dict[str, int]] = {m: {c: 0 for c in clean} for m in outcomes}
    for fp, rs in runs_by_fp.items():
        for r in rs:
            present = [c for c in clean if c in r]
            for m in outcomes:
                if m in r:
                    pools[m].append((fp, r))
                    cov = coverage[m]
                    for c in present:
                        cov[c] += 1
    rows: list[dict] = []
    for m in outcomes:
        m_meta = meta.get(m) or {}
        pool = pools[m]
        # Complete-case over the covariates that are actually collected: requiring a
        # covariate most runs never measured would throw away the dataset to keep a
        # column of blanks.
        used = [c for c in clean if coverage[m][c] >= WEATHER_R2_COV_COVERAGE * len(pool)] if pool else []
        by_fp: dict[str, list[dict[str, float]]] = {}
        for fp, r in pool:
            if all(c in r for c in used):
                by_fp.setdefault(fp, []).append(r)
        by_fp = {fp: rs for fp, rs in by_fp.items() if len(rs) >= WEATHER_R2_MIN_PER_PROFILE}
        n = sum(len(rs) for rs in by_fp.values())
        g, k = len(by_fp), len(used)
        base = {
            "outcome": m,
            "outcome_label": m_meta.get("label") or m,
            "unit": m_meta.get("unit") or ("pts" if m == "overall" else "ms"),
            "covariates_used": used,
            "runs": n,
            "profiles": g,
            "explained_share": None,
            "r2": None,
            "within_sd": None,
            "residual_sd": None,
            "why": None,
        }
        if not pool:
            rows.append({**base, "why": "no run has measured this outcome yet"})
            continue
        if not used:
            rows.append({**base, "why": "no clean covariate is measured on enough of these runs"})
            continue
        if n < WEATHER_R2_MIN_RUNS or n - g - k < 3:
            rows.append({**base, "why": (
                f"needs ≥{WEATHER_R2_MIN_RUNS} complete runs across repeat-measured profiles (have {n})"
            )})
            continue
        ys: list[float] = []
        xs: list[list[float]] = []
        for rs in by_fp.values():
            cnt = len(rs)
            ym = sum(r[m] for r in rs) / cnt
            xm = [sum(r[c] for r in rs) / cnt for c in used]
            for r in rs:
                ys.append(r[m] - ym)
                xs.append([r[c] - xm[j] for j, c in enumerate(used)])
        sst = sum(y * y for y in ys)
        if sst <= 0:
            rows.append({**base, "why": "these runs show no within-profile variance to explain"})
            continue
        ssr = _ols_residual_ss(xs, ys)
        if ssr is None:
            rows.append({**base, "why": "covariate design unsolvable"})
            continue
        ssr = min(ssr, sst)  # least squares can't do worse than the mean; anything past it is float dust
        df_tot, df_res = n - g, n - g - k
        adj = 1.0 - (ssr / df_res) / (sst / df_tot)
        rows.append({**base,
            "explained_share": round(max(0.0, min(1.0, adj)), 3),
            "r2": round(1.0 - ssr / sst, 3),
            "within_sd": round((sst / df_tot) ** 0.5, 2),
            "residual_sd": round((ssr / df_res) ** 0.5, 2),
        })

    ov = next((r for r in rows if r["outcome"] == "overall"), None)
    if ov and ov.get("explained_share") is not None:
        pct = round(ov["explained_share"] * 100)
        headline = (
            f"The clean covariates explain {pct}% of the Overall's within-profile run-to-run "
            f"variance (±{ov['within_sd']} → ±{ov['residual_sd']} pts over {ov['runs']} runs). "
            f"That's the ceiling on what any covariate-based weather adjustment could remove; "
            f"the other {100 - pct}% is conditions no probe sees plus run noise — the share only "
            f"a paired back-to-back comparison (the duel) controls for."
        )
    else:
        headline = (
            "Not enough repeat-measured runs to decompose the Overall's noise yet — "
            + ((ov or {}).get("why") or "no per-run Overall on record")
            + "."
        )
    return {
        "min_runs": WEATHER_R2_MIN_RUNS,
        "min_runs_per_profile": WEATHER_R2_MIN_PER_PROFILE,
        "outcomes": rows,
        "headline": headline,
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
            # OWN tunable quantum/target/interval/ecn/limit and its own bandwidth (in that
            # pipe's `download_bandwidth` field regardless of direction — `upload_bandwidth` is
            # unused/null). Upload shaping matters as much as download — tune both pipes.
            "pipes_note": (
                "Each profile has one pipe per direction (see each pipe's 'label', e.g. Download "
                "and Upload). Tune BOTH: every pipe has independent quantum/target/interval/ecn/"
                "limit and its own bandwidth (the pipe's 'download_bandwidth' field is that "
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


# ── Outliers ────────────────────────────────────────────────────────────────────────
# A Settings-Impact scatter with one profile at FCP 2300 ms while the field sits at 600 ms
# is a scatter of one dot and a smudge: the axes scale to the outlier and the profiles the
# page exists to separate collapse into a corner. Two different questions hide in "what do
# we do about it?". *Hiding* is a VIEW decision (let the axes fit the pack); *re-running* is
# an EVIDENCE decision (is that reading real?). They are answered separately here: the
# flag is computed once, the client decides what to show, and the refresh engine can be
# pointed at exactly the flagged profiles. The statistic is the modified z-score
# (Iglewicz–Hoaglin: 0.6745·(x − median)/MAD, flag |z| > 3.5) over the PROFILE MEDIANS,
# because the ordinary mean/stddev z is itself moved by the outlier it is meant to find.
# Most flagged profiles are thin — a two-iteration profile whose median IS one bad run —
# which is why the summary splits thin from confident: a thin outlier wants re-measuring,
# a confident one is a real (bad) result and is information, not noise.
OUTLIER_Z = 3.5
OUTLIER_MIN_PROFILES = 5
_MAD_TO_SIGMA = 0.6745


def _robust_z(values: list[float]) -> tuple[float, float | None]:
    """Field median + the MAD-based scale (``None`` when the field is degenerate — a MAD
    of 0 means more than half the profiles share one value, so no z is meaningful)."""
    med = median(values)
    mad = median(abs(v - med) for v in values)
    if mad <= 0:
        return med, None
    return med, mad / _MAD_TO_SIGMA


def _outlier_report(profiles: list[dict], crown_metrics: list[str], definition: dict,
                    min_iterations: int) -> dict:
    """Tag each profile whose median on a crown metric — or whose Overall — sits more than
    ``OUTLIER_Z`` robust standard deviations from the field's median. Writes ``outlier``
    onto the profile (``None`` when it is not one) and returns the field-level summary."""
    meta = {m.get("key"): m for m in (definition or {}).get("metrics", [])}
    checks: list[tuple[str, str, bool, callable]] = []
    for key in crown_metrics:
        m = meta.get(key) or {}
        checks.append((
            key, m.get("label") or key, bool(m.get("higher_is_better")),
            lambda p, k=key: (p.get("metrics") or {}).get(k),
        ))
    checks.append(("overall", "Overall", True, lambda p: p.get("overall")))

    flagged: dict[str, list[dict]] = {}
    for key, label, higher, read in checks:
        pts = [(p, read(p)) for p in profiles]
        pts = [(p, float(v)) for p, v in pts if v is not None]
        if len(pts) < OUTLIER_MIN_PROFILES:
            continue
        med, scale = _robust_z([v for _, v in pts])
        if scale is None:
            continue
        for p, v in pts:
            z = (v - med) / scale
            if abs(z) <= OUTLIER_Z:
                continue
            # "worse" = the bad side for this metric, so a reader knows whether the profile
            # is implausibly slow (the usual) or implausibly fast (a lucky thin reading).
            worse = (z < 0) if higher else (z > 0)
            flagged.setdefault(p["fingerprint"], []).append({
                "key": key,
                "label": label,
                "value": round(v, 2),
                "field_median": round(med, 2),
                "z": round(z, 1),
                "side": "worse" if worse else "better",
            })

    thin = confident = 0
    fps: list[str] = []
    for p in profiles:
        hits = flagged.get(p["fingerprint"])
        if not hits:
            p["outlier"] = None
            continue
        is_thin = int(p.get("iterations") or 0) < min_iterations
        p["outlier"] = {
            "metrics": sorted(hits, key=lambda h: -abs(h["z"])),
            "thin": is_thin,
        }
        fps.append(p["fingerprint"])
        if is_thin:
            thin += 1
        else:
            confident += 1
    return {
        "count": len(fps),
        "thin": thin,
        "confident": confident,
        "fingerprints": fps,
        "threshold_z": OUTLIER_Z,
        "min_profiles": OUTLIER_MIN_PROFILES,
        "method": "modified z-score (MAD) over profile medians, per crown metric and the Overall",
    }


def _seeded_field(session: Session, result: dict) -> dict:
    """The field with the prior methodology's standings folded in for every profile the
    current one has no data on yet (a current crown is kept). See
    ``refresh.seed_field_from_prior``."""
    try:
        return refresh_mod.seed_field_from_prior(
            session, result, result.get("min_iterations") or _min_iterations(session)
        )
    except Exception:  # noqa: BLE001 — a seed orders the heirs; it must never break the page
        log.warning("Profiles: could not seed the field from the prior methodology", exc_info=True)
        return result


def _seed_summary(session: Session, seeded: dict) -> dict | None:
    """What the page says about a seeded field: which version seeded it, the profile that
    version crowned (the stand-in defender the ladder and race will use), and how many
    stored profiles have nothing measured under the current version yet."""
    version = seeded.get("seeded_from")
    if not version:
        return None
    # The PRIOR's crown, not the field's: with a current crown present the field's best is
    # a current measurement and the prior crown is just the first profile to challenge it.
    best_fp = seeded.get("seeded_best_fingerprint") or seeded.get("best_fingerprint")
    best = next((p for p in seeded.get("profiles", []) if p["fingerprint"] == best_fp), None)
    without = [p for p in seeded.get("profiles", []) if p.get("no_data")]
    return {
        "version": version,
        "best_fingerprint": best_fp,
        "best_name": (best or {}).get("name") or (best or {}).get("label"),
        "best_prior_overall": (best or {}).get("prior_overall"),
        "profiles_without_data": len(without),
        "profiles_seeded": int(seeded.get("seeded_profiles") or 0),
        # Whether the current version already has a crown of its own (the seed then orders
        # only the unmeasured rest) or the prior crown is standing in for one.
        "current_crown": bool(seeded.get("seeded_has_current_crown")),
    }


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

    Returns ``{items, all_items, total, limit, crown_overall}``: ``total`` is every
    qualifying heir (drives the "N could beat your crown" badge), ``items`` the top
    ``limit`` by ceiling-above-crown — the CARD's cut — and ``all_items`` every heir in the
    same order, for the engines: the duel ladder's ``leaders``/``heirs`` contender modes
    draw their challengers from this list (``duel._engine_heirs``), and a seeded field of
    two hundred prior-ranked profiles reaching the ring five at a time would be exactly the
    "racing a random dozen" failure. Profiles that never produced a comparable run under
    any version have no ceiling to rank by and aren't here — the Race button's bootstrap
    path still picks them up."""
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
                # The prior methodology's Overall — set only on a seeded field, and only
                # ever used to order the untested tier (see ``_heir_key``).
                "prior_overall": p.get("prior_overall"),
            }
        )

    # Order to mirror the race's sampling priority (challenger.rank_challengers): confront the
    # biggest known threat first, then refresh nearby stale incumbents, then fill in unknowns —
    # so the top heir on the card is the first profile a race would actually run.
    #   tier 0 — limited-data with a known ceiling: highest optimistic ceiling first
    #   tier 1 — stale confident: closest to the crown first (smallest |Overall − crown|)
    #   tier 2 — untested (no ceiling estimate yet): listed last, the prior methodology's
    #            winners first when the field was seeded from one
    def _heir_key(h: dict) -> tuple:
        if h["reason"] == "stale":
            closeness = abs((h.get("overall") or 0.0) - (crown_overall or 0.0))
            return (1, closeness, 0.0)
        if h.get("optimistic") is not None:
            return (0, 0.0, -h["optimistic"])  # biggest threat (highest ceiling) first
        prior = h.get("prior_overall")
        return (2, 0.0, -prior if prior is not None else 1e9)

    heirs.sort(key=_heir_key)
    return {
        "items": heirs[:limit],
        "all_items": heirs,
        "total": len(heirs),
        "limit": limit,
        "crown_overall": crown_overall,
    }


# ── The field, computed once and shared ───────────────────────────────────────────────
#
# `compute_profiles` is the most expensive thing PathBrain does: it walks every completed
# run, decodes each one's stored scalars, re-normalizes the crown against the whole field
# and resolves a time-of-day baseline per profile. That is inherent to what it answers —
# but it was being recomputed from scratch by every caller that needed the field, and
# there are seven of them. The consequence was not merely a slow endpoint. It is a *pure
# Python* pass, so it holds the GIL for its whole duration: while the duel ladder (which
# calls it at every session start) or the crown follower is computing the field, every
# request in the process is stalled behind it — which is why the Dueling Champions page
# could not load its config, a payload of a few hundred bytes.
#
# It is also a pure function of the runs, the scores and the methodology, which is exactly
# the property that makes it cacheable. `_field_stamp` is the cheap identity of that
# input — two indexed aggregates, ~20ms against a 100k-run table versus ~30s to recompute
# — so a hit is a stamp query and a dict lookup, and any change to the underlying data
# (a completed run, a re-grade writing new Score rows, a refingerprint, a methodology
# switch, a changed confidence bar) yields a different stamp and recomputes. The cache is
# therefore never stale by construction; `invalidate()` exists for callers that would
# rather not wait for the stamp to notice, not because correctness depends on it.
_FIELD_LOCK = threading.Lock()
_FIELD_CACHE: "OrderedDict[tuple, tuple[tuple, dict]]" = OrderedDict()
_FIELD_CACHE_MAX = 6
# One field pass in flight per (key, stamp): the second caller asking the same question of
# the same data WAITS for the first one's answer rather than starting its own pass. "Two
# callers racing a cold cache both compute" was written when the callers were a page and a
# ladder session an hour apart; once four pages each asked on load, a cold cache meant four
# concurrent pure-Python passes, each holding the field in memory and each taking the GIL
# in turn — the whole process going dark on the NAS. Waiting is bounded by one pass, and
# the waiter re-checks the cache rather than trusting the leader (a leader that raised
# clears its slot in `finally`, so a waiter then computes for itself).
_FIELD_INFLIGHT: dict[tuple, threading.Event] = {}


def _field_stamp(session: Session) -> tuple:
    """The cheap identity of everything `compute_profiles` reads.

    Anything that could change the field changes this: new runs, re-graded or re-derived
    scores (new `Score` rows), a refingerprint (same count, but the max ids move when rows
    are rewritten — and the explicit `invalidate()` on that path covers a pure in-place
    re-key), the active methodology, and the confidence/significance bars that decide who
    is crownable.
    """
    max_run, run_count = session.execute(
        select(func.max(Run.id), func.count(Run.id)).where(Run.status == RunStatus.COMPLETE)
    ).one()
    # A change stamp over every row (not routed through latest_score): any write moves it.
    max_score, score_count = session.execute(
        select(func.max(Score.id), func.count(Score.id))
    ).one()
    config = get_config(session)
    methodology = ensure_current_methodology(session, config)
    correlation = config.get("correlation", {}) or {}
    return (
        max_run, run_count, max_score, score_count, methodology.version,
        correlation.get("min_runs"), correlation.get("min_iterations"),
        (config.get("trends", {}) or {}).get("min_samples"),
    )


def invalidate_profiles_cache() -> None:
    """Drop the memoized field **and** the per-profile rollups.

    Called by the paths that rewrite scores or fingerprints wholesale. The rollups verify
    themselves on every read, so this is never what makes them correct — it is what makes
    the rebuild happen once, at a known moment, rather than being discovered profile by
    profile over the following hour. Both caches are dropped together because a caller that
    knows the field moved should not have to know how many layers cache it.
    """
    from .. import profile_aggregates

    global _weather_memo
    with _FIELD_LOCK:
        _FIELD_CACHE.clear()
    profile_aggregates.invalidate()
    # The seed's caches (stored-profile list keyed on run ids, prior-version field keyed on
    # its scores) are blind to an in-place re-key for the same reason the field memo is.
    refresh_mod.invalidate_seed_cache()
    # The weather-sensitivity memo keys on the same field stamp, and the stamp is blind to
    # the same event this hook exists for: an in-place refingerprint moves runs between
    # profiles without changing any id or count, and the decomposition's within-profile
    # grouping would silently keep the old world.
    _weather_memo = None


def compute_profiles(
    session: Session,
    complete_only: bool = True,
    tz_offset: int = 0,
    custom_crown_metrics: list[str] | None = None,
    include_weather: bool = True,
) -> dict:
    """The memoized `_compute_profiles_uncached` — see the note above.

    Identical output to computing it directly; the only difference is that a second caller
    asking the same question of unchanged data gets the first one's answer. `include_weather`
    is part of the key because it changes the payload (the two display-only weather fields),
    so a caller that skips the pass can never be served a cached field that included it, or
    the reverse.
    """
    key = (complete_only, tz_offset, tuple(custom_crown_metrics or ()), include_weather)
    stamp = _field_stamp(session)
    flight = (key, stamp)
    while True:
        with _FIELD_LOCK:
            hit = _FIELD_CACHE.get(key)
            if hit is not None and hit[0] == stamp:
                _FIELD_CACHE.move_to_end(key)
                return hit[1]
            pending = _FIELD_INFLIGHT.get(flight)
            if pending is None:
                pending = _FIELD_INFLIGHT[flight] = threading.Event()
                leader = True
            else:
                leader = False
        if leader:
            break
        # Another caller is computing exactly this field: wait for it, then re-check the
        # cache. The wait is bounded by that one pass (the leader always clears its slot),
        # and re-checking rather than reading the leader's result keeps this loop correct
        # if the leader raised — then the slot is gone and the next pass through leads.
        pending.wait()
    # Computed OUTSIDE the lock: it takes tens of seconds, and holding a mutex across it
    # would convert "slow" into "serialized" for every OTHER key too. Only callers asking
    # this exact question of this exact data wait, on the in-flight event above.
    try:
        field = _compute_profiles_uncached(
            session, complete_only, tz_offset, custom_crown_metrics, include_weather
        )
        with _FIELD_LOCK:
            _FIELD_CACHE[key] = (stamp, field)
            _FIELD_CACHE.move_to_end(key)
            while len(_FIELD_CACHE) > _FIELD_CACHE_MAX:
                _FIELD_CACHE.popitem(last=False)
    finally:
        with _FIELD_LOCK:
            _FIELD_INFLIGHT.pop(flight, None)
        pending.set()
    return field


def _profile_settings(session: Session, fingerprint_: str) -> list[dict] | None:
    """The stored normalized settings for a profile (latest run that captured it)."""
    settings = session.scalars(
        select(effective_settings())
        .where(effective_fingerprint() == fingerprint_, effective_settings().is_not(None))
        .order_by(Run.created_at.desc())
        .limit(1)
    ).first()
    return settings if settings is not None else None


@router.get("/settings/profiles/{fingerprint_}/settings")
def profile_settings_view(fingerprint_: str, session: Session = Depends(get_session)) -> dict:
    """What this profile actually **is** — every shaper field, per pipe, against the live
    firewall — and whether the firewall can be put on it.

    Profile Detail led with a call sign, a grade and a bout tape and never showed the
    settings, which is the one thing a profile *is*; the technical summary in the subtitle
    is a lossy one-liner with no units, no per-pipe split and nothing to compare against.

    Served from the server rather than rendered off the settings blob the page already
    holds because **which fields are writable is the registry's answer**: a hardcoded list
    in a component is the drift that offered the ECN field a value of 4096, and it would
    leave "can this profile exist?" answered in two places. This runs the same
    ``unreachable_fields`` primitive as the Methodology page's audit, so the two can never
    disagree about which profiles are out of reach.
    """
    from .. import reachability

    try:
        live = get_provider().discover()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"Could not read the live firewall: {exc}"
        ) from exc
    out = reachability.profile_view(session, fingerprint_, live)
    if out is None:
        raise HTTPException(status_code=404, detail="No stored settings for that profile")
    return out


def _profile_iterations(session: Session, fingerprint_: str) -> int:
    """Total iterations a profile has accumulated across its *comparable* completed
    runs — the same count ``settings_profiles`` uses for the confidence flag."""
    methodology = ensure_current_methodology(session, get_config(session))
    rows = session.execute(
        select(Run, Score)
        .join(Score, Score.run_id == Run.id)
        .where(
            Run.status == RunStatus.COMPLETE,
            effective_fingerprint() == fingerprint_,
            Score.methodology_version == methodology.version,
            latest_score(),
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
    # A one-way apply is the user's call, so this is stated rather than refused: the fields
    # PathBrain never writes are listed in the confirm dialog, and whatever else the profile
    # asks for is still applied. Without it the apply reports success while the firewall
    # settles on a profile with a different fingerprint, and nothing on screen says why.
    for drop in unreachable_fields(target, live):
        warnings.append(
            f"{drop['label']}·{drop['field_label']} stays at {drop.get('from')} "
            f"(this profile wants {drop.get('to')}) — {drop['reason']}. The firewall will "
            "end up on a different profile than the one named."
        )

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
    """Apply a planned change list to the firewall as ONE switch — ``provider.apply_many``,
    every field on every pipe and a single reconfigure — returning the applied summaries.
    Raises ``HTTPException`` on failure. Shared by the apply-profile and apply-settings
    endpoints. (It used to write one field per call, each with its own shaper reload; the
    reload is what the firewall feels, so a switch now costs exactly one.)"""
    if not changes:
        return []
    batch = normalize_changes(changes)
    try:
        apply_changes(provider, batch)
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=400, detail=f"The {provider.name} provider can't write changes.",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("apply write failed (%d change(s) in one switch)", len(batch))
        raise HTTPException(
            status_code=502,
            detail=(
                f"Applying {len(batch)} change(s) failed: {type(exc).__name__}: {exc}. "
                "The firewall may be partially changed — re-apply once the issue is resolved."
            ),
        ) from exc
    return [{"label": ch["label"], "field_label": ch["field_label"], "to": ch["to"]} for ch in changes]


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

    # Refused rather than warned, because this test is a loop that would never terminate.
    # ``plan_apply`` writes only writable fields, so a profile differing in one PathBrain
    # never writes (``flows``, ``scheduler``, ``queues``, ``upload_bandwidth``) applies
    # cleanly onto a firewall that stays on a DIFFERENT profile; the runs are filed under
    # that profile's fingerprint — correctly — so "top up to the minimum" would add
    # iterations to the wrong profile and this one would never reach confidence. Best-effort:
    # a firewall we cannot read is not a reason to refuse a test.
    unreachable: list[dict] = []
    try:
        unreachable = unreachable_fields(target, get_provider().discover())
    except Exception:  # noqa: BLE001
        log.debug("test-profile: could not check reachability", exc_info=True)
    if unreachable:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unreachable: {describe_unreachable(unreachable)}. "
                f"{unreachable[0]['reason']}, so the firewall cannot be put on this profile — "
                "testing it would measure whichever profile it settled on instead."
            ),
        )

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
    except Exception as exc:  # noqa: BLE001
        log.exception("test-profile start failed")
        raise HTTPException(
            status_code=502, detail=f"Could not start the profile test: {type(exc).__name__}: {exc}"
        ) from exc

    log.info("Profile test %s queued for %s: %s iteration(s)", test_id, fp, needed)
    return {
        "id": test_id,
        "fingerprint": fp,
        "iterations": needed,
        "current_iterations": current_iters,
        "min_iterations": min_iterations,
        # Which of the two lengths ran, so the caller can say "quick test" vs "topping up"
        # without re-deriving it from the counts.
        "mode": "exact" if requested is not None else "top_up",
        **_queue_placement(test_id),
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


def _queue_placement(test_id: int) -> dict:
    """Where a just-enqueued test landed: does it start now, and if not, what is ahead of it.

    Both start paths return this, so the two callers cannot describe the same queue two
    ways. It is read *after* the row exists, so the test counts itself: a placement of 1
    means "next up, nothing else waiting".
    """
    status = profile_test_mod.queue_status()
    pending = status.get("pending") or []
    position = next(
        (p.get("queue_position") for p in pending if p.get("id") == test_id), None
    )
    running = status.get("running")
    ahead = (position - 1 if position else 0) + (1 if running else 0)
    # ``blocked_by`` comes from the shared queue so every start path names the holder the
    # same way — a profile test waiting behind a sweep must not say something different
    # from a sweep waiting behind a duel.
    return {
        # True when something has to finish before this test starts — either the pipeline
        # is held by another engine, or other tests are queued in front of it.
        "queued": bool(status.get("busy")) or ahead > 0,
        "ticket_id": None,
        "queue_position": position,
        "queue_ahead": ahead,
        "blocked_by": job_queue.blocked_by() or status.get("owner_label") or status.get("owner"),
    }


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

    Returns ``{id, fingerprint, iterations, label, existing_iterations}`` plus its queue
    placement (``queued``/``queue_position``/``queue_ahead``/``blocked_by``); raises
    ``HTTPException`` only for a no-op or an unreachable change. A busy pipeline is **not**
    a refusal — the test is queued behind whatever holds it.
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
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not start the test: {exc}") from exc
    log.info("Test-settings %s queued (fp %s, %s iteration(s))", test_id, target_fp, wanted)
    return {
        "id": test_id,
        "fingerprint": target_fp,
        "iterations": wanted,
        "label": label,
        "existing_iterations": existing,
        # Requested fields the firewall cannot write, reverted so the fingerprint above is real.
        "warnings": warnings,
        **_queue_placement(test_id),
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


@router.get("/settings/test-profile/queue")
def profile_test_queue() -> dict:
    """What is running, what is queued, and what holds the pipeline.

    The read behind "Busy now — queue this?": before spending a benchmark, a caller can
    ask whether it would start immediately, and if not, what is in the way and how many
    tests are already waiting. Cheap (one indexed query plus the in-memory lock state), so
    it is safe to call on every press.
    """
    return profile_test_mod.queue_status()


@router.post("/settings/test-profile/{test_id}/cancel")
def cancel_profile_test_by_id(test_id: int) -> dict:
    """Cancel one specific profile test — the queue's per-row cancel.

    Separate from the bodiless endpoint above because the jobs feed cancels by URL alone
    (``cancelJob`` POSTs no body), and with a queue there is more than one cancellable
    test on screen at a time.
    """
    cancelled = profile_test_mod.cancel(test_id)
    return {
        "cancelled": cancelled,
        "id": test_id,
        "queue_depth": profile_test_mod.queue_depth(),
    }


@router.post("/settings/test-profile/cancel")
def cancel_profile_test(body: dict | None = Body(None)) -> dict:
    """Cancel a profile test. Returns whether one was cancelled.

    With no body, cancels the test currently claimed — the toolbar button. With
    ``{"id": N}`` cancels that specific one, which is how a *queued* test is dropped: it
    has applied nothing, so it leaves the queue immediately rather than starting just to
    stop. A running test stops after its current chunk and still restores the baseline.
    """
    test_id = (body or {}).get("id")
    if test_id is not None:
        try:
            test_id = int(test_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="id must be a number") from None
    cancelled = profile_test_mod.cancel(test_id)
    return {
        "cancelled": cancelled,
        "status": (profile_test_mod.current() or {}).get("status"),
        "queue_depth": profile_test_mod.queue_depth(),
    }


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
        submission = job_queue.submit(
            "race",
            f"Challenger race · {minutes:g} min",
            spec={"time_budget_s": int(minutes * 60), "auto_promote": auto_promote},
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("race start failed")
        raise HTTPException(
            status_code=502, detail=f"Could not start the race: {type(exc).__name__}: {exc}"
        ) from exc

    return {
        "id": submission.result,
        "contenders": len(contenders),
        "auto_promote": auto_promote,
        **submission.placement(),
    }


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
    fingerprints: str | None = Query(
        None, description="Comma-separated fingerprints to re-run exactly (overrides top/rank_by)."
    ),
    session: Session = Depends(get_session),
) -> dict:
    """Preview a 'Re-run profiles' batch: how many profiles, total iterations, and an
    estimated duration (from recent runs' per-iteration timing) — so the UI can show
    'N profiles × M iterations ≈ ~T' before committing. With ``top`` set, previews a
    winner-first subset ranked by ``rank_by`` (or the prior methodology); with
    ``fingerprints`` set, exactly those profiles (the "Re-run outliers" scope)."""
    fps = [f.strip() for f in (fingerprints or "").split(",") if f.strip()] or None
    return refresh_mod.preview(session, iterations, top=top, rank_by=rank_by, fingerprints=fps)


@router.post("/settings/refresh")
def start_refresh(body: dict = Body(...), session: Session = Depends(get_session)) -> dict:
    """Start a 'Re-run profiles' batch: apply each stored profile, run ``iterations``
    benchmarks on it, and restore the baseline at the end (see ``refresh.py``).

    Body: ``{"iterations": <number>, "top"?: <N>, "rank_by"?: <version>,
    "fingerprints"?: [<fp>, …]}``. With ``top`` set, only the top-N profiles are re-run,
    **winner-first** by their Overall under ``rank_by`` (or the prior methodology) — fresh
    data for the best performers first after a methodology publish. With ``fingerprints``
    set, exactly those profiles are re-run in that order (the Settings-Impact "Re-run
    outliers" action). Returns the refresh id; poll ``GET /settings/refresh`` for status.
    """
    iterations = int((body or {}).get("iterations") or 0)
    if iterations <= 0:
        raise HTTPException(status_code=400, detail="iterations must be > 0")
    top_raw = (body or {}).get("top")
    top = int(top_raw) if top_raw not in (None, "") else None
    if top is not None and top <= 0:
        raise HTTPException(status_code=400, detail="top must be > 0 when provided")
    rank_by = (body or {}).get("rank_by") or None
    fps_raw = (body or {}).get("fingerprints")
    if fps_raw is not None and not isinstance(fps_raw, list):
        raise HTTPException(status_code=400, detail="fingerprints must be a list when provided")
    fingerprints = [str(f) for f in (fps_raw or []) if f] or None
    try:
        submission = job_queue.submit(
            "refresh",
            f"Re-run profiles · {iterations} iteration(s) each",
            spec={"iterations": iterations, "top": top, "rank_by": rank_by, "fingerprints": fingerprints},
        )
    except RuntimeError as exc:  # nothing to re-run — a bad request, not a conflict
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("refresh start failed")
        raise HTTPException(
            status_code=502, detail=f"Could not start the refresh: {type(exc).__name__}: {exc}"
        ) from exc
    return {
        "id": submission.result,
        "iterations": iterations,
        "top": top if not fingerprints else None,
        "fingerprints": len(fingerprints) if fingerprints else None,
        **submission.placement(),
    }


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

    freshness = duel_mod.champion_freshness_days(get_config(session).get("duel", {}) or {})
    pooled = crown_follower.current_crown(session)
    resolution = crowning.resolve(session, pooled_best_fp=(pooled or {}).get("fingerprint"))

    # The duel side: the ladder's champion plus its record in the ring. `latest_champion`
    # returns None once a verdict ages past the rematch window, so a stale champion is
    # still shown — labelled expired — rather than silently vanishing from the dashboard.
    table = duel_mod.standings()
    champion = table.get("champion")
    fresh = duel_mod.latest_champion(session, max_age_days=freshness)
    duel_out = None
    if champion:
        record = next(
            (r for r in table.get("standings", []) if r["fingerprint"] == champion["fingerprint"]),
            None,
        )
        duel_out = {
            **champion,
            "fresh": fresh is not None,
            "freshness_days": freshness,
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

    # The fused verdict (the Overall page's crown) — present whatever the policy, so the
    # card can show all three names and say which one automation follows.
    fused_out = None
    fused = resolution.get("fused")
    if fused is None and resolution["policy"] != "fused":
        try:
            from .. import overall_ranking

            fused = overall_ranking.crown(session)
        except Exception:  # noqa: BLE001 — a reading, never a reason the card fails
            log.debug("Two-crowns: the fused crown could not be read", exc_info=True)
    if fused:
        fused_out = {**fused, "name": fused.get("name") or call_signs.get(fused["fingerprint"])}

    # What each verdict costs in raw time against the fastest on paper — the price of the
    # experience it was crowned for. Best-effort, like every reading on this card.
    math = _math_crown(session)
    lcp_first = resolution.get("lcp_first") or _leg_first(session, "lcp_first")
    fcp_first = resolution.get("fcp_first") or _leg_first(session, "fcp_first")
    if math:
        try:
            from ..pooled_field import paper_cost

            version = ensure_current_methodology(session, get_config(session)).version
            sides = [s for s in (pooled, duel_out, fused_out, lcp_first, fcp_first) if s and s.get("fingerprint")]
            cost = paper_cost(session, version, math, [s["fingerprint"] for s in sides])
            for side in sides:
                side["vs_paper"] = cost.get(side["fingerprint"])
        except Exception:  # noqa: BLE001
            log.warning("Two-crowns: paper cost unavailable", exc_info=True)
    # And each verdict against the field's fastest FCP and fastest LCP, leg by leg.
    fastest = None
    try:
        from ..pooled_field import fastest_legs, leg_gaps

        cfg_ = get_config(session)
        version = ensure_current_methodology(session, cfg_).version
        min_iter = int((cfg_.get("correlation") or {}).get("min_iterations") or 15)
        fastest = fastest_legs(session, version, min_iter)
        sides = [s for s in (pooled, duel_out, fused_out, math, lcp_first, fcp_first) if s and s.get("fingerprint")]
        gaps = leg_gaps(session, version, fastest, [s["fingerprint"] for s in sides])
        for side in sides:
            side["vs_fastest"] = gaps.get(side["fingerprint"])
    except Exception:  # noqa: BLE001
        log.warning("Two-crowns: leg gaps unavailable", exc_info=True)

    last = crown_follower.status().get("last_result") or {}
    return {
        "policy": resolution["policy"],
        "pooled": pooled,
        "duel": duel_out,
        "fused": fused_out,
        # Which verdict automation currently follows, and whether it fell back.
        "governing": {
            "source": resolution["source"],
            "fingerprint": resolution["fingerprint"],
            "detail": resolution["detail"],
            # The speed-budget check on the policy's choice: within, or which crown it
            # replaced and by how much it was over.
            "speed_budget": resolution.get("speed_budget"),
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
        # The mathematical crown — lowest median FCP × LCP in raw ms — shown beside the
        # verdicts as a reference. Never part of `agree`.
        "math": math,
        # The LCP-first crown — the fastest FCP among profiles within the configured band of
        # the best LCP. A candidate for the real crown; never part of `agree`.
        "lcp_first": lcp_first,
        # Its mirror — the fastest LCP among profiles within the band of the best FCP.
        "fcp_first": fcp_first,
        # The field's fastest FCP and fastest LCP and who holds them; every verdict above
        # carries `vs_fastest`, its gap to each.
        "fastest": fastest,
    }


def _leg_first(session, kind: str) -> dict | None:
    try:
        from ..pooled_field import band_pct, leg_first_crown

        cfg = get_config(session)
        version = ensure_current_methodology(session, cfg).version
        min_iter = int((cfg.get("correlation") or {}).get("min_iterations") or 15)
        return leg_first_crown(session, version, min_iter, kind, band_pct(cfg, kind))
    except Exception:  # noqa: BLE001 — a reference tile can't be why the card fails
        log.warning("%s crown unavailable", kind, exc_info=True)
        return None


def _math_crown(session) -> dict | None:
    try:
        from ..pooled_field import math_crown

        cfg = get_config(session)
        version = ensure_current_methodology(session, cfg).version
        min_iter = int((cfg.get("correlation") or {}).get("min_iterations") or 15)
        return math_crown(session, version, min_iter)
    except Exception:  # noqa: BLE001 — a reference tile can't be why the card fails
        log.warning("math crown unavailable", exc_info=True)
        return None


@router.get("/settings/fastest-gaps")
def fastest_gaps(fp: list[str] = Query(default=[]), session: Session = Depends(get_session)) -> dict:
    """Any profiles against the field's fastest FCP and fastest LCP, leg by leg — the one
    reading behind the "vs fastest" line on every view that names a crown but does not
    already carry it (the Duels champion, the Auto-apply popover). Same arithmetic as the
    crowns card and the Best profile page (`pooled_field.leg_gaps`). Read off the rollup;
    best-effort — an unreadable field returns an empty answer, never an error."""
    wanted = [f for f in dict.fromkeys(fp) if f][:20]
    try:
        from ..pooled_field import fastest_legs, leg_gaps

        cfg = get_config(session)
        version = ensure_current_methodology(session, cfg).version
        min_iter = int((cfg.get("correlation") or {}).get("min_iterations") or 15)
        fastest = fastest_legs(session, version, min_iter)
        return {"fastest": fastest, "gaps": leg_gaps(session, version, fastest, wanted)}
    except Exception:  # noqa: BLE001
        log.warning("fastest-gaps unavailable", exc_info=True)
        return {"fastest": None, "gaps": {}}


@router.get("/settings/crown-follow")
def crown_follow_status(session: Session = Depends(get_session)) -> dict:
    """The crown follower's config, last-check status, crown-churn statistics, and the
    newest ledger events — everything the top-bar "Follow best" switch + popover shows.
    Read-only; the churn ledger accrues whether or not following is enabled."""
    from .. import crown_follower

    from .. import crowning
    from .. import duel as duel_mod

    cfg = get_config(session).get("crown_follow", {}) or {}
    freshness = duel_mod.champion_freshness_days(get_config(session).get("duel", {}) or {})

    champion = duel_mod.latest_champion(session, max_age_days=freshness)
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
            "lcp_band_pct": _band(session),
            "fcp_band_pct": _band(session, "fcp_first"),
            "speed_budget": crowning.speed_budget(get_config(session)),
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
        policy = str(body["policy"] or "").lower()
        if policy not in crowning.POLICIES:
            raise HTTPException(
                status_code=400, detail=f"policy must be one of {list(crowning.POLICIES)}"
            )
        updates["policy"] = policy
    for key in ("lcp_band_pct", "fcp_band_pct"):
        if key not in (body or {}):
            continue
        from ..pooled_field import BAND_RANGE

        try:
            band = float(body[key])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{key} must be a number") from None
        lo, hi = BAND_RANGE
        if not (lo <= band <= hi):
            raise HTTPException(status_code=400, detail=f"{key} must be between {lo:g} and {hi:g}")
        updates[key] = band
    if "speed_budget" in (body or {}):
        raw = body["speed_budget"]
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="speed_budget must be an object")
        sb = dict(crowning.speed_budget(get_config(session)))
        if "enabled" in raw:
            sb["enabled"] = bool(raw["enabled"])
        lo, hi = crowning.SPEED_BUDGET_RANGE
        for key in ("lcp_pct", "fcp_pct"):
            if key not in raw:
                continue
            try:
                v = float(raw[key])
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"speed_budget.{key} must be a number") from None
            if not (lo <= v <= hi):
                raise HTTPException(status_code=400, detail=f"speed_budget.{key} must be between {lo:g} and {hi:g}")
            sb[key] = v
        updates["speed_budget"] = sb
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
            "policy": crowning.active_policy(session),
            "lcp_band_pct": _band(session),
            "fcp_band_pct": _band(session, "fcp_first"),
            "speed_budget": crowning.speed_budget(get_config(session)),
        }
    }


def _band(session, kind: str = "lcp_first") -> float:
    from ..pooled_field import band_pct

    return band_pct(get_config(session), kind)


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
            effective_fingerprint(), effective_settings(), Run.iterations, Run.created_at,
            Score.axis_scores, Score.comparability,
        )
        .join(Score, Score.run_id == Run.id)
        .where(
            Run.status == RunStatus.COMPLETE,
            effective_fingerprint().is_not(None),
            Score.methodology_version == methodology.version,
            latest_score(),
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
