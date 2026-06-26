"""Perceived load-smoothness metrics, computed purely from browser timing APIs.

Smoothness is the *shape of the delivery curve over time*, not a finish timestamp.
Two loads can hit 100% at the same instant yet trace very different curves — one a
steady ramp, one a flat-then-jump. These pure functions measure the ramp from
Navigation Timing, Resource Timing and Paint Timing (the byte-arrival layer —
exactly the variable network shaping touches), with no pixel/video/Speed-Index
visual capture (that blends byte arrival with paint/layout/decode we don't change).

Everything here is a pure function over a normalized event series so it can be
unit-tested with synthetic data and re-run over stored ``raw`` observations.
Direction notes per metric: lower = smoother unless stated. All times in ms; all
events share ``performance.timeOrigin`` so they're directly comparable.
"""
from __future__ import annotations

import math
from statistics import mean, pstdev

# Default perceived-time weights. The balance knob is ``w_unoccupied / w_occupied``
# (occupied time feels shorter than unoccupied time — Maister's baggage-claim).
# Real values come from the calibration harness fit to Josh's own ratings; these
# are reasonable starting defaults and are stored alongside each record.
PERCEIVED_DEFAULTS = {
    "slice_ms": 100.0,
    "w_occupied": 1.0,
    # Stalls (unoccupied time) cost 4× time with visible progress. Raised from 3.0
    # (reasoned recalibration: a mostly-stall load was scoring green); the exact
    # ratio is what the calibration harness fits to subjective ratings.
    "w_unoccupied": 4.0,
}

# A gap below this isn't a "stall" — normal back-to-back completions. Used by the
# attribution sums so micro-gaps don't get charged to network or render.
MIN_STALL_MS = 50.0


def _f(v) -> float | None:
    """Coerce to float, or None if not a finite number."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _live_resources(resources: list | None) -> list[dict]:
    """Resource entries with a real ``responseEnd`` (drops blocked/cached-zero)."""
    out: list[dict] = []
    for r in resources or []:
        if not isinstance(r, dict):
            continue
        end = _f(r.get("responseEnd"))
        if end is not None and end > 0:
            out.append(r)
    return out


# ── R2: the completion-event series (input to R3–R6, R8) ─────────────────────


def completion_series(
    resources: list | None,
    *,
    fcp: float | None = None,
    doc_response_end: float | None = None,
    load_event_end: float | None = None,
) -> list[float]:
    """Sorted ascending ``responseEnd`` timestamps, with optional boundary events.

    Filtered to ``responseEnd > 0``. FCP, the document's ``responseEnd`` and
    ``loadEventEnd`` are injected as boundaries when provided, so the series spans
    the whole visible-progress window even on pages with few subresources.
    """
    times = [float(_f(r.get("responseEnd"))) for r in _live_resources(resources)]
    for boundary in (fcp, doc_response_end, load_event_end):
        b = _f(boundary)
        if b is not None and b > 0:
            times.append(b)
    return sorted(times)


def _gaps(series: list[float]) -> list[float]:
    return [b - a for a, b in zip(series, series[1:])]


# ── R3: longest stall (primary metric) ───────────────────────────────────────


def longest_stall(series: list[float]) -> float | None:
    """Longest stretch where *nothing finished* — the field-measurable "standing at
    the carousel seeing nothing." The single signal most tied to the bad feeling."""
    gaps = _gaps(series)
    return round(max(gaps), 3) if gaps else None


def longest_stall_window(series: list[float]) -> tuple[float, float, float] | None:
    """``(start, end, duration)`` of the largest inter-completion gap, or ``None``."""
    gaps = _gaps(series)
    if not gaps:
        return None
    i = max(range(len(gaps)), key=lambda k: gaps[k])
    return (series[i], series[i + 1], gaps[i])


# ── R4: cadence regularity ───────────────────────────────────────────────────


def cadence_cov(series: list[float]) -> float | None:
    """Coefficient of variation of inter-completion gaps: ``stddev(gaps)/mean(gaps)``.

    Low = metronomic delivery (smooth); high = clumps and stalls (chunky)."""
    gaps = _gaps(series)
    if len(gaps) < 2:
        return None
    m = mean(gaps)
    if m <= 0:
        return None
    return round(pstdev(gaps) / m, 4)


# ── R5: byte-weighted earliness (Speed-Index analog, in bytes not pixels) ─────


def byte_earliness(resources: list | None, start: float | None) -> float | None:
    """Area ABOVE the cumulative-bytes-delivered curve over the load window.

    Directly analogous to Speed Index being the area above the visual-completeness
    curve — but in bytes, so it isolates byte-arrival cadence from rendering.
    Lower = bytes delivered earlier."""
    res = _live_resources(resources)
    total = sum(_f(r.get("transferSize")) or 0.0 for r in res)
    s = _f(start)
    if total <= 0 or s is None:
        return None
    res = sorted(res, key=lambda r: _f(r.get("responseEnd")))
    cum = 0.0
    area = 0.0
    prev = s
    for r in res:
        end = _f(r.get("responseEnd"))
        dt = end - prev
        if dt > 0:
            area += (1.0 - cum / total) * dt
        cum += _f(r.get("transferSize")) or 0.0
        prev = max(prev, end)
    return round(area, 3)


# ── R6: delivery evenness (optional scalar) ──────────────────────────────────


def _gini(values: list[float]) -> float:
    """Gini coefficient of non-negative values (0 = perfectly even, →1 = concentrated)."""
    vals = sorted(v for v in values if v >= 0)
    n = len(vals)
    s = sum(vals)
    if n == 0 or s == 0:
        return 0.0
    cum = sum(i * v for i, v in enumerate(vals, start=1))
    return (2.0 * cum) / (n * s) - (n + 1.0) / n


def delivery_gini(
    resources: list | None,
    start: float | None,
    end: float | None,
    *,
    slices: int = 20,
) -> float | None:
    """Gini coefficient of bytes-delivered across equal time slices of the window.

    High = bytes arrived very unevenly (chunky); low = evenly (smooth). Bounded
    0–1, interpretable. Lower = smoother."""
    res = _live_resources(resources)
    total = sum(_f(r.get("transferSize")) or 0.0 for r in res)
    s, e = _f(start), _f(end)
    if total <= 0 or s is None or e is None or e <= s:
        return None
    width = (e - s) / slices
    buckets = [0.0] * slices
    for r in res:
        t = _f(r.get("responseEnd"))
        idx = int((t - s) / width) if width > 0 else 0
        idx = min(max(idx, 0), slices - 1)
        buckets[idx] += _f(r.get("transferSize")) or 0.0
    return round(_gini(buckets), 4)


# ── R8: perceived-time scalar ────────────────────────────────────────────────


def perceived_time(
    events: list[float],
    start: float | None,
    end: float | None,
    *,
    slice_ms: float = PERCEIVED_DEFAULTS["slice_ms"],
    w_occupied: float = PERCEIVED_DEFAULTS["w_occupied"],
    w_unoccupied: float = PERCEIVED_DEFAULTS["w_unoccupied"],
) -> float | None:
    """``Σ w(slice) · Δt`` over fixed time slices of ``[start, end]``.

    A slice is *occupied* (low weight) if a completion/paint occurred within it,
    else *unoccupied/stall* (high weight). You can lengthen real total time and
    still lower perceived time by converting high-weight stalls into low-weight
    occupied slices — and past the knee, added time only adds occupied slices, so
    the model self-limits. Lower = better perceived."""
    s, e = _f(start), _f(end)
    if s is None or e is None or e <= s:
        return None
    ev = sorted(float(x) for x in (_f(v) for v in events) if x is not None)
    n = int(math.ceil((e - s) / slice_ms))
    j = 0  # events are sorted; advance a pointer instead of rescanning
    total = 0.0
    for k in range(n):
        s0 = s + k * slice_ms
        s1 = min(s0 + slice_ms, e)
        while j < len(ev) and ev[j] < s0:
            j += 1
        occupied = j < len(ev) and ev[j] < s1
        total += (w_occupied if occupied else w_unoccupied) * (s1 - s0)
    return round(total, 3)


# ── R7: network-vs-render attribution ────────────────────────────────────────


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _covered_by_loaf(start: float, end: float, loaf: list | None) -> float:
    """Total of the ``[start, end]`` window covered by long animation frames/tasks."""
    covered = 0.0
    for entry in loaf or []:
        if not isinstance(entry, dict):
            continue
        s = _f(entry.get("startTime"))
        d = _f(entry.get("duration"))
        if s is None or d is None:
            continue
        covered += _overlap(start, end, s, s + d)
    return covered


def attribute_stall(
    window: tuple[float, float, float] | None,
    loaf: list | None,
    loaf_source: str | None,
) -> str | None:
    """Tag a stall ``network`` | ``render`` | ``mixed`` | ``unknown``.

    A stall with no overlapping long task is the *tunable* layer (network); one
    overlapped by a long task is render-bound (network shaping won't move it). With
    no LoAF/longtask support at all we can't tell — ``unknown`` (don't guess)."""
    if window is None:
        return None
    start, end, dur = window
    if loaf_source is None:
        return "unknown"
    if dur <= 0:
        return "network"
    frac = _covered_by_loaf(start, end, loaf) / dur
    if frac <= 0.01:
        return "network"
    if frac >= 0.5:
        return "render"
    return "mixed"


def stall_attribution_times(
    series: list[float],
    loaf: list | None,
    loaf_source: str | None,
    *,
    min_stall_ms: float = MIN_STALL_MS,
) -> dict[str, float]:
    """Sum network- vs render- vs unknown-attributed stall time across all gaps.

    Only gaps ≥ ``min_stall_ms`` count. ``mixed`` gaps are split by the fraction a
    long task actually covered, so the network share is the part shaping could fix."""
    network = render = unknown = 0.0
    for a, b in zip(series, series[1:]):
        dur = b - a
        if dur < min_stall_ms:
            continue
        if loaf_source is None:
            unknown += dur
            continue
        covered = _covered_by_loaf(a, b, loaf)
        render_part = min(covered, dur)
        render += render_part
        network += dur - render_part
    return {
        "network_ms": round(network, 3),
        "render_ms": round(render, 3),
        "unknown_ms": round(unknown, 3),
    }


def protocol_mix(resources: list | None) -> dict[str, int]:
    """Counts of ``nextHopProtocol`` (e.g. ``h2`` vs ``h3``) over live resources.

    QUIC/h3 behavior is directly relevant to network tuning and may correlate with
    stalls, so it travels with every record."""
    counts: dict[str, int] = {}
    for r in _live_resources(resources):
        proto = (r.get("nextHopProtocol") or "").strip() or "unknown"
        counts[proto] = counts.get(proto, 0) + 1
    return counts


# ── assemblers ───────────────────────────────────────────────────────────────


def _window_bounds(nav: dict, paint: dict, series: list[float]) -> tuple[float | None, float | None]:
    """``(start, end)`` of the load window: TTFB → loadEventEnd (fallbacks last)."""
    start = _f(nav.get("responseStart")) or _f(paint.get("fcp")) or (series[0] if series else None)
    end = _f(nav.get("loadEventEnd")) or (series[-1] if series else None)
    return start, end


def smoothness_metrics(
    nav: dict | None,
    resources: list | None,
    paint: dict | None,
    loaf_obj: dict | None,
    *,
    perceived_params: dict | None = None,
) -> dict[str, float]:
    """Numeric smoothness metrics for one load (the catalog/scoreable subset).

    Returns ``{}``-friendly floats (missing signals omitted) so the derive layer
    can average them across URLs and they ride in the metric record like any other."""
    nav = nav or {}
    paint = paint or {}
    loaf_obj = loaf_obj or {}
    loaf = loaf_obj.get("entries") or []
    loaf_source = loaf_obj.get("source")
    pp = {**PERCEIVED_DEFAULTS, **(perceived_params or {})}

    series = completion_series(
        resources,
        fcp=paint.get("fcp"),
        doc_response_end=nav.get("responseEnd"),
        load_event_end=nav.get("loadEventEnd"),
    )
    start, end = _window_bounds(nav, paint, series)
    # Occupancy events: real completions + the first paint (no synthetic boundaries,
    # so injected loadEventEnd doesn't mark the tail slice "occupied" for free).
    events = completion_series(resources, fcp=paint.get("fcp"))

    out: dict[str, float | None] = {
        "longest_stall_ms": longest_stall(series),
        "cadence_cov": cadence_cov(series),
        "byte_earliness_ms": byte_earliness(resources, start),
        "delivery_gini": delivery_gini(resources, start, end),
        "perceived_time_ms": perceived_time(
            events, start, end,
            slice_ms=pp["slice_ms"], w_occupied=pp["w_occupied"], w_unoccupied=pp["w_unoccupied"],
        ),
    }
    # Attribution sums only make sense once there's a series to attribute over;
    # 0ms-of-stall is meaningful, but only when we actually measured a load.
    if len(series) >= 2:
        attr = stall_attribution_times(series, loaf, loaf_source)
        out["network_stall_ms"] = attr["network_ms"]
        out["render_stall_ms"] = attr["render_ms"]
        # Stall time we can't attribute (no LoAF/longtask support). Kept so the
        # network/render split isn't silently overcounted as "no stall".
        out["unknown_stall_ms"] = attr["unknown_ms"]
    return {k: v for k, v in out.items() if v is not None}


def smoothness_record(
    nav: dict | None,
    resources: list | None,
    paint: dict | None,
    loaf_obj: dict | None,
    *,
    perceived_params: dict | None = None,
) -> dict:
    """Full smoothness record for one load: numeric metrics + categorical context.

    Travels alongside the speed-side finish metrics (``loadEventEnd``, ``lcp``) so
    the smoothness-vs-speed tradeoff is legible per load. This is what the
    smoothness API surfaces; the derive layer uses :func:`smoothness_metrics`."""
    nav = nav or {}
    paint = paint or {}
    loaf_obj = loaf_obj or {}
    loaf = loaf_obj.get("entries") or []
    loaf_source = loaf_obj.get("source")
    pp = {**PERCEIVED_DEFAULTS, **(perceived_params or {})}

    series = completion_series(
        resources,
        fcp=paint.get("fcp"),
        doc_response_end=nav.get("responseEnd"),
        load_event_end=nav.get("loadEventEnd"),
    )
    window = longest_stall_window(series)
    metrics = smoothness_metrics(nav, resources, paint, loaf_obj, perceived_params=pp)

    record = dict(metrics)
    record.update(
        {
            # Phase scalars + speed-side context (R1): the tradeoff travels together.
            "ttfb_ms": _f(nav.get("responseStart")),
            "fcp_ms": _f(paint.get("fcp")),
            "lcp_ms": _f(paint.get("lcp")),
            "dom_content_loaded_ms": _f(nav.get("domContentLoadedEventEnd")),
            "load_event_ms": _f(nav.get("loadEventEnd")),
            # Attribution (R7).
            "longest_stall_attribution": attribute_stall(window, loaf, loaf_source),
            "loaf_source": loaf_source,
            "protocol_mix": protocol_mix(resources),
            "resource_count": len(_live_resources(resources)),
            "perceived_time_params": pp,
        }
    )
    return record
