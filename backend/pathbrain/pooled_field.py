"""**The pooled field, graded off the rollup** — every stored profile with its Overall and
the band around it, the pooled anchors the fused ranking (`overall_ranking`) is fitted to.

Deliberately never a ``compute_profiles`` pass: this is read on every Dashboard load through
``GET /api/overall``, and a cold field pass on page load is the documented way to take the
process down (`_field_stamp`, the unresponsiveness incident). Everything here comes from
`profile_aggregates` — one stamp-verified row per profile — graded through the one
`crown_follower._grade_medians`, so the Overall, its own p25/p75 and the standings' grade are
the same arithmetic read at three places in the rollup rather than two implementations that
agree until one is edited.

This module used to be ``verdict.py`` and carried a second "which profile should I run?"
answer, ``GET /api/settings/verdict`` — the pooled argmax with a tie set and a sentence. The
Dashboard's verdict card never read it: the card reads ``GET /api/overall``, the fused
ranking, which is the one verdict the crowning policy acts on. Two "the answer" endpoints
with one of them dead is exactly how a card and a standings chip come to disagree on one
screen, so the pooled sentence is gone and what survives is the arithmetic both still share.
"""
from __future__ import annotations

from .config_store import get_config
from .logging_config import get_logger
from .methodology import ensure_current_methodology, overall_metrics, overall_weights
from .settings_profile import is_sqm_off_profile

log = get_logger(__name__)


def quartile_grade(metrics: dict, key: str, crown: list[str], required: list[str],
                   weights: dict, iters: int) -> float | None:
    """The Overall formed from one quartile (``median``/``p25``/``p75``) of each crown
    metric's subscore, through the single place the crown grade is formed."""
    from .crown_follower import _grade_medians

    row = {
        m: v.get(key)
        for m, v in (metrics or {}).items()
        if isinstance(v, dict) and v.get(key) is not None
    }
    return _grade_medians(row, iters, crown, required, weights)[0]


def se(p25: float | None, p75: float | None, runs: int) -> float | None:
    """Standard error of the median Overall — `profile_aggregates.median_se`, the one
    arithmetic the standings (`routes_settings._overall_se`) and the fused fit read too.
    ``runs`` is the RUN count: the quartiles are over per-run values, one Score per run, so
    dividing by the iteration total (as this once did) understated the SE by √(iterations
    per run) — ×2.2 at five iterations a run — and over-trusted every pooled anchor the
    fused ranking was handed. ``None`` when the spread or the sample size is unknown."""
    if p25 is None or p75 is None:
        return None
    from .profile_aggregates import median_se

    return median_se(max(0.0, float(p75) - float(p25)), runs)


def pooled_se(a: dict, b: dict) -> float:
    """SE of the difference of two medians, ``√(SE_a² + SE_b²)``.

    An unknown SE contributes **0**, not "cannot say" — the same call
    ``routes_settings._finite`` makes, and for the same reason: absent evidence of noise
    must not *inflate* the bar a challenger has to clear. Matching it is what keeps the
    fused ranking's "tied" and the standings' "tied" chip meaning the same thing.
    """
    se_a, se_b = a.get("se") or 0.0, b.get("se") or 0.0
    return (se_a ** 2 + se_b ** 2) ** 0.5


def graded_field(session, version: str) -> list[dict]:
    """Every stored profile with its Overall and band, straight off the rollup.

    ``[{fingerprint, name, label, overall, iterations, runs, se, iqr, is_sqm_off}]``.
    ``is_sqm_off`` is the one predicate every surface keys on
    (`settings_profile.is_sqm_off_profile`): the collapsed fingerprint *or* a stored pipe
    with shaping switched off, so a baseline run recorded before the SQM-off collapse reads
    as the baseline here exactly as it does on Settings Impact.
    """
    from . import profile_aggregates
    from .refresh import list_profiles

    stored = list_profiles(session)
    if not stored:
        return []
    definition = ensure_current_methodology(session, get_config(session)).definition or {}
    crown, required = overall_metrics(definition)
    weights = overall_weights(definition)

    rolled = profile_aggregates.aggregates(session, version, [p["fingerprint"] for p in stored])
    out: list[dict] = []
    for p in stored:
        fp = p["fingerprint"]
        agg = rolled.get(fp) or {}
        metrics, iters = agg.get("metrics") or {}, int(agg.get("iterations") or 0)
        runs = int(agg.get("run_count") or 0)
        overall = quartile_grade(metrics, "median", crown, required, weights, iters)
        if overall is None:
            continue
        p25 = quartile_grade(metrics, "p25", crown, required, weights, iters)
        p75 = quartile_grade(metrics, "p75", crown, required, weights, iters)
        out.append({
            "fingerprint": fp,
            "name": p.get("name"),
            "label": p.get("label"),
            "overall": round(float(overall), 2),
            "iterations": iters,
            "runs": runs,
            # The band is over per-run values, so its SE divides by the RUN count.
            "se": se(p25, p75, runs),
            "iqr": None if p25 is None or p75 is None else max(0.0, float(p75) - float(p25)),
            "is_sqm_off": is_sqm_off_profile(fp, p.get("settings")),
        })
    # The thin-profile floor (`shrinkage.profile_se`): a one-run profile has an IQR of zero and
    # so an SE of zero, which made it a *tied* co-leader nothing could separate and, as a
    # pooled anchor in the fused ranking, a point held as firmly as the crown's 3000 iterations.
    # Under `OWN_SE_MIN_RUNS` runs the SE is the field's own per-run noise over √runs; above it
    # the profile's own, floored at half of that.
    from .shrinkage import profile_se, run_noise

    noise, _n, _basis = run_noise([(r["iqr"], r["runs"]) for r in out])
    for r in out:
        floored = profile_se(r["iqr"], r["runs"], noise)
        r["se"] = floored if floored is not None else r["se"]
    return out


__all__ = [
    "graded_field", "paper_cost", "pooled_se", "quartile_grade", "se", "math_crown",
    "lcp_first_crown", "lcp_band_pct", "DEFAULT_LCP_BAND_PCT",
    "fcp_first_crown", "fcp_band_pct", "DEFAULT_FCP_BAND_PCT",
    "leg_first_crown", "band_pct", "LEG_FIRST", "fastest_legs", "leg_gaps",
]


def _raw_rows(session, version: str, min_iterations: int) -> list[dict]:
    """Every confident, shaped profile with both raw medians: ``{fingerprint, name, label,
    fcp_ms, lcp_ms, iterations}``. The one read behind both raw-millisecond crowns."""
    from . import profile_aggregates
    from .refresh import list_profiles

    stored = list_profiles(session)
    if not stored:
        return []
    rolled = profile_aggregates.aggregates(session, version, [p["fingerprint"] for p in stored])
    rows: list[dict] = []
    for p in stored:
        fp = p["fingerprint"]
        if is_sqm_off_profile(fp, p.get("settings")):
            continue
        agg = rolled.get(fp) or {}
        iters = int(agg.get("iterations") or 0)
        if iters < int(min_iterations or 0):
            continue
        raw = agg.get("raw") or {}
        fcp = (raw.get("fcp") or {}).get("median")
        lcp = (raw.get("lcp") or {}).get("median")
        if not isinstance(fcp, (int, float)) or not isinstance(lcp, (int, float)):
            continue
        if fcp <= 0 or lcp <= 0:
            continue
        rows.append({
            "fingerprint": fp,
            "name": p.get("name"),
            "label": p.get("label"),
            "fcp_ms": round(float(fcp), 1),
            "lcp_ms": round(float(lcp), 1),
            "_fcp": float(fcp),
            "_lcp": float(lcp),
            "iterations": iters,
        })
    return rows


def _public(r: dict) -> dict:
    return {k: v for k, v in r.items() if not k.startswith("_")}


def math_crown(session, version: str, min_iterations: int) -> dict | None:
    """The mathematical crown: the confident profile with the lowest **median FCP × median
    LCP**, in raw milliseconds.

    Every other verdict grades a profile — perception-calibrated subscores, weights, a
    field prior, the ring's paired rounds. This one deliberately does none of that. It asks
    the plainest question the measurements can answer, "which profile shows first paint and
    main content fastest?", with the product making it scale-free: a 10% gain on either leg
    counts the same. A reference beside the verdicts; automation follows it only under the
    opt-in ``math`` policy.

    Confident means ``iterations ≥ min_iterations`` (the crown's own bar); the unshaped
    baseline is left out (`settings_profile.is_sqm_off_profile`). Ties break toward more
    iterations. Returns the winner with its two medians, the product and the runner-up, or
    None when no confident profile has both readings. Read off the rollup, so it costs what
    `graded_field` costs."""
    rows = _raw_rows(session, version, min_iterations)
    if not rows:
        return None
    for r in rows:
        r["kind"] = "math"
        r["product"] = r["_fcp"] * r["_lcp"]
    rows.sort(key=lambda r: (r["product"], -r["iterations"], r["fingerprint"]))
    best = _public(rows[0])
    best["product"] = round(best["product"], 1)
    ru = rows[1] if len(rows) > 1 else None
    best["runner_up"] = None if ru is None else {
        "fingerprint": ru["fingerprint"], "name": ru["name"], "label": ru["label"],
        "fcp_ms": ru["fcp_ms"], "lcp_ms": ru["lcp_ms"],
    }
    best["profiles"] = len(rows)
    return best


#: How far behind the best LCP a profile may sit and still qualify for the LCP-first crown,
#: in percent. The owner's own statement of the trade: "we would sacrifice maybe 10% of best
#: LCP to get to FCP faster".
DEFAULT_LCP_BAND_PCT = 10.0
#: The mirror trade for the FCP-first crown: give up this much of the best first paint to
#: reach main content sooner. Same default, its own dial.
DEFAULT_FCP_BAND_PCT = 10.0
#: The band is a stated trade, not a free dial: below 0 nothing qualifies but the best, and
#: past 50% "LCP first" stops meaning anything. One range for both leg-first crowns.
LCP_BAND_RANGE = (0.0, 50.0)
BAND_RANGE = LCP_BAND_RANGE
#: Qualifiers listed on the response, best on the second leg first.
LCP_FIRST_LISTED = 8

#: The two leg-first crowns: which leg sets the bar, and which one is raced inside it.
#: One rule, two orders — never two implementations.
LEG_FIRST = {
    "lcp_first": {"lead": "lcp", "then": "fcp", "config": "lcp_band_pct",
                  "default": DEFAULT_LCP_BAND_PCT},
    "fcp_first": {"lead": "fcp", "then": "lcp", "config": "fcp_band_pct",
                  "default": DEFAULT_FCP_BAND_PCT},
}


def band_pct(cfg: dict | None, kind: str) -> float:
    """A leg-first crown's configured band (`crown_follow.<lead>_band_pct`), clamped."""
    spec = LEG_FIRST[kind]
    raw = ((cfg or {}).get("crown_follow") or {}).get(spec["config"], spec["default"])
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = spec["default"]
    lo, hi = BAND_RANGE
    return min(hi, max(lo, v))


def lcp_band_pct(cfg: dict | None) -> float:
    """The configured band (`crown_follow.lcp_band_pct`), clamped to `LCP_BAND_RANGE`."""
    return band_pct(cfg, "lcp_first")


def fcp_band_pct(cfg: dict | None) -> float:
    """The configured band (`crown_follow.fcp_band_pct`), clamped to `BAND_RANGE`."""
    return band_pct(cfg, "fcp_first")


def leg_first_crown(
    session, version: str, min_iterations: int, kind: str, band_pct: float | None = None,
) -> dict | None:
    """A leg-first crown: one leg sets the bar, the other is raced inside it.

    Among confident profiles (``iterations ≥ min_iterations``, the unshaped baseline left
    out), find the best median of the **lead** leg in raw milliseconds; every profile whose
    lead median is within ``band_pct`` percent of it **qualifies**; of the qualifiers the
    one with the lowest median on the **then** leg is crowned (ties → lower lead, then more
    iterations). ``lcp_first`` leads with LCP and races FCP; ``fcp_first`` leads with FCP
    and races LCP. No perception curve, no weights, no smoothness leg, no ring — a stated
    preference applied to two measured numbers, so every crowning reads as a sentence with
    its numbers in it.

    Returns ``{fingerprint, name, label, kind, fcp_ms, lcp_ms, iterations, band_pct,
    best_<lead>: {...}, <lead>_limit_ms, <lead>_behind_pct, <then>_gain_ms, qualifiers,
    profiles, qualifying: [...]}`` or None when no confident profile has both readings."""
    spec = LEG_FIRST[kind]
    lead, then = spec["lead"], spec["then"]
    rows = _raw_rows(session, version, min_iterations)
    if not rows:
        return None
    band = max(0.0, float(spec["default"] if band_pct is None else band_pct))
    lk, tk = f"_{lead}", f"_{then}"
    best = min(rows, key=lambda r: (r[lk], -r["iterations"], r["fingerprint"]))
    limit = best[lk] * (1.0 + band / 100.0)
    qualifying = [r for r in rows if r[lk] <= limit + 1e-9]
    qualifying.sort(key=lambda r: (r[tk], r[lk], -r["iterations"], r["fingerprint"]))
    win = qualifying[0]
    out = _public(win)
    out.update({
        "kind": kind,
        "lead": lead,
        "then": then,
        "band_pct": round(band, 1),
        f"{lead}_limit_ms": round(limit, 1),
        f"{lead}_behind_pct": round((win[lk] / best[lk] - 1.0) * 100.0, 1),
        f"best_{lead}": {
            "fingerprint": best["fingerprint"], "name": best["name"],
            "label": best["label"], "lcp_ms": best["lcp_ms"],
            "fcp_ms": best["fcp_ms"], "iterations": best["iterations"],
        },
        f"{then}_gain_ms": round(best[tk] - win[tk], 1),
        "qualifiers": len(qualifying),
        "profiles": len(rows),
        "qualifying": [_public(r) for r in qualifying[:LCP_FIRST_LISTED]],
    })
    return out


def lcp_first_crown(
    session, version: str, min_iterations: int, band_pct: float = DEFAULT_LCP_BAND_PCT,
) -> dict | None:
    """The LCP-first crown: main content first, then first paint, as the owner ranks them
    (`leg_first_crown` with LCP setting the bar and FCP raced inside it)."""
    return leg_first_crown(session, version, min_iterations, "lcp_first", band_pct)


def fcp_first_crown(
    session, version: str, min_iterations: int, band_pct: float = DEFAULT_FCP_BAND_PCT,
) -> dict | None:
    """The FCP-first crown: first paint first, then main content — the mirror of
    `lcp_first_crown` (`leg_first_crown` with FCP setting the bar and LCP raced inside it)."""
    return leg_first_crown(session, version, min_iterations, "fcp_first", band_pct)


#: The tie bar for "within noise": the same 2σ the crown's own `crown_tie_sigma` uses.
PAPER_TIE_SIGMA = 2.0


def _leg(raw: dict, metric: str) -> tuple[float | None, float | None]:
    """(median, SE of the median) for one raw leg off a rollup row — IQR/√runs, the
    rollup's one SE arithmetic (`profile_aggregates.median_se`)."""
    from .profile_aggregates import median_se

    s = (raw or {}).get(metric) or {}
    med = s.get("median")
    if not isinstance(med, (int, float)) or med <= 0:
        return None, None
    p25, p75 = s.get("p25"), s.get("p75")
    iqr = (p75 - p25) if isinstance(p25, (int, float)) and isinstance(p75, (int, float)) else None
    return float(med), median_se(iqr, s.get("n"))


def paper_cost(
    session, version: str, math: dict | None, fingerprints: list[str],
    sigma: float = PAPER_TIE_SIGMA,
) -> dict[str, dict]:
    """What each of these profiles costs in raw time against the fastest on paper.

    The other crowns grade experience — smoothness, perception curves, the ring — and may
    name a profile that is *slower* to paint than the mathematically fastest one. Whether
    that trade is worth it ("a smoother route three minutes longer on a 45-minute commute")
    is a person's call, and it cannot be made without the price. So per profile: its median
    FCP and LCP, the milliseconds and percent each sits behind the fastest-on-paper profile
    (positive = slower), the FCP × LCP product's percent, and whether each leg's gap clears
    ``sigma`` × the pooled SE of the two medians — a gap inside the noise is reported as
    such, never as a cost. Read off the rollup; a profile without both medians is left out.
    """
    from . import profile_aggregates

    if not math or not math.get("fingerprint"):
        return {}
    ref_fp = math["fingerprint"]
    wanted = [fp for fp in dict.fromkeys([ref_fp, *fingerprints]) if fp]
    rolled = profile_aggregates.aggregates(session, version, wanted)
    ref_raw = (rolled.get(ref_fp) or {}).get("raw") or {}
    ref = {m: _leg(ref_raw, m) for m in ("fcp", "lcp")}
    if ref["fcp"][0] is None or ref["lcp"][0] is None:
        return {}
    out: dict[str, dict] = {}
    for fp in dict.fromkeys(fingerprints):
        if not fp:
            continue
        if fp == ref_fp:
            out[fp] = {"is_math": True, "fcp_ms": ref["fcp"][0], "lcp_ms": ref["lcp"][0],
                       "fcp_delta_ms": 0.0, "lcp_delta_ms": 0.0, "fcp_pct": 0.0,
                       "lcp_pct": 0.0, "product_pct": 0.0, "within_noise": True}
            continue
        raw = (rolled.get(fp) or {}).get("raw") or {}
        legs = {m: _leg(raw, m) for m in ("fcp", "lcp")}
        if legs["fcp"][0] is None or legs["lcp"][0] is None:
            continue
        row: dict = {"is_math": False}
        clear_any = False
        for m in ("fcp", "lcp"):
            med, se = legs[m]
            rmed, rse = ref[m]
            delta = med - rmed
            row[f"{m}_ms"] = round(med, 1)
            row[f"{m}_delta_ms"] = round(delta, 1)
            row[f"{m}_pct"] = round(100.0 * delta / rmed, 1)
            bar = None if se is None or rse is None else sigma * (se ** 2 + rse ** 2) ** 0.5
            row[f"{m}_bar_ms"] = None if bar is None else round(bar, 1)
            row[f"{m}_clear"] = bar is not None and abs(delta) > bar
            clear_any = clear_any or row[f"{m}_clear"]
        row["product_pct"] = round(
            100.0 * ((legs["fcp"][0] * legs["lcp"][0]) / (ref["fcp"][0] * ref["lcp"][0]) - 1.0), 1
        )
        row["within_noise"] = not clear_any
        out[fp] = row
    return out


def fastest_legs(session, version: str, min_iterations: int) -> dict | None:
    """The field's fastest median FCP and fastest median LCP, each with the profile that
    holds it — usually two different profiles. Confident, shaped profiles only (the same
    read as the math and leg-first crowns). None when no profile has both medians."""
    rows = _raw_rows(session, version, min_iterations)
    if not rows:
        return None
    out: dict = {"profiles": len(rows)}
    for m in ("fcp", "lcp"):
        r = min(rows, key=lambda x: (x[f"_{m}"], -x["iterations"], x["fingerprint"]))
        out[m] = {"fingerprint": r["fingerprint"], "name": r["name"], "label": r["label"],
                  "ms": r[f"{m}_ms"], "iterations": r["iterations"]}
    return out


def leg_gaps(
    session, version: str, fastest: dict | None, fingerprints: list[str],
    sigma: float = PAPER_TIE_SIGMA,
) -> dict[str, dict]:
    """How much faster or slower each profile is than the field's fastest FCP and fastest
    LCP, leg by leg.

    `paper_cost` prices a profile against ONE reference, the best FCP × LCP product, which
    is rarely the fastest on either leg alone. This answers the plainer question every crown
    view should be able to state: per leg, this profile's median, the milliseconds and
    percent behind the field's fastest median on that leg (positive = slower; 0 for the
    holder), and whether the gap clears ``sigma`` × the pooled SE of the two medians — a gap
    inside that is noise, never a cost. Read off the rollup; a profile without the leg's
    median gets no entry for that leg.
    """
    from . import profile_aggregates

    if not fastest:
        return {}
    refs = {m: (fastest.get(m) or {}).get("fingerprint") for m in ("fcp", "lcp")}
    wanted = [fp for fp in dict.fromkeys([*refs.values(), *fingerprints]) if fp]
    rolled = profile_aggregates.aggregates(session, version, wanted)
    ref = {m: _leg((rolled.get(refs[m]) or {}).get("raw") or {}, m) if refs[m] else (None, None)
           for m in ("fcp", "lcp")}
    out: dict[str, dict] = {}
    for fp in dict.fromkeys(fingerprints):
        if not fp:
            continue
        raw = (rolled.get(fp) or {}).get("raw") or {}
        row: dict = {}
        for m in ("fcp", "lcp"):
            med, se = _leg(raw, m)
            rmed, rse = ref[m]
            if med is None or rmed is None:
                continue
            delta = med - rmed
            bar = None if se is None or rse is None else sigma * (se ** 2 + rse ** 2) ** 0.5
            row[m] = {
                "ms": round(med, 1),
                "fastest_ms": round(rmed, 1),
                "delta_ms": round(delta, 1),
                "pct": round(100.0 * delta / rmed, 1),
                "bar_ms": None if bar is None else round(bar, 1),
                "clear": bar is not None and abs(delta) > bar,
                "is_fastest": fp == refs[m],
            }
        if row:
            out[fp] = row
    return out
