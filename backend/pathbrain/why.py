"""Where a win lives: what the Overall gap between two profiles is *made of*.

The crown says **which** profile is best and by how much; nothing said **where** that margin
comes from. A 3-point lead could be three points of network stall on every page, or a
30 ms LCP edge on one site and nothing anywhere else — those are different findings with
different next steps, and a single number reads the same for both. This module takes any
two profiles and splits their gap three ways, each on the same runs the crown graded:

1. **Crown legs** — under the weighted crown the Overall is a weighted mean of each leg's
   median subscore, so the gap is *exactly* the sum of ``w_k · (sub_A,k − sub_B,k) / Σw``:
   one number per leg, in Overall points, that add up to the gap. The medians are the
   rounded medians ``crown_follower._grade_medians`` grades, read from the same per-profile
   rollup, so the legs here and the grade on the standings are one arithmetic. (Under a
   corner crown no additive split exists; ``exact`` is False and each leg's number is the
   change from swapping that leg alone, stated as such.)
2. **Navigation phases** — the load's independent phases (DNS → TCP → TLS → request →
   response → render) from the browser result, so a crown-leg edge can be traced to the
   part of the load it sits in: an FCP lead that is all in *request wait* is the server
   answering sooner; one that is all in *response* is bytes arriving sooner through the
   queue; one in *render* is the machine, not the shaper.
3. **Sites** — the crown legs re-derived **per page** from the stored raw (the same
   ``interpret.derive`` the run's own metrics came from), then priced on the methodology's
   own thresholds and weights, so each site reports the gap *it alone* would produce. Not
   an additive split of the pooled gap — the pooled metric is a mean over pages that is then
   subscored, which is not linear — but the answer to "is this win everywhere or one page?".

Every delta carries a noise bar: the standard error of the median on each side (IQR/√n, the
convention ``routes_settings._overall_se`` uses) pooled in quadrature, and ``clear`` when the
delta exceeds ``correlation.crown_tie_sigma`` of it — the same bar that decides whether the
crown's lead is a tie. Read-only, bounded by the two profiles (the site pass by
``SITE_RUN_LIMIT`` runs a side); nothing here changes a score.
"""
from __future__ import annotations

from statistics import median, quantiles
from urllib.parse import urlsplit

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from . import crown_follower, profile_aggregates, profile_names
from .config_store import get_config
from .interpret import derive
from .logging_config import get_logger
from .methodology import (
    corner_score,
    ensure_current_methodology,
    overall_method,
    overall_metrics,
    overall_weights,
)
from .metrics import METRICS
from .models import BenchmarkResult, Run, RunStatus, Score
from .raw_access import browser_url_observations
from .scoring.engine import _normalize
from .settings_profile import SQM_OFF_FINGERPRINT, summarize

log = get_logger("why")

#: The load's independent phases, in load order (``interpret.waterfall``). ``nav_stall`` is
#: left out: it is redirect/queueing time before DNS starts, which no profile moves.
NAV_PHASES: tuple[str, ...] = ("nav_dns", "nav_tcp", "nav_tls", "nav_request", "nav_response", "nav_render")
#: Newest comparable runs read per side for the per-site pass. The raw is the heavy read
#: (every resource-timing entry of every page), and a median over the recent thirty runs is
#: the profile's per-site standing; the whole history would be a slower version of the same
#: number.
SITE_RUN_LIMIT = 30
DEFAULT_SIGMA = 2.0
#: Gaps smaller than this (in Overall points) are reported as level rather than as a win
#: for either side — below the crown's own 0.1 rounding, so it would be a claim about noise.
LEVEL_POINTS = 0.05

_META = {m.key: m for m in METRICS}


class NoData(LookupError):
    """The comparison cannot be made: a side has no comparable run, or no reference exists."""


# ── noise ─────────────────────────────────────────────────────────────────────


def _se(values: list[float]) -> float | None:
    """Standard error of the **median** ≈ IQR/√n — how precisely the median is known, not how
    much runs bounce (``routes_settings._overall_se`` uses the same convention). None on a
    single sample: one run says nothing about its own noise."""
    n = len(values)
    if n < 2:
        return None
    q = quantiles(sorted(values), n=4)
    return (q[2] - q[0]) / (n ** 0.5)


def _se_from_spread(entry: dict | None) -> float | None:
    """The same SE off a stored rollup entry ``{n, median, p25, p75}``."""
    if not entry or int(entry.get("n") or 0) < 2:
        return None
    p25, p75 = entry.get("p25"), entry.get("p75")
    if p25 is None or p75 is None:
        return None
    return (float(p75) - float(p25)) / (int(entry["n"]) ** 0.5)


def _pooled(*ses: float | None) -> float | None:
    """SEs of independent estimates pool in quadrature. None when *either* side's noise is
    unknown — a bar drawn from one side only would understate it."""
    if any(s is None for s in ses):
        return None
    return sum(s * s for s in ses) ** 0.5  # type: ignore[operator]


def _clear(delta: float | None, se: float | None, sigma: float) -> bool | None:
    """Does the delta exceed ``sigma`` standard errors? None when unknowable."""
    if delta is None or se is None:
        return None
    return abs(delta) > sigma * se


def _r(v: float | None, n: int = 2) -> float | None:
    return None if v is None else round(float(v), n)


# ── crown legs ────────────────────────────────────────────────────────────────


def decompose_legs(
    definition: dict,
    a: dict[str, dict],
    b: dict[str, dict],
    sigma: float = DEFAULT_SIGMA,
) -> dict:
    """Split the Overall gap A − B into per-leg contributions.

    ``a``/``b`` are per-metric rollup entries ``{n, median, p25, p75}`` over each side's
    **subscores** (what ``profile_aggregates`` stores). Pure: no session, no config.

    Weighted crown: each leg contributes ``w_k · (med_A − med_B) / Σw`` exactly, over the
    medians rounded as ``_grade_medians`` rounds them. Any other method: the change in the
    corner from swapping that one leg to the other side's value, and ``exact`` is False.
    """
    crown, required = overall_metrics(definition)
    weights = overall_weights(definition)
    method = overall_method(definition)
    med_a = {m: round(float(a[m]["median"]), 2) for m in crown if a.get(m) and a[m].get("median") is not None}
    med_b = {m: round(float(b[m]["median"]), 2) for m in crown if b.get(m) and b[m].get("median") is not None}
    exact = method == "weighted" and all(m in med_a and m in med_b for m in crown)
    total_w = sum(float(weights.get(m, 1.0)) for m in crown) or 1.0

    legs: list[dict] = []
    for m in crown:
        w = float(weights.get(m, 1.0))
        meta = _META.get(m)
        sa, sb = med_a.get(m), med_b.get(m)
        missing = sa is None or sb is None
        se_sub = _pooled(_se_from_spread(a.get(m)), _se_from_spread(b.get(m)))
        if missing:
            points = points_se = None
        elif method == "weighted":
            points = w * (sa - sb) / total_w
            points_se = None if se_sub is None else w * se_sub / total_w
        else:
            # One-leg swap on the corner: A's corner minus the corner with this leg at B's value.
            base = corner_score([med_a[k] for k in crown if k in med_a])
            swapped = corner_score([(sb if k == m else med_a[k]) for k in crown if k in med_a])
            points = None if base is None or swapped is None else base - swapped
            points_se = None
        legs.append({
            "metric": m,
            "label": meta.label if meta else m,
            "unit": meta.unit if meta else "",
            "weight": w,
            "share_of_weight": round(w / total_w, 4),
            "required": m in required,
            "a": {"subscore": sa, "n": int((a.get(m) or {}).get("n") or 0)},
            "b": {"subscore": sb, "n": int((b.get(m) or {}).get("n") or 0)},
            "delta_subscore": None if missing else _r(sa - sb),
            "points": _r(points),
            "se": _r(points_se),
            "clear": _clear(points, points_se, sigma),
            "missing": missing,
        })

    present = [l for l in legs if l["points"] is not None]
    gap = sum(l["points"] for l in present) if present and not any(l["missing"] for l in legs) else None
    gap_se = (
        sum((l["se"] or 0.0) ** 2 for l in present) ** 0.5
        if present and all(l["se"] is not None for l in present) else None
    )
    return {
        "method": method,
        "exact": exact,
        "legs": legs,
        "gap": {"points": _r(gap), "se": _r(gap_se), "clear": _clear(gap, gap_se, sigma)},
    }


# ── reading the two sides ─────────────────────────────────────────────────────


def _comparable(version: str, fp: str):
    return (
        Run.status == RunStatus.COMPLETE,
        Run.settings_fingerprint == fp,
        Score.methodology_version == version,
        Score.comparability != "incomparable",
    )


def _browser_join():
    return and_(BenchmarkResult.run_id == Run.id, BenchmarkResult.plugin == "browser")


def _side_values(session: Session, version: str, fp: str, crown: list[str]) -> dict:
    """Per-run scalars for one side, read in SQL (never a decoded document per run): the
    crown metrics' raw values off the graded ``Score.metric_values`` and the navigation
    phases off the browser result's metrics. Bounded by this profile's runs."""
    phase_cols = [BenchmarkResult.metrics[_META[k].source_key].as_float() for k in NAV_PHASES]
    crown_cols = [Score.metric_values[m].as_float() for m in crown]
    q = (
        select(Run.id, Run.iterations, *crown_cols, *phase_cols)
        .join(Score, Score.run_id == Run.id)
        .outerjoin(BenchmarkResult, _browser_join())
        .where(*_comparable(version, fp))
    )
    raw: dict[str, list[float]] = {m: [] for m in crown}
    phases: dict[str, list[float]] = {k: [] for k in NAV_PHASES}
    runs = iterations = 0
    for row in session.execute(q):
        runs += 1
        iterations += int(row[1] or 1)
        vals = row[2:]
        for i, m in enumerate(crown):
            if vals[i] is not None:
                raw[m].append(float(vals[i]))
        for j, k in enumerate(NAV_PHASES):
            v = vals[len(crown) + j]
            if v is not None:
                phases[k].append(float(v))
    return {"runs": runs, "iterations": iterations, "raw": raw, "phases": phases}


def _label(session: Session, fp: str) -> str | None:
    settings = session.scalar(
        select(Run.settings)
        .where(Run.settings_fingerprint == fp, Run.settings.is_not(None))
        .order_by(Run.id.desc())
        .limit(1)
    )
    try:
        return summarize(settings) if settings else None
    except Exception:  # noqa: BLE001 — a label is decoration
        return None


def _has_runs(session: Session, version: str, fp: str | None) -> bool:
    return bool(fp) and fp in profile_aggregates.stamps(session, version, [fp])


def _pick_reference(session: Session, version: str, fp: str, vs: str | None, min_iterations: int) -> tuple[str, str]:
    """Who to compare against, and why. An explicit ``vs`` wins. Otherwise the unshaped link
    (the question "what is shaping buying?") when it has comparable runs, else the sitting
    crown ("what separates this from the best?"), else the best-graded other profile."""
    if vs:
        if vs == fp:
            raise NoData("A profile cannot be compared against itself")
        if not _has_runs(session, version, vs):
            raise NoData(f"Profile {vs} has no comparable runs under {version}")
        return vs, "chosen"
    if fp != SQM_OFF_FINGERPRINT and _has_runs(session, version, SQM_OFF_FINGERPRINT):
        return SQM_OFF_FINGERPRINT, "sqm_off"
    crown = crown_follower.current_crown(session) or {}
    cfp = crown.get("fingerprint")
    if cfp and cfp != fp and _has_runs(session, version, cfp):
        return cfp, "crown"
    # No ledger crown to lean on: the best-graded confident profile that isn't this one.
    definition = ensure_current_methodology(session, get_config(session)).definition or {}
    crown_metrics, required = overall_metrics(definition)
    weights = overall_weights(definition)
    best: tuple[float, int, str] | None = None
    for other, agg in profile_aggregates.aggregates(session, version).items():
        if other == fp:
            continue
        med = {m: v["median"] for m, v in (agg["metrics"] or {}).items() if v.get("median") is not None}
        overall, iters = crown_follower._grade_medians(med, agg["iterations"], crown_metrics, required, weights)
        if overall is None:
            continue
        key = (1 if iters >= min_iterations else 0, overall, iters)
        if best is None or key > (best[0], best[1], best[2]):
            best = key  # type: ignore[assignment]
            best_fp = other
    if best is None:
        raise NoData("No other profile has comparable runs to compare against")
    return best_fp, "best_other"


# ── sites ─────────────────────────────────────────────────────────────────────


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc or url
    except ValueError:
        return url


def _site_samples(session: Session, version: str, fp: str, limit: int, keys: tuple[str, ...]) -> tuple[dict[str, dict[str, list[float]]], int]:
    """``{url: {source_key: [per-run median]}}`` over the newest ``limit`` comparable runs
    with browser raw, each page re-derived through ``interpret.derive`` — the same function
    the run's own metrics came from, so a per-site number is the run's number restricted to
    one page, not a second reading of the raw."""
    if limit <= 0:
        return {}, 0
    q = (
        select(Run.id, BenchmarkResult.raw)
        .join(Score, Score.run_id == Run.id)
        .join(BenchmarkResult, _browser_join())
        .where(*_comparable(version, fp), BenchmarkResult.raw.is_not(None))
        .order_by(Run.id.desc())
        .limit(limit)
    )
    out: dict[str, dict[str, list[float]]] = {}
    used = 0
    for _run_id, raw in session.execute(q):
        per_url: dict[str, dict[str, list[float]]] = {}
        for _i, url, obs in browser_url_observations(raw):
            if "nav" not in obs:
                continue
            try:
                m = derive("browser", {"urls": {url: obs}})
            except Exception:  # noqa: BLE001 — one bad page must not blank the table
                log.debug("per-site derive failed for %s", url, exc_info=True)
                continue
            slot = per_url.setdefault(url, {})
            for k in keys:
                if m.get(k) is not None:
                    slot.setdefault(k, []).append(float(m[k]))
        if not per_url:
            continue
        used += 1
        for url, series in per_url.items():
            slot = out.setdefault(url, {})
            for k, vals in series.items():
                slot.setdefault(k, []).append(median(vals))
    return out, used


def _thresholds(definition: dict) -> dict[str, tuple[float, float]]:
    return {
        m["key"]: (float(m["best"]), float(m["worst"]))
        for m in (definition or {}).get("metrics", [])
        if m.get("best") is not None and m.get("worst") is not None
    }


def _site_rows(
    definition: dict,
    a: dict[str, dict[str, list[float]]],
    b: dict[str, dict[str, list[float]]],
    sigma: float,
) -> list[dict]:
    crown, _required = overall_metrics(definition)
    weights = overall_weights(definition)
    thr = _thresholds(definition)
    total_w = sum(float(weights.get(m, 1.0)) for m in crown) or 1.0
    rows: list[dict] = []
    for url in sorted(set(a) & set(b)):
        sa, sb = a[url], b[url]
        legs: list[dict] = []
        for m in crown:
            key = _META[m].source_key if m in _META else m
            va, vb = sa.get(key) or [], sb.get(key) or []
            if not va or not vb or m not in thr:
                legs.append({"metric": m, "label": _META[m].label if m in _META else m, "a": None, "b": None,
                             "delta": None, "se": None, "clear": None, "points": None})
                continue
            ma, mb = median(va), median(vb)
            best, worst = thr[m]
            w = float(weights.get(m, 1.0))
            points = w * (_normalize(ma, best, worst) - _normalize(mb, best, worst)) / total_w
            se = _pooled(_se(va), _se(vb))
            legs.append({
                "metric": m, "label": _META[m].label if m in _META else m,
                "a": _r(ma, 1), "b": _r(mb, 1), "delta": _r(ma - mb, 1), "se": _r(se, 1),
                "clear": _clear(ma - mb, se, sigma), "points": _r(points),
            })
        phases: list[dict] = []
        for k in NAV_PHASES:
            key = _META[k].source_key
            va, vb = sa.get(key) or [], sb.get(key) or []
            if not va or not vb:
                continue
            ma, mb = median(va), median(vb)
            se = _pooled(_se(va), _se(vb))
            phases.append({"metric": k, "label": _META[k].label, "a": _r(ma, 1), "b": _r(mb, 1),
                           "delta": _r(ma - mb, 1), "se": _r(se, 1), "clear": _clear(ma - mb, se, sigma)})
        complete = all(l["points"] is not None for l in legs)
        runs_a = max((len(v) for v in sa.values()), default=0)
        runs_b = max((len(v) for v in sb.values()), default=0)
        clear_phases = [p for p in phases if p["clear"] and p["delta"]]
        rows.append({
            "url": url,
            "host": _host(url),
            "runs_a": runs_a,
            "runs_b": runs_b,
            "points": _r(sum(l["points"] for l in legs)) if complete else None,
            "legs": legs,
            "phases": phases,
            # Where in this page's load the biggest clear difference sits, if any.
            "top_phase": max(clear_phases, key=lambda p: abs(p["delta"])) if clear_phases else None,
        })
    rows.sort(key=lambda r: (r["points"] is None, -abs(r["points"] or 0.0)))
    return rows


# ── the verdict ───────────────────────────────────────────────────────────────


def _pts(v: float) -> str:
    return f"{abs(v):.1f} point{'' if abs(v) == 1 else 's'}"


def verdict(names: tuple[str, str], gap: dict, legs: list[dict], phases: list[dict], sites: list[dict]) -> str:
    """One paragraph, with its numbers in it, read from the winner's side."""
    a_name, b_name = names
    g = gap.get("points")
    if g is None:
        return f"{a_name} and {b_name} cannot be compared leg for leg: a crown metric is missing on one side."
    if abs(g) < LEVEL_POINTS:
        return f"{a_name} and {b_name} are level on the Overall (within {gap.get('se') or 0:.1f} of noise)."
    win, lose, sign = (a_name, b_name, 1.0) if g > 0 else (b_name, a_name, -1.0)
    noise = (
        "clear of noise" if gap.get("clear") else
        (f"within noise, ±{gap['se']:.1f}" if gap.get("se") is not None else "noise unknown")
    )
    parts = [f"{win} beats {lose} by {_pts(g)} on the Overall ({noise})."]

    scored = [l for l in legs if l["points"] is not None]
    helping = sorted([l for l in scored if l["points"] * sign > 0], key=lambda l: -abs(l["points"]))
    hurting = sorted([l for l in scored if l["points"] * sign < 0], key=lambda l: -abs(l["points"]))
    if helping:
        parts.append(
            "Of that, " + ", ".join(f"{_pts(l['points'])} come{'s' if abs(l['points']) == 1 else ''} from {l['label']}"
                                    for l in helping[:3]) + "."
        )
    if hurting:
        parts.append(
            "It gives back " + ", ".join(f"{_pts(l['points'])} on {l['label']}" for l in hurting[:2]) + "."
        )
    clear_phases = [p for p in phases if p["clear"] and p["delta"] and (p["delta"] * sign) < 0]
    if clear_phases:
        p = max(clear_phases, key=lambda p: abs(p["delta"]))
        parts.append(f"In the load itself the edge sits in {p['label']}: {abs(p['delta']):.0f} ms sooner.")
    else:
        parts.append("No navigation phase separates them clearly.")
    priced = [s for s in sites if s["points"] is not None]
    if priced:
        top = max(priced, key=lambda s: s["points"] * sign)
        if top["points"] * sign > 0:
            leg = max((l for l in top["legs"] if l["points"] is not None), key=lambda l: l["points"] * sign)
            against = [s for s in priced if s["points"] * sign < 0]
            spread = (
                f"; it loses on {len(against)} of {len(priced)} sites" if against else
                f"; it leads on every one of the {len(priced)} sites" if len(priced) > 1 else ""
            )
            parts.append(
                f"The clearest site-level edge is {top['host']} ({_pts(top['points'])}, "
                f"{leg['label']} {abs(leg['delta'] or 0):.0f} ms sooner{spread})."
            )
        else:
            parts.append(f"No single site shows the edge on its own ({len(priced)} sites priced).")
    return " ".join(parts)


# ── entry point ───────────────────────────────────────────────────────────────


def explain(session: Session, fingerprint: str, vs: str | None = None, *, site_run_limit: int = SITE_RUN_LIMIT) -> dict:
    """The full "where a win lives" reading for ``fingerprint`` against ``vs``."""
    config = get_config(session)
    methodology = ensure_current_methodology(session, config)
    definition = methodology.definition or {}
    version = methodology.version
    corr = config.get("correlation", {}) or {}
    sigma = float(corr.get("crown_tie_sigma", DEFAULT_SIGMA) or DEFAULT_SIGMA)
    min_iterations = int(corr.get("min_iterations", 15) or 15)
    crown, required = overall_metrics(definition)
    if not crown:
        raise NoData(f"Methodology {version} defines no crown metrics")
    if not _has_runs(session, version, fingerprint):
        raise NoData(f"Profile {fingerprint} has no comparable runs under {version}")

    ref, why = _pick_reference(session, version, fingerprint, vs, min_iterations)
    weights = overall_weights(definition)
    aggs = profile_aggregates.aggregates(session, version, [fingerprint, ref])
    agg_a, agg_b = aggs.get(fingerprint) or {}, aggs.get(ref) or {}
    split = decompose_legs(definition, agg_a.get("metrics") or {}, agg_b.get("metrics") or {}, sigma)

    def _grade(agg: dict) -> float | None:
        med = {m: v["median"] for m, v in (agg.get("metrics") or {}).items() if v.get("median") is not None}
        return crown_follower._grade_medians(med, int(agg.get("iterations") or 0), crown, required, weights)[0]

    side_a = _side_values(session, version, fingerprint, crown)
    side_b = _side_values(session, version, ref, crown)
    # Raw medians onto the legs (ms), beside the subscores the points were formed from.
    for leg in split["legs"]:
        m = leg["metric"]
        va, vb = side_a["raw"].get(m) or [], side_b["raw"].get(m) or []
        ma = median(va) if va else None
        mb = median(vb) if vb else None
        se = _pooled(_se(va), _se(vb)) if va and vb else None
        leg["a"].update({"raw": _r(ma, 1), "raw_se": _r(_se(va) if va else None, 1)})
        leg["b"].update({"raw": _r(mb, 1), "raw_se": _r(_se(vb) if vb else None, 1)})
        leg["delta_raw"] = _r(ma - mb, 1) if ma is not None and mb is not None else None
        leg["raw_clear"] = _clear(leg["delta_raw"], se, sigma)

    phases: list[dict] = []
    for k in NAV_PHASES:
        va, vb = side_a["phases"].get(k) or [], side_b["phases"].get(k) or []
        ma = median(va) if va else None
        mb = median(vb) if vb else None
        se = _pooled(_se(va), _se(vb)) if va and vb else None
        delta = ma - mb if ma is not None and mb is not None else None
        phases.append({
            "metric": k,
            "label": _META[k].label,
            "a": {"median": _r(ma, 1), "se": _r(_se(va) if va else None, 1), "n": len(va)},
            "b": {"median": _r(mb, 1), "se": _r(_se(vb) if vb else None, 1), "n": len(vb)},
            "delta": _r(delta, 1),
            "se": _r(se, 1),
            "clear": _clear(delta, se, sigma),
        })

    keys = tuple(_META[m].source_key for m in crown if m in _META) + tuple(_META[k].source_key for k in NAV_PHASES)
    sites_a, used_a = _site_samples(session, version, fingerprint, site_run_limit, keys)
    sites_b, used_b = _site_samples(session, version, ref, site_run_limit, keys)
    sites = _site_rows(definition, sites_a, sites_b, sigma)

    names = profile_names.names_for(session, [fingerprint, ref])
    name_a = names.get(fingerprint) or fingerprint
    name_b = names.get(ref) or ref
    notes = [
        "Points are Overall points, signed from this profile's side: positive means this profile is ahead.",
        f"Noise bars are the standard error of the median (IQR/√n) on each side, pooled; 'clear' means the "
        f"difference exceeds {sigma:g} of them — the same bar the crown uses to call a tie.",
        "Per-site points are the gap that site alone would produce on the methodology's thresholds — "
        "a check on whether the win is everywhere or on one page, not a split of the pooled gap.",
    ]
    if not split["exact"]:
        notes.append(
            "This crown is not a weighted mean, so the legs do not add up to the gap: each leg's points "
            "are the change from swapping that leg alone."
        )
    return {
        "fingerprint": fingerprint,
        "methodology": version,
        "method": split["method"],
        "exact": split["exact"],
        "sigma": sigma,
        "reference": {"why": why},
        "a": {"fingerprint": fingerprint, "name": name_a, "label": _label(session, fingerprint),
              "overall": _grade(agg_a), "iterations": int(agg_a.get("iterations") or 0),
              "runs": int(agg_a.get("run_count") or 0)},
        "b": {"fingerprint": ref, "name": name_b, "label": _label(session, ref),
              "overall": _grade(agg_b), "iterations": int(agg_b.get("iterations") or 0),
              "runs": int(agg_b.get("run_count") or 0)},
        "gap": split["gap"],
        "legs": split["legs"],
        "phases": phases,
        "sites": sites,
        "site_run_limit": site_run_limit,
        "site_runs": {"a": used_a, "b": used_b},
        "verdict": verdict((name_a, name_b), split["gap"], split["legs"], phases, sites),
        "notes": notes,
    }
