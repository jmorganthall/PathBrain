"""Single source of truth for benchmark metrics.

Every metric is defined exactly once here: its source plugin + key, which score
axis it feeds (if any), its rubric (default weight + best/worst thresholds), and
its display metadata (label, description, unit, direction). The scoring sources,
the default weights/thresholds, the "latest rubric" markers, and the API catalog
the UI consumes are all *derived* from this list — so bringing in a new
measurement is a one-place change here (plus the plugin that emits it).

Keep behaviour stable: the derived ``METRIC_SOURCES`` / weights / thresholds must
match what the engine and config previously hardcoded.
"""
from __future__ import annotations

from dataclasses import dataclass

# Score axes.
SOPS = "sops"               # headline human-feel score (perception-led)
COMPLETION = "completion"   # secondary pure-infrastructure axis


@dataclass(frozen=True)
class MetricDef:
    """One measured metric and everything the rest of the app needs to know about it."""

    key: str                 # logical name / axis subscore key, e.g. "fcp"
    plugin: str              # source plugin name
    source_key: str          # key within that plugin's metrics dict, e.g. "fcp_ms"
    label: str               # short human label
    description: str         # plain-English explanation (UI tooltip)
    unit: str = ""           # "ms", "%", "Mbps", …
    axis: str | None = None  # SOPS, COMPLETION, or None for display-only (not scored)
    weight: float = 0.0      # default weight within its axis (scored metrics only)
    best: float | None = None   # threshold value that scores 100
    worst: float | None = None  # threshold value that scores 0
    higher_is_better: bool = False  # almost all are lower-is-better; transfer speed isn't
    marks_latest: bool = False      # presence flags a run as scored under the latest rubric


# NOTE: thresholds/weights below reproduce the previously-hardcoded calibration.
# SOPS `best` is anchored to near-physical-floor conditions (so 100 is reachable
# but genuinely hard); `worst` ≈ Web Vitals "poor" / Nielsen-RAIL "slow".
METRICS: list[MetricDef] = [
    # ── SOPS: the human-feel score (paint timing + the most perceptual completion) ──
    MetricDef(
        "fcp", "browser", "fcp_ms", "First Contentful Paint", unit="ms",
        axis=SOPS, weight=20, best=150.0, worst=4000.0, marks_latest=True,
        description=(
            "When the first real content (text/image) paints — the 'it's responding' "
            "moment. Perceptual, not completion: how soon you see *something*. Lower is better."
        ),
    ),
    MetricDef(
        "lcp", "browser", "lcp_ms", "Largest Contentful Paint", unit="ms",
        axis=SOPS, weight=25, best=250.0, worst=6000.0, marks_latest=True,
        description=(
            "When the main content becomes visible. Google's core 'is it usefully loaded' "
            "signal. Lower is better."
        ),
    ),
    MetricDef(
        "inp", "browser", "inp_ms", "Interaction to Next Paint", unit="ms",
        axis=SOPS, weight=15, best=40.0, worst=500.0,
        description=(
            "How quickly the page paints a response to input — responsiveness to taps/keys "
            "(good ≤200ms). Best-effort here (synthetic interaction); may be blank. Lower is better."
        ),
    ),
    MetricDef(
        "ttfb", "http", "ttfb_ms", "Time to First Byte", unit="ms",
        axis=SOPS, weight=15, best=30.0, worst=1000.0,
        description=(
            "From sending the request to the first byte of the response arriving — how long "
            "until a page begins to appear. Lower is better."
        ),
    ),
    MetricDef(
        "render", "browser", "total_render_ms", "Total render", unit="ms",
        axis=SOPS, weight=25, best=500.0, worst=6000.0,
        description=(
            "Wall-clock time for a real headless browser to fetch, parse and fully render the "
            "page. The closest measure to how slow a site actually feels. Lower is better."
        ),
    ),
    # ── Completion: pure-infrastructure timing (diagnostic, never folded into SOPS) ──
    MetricDef(
        "dns", "dns", "lookup_ms", "DNS lookup", unit="ms",
        axis=COMPLETION, weight=10, best=10.0, worst=150.0,
        description=(
            "Time to translate a hostname into an IP address. Happens before a page can even "
            "start loading. Lower is better."
        ),
    ),
    MetricDef(
        "tcp", "tcp", "connect_ms", "TCP connect", unit="ms",
        axis=COMPLETION, weight=15, best=10.0, worst=250.0,
        description="Time to open a TCP connection (the handshake) to a server. Lower is better.",
    ),
    MetricDef(
        "tls", "tls", "handshake_ms", "TLS handshake", unit="ms",
        axis=COMPLETION, weight=20, best=30.0, worst=500.0,
        description=(
            "Time to negotiate the encrypted (HTTPS) session after connecting. Lower is better."
        ),
    ),
    MetricDef(
        "jitter", "icmp", "jitter_ms", "Jitter", unit="ms",
        axis=COMPLETION, weight=5, best=1.0, worst=30.0,
        description=(
            "How much latency varies between pings. High jitter makes calls and games feel "
            "choppy even when average ping is fine. Lower is better."
        ),
    ),
    MetricDef(
        "packet_loss", "icmp", "packet_loss_pct", "Packet loss", unit="%",
        axis=COMPLETION, weight=5, best=0.0, worst=2.5,
        description=(
            "Percentage of ping packets that never came back. Anything above ~1% causes "
            "stutters and retransmits. Lower is better."
        ),
    ),
    # ── Display-only: shown on a run but not part of any score ──
    MetricDef(
        "latency", "icmp", "latency_ms", "Latency (ping)", unit="ms",
        description=(
            "Round-trip ping time to your targets — the base delay behind everything online. "
            "Lower is better."
        ),
    ),
    MetricDef(
        "download", "http", "download_ms", "Download time", unit="ms",
        description="Time spent downloading the response body after the first byte. Lower is better.",
    ),
    MetricDef(
        "transfer", "http", "transfer_mbps", "Transfer speed", unit="Mbps", higher_is_better=True,
        description=(
            "Throughput while downloading, in megabits per second. The one measure where "
            "HIGHER is better."
        ),
    ),
    MetricDef(
        "dom_content_loaded", "browser", "dom_content_loaded_ms", "DOM content loaded", unit="ms",
        description=(
            "Time until the page's HTML is parsed and the DOM is ready (DOMContentLoaded). "
            "Lower is better."
        ),
    ),
    MetricDef(
        "load_event", "browser", "load_event_ms", "Page load", unit="ms",
        description=(
            "Time until the browser's load event fires — all initial resources fetched. "
            "Lower is better."
        ),
    ),
]


def _by_axis(axis: str) -> list[MetricDef]:
    return [m for m in METRICS if m.axis == axis]


def metric_sources(axis: str) -> dict[str, tuple[str, str]]:
    """Map ``{logical_key: (plugin, source_key)}`` for one axis (for the scorer)."""
    return {m.key: (m.plugin, m.source_key) for m in _by_axis(axis)}


def default_weights(axis: str) -> dict[str, float]:
    return {m.key: m.weight for m in _by_axis(axis)}


def default_thresholds(axis: str) -> dict[str, dict[str, float]]:
    return {m.key: {"best": m.best, "worst": m.worst} for m in _by_axis(axis)}


def latest_metric_keys() -> tuple[str, ...]:
    """Logical keys whose presence marks a run as scored under the latest rubric."""
    return tuple(m.key for m in METRICS if m.marks_latest)


def catalog() -> list[dict]:
    """Serializable metric definitions for the API / UI (the single metadata source)."""
    return [
        {
            "key": m.key,
            "source_key": m.source_key,
            "plugin": m.plugin,
            "label": m.label,
            "description": m.description,
            "unit": m.unit,
            "axis": m.axis,
            "weight": m.weight,
            "best": m.best,
            "worst": m.worst,
            "higher_is_better": m.higher_is_better,
        }
        for m in METRICS
    ]


# Derived, ready-to-use constants (kept here so importers don't recompute).
SOPS_METRIC_SOURCES = metric_sources(SOPS)
COMPLETION_METRIC_SOURCES = metric_sources(COMPLETION)
