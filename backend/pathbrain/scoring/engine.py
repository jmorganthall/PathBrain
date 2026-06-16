"""The Seat of Pants Score (SOPS) engine.

SOPS estimates *human-perceived responsiveness* rather than raw throughput or
ping. It is a weighted average of per-metric subscores, where each metric is
normalized to 0..100 against configurable best/worst thresholds (lower latency =
higher score).

Key design choices:

* **Ping does not dominate.** Latency-derived metrics (jitter, packet loss) carry
  small weight by default; perceptual metrics (TTFB, render, TLS) carry the most.
* **Missing metrics don't penalize.** If a metric isn't available (e.g. render
  before the Playwright engine ships, or a failed probe), its weight is
  redistributed proportionally across the metrics that *are* present, so the
  score stays on a stable 0..100 scale and remains comparable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

# Maps a SOPS metric name -> (plugin name, metric key within that plugin).
# This is the *completion* axis: how quickly things finish (latency/throughput).
METRIC_SOURCES: dict[str, tuple[str, str]] = {
    "dns": ("dns", "lookup_ms"),
    "tcp": ("tcp", "connect_ms"),
    "tls": ("tls", "handshake_ms"),
    "ttfb": ("http", "ttfb_ms"),
    "render": ("browser", "total_render_ms"),  # reserved for Phase 2 (Playwright)
    "jitter": ("icmp", "jitter_ms"),
    "packet_loss": ("icmp", "packet_loss_pct"),
}

# The *perceptual* axis: when content starts appearing and how responsive the page
# feels — paint timing, not completion. Scored separately from SOPS (never folded
# in) so completion-optimal and responsiveness-optimal settings can visibly pull
# apart. All from the browser (Playwright) engine; Speed Index is a future add.
PERCEPTUAL_METRIC_SOURCES: dict[str, tuple[str, str]] = {
    "fcp": ("browser", "fcp_ms"),   # First Contentful Paint
    "lcp": ("browser", "lcp_ms"),   # Largest Contentful Paint
    "inp": ("browser", "inp_ms"),   # Interaction to Next Paint (best-effort)
}


@dataclass
class ScoreBreakdown:
    sops: float
    subscores: dict[str, float] = field(default_factory=dict)
    weights_used: dict[str, float] = field(default_factory=dict)
    metric_values: dict[str, float] = field(default_factory=dict)


def _normalize(value: float, best: float, worst: float) -> float:
    """Map a lower-is-better value to a 0..100 subscore, clamped.

    For lower-is-better metrics with positive bounds we interpolate on a
    *logarithmic* scale (Weber–Fechner: perceived magnitude grows with the log of
    the stimulus). Equal *ratios* of latency cost equal score — e.g. 20→40ms drops
    the same as 200→400ms — which models human perception far better than a linear
    ramp where 900→1000ms would matter as much as 20→120ms.
    """
    if worst == best:
        return 100.0 if value <= best else 0.0
    if worst > best:  # normal: smaller is better
        if value <= best:
            return 100.0
        if value >= worst:
            return 0.0
        if best > 0 and worst > 0 and value > 0:
            score = (math.log(worst) - math.log(value)) / (math.log(worst) - math.log(best)) * 100.0
            return round(score, 2)
        return round((worst - value) / (worst - best) * 100.0, 2)
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

    ``metric_sources`` selects which axis is scored: SOPS (completion, the
    default) or the perceptual axis (``PERCEPTUAL_METRIC_SOURCES``). The math is
    identical; only the metric set and rubric differ.
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


def compute_responsiveness(
    plugin_metrics: dict[str, dict],
    weights: dict[str, float],
    thresholds: dict[str, dict[str, float]],
) -> ScoreBreakdown:
    """Compute the perceptual Responsiveness Score (paint timing).

    A sibling of :func:`compute_score` over ``PERCEPTUAL_METRIC_SOURCES``. Kept
    distinct from SOPS so the two axes are never blended. ``subscores`` is empty
    when no paint metrics were captured (e.g. browser engine unavailable), which
    the caller treats as "no responsiveness score" rather than a zero.
    """
    return compute_score(
        plugin_metrics, weights, thresholds, metric_sources=PERCEPTUAL_METRIC_SOURCES
    )
