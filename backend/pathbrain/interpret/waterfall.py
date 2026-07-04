"""Navigation-timing waterfall: one page load decomposed into independent phases.

**Silver layer.** *Bronze* is the raw W3C ``PerformanceNavigationTiming`` marks the
browser plugin captures verbatim (``performance.getEntriesByType('navigation')[0]
.toJSON()``) plus FCP/LCP from the paint observer — no abstractions. Here we turn
those absolute marks (all in ms since ``timeOrigin``, so directly comparable) into an
**additive, non-overlapping** sequence of phase *durations* that tile the whole load
left-to-right — the thing a waterfall renders, and the set of genuinely independent
measurables underneath the milestone timings.

Why this exists: FCP/LCP are measured *from navigation start*, so they silently
**include** DNS + TCP + TLS + request/TTFB. A profile can look faster on LCP purely
because the network setup happened to be quick at that moment (network "weather"),
not because shaping helped. The phases isolate three regimes that get lumped together
in a paint milestone — and the boundary that matters most is **responseEnd**:

* the **setup prefix** — everything up to the first response byte (``responseStart``),
  DNS/TCP/TLS/request, which is weather-dominated and baked into every milestone;
* **body delivery** — ``responseStart → responseEnd``: packet arrivals through the
  queue, ACK-clocked and spacing-sensitive. The single most SQM-facing phase in the
  load, and the network measure worth ranking on; and
* the **client residual** — ``responseEnd → FCP`` (and the LCP render delay): pure
  client CPU (parse/style/layout/paint), which firewall shaping cannot move. A
  near-constant health check, not a ranking metric.

``FCP − responseStart`` is therefore **not** "the part shaping can't move" — it is
delivery + client combined; only ``responseEnd → FCP`` is the shaping-immune piece.
The phases telescope exactly (``stall + dns + tcp + tls + request == responseStart``,
the cumulative TTFB) and the full chain sums to the load endpoint. Every duration is
clamped ``>= 0`` and derived purely from the stored raw, so the whole waterfall
re-derives over history with no re-collection. Gold-layer scoring is unaffected —
these are display-only measurables.
"""
from __future__ import annotations

import math

# The additive phase sequence, in wall-clock order. Each entry's endpoint is a
# navigation-timing mark; a phase spans from the previous endpoint to its own, so
# the phases tile [navigationStart, endpoint] with no gaps. ``nav_tcp_ms`` ends at
# the TLS start (so TCP excludes the handshake — the two used to overlap), and
# ``nav_render_ms`` is the render residual (responseEnd → first paint).
SEGMENT_KEYS = (
    "nav_stall_ms",       # navigationStart → domainLookupStart (redirect/blocked/queue)
    "nav_dns_ms",         # domainLookupStart → domainLookupEnd
    "nav_tcp_ms",         # → secureConnectionStart (TCP only, excludes TLS)
    "nav_tls_ms",         # secureConnectionStart → connectEnd (0 when no TLS)
    "nav_request_ms",     # connectEnd → responseStart (request send + server think = TTFB wait)
    "nav_response_ms",    # responseStart → responseEnd (document download)
    "nav_render_ms",      # responseEnd → FCP (parse → first paint; the render residual)
    "nav_fcp_lcp_ms",     # FCP → LCP (first → largest contentful paint)
    "nav_lcp_load_ms",    # LCP → loadEventEnd (largest paint → page-load event)
)


def _f(v) -> float | None:
    """Coerce to a finite float, else None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def navigation_phases(nav: dict | None, paint: dict | None) -> dict[str, float]:
    """Additive per-phase durations for one load, from raw nav marks + FCP/LCP.

    Returns a flat ``{source_key: ms}`` dict (the same shape the derive layer merges
    into a plugin's metric cache). Emits a phase only when its endpoint mark actually
    exists, so a partial capture yields fewer phases rather than fabricated zeros.
    Also emits three roll-ups: ``nav_ttfb_cumulative_ms`` (the setup prefix =
    ``responseStart``) and ``nav_fcp_after_ttfb_ms`` / ``nav_lcp_after_ttfb_ms`` (paint
    milestones minus that prefix). The after-TTFB figures are context, not clean
    residuals — they still contain body delivery (SQM-facing); the shaping-immune part
    is only ``nav_render_ms`` (responseEnd → FCP).
    """
    nav = nav or {}
    paint = paint or {}

    def g(key: str) -> float | None:
        return _f(nav.get(key))

    dls, dle = g("domainLookupStart"), g("domainLookupEnd")
    cs, sec, ce = g("connectStart"), g("secureConnectionStart"), g("connectEnd")
    rs, re_ = g("responseStart"), g("responseEnd")
    le = g("loadEventEnd")
    fcp, lcp = _f(paint.get("fcp")), _f(paint.get("lcp"))

    # No document-response timing at all → nothing meaningful to decompose (e.g. a
    # legacy run that captured an empty nav). Leave it to the smoothness/paint metrics.
    if rs is None and re_ is None:
        return {}

    # secureConnectionStart is 0 when the connection carried no TLS; fold the "TLS"
    # checkpoint onto connectEnd so TCP spans the whole connect and TLS reads 0 —
    # instead of misattributing the whole handshake window to TLS.
    secure_boundary = sec if (sec is not None and sec > 0) else ce

    # (phase source_key, its endpoint mark). The phase's start is the previous
    # endpoint, so consecutive phases abut with no gaps (any tiny inter-mark gap is
    # absorbed into the following phase). Monotone-clamped below.
    steps = [
        ("nav_stall_ms", dls),
        ("nav_dns_ms", dle),
        ("nav_tcp_ms", secure_boundary),
        ("nav_tls_ms", ce),
        ("nav_request_ms", rs),
        ("nav_response_ms", re_),
        ("nav_render_ms", fcp),
        ("nav_fcp_lcp_ms", lcp),
        ("nav_lcp_load_ms", le),
    ]

    out: dict[str, float] = {}
    prev = 0.0
    for key, raw in steps:
        # Carry the previous checkpoint forward when a mark is missing or (defensively)
        # out of order, so every duration is >= 0 and the phases stay monotone.
        cp = prev if (raw is None or raw < prev) else raw
        if raw is not None:
            out[key] = round(cp - prev, 3)
        prev = cp

    # Roll-ups: the crux of "is this profile's paint edge real or network luck?".
    if rs is not None:
        # Everything up to the first response byte — the cumulative TTFB that is baked
        # into (and confounds) FCP/LCP. Telescopes: == stall+dns+tcp+tls+request.
        out["nav_ttfb_cumulative_ms"] = round(rs, 3)
        if fcp is not None:
            out["nav_fcp_after_ttfb_ms"] = round(max(0.0, fcp - rs), 3)
        if lcp is not None:
            out["nav_lcp_after_ttfb_ms"] = round(max(0.0, lcp - rs), 3)
    return out
