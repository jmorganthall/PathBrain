"""The percentile crown's helpers: corner a profile's crown subscores, percentile-rank a raw
measurement within the field, and read the tie-aware crown's parameters.

Moved verbatim from ``api/routes_settings.py`` (S16), which imports them back so its
callers and any patch of ``routes_settings._crown_corner`` and friends keep working. Pure
reads: nothing here touches the firewall.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .config_store import get_config
from .methodology import corner_score


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
