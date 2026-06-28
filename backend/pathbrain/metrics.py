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


# Threshold anchoring (rubric perceptual-v5): `best`/`worst` are anchored to
# established perception thresholds so a value "comfortably inside good" reads green.
# For the Core Web Vitals metrics `best` = the CWV "good" boundary and `worst` = the
# "poor" boundary (FCP 1.8s/3s, LCP 2.5s/4s, INP 200/500ms, CLS 0.1/0.25); TTFB uses
# Google's 800ms/1800ms; render uses Nielsen's ~1s "flow" up to a long tail. The
# byte-arrival smoothness metrics (byte_earliness/longest_stall) keep their tuned
# bounds — they're the signal this tool exists to surface, not a CWV restatement.
METRICS: list[MetricDef] = [
    # ── SOPS: the human-feel score (paint timing + the most perceptual completion) ──
    MetricDef(
        "byte_earliness", "browser", "byte_earliness_ms", "Byte earliness", unit="ms",
        axis=SOPS, weight=25, best=300.0, worst=5000.0,
        description=(
            "Area above the cumulative-bytes-delivered curve — a Speed-Index analog in bytes, "
            "not pixels. Rewards delivering bytes *early and progressively* rather than in a "
            "late burst, isolating the byte-arrival layer that network shaping actually moves. "
            "The core seat-of-the-pants delivery metric. Lower is better."
        ),
    ),
    MetricDef(
        "fcp", "browser", "fcp_ms", "First Contentful Paint", unit="ms",
        axis=SOPS, weight=20, best=1800.0, worst=3000.0,
        description=(
            "When the first real content (text/image) paints — the 'it's responding' "
            "moment. Perceptual, not completion: how soon you see *something*. Lower is better."
        ),
    ),
    MetricDef(
        "longest_stall", "browser", "longest_stall_ms", "Longest stall", unit="ms",
        axis=SOPS, weight=10, best=50.0, worst=2000.0, marks_latest=True,
        description=(
            "The longest stretch where nothing finished loading — the field-measurable "
            "'standing at the carousel seeing nothing'. The single signal most tied to a load "
            "feeling chunky, computed from Resource Timing (no pixels). Lower is smoother."
        ),
    ),
    MetricDef(
        "perceived_time", "browser", "perceived_time_ms", "Perceived time", unit="ms",
        axis=SOPS, weight=5, best=500.0, worst=8000.0,
        description=(
            "Weighted load time where stalls (unoccupied waiting) cost more than time with "
            "visible progress (occupied) — so a smoother-but-slightly-slower load can score "
            "better. Weights are calibratable to subjective ratings. Lower is better perceived."
        ),
    ),
    MetricDef(
        "cls", "browser", "cls", "Layout stability",
        axis=SOPS, weight=5, best=0.1, worst=0.25,
        description=(
            "Cumulative Layout Shift — how much visible content jumps around as the page "
            "loads. Janky reflow feels worse even when timings are identical. Lower is better."
        ),
    ),
    MetricDef(
        "lcp", "browser", "lcp_ms", "Largest Contentful Paint", unit="ms",
        axis=SOPS, weight=10, best=2500.0, worst=4000.0,
        description=(
            "When the main content becomes visible. Google's core 'is it usefully loaded' "
            "signal. A completion milestone — weighted below the trajectory metrics. Lower is better."
        ),
    ),
    MetricDef(
        "inp", "browser", "inp_ms", "Interaction to Next Paint", unit="ms",
        axis=SOPS, weight=10, best=200.0, worst=500.0,
        description=(
            "How quickly the page paints a response to input — responsiveness to taps/keys "
            "(good ≤200ms). Best-effort here (synthetic interaction); may be blank. Lower is better."
        ),
    ),
    MetricDef(
        "ttfb", "http", "ttfb_ms", "Time to First Byte", unit="ms",
        axis=SOPS, weight=10, best=800.0, worst=1800.0,
        description=(
            "From sending the request to the first byte of the response arriving — how long "
            "until a page begins to appear. Lower is better."
        ),
    ),
    MetricDef(
        "render", "browser", "total_render_ms", "Total render", unit="ms",
        axis=SOPS, weight=5, best=1000.0, worst=8000.0,
        description=(
            "Wall-clock time to fully render to network-idle — a pure *completion* time. "
            "Deliberately low-weighted: it rewards finishing fast on an empty pipe, which is "
            "not what smoothness feels like. Lower is better."
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
    # ── Perceived load smoothness (display-only diagnostics) ──
    # The scored smoothness metrics (byte_earliness, longest_stall, perceived_time)
    # live in the SOPS block above; these are the supporting shape statistics.
    MetricDef(
        "cadence_cov", "browser", "cadence_cov", "Delivery cadence",
        description=(
            "Regularity of the gaps between resource completions (coefficient of variation). "
            "Low = metronomic, steady delivery; high = clumps and stalls. Lower is smoother."
        ),
    ),
    MetricDef(
        "delivery_gini", "browser", "delivery_gini", "Delivery evenness",
        description=(
            "Gini coefficient of bytes delivered across the load window (0–1). High = bytes "
            "arrived very unevenly (chunky); low = evenly (smooth). Lower is smoother."
        ),
    ),
    MetricDef(
        "network_stall", "browser", "network_stall_ms", "Network stall time", unit="ms",
        description=(
            "Total stall time attributed to the network — no resource arriving and no long "
            "main-thread task. This is the tunable share that network shaping can move. Lower is better."
        ),
    ),
    MetricDef(
        "total_stall", "browser", "total_stall_ms", "Total stall time", unit="ms",
        description=(
            "Cumulative dead-air — how much time delivery ran behind its own typical pace "
            "(the excess of each completion gap over the median gap). The cumulative "
            "counterpart to the longest single stall, and the crown's standalone stall "
            "dimension. Steady delivery scores ~0. Lower is better."
        ),
    ),
    MetricDef(
        "render_stall", "browser", "render_stall_ms", "Render stall time", unit="ms",
        description=(
            "Total stall time overlapped by a long main-thread task — render-bound, so network "
            "config changes should not be expected to move it. Lower is better."
        ),
    ),
    MetricDef(
        "unknown_stall", "browser", "unknown_stall_ms", "Unattributed stall time", unit="ms",
        description=(
            "Stall time that couldn't be attributed to network or render because the browser "
            "exposed no Long Animation Frame / long-task data. Lower is better."
        ),
    ),
    # ── Pixel-based visual metrics (display-only; require the opt-in filmstrip) ──
    # Demoted from SOPS in favor of the byte-arrival smoothness metrics, which
    # isolate the network layer without the CPU cost of per-frame screencast.
    # Still derived as diagnostics when `browser.filmstrip` is enabled.
    MetricDef(
        "speed_index", "browser", "speed_index_ms", "Speed Index", unit="ms",
        description=(
            "The average time at which visible content is displayed — area over the visual-"
            "completeness curve, from the screencast filmstrip. A pixel-based diagnostic; the "
            "scored delivery signal is now byte_earliness. Lower is better."
        ),
    ),
    MetricDef(
        "paint_cadence", "browser", "paint_cadence", "Paint smoothness",
        description=(
            "The largest single jump in visual completeness between filmstrip frames (0–1). "
            "Low = filled in steadily; high = stalled then painted at once. Pixel-based "
            "diagnostic (filmstrip only); the scored stall signal is now longest_stall. Lower is better."
        ),
    ),
]


# The order metrics happen / make sense to read in: connection setup → response →
# paint → load → interaction → network quality. Drives the UI ordering (score
# breakdown + plugin results) so the sequence reads chronologically, not by weight.
DISPLAY_ORDER = [
    "dns", "tcp", "tls",                                  # connection setup
    "ttfb", "download", "transfer",                       # response
    "fcp", "speed_index", "dom_content_loaded", "lcp",    # paint trajectory
    "load_event", "render", "paint_cadence", "cls",       # completion + smoothness
    "inp",                                                # interaction (after load)
    # perceived load smoothness (delivery-curve shape, byte-arrival)
    "perceived_time", "longest_stall", "cadence_cov",
    "byte_earliness", "delivery_gini", "network_stall", "render_stall", "unknown_stall",
    "latency", "jitter", "packet_loss",                   # network quality (continuous)
]


def _order(key: str) -> int:
    return DISPLAY_ORDER.index(key) if key in DISPLAY_ORDER else len(DISPLAY_ORDER)


def _by_axis(axis: str) -> list[MetricDef]:
    return [m for m in METRICS if m.axis == axis]


def metric_sources(axis: str) -> dict[str, tuple[str, str]]:
    """Map ``{logical_key: (plugin, source_key)}`` for one axis (for the scorer)."""
    return {m.key: (m.plugin, m.source_key) for m in _by_axis(axis)}


def all_metric_sources() -> dict[str, tuple[str, str]]:
    """``{logical_key: (plugin, source_key)}`` for **every** metric — scored and
    display-only. Lets callers pull every numeric value a run captured (e.g. the
    Settings-Impact per-profile aggregates) back out of the plugins' metric caches."""
    return {m.key: (m.plugin, m.source_key) for m in METRICS}


def default_weights(axis: str) -> dict[str, float]:
    return {m.key: m.weight for m in _by_axis(axis)}


def default_thresholds(axis: str) -> dict[str, dict[str, float]]:
    return {m.key: {"best": m.best, "worst": m.worst} for m in _by_axis(axis)}


def latest_metric_keys() -> tuple[str, ...]:
    """Logical keys whose presence marks a run as scored under the latest rubric."""
    return tuple(m.key for m in METRICS if m.marks_latest)


def has_latest_metrics(metric_values: dict | None) -> bool:
    """True if a score's metric values include the current-rubric markers.

    Runs missing them predate the latest scoring (e.g. before paint capture), so
    their SOPS isn't comparable — callers quarantine these as "legacy".
    """
    mv = metric_values or {}
    return all(mv.get(k) is not None for k in latest_metric_keys())


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
            "order": _order(m.key),
        }
        for m in METRICS
    ]


# Derived, ready-to-use constants (kept here so importers don't recompute).
SOPS_METRIC_SOURCES = metric_sources(SOPS)
COMPLETION_METRIC_SOURCES = metric_sources(COMPLETION)
