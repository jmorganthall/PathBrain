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


__all__ = ["graded_field", "paper_cost", "pooled_se", "quartile_grade", "se", "math_crown"]


def math_crown(session, version: str, min_iterations: int) -> dict | None:
    """The mathematical crown: the confident profile with the lowest **median FCP × median
    LCP**, in raw milliseconds.

    Every other verdict grades a profile — perception-calibrated subscores, weights, a
    field prior, the ring's paired rounds. This one deliberately does none of that. It asks
    the plainest question the measurements can answer, "which profile shows first paint and
    main content fastest?", with the product making it scale-free: a 10% gain on either leg
    counts the same. It is a reference beside the verdicts, never a crowning policy —
    nothing applies it to the firewall.

    Confident means ``iterations ≥ min_iterations`` (the crown's own bar); the unshaped
    baseline is left out (`settings_profile.is_sqm_off_profile`). Ties break toward more
    iterations. Returns the winner with its two medians, the product and the runner-up, or
    None when no confident profile has both readings. Read off the rollup, so it costs what
    `graded_field` costs."""
    from . import profile_aggregates
    from .refresh import list_profiles

    stored = list_profiles(session)
    if not stored:
        return None
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
            "kind": "math",
            "fcp_ms": round(float(fcp), 1),
            "lcp_ms": round(float(lcp), 1),
            "product": float(fcp) * float(lcp),
            "iterations": iters,
        })
    if not rows:
        return None
    rows.sort(key=lambda r: (r["product"], -r["iterations"], r["fingerprint"]))
    best = dict(rows[0])
    best["product"] = round(best["product"], 1)
    ru = rows[1] if len(rows) > 1 else None
    best["runner_up"] = None if ru is None else {
        "fingerprint": ru["fingerprint"], "name": ru["name"], "label": ru["label"],
        "fcp_ms": ru["fcp_ms"], "lcp_ms": ru["lcp_ms"],
    }
    best["profiles"] = len(rows)
    return best


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
