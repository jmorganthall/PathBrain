"""The Seat of Pants Score (SOPS) engine.

SOPS estimates *human-perceived responsiveness* rather than raw throughput or
ping. It is a weighted average of per-metric subscores, where each metric is
normalized to 0..100 against configurable best/worst thresholds (lower latency =
higher score).

Key design choices:

* **Two axes, never blended.** SOPS (``METRIC_SOURCES``) is perception-led — when
  content actually appears/responds (paint timing) plus TTFB and render. Raw
  infrastructure timing (DNS/TCP/TLS/jitter/loss) is the separate *Completion*
  axis (``COMPLETION_METRIC_SOURCES``), because it barely moves the human sense of
  speed. Both use the identical math below; only the metric set + rubric differ.
* **Missing metrics don't penalize.** If a metric isn't available (e.g. paint
  timing where the browser engine didn't run, or a failed probe), its weight is
  redistributed proportionally across the metrics that *are* present, so the
  score stays on a stable 0..100 scale and remains comparable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# SOPS — the "Seat of Pants" score: the headline *human-feel* measure. This is
# perception-led: when content actually starts/finishes appearing (paint timing)
# plus the most perceptual completion metrics (TTFB, total render). It is what we
# rank profiles by, chart, and have experiments optimize.
METRIC_SOURCES: dict[str, tuple[str, str]] = {
    "fcp": ("browser", "fcp_ms"),               # First Contentful Paint
    "lcp": ("browser", "lcp_ms"),               # Largest Contentful Paint
    "inp": ("browser", "inp_ms"),               # Interaction to Next Paint (best-effort)
    "ttfb": ("http", "ttfb_ms"),                # time-to-first-byte (start of response)
    "render": ("browser", "total_render_ms"),   # wall-clock full render
}

# Completion — pure-infrastructure timing (connection setup + ICMP). A diagnostic
# secondary axis kept *separate* from SOPS, so latency-optimal vs. feels-fast
# settings can visibly pull apart. Raw metrics don't move the human sense of
# speed much on their own, which is exactly why they're not in SOPS.
COMPLETION_METRIC_SOURCES: dict[str, tuple[str, str]] = {
    "dns": ("dns", "lookup_ms"),
    "tcp": ("tcp", "connect_ms"),
    "tls": ("tls", "handshake_ms"),
    "jitter": ("icmp", "jitter_ms"),
    "packet_loss": ("icmp", "packet_loss_pct"),
}


@dataclass
class ScoreBreakdown:
    sops: float
    subscores: dict[str, float] = field(default_factory=dict)
    weights_used: dict[str, float] = field(default_factory=dict)
    metric_values: dict[str, float] = field(default_factory=dict)


# The score curve approaches 100 asymptotically but never reaches it for any real
# measurement: there is always headroom ("everything can be better"). Below this
# knee the mapping is the exact log-ratio line (Weber–Fechner); above it the line
# is squashed toward — but never onto — 100. So the `best` threshold means
# "excellent" (~92), not a perfect/unbeatable 100.
_CEIL_KNEE = 85.0   # raw score above which we start squashing toward 100
_CEIL_TAU = 25.0    # gentleness of the approach (larger = slower, more headroom)


def _soft_ceiling(raw: float) -> float:
    """Squash a raw 0..∞ log-ratio score so it approaches but never reaches 100."""
    if raw <= 0.0:
        return 0.0
    if raw <= _CEIL_KNEE:
        return round(raw, 2)
    return round(100.0 - (100.0 - _CEIL_KNEE) * math.exp(-(raw - _CEIL_KNEE) / _CEIL_TAU), 2)


def _normalize(value: float, best: float, worst: float) -> float:
    """Map a lower-is-better value to a 0..100 subscore.

    For lower-is-better metrics with positive bounds we interpolate on a
    *logarithmic* scale (Weber–Fechner: perceived magnitude grows with the log of
    the stimulus). Equal *ratios* of latency cost equal score — e.g. 20→40ms drops
    the same as 200→400ms — which models human perception far better than a linear
    ramp where 900→1000ms would matter as much as 20→120ms.

    The top of the scale is **asymptotic**: a value at ``best`` scores ~92, and
    scores only approach 100 as the value approaches zero, so a real measurement
    never earns a perfect score — there is always room to improve. The bottom
    stays hard: at/beyond ``worst`` the subscore is 0 (unusable).
    """
    if worst == best:
        return _soft_ceiling(100.0) if value <= best else 0.0
    if worst > best:  # normal: smaller is better
        if value >= worst:
            return 0.0
        if best > 0 and worst > 0 and value > 0:
            raw = (math.log(worst) - math.log(value)) / (math.log(worst) - math.log(best)) * 100.0
        else:
            raw = (worst - value) / (worst - best) * 100.0
        return _soft_ceiling(raw)
    # Inverted thresholds (higher is better) — linear; supports future metrics.
    if value >= best:
        return 100.0
    if value <= worst:
        return 0.0
    return round((value - worst) / (best - worst) * 100.0, 2)


def _collect_metric_values(
    plugin_metrics: dict[str, dict],
    metric_sources: dict[str, tuple[str, str]],
) -> dict[str, float]:
    """Pull each axis metric's raw value out of the per-plugin metrics."""
    values: dict[str, float] = {}
    for metric, (plugin, key) in metric_sources.items():
        metrics = plugin_metrics.get(plugin) or {}
        value = metrics.get(key)
        if value is not None:
            values[metric] = float(value)
    return values


def compute_score(
    plugin_metrics: dict[str, dict],
    weights: dict[str, float],
    thresholds: dict[str, dict[str, float]],
    metric_sources: dict[str, tuple[str, str]] | None = None,
) -> ScoreBreakdown:
    """Compute a 0..100 weighted score from per-plugin metrics.

    ``plugin_metrics`` maps plugin name -> its flat metrics dict, e.g.
    ``{"dns": {"lookup_ms": 12.0}, "icmp": {"jitter_ms": 1.1, ...}}``.

    ``metric_sources`` selects which axis is scored: SOPS (the perception-led
    human-feel score, the default) or Completion (``COMPLETION_METRIC_SOURCES``).
    The math is identical; only the metric set and rubric differ.
    """
    values = _collect_metric_values(plugin_metrics, metric_sources or METRIC_SOURCES)

    subscores: dict[str, float] = {}
    available_weights: dict[str, float] = {}
    for metric, value in values.items():
        thr = thresholds.get(metric)
        weight = weights.get(metric, 0.0)
        if thr is None or weight <= 0:
            continue
        subscores[metric] = _normalize(value, thr["best"], thr["worst"])
        available_weights[metric] = float(weight)

    total_weight = sum(available_weights.values())
    if total_weight <= 0:
        return ScoreBreakdown(sops=0.0, metric_values=values)

    # Redistribute proportionally by normalizing the available weights to 1.0.
    weights_used = {m: w / total_weight for m, w in available_weights.items()}
    sops = sum(subscores[m] * weights_used[m] for m in subscores)

    return ScoreBreakdown(
        sops=round(sops, 2),
        subscores=subscores,
        weights_used={m: round(w, 4) for m, w in weights_used.items()},
        metric_values=values,
    )


def compute_completion(
    plugin_metrics: dict[str, dict],
    weights: dict[str, float],
    thresholds: dict[str, dict[str, float]],
) -> ScoreBreakdown:
    """Compute the Completion score (pure-infrastructure timing).

    A sibling of :func:`compute_score` over ``COMPLETION_METRIC_SOURCES``. Kept
    distinct from SOPS so the two axes are never blended. ``subscores`` is empty
    when none of its metrics were captured, which the caller treats as "no
    completion score" rather than a zero.
    """
    return compute_score(
        plugin_metrics, weights, thresholds, metric_sources=COMPLETION_METRIC_SOURCES
    )
