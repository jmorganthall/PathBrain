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


__all__ = ["graded_field", "pooled_se", "quartile_grade", "se"]
