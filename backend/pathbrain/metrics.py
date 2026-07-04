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
        "total_stall", "browser", "total_stall_ms", "Total stall (relative)", unit="ms",
        description=(
            "Cumulative dead-air *relative to each run's own median pace* — the excess of each "
            "completion gap over the run's median gap. A relative shape statistic (steady "
            "delivery scores ~0 regardless of speed). Superseded in the v8 crown by the absolute "
            "'Stall time'; kept as a display-only diagnostic. Lower is better."
        ),
    ),
    MetricDef(
        "stall_time", "browser", "stall_time_ms", "Stall time", unit="ms",
        description=(
            "Total dead-air as an *actual measurement*: the summed duration of every completion "
            "gap longer than a fixed perceptible-stall threshold (200ms) — 'how many ms the load "
            "spent frozen'. Unlike total_stall (relative to each run's own median pace), it uses "
            "one fixed yardstick for every run, so profiles compare on measured values not "
            "averages-of-averages. An *absolute* measure — it drifts with server pacing, so the "
            "rank-eligible sibling is the ratio 'Jank fraction'. Kept for display. Lower is better."
        ),
    ),
    MetricDef(
        "jank_fraction", "browser", "jank_fraction", "Jank fraction",
        description=(
            "The *ratio* form of stall time: the fraction of the delivery window "
            "(responseStart → main content / LCP) spent in perceptible ≥200ms stalls — 'how much "
            "of the wait for main content was jank' (0–1). Normalizing by the window cancels the "
            "load's absolute pace, so — unlike the absolute stall_time — it stands largely "
            "weather-independent, making it the rank-eligible stall measure. Lower is smoother."
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
    # ── Navigation waterfall (display-only; additive, independent load phases) ──
    # The load split into non-overlapping phase durations that tile navigationStart →
    # load, from the raw W3C Navigation Timing marks + FCP/LCP (interpret/waterfall.py).
    # These telescope: stall+dns+tcp+tls+request == nav_ttfb_cumulative (responseStart),
    # the network-confounded prefix baked into every paint milestone. Not scored — they
    # exist to make the DNS/TLS/TTFB-vs-render breakdown visible (the waterfall view).
    MetricDef(
        "nav_stall", "browser", "nav_stall_ms", "Pre-connect stall", unit="ms",
        description=(
            "Time before DNS even starts — redirects, request queueing and connection "
            "blocking (navigationStart → domainLookupStart). Lower is better."
        ),
    ),
    MetricDef(
        "nav_dns", "browser", "nav_dns_ms", "DNS (page nav)", unit="ms",
        description=(
            "DNS resolution phase of the real page navigation (domainLookupStart → "
            "domainLookupEnd) — distinct from the standalone DNS probe. Lower is better."
        ),
    ),
    MetricDef(
        "nav_tcp", "browser", "nav_tcp_ms", "TCP connect (page nav)", unit="ms",
        description=(
            "TCP handshake phase of the page navigation, excluding TLS (connectStart → "
            "secureConnectionStart). An independent phase — no longer overlapping TLS. Lower is better."
        ),
    ),
    MetricDef(
        "nav_tls", "browser", "nav_tls_ms", "TLS (page nav)", unit="ms",
        description=(
            "TLS handshake phase of the page navigation (secureConnectionStart → "
            "connectEnd); 0 on a non-HTTPS connection. Lower is better."
        ),
    ),
    MetricDef(
        "nav_request", "browser", "nav_request_ms", "Request / TTFB wait", unit="ms",
        description=(
            "Request send + server processing until the first response byte (connectEnd → "
            "responseStart) — the 'wait' phase, once the connection is open. Lower is better."
        ),
    ),
    MetricDef(
        "nav_response", "browser", "nav_response_ms", "Body delivery", unit="ms",
        description=(
            "Body delivery: first response byte to last (responseStart → responseEnd). These "
            "are packet arrivals through your queue — ACK-clocked and spacing-sensitive, the "
            "single most SQM-facing phase in the whole load (where target/quantum live under "
            "load) and the crown-eligible network measure. Lower is better."
        ),
    ),
    MetricDef(
        "nav_render", "browser", "nav_render_ms", "Client render (→FCP)", unit="ms",
        description=(
            "The client residual: document download done → First Contentful Paint (responseEnd "
            "→ FCP) — pure client CPU (parse/style/layout/paint), which network shaping cannot "
            "move. Tracked as an instrument health-check: it should be near-constant across "
            "profiles; if it varies by profile, the measurement is broken, not the shaper. Lower is better."
        ),
    ),
    MetricDef(
        "nav_fcp_lcp", "browser", "nav_fcp_lcp_ms", "First → largest paint", unit="ms",
        description=(
            "Time between the first content painting and the largest content painting "
            "(FCP → LCP). Lower is better."
        ),
    ),
    MetricDef(
        "nav_lcp_load", "browser", "nav_lcp_load_ms", "Largest paint → load", unit="ms",
        description=(
            "From the largest contentful paint to the page-load event (LCP → loadEventEnd). "
            "Lower is better."
        ),
    ),
    MetricDef(
        "nav_ttfb_cumulative", "browser", "nav_ttfb_cumulative_ms", "TTFB (cumulative)", unit="ms",
        description=(
            "The whole network prefix up to the first response byte (== stall+DNS+TCP+TLS+"
            "request). This is the network-'weather'-confounded time that is baked into every "
            "paint milestone — the reason a profile can post a better LCP purely because DNS/"
            "TLS were fast at that moment. Lower is better."
        ),
    ),
    MetricDef(
        "nav_fcp_after_ttfb", "browser", "nav_fcp_after_ttfb_ms", "FCP after first byte", unit="ms",
        description=(
            "FCP − responseStart. Strips the DNS/TCP/TLS *setup* confound — but is NOT "
            "network-independent: it is body delivery (SQM-facing) + client render combined. "
            "Split it into 'Body delivery' and 'Client render' to separate the phase you can "
            "shape from the client CPU you can't. Kept as context, not a ranking metric. Lower is better."
        ),
    ),
    MetricDef(
        "nav_lcp_after_ttfb", "browser", "nav_lcp_after_ttfb_ms", "LCP after first byte", unit="ms",
        description=(
            "LCP − responseStart. Removes the connection-setup confound, but still mixes "
            "resource delivery (shapeable) with render delay (client). Context only — the "
            "rankable network signal is Body delivery, the health-check is Client render. Lower is better."
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


# ── Ledger buckets: each measurable's provenance / controllability role ───────
# This is a *governance* classification, orthogonal to the temporal axes: axes group
# by phase-of-the-load (Responsiveness/Smoothness/Speed), buckets group by where a
# measurement comes from and whether shaping can move it. The axes ignored this
# boundary — which is how a weather instrument (probe TTFB) ended up scored inside the
# Responsiveness headline. Buckets sit between silver and gold: a tag on each derived
# measurable that governs whether it may enter the headline/crown ranking at all.
#
#   W  Weather instrument   independent probe socket; settings-independent by
#                           construction. Feeds the ±2h rolling baseline; never ranked.
#   N  Network phase        a navigation-timing phase. Body delivery (responseStart→
#                           responseEnd) is the crown-eligible one; the setup phases
#                           (dns/tcp/tls/request) + prefix roll-up are weather-dominated.
#   C  Client               client CPU (render/paint/interaction/layout). Shaping-immune;
#                           an instrument health-check (should be flat across profiles).
#   S  Shape statistic      byte-arrival shape. Ratio members (gini/cadence) rank raw;
#                           absolute-gap members (stall_time/longest_stall) still drift
#                           with server pacing and need the weather lens even post-anchor.
#   O  Opaque milestone     a sum that spans buckets (FCP/LCP/load/render/…). What a human
#                           means by "fast", but unattributable — display only, never ranked.
ROLE_WEATHER = "W"
ROLE_NETWORK = "N"
ROLE_CLIENT = "C"
ROLE_SHAPE = "S"
ROLE_COMPOSITE = "O"
VALID_ROLES = {ROLE_WEATHER, ROLE_NETWORK, ROLE_CLIENT, ROLE_SHAPE, ROLE_COMPOSITE}

# Roles that may enter headline / crown ranking. W (instrument) and C (client health-
# check) are excluded on principle; O (opaque sums) are unattributable so display-only.
# Note this is the *coarse* gate that keeps the clearly-ineligible out — the finer
# positive selection (delivery within N; ratio-vs-absolute within S) is the crown's job.
RANKABLE_ROLES = {ROLE_NETWORK, ROLE_SHAPE}

# The ledger. One entry per metric (completeness asserted below, shaper_fields-style:
# adding a metric forces a bucket choice). The single readable view of the partition.
METRIC_ROLES: dict[str, str] = {
    # W — weather instruments: independent probe sockets, their own DNS/connect/request,
    # so they share only the slow "weather" mode with the browser and belong in a baseline.
    "ttfb": ROLE_WEATHER,   # the HTTP-probe socket TTFB — NOT the nav TTFB phase (that's nav_request)
    "dns": ROLE_WEATHER, "tcp": ROLE_WEATHER, "tls": ROLE_WEATHER,
    "latency": ROLE_WEATHER, "jitter": ROLE_WEATHER, "packet_loss": ROLE_WEATHER,
    "download": ROLE_WEATHER, "transfer": ROLE_WEATHER,
    # N — navigation network phases (the additive waterfall).
    "nav_stall": ROLE_NETWORK, "nav_dns": ROLE_NETWORK, "nav_tcp": ROLE_NETWORK,
    "nav_tls": ROLE_NETWORK, "nav_request": ROLE_NETWORK, "nav_ttfb_cumulative": ROLE_NETWORK,
    "nav_response": ROLE_NETWORK,  # body delivery — the SQM-facing, crown-eligible phase
    # C — client CPU (shaping-immune; instrument health-checks).
    "nav_render": ROLE_CLIENT, "inp": ROLE_CLIENT, "cls": ROLE_CLIENT,
    # S — byte-arrival shape statistics.
    "byte_earliness": ROLE_SHAPE, "cadence_cov": ROLE_SHAPE, "delivery_gini": ROLE_SHAPE,
    "perceived_time": ROLE_SHAPE, "longest_stall": ROLE_SHAPE, "stall_time": ROLE_SHAPE,
    "jank_fraction": ROLE_SHAPE, "total_stall": ROLE_SHAPE, "network_stall": ROLE_SHAPE,
    "render_stall": ROLE_SHAPE, "unknown_stall": ROLE_SHAPE,
    # O — opaque milestone sums (span buckets → display only, never ranked).
    "fcp": ROLE_COMPOSITE, "lcp": ROLE_COMPOSITE, "render": ROLE_COMPOSITE,
    "dom_content_loaded": ROLE_COMPOSITE, "load_event": ROLE_COMPOSITE,
    "speed_index": ROLE_COMPOSITE, "paint_cadence": ROLE_COMPOSITE,
    "nav_fcp_lcp": ROLE_COMPOSITE, "nav_lcp_load": ROLE_COMPOSITE,
    "nav_fcp_after_ttfb": ROLE_COMPOSITE, "nav_lcp_after_ttfb": ROLE_COMPOSITE,
}


def role_of(key: str) -> str | None:
    """The ledger bucket (``W``/``N``/``C``/``S``/``O``) for a metric key, or None."""
    return METRIC_ROLES.get(key)


def rank_eligible(key: str) -> bool:
    """True if a metric may enter headline/crown ranking (its role is N or S)."""
    return METRIC_ROLES.get(key) in RANKABLE_ROLES


def ineligible_scored(keys) -> dict[str, str]:
    """``{key: role}`` for any of ``keys`` that must NOT be ranked (a weather instrument,
    client health-check, or opaque milestone sum). Empty == the set is crown-clean. This
    is the executable guard: run it over a methodology's headline/crown metrics and a
    non-empty result names exactly what's leaking network weather (or client noise) into
    the ranking — e.g. scored ``ttfb`` (a probe) in the Responsiveness axis."""
    return {k: METRIC_ROLES[k] for k in keys if METRIC_ROLES.get(k) not in RANKABLE_ROLES and k in METRIC_ROLES}


# Executable invariants (shaper_fields-style: at import *and* in test_metrics).
assert set(METRIC_ROLES.values()) <= VALID_ROLES, (
    f"Unknown ledger role(s): {set(METRIC_ROLES.values()) - VALID_ROLES}"
)
_ROLE_KEYS = set(METRIC_ROLES)
_METRIC_KEYS = {m.key for m in METRICS}
assert _ROLE_KEYS == _METRIC_KEYS, (
    "Every metric needs exactly one ledger bucket. "
    f"Missing a bucket: {sorted(_METRIC_KEYS - _ROLE_KEYS)}; "
    f"bucketed but not a metric: {sorted(_ROLE_KEYS - _METRIC_KEYS)}"
)


# The order metrics happen / make sense to read in: connection setup → response →
# paint → load → interaction → network quality. Drives the UI ordering (score
# breakdown + plugin results) so the sequence reads chronologically, not by weight.
DISPLAY_ORDER = [
    "dns", "tcp", "tls",                                  # connection setup
    "ttfb", "download", "transfer",                       # response
    # navigation waterfall — the load's independent phases, in wall-clock order
    "nav_stall", "nav_dns", "nav_tcp", "nav_tls", "nav_request", "nav_response",
    "nav_render", "nav_fcp_lcp", "nav_lcp_load",
    "nav_ttfb_cumulative", "nav_fcp_after_ttfb", "nav_lcp_after_ttfb",
    "fcp", "speed_index", "dom_content_loaded", "lcp",    # paint trajectory
    "load_event", "render", "paint_cadence", "cls",       # completion + smoothness
    "inp",                                                # interaction (after load)
    # perceived load smoothness (delivery-curve shape, byte-arrival)
    "perceived_time", "longest_stall", "total_stall", "stall_time", "jank_fraction", "cadence_cov",
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
            "role": METRIC_ROLES.get(m.key),
        }
        for m in METRICS
    ]


# Derived, ready-to-use constants (kept here so importers don't recompute).
SOPS_METRIC_SOURCES = metric_sources(SOPS)
COMPLETION_METRIC_SOURCES = metric_sources(COMPLETION)
