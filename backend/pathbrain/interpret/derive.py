"""Raw observations → derived metric values (the interpretation seam).

``derive(plugin, raw)`` reproduces the scoreable metric dict each plugin used to
emit, but from the stored *raw* observations — so it can be re-run over history.
Every interpretation lives here: means across targets, jitter = stddev(RTTs),
transfer = bytes·8/duration, and the trajectory metrics (Speed Index, paint
cadence, CLS) computed from the browser filmstrip.

Bump ``DERIVATION_VERSION`` whenever a formula here changes, so callers know a run's
cached metric values predate the current derivation and need a re-derive.
"""
from __future__ import annotations

import os
from collections import defaultdict
from statistics import mean, pstdev

# Reuse the browser plugin's pure nav/paint extractors (importing the module is
# safe — it imports Playwright lazily inside run()).
from ..plugins.benchmark_browser import compute_navigation_metrics, extract_paint_metrics
from .smoothness import smoothness_metrics
from .waterfall import navigation_phases

# derive-v6: the browser derivation now also emits the navigation-timing *waterfall* —
# an additive, non-overlapping phase decomposition of the load (stall/DNS/TCP/TLS/request/
# response/render/paint) plus after-first-byte roll-ups (`nav_ttfb_cumulative_ms`,
# `nav_fcp_after_ttfb_ms`, `nav_lcp_after_ttfb_ms`). The decomposition's key boundary is
# responseEnd: responseStart→responseEnd is body delivery (SQM-facing, shapeable) while
# responseEnd→FCP is client CPU (shaping-immune). Purely additive: computed from the
# already-captured raw nav marks + FCP/LCP, so history re-derives with no re-collection, and
# no existing formula changed. Display-only (silver-layer measurables); gold scoring untouched.
# (derive-v5 added stall_time_ms; derive-v4 added total_stall_ms.)
DERIVATION_VERSION = "derive-v6"


def _round(v: float | None, n: int = 3) -> float | None:
    return round(v, n) if v is not None else None


# ── per-plugin derivations (reproduce the old in-plugin aggregation) ─────────


def _derive_icmp(raw: dict, _art: str | None) -> dict:
    """Per-target latency=mean(RTTs), jitter=stddev(RTTs), loss=(sent−recv)/sent,
    then averaged across targets (dead targets count as 100% loss)."""
    latencies: list[float] = []
    jitters: list[float] = []
    losses: list[float] = []
    for d in (raw.get("targets") or {}).values():
        rtts = [float(r) for r in (d.get("rtts_ms") or [])]
        sent = int(d.get("sent") or 0)
        received = int(d.get("received") or 0)
        if received > 0 and rtts:
            latencies.append(mean(rtts))
            jitters.append(pstdev(rtts) if len(rtts) > 1 else 0.0)
        losses.append(((sent - received) / sent * 100.0) if sent else 100.0)
    return {
        "latency_ms": _round(mean(latencies)) if latencies else None,
        "jitter_ms": _round(mean(jitters)) if jitters else None,
        "packet_loss_pct": _round(mean(losses)) if losses else None,
    }


def _derive_dns(raw: dict, _art: str | None) -> dict:
    times = [float(t) for p in (raw.get("providers") or []) for t in (p.get("lookups_ms") or [])]
    return {"lookup_ms": _round(mean(times)) if times else None}


def _derive_tcp(raw: dict, _art: str | None) -> dict:
    times = [
        float(d["connect_ms"])
        for d in (raw.get("targets") or {}).values()
        if d.get("connect_ms") is not None
    ]
    return {"connect_ms": _round(mean(times)) if times else None}


def _derive_tls(raw: dict, _art: str | None) -> dict:
    times = [
        float(d["handshake_ms"])
        for d in (raw.get("targets") or {}).values()
        if d.get("handshake_ms") is not None
    ]
    return {"handshake_ms": _round(mean(times)) if times else None}


def _derive_http(raw: dict, _art: str | None) -> dict:
    ttfbs: list[float] = []
    downloads: list[float] = []
    speeds: list[float] = []
    for u in (raw.get("urls") or {}).values():
        if not isinstance(u, dict) or u.get("download_ms") is None:
            continue
        if u.get("ttfb_ms") is not None:
            ttfbs.append(float(u["ttfb_ms"]))
        dl = float(u["download_ms"])
        downloads.append(dl)
        nbytes = float(u.get("bytes") or 0)
        if dl > 0 and nbytes > 0:
            speeds.append((nbytes * 8) / (dl / 1000.0) / 1_000_000.0)
    return {
        "ttfb_ms": _round(mean(ttfbs)) if ttfbs else None,
        "download_ms": _round(mean(downloads)) if downloads else None,
        "transfer_mbps": _round(mean(speeds)) if speeds else None,
    }


# ── trajectory metrics (the perception-true signal) ──────────────────────────


def visual_progress_from_frames(frames: list, artifact_dir: str | None) -> list[tuple[float, float]]:
    """Visual-completeness curve ``[(t_ms, 0..1)]`` from filmstrip frames.

    Completeness = histogram similarity to the final frame, normalized by the first
    frame's distance (WebPageTest/Lighthouse style). Needs Pillow + the frame files;
    returns ``[]`` when either is unavailable, so Speed Index simply isn't derived.
    """
    if not frames or not artifact_dir:
        return []
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 — Pillow not installed
        return []

    ts: list[float] = []
    hists: list[list[int]] = []
    for f in frames:
        path = os.path.join(artifact_dir, f.get("frame", ""))
        try:
            with Image.open(path) as img:
                hists.append(img.convert("RGB").histogram())
            ts.append(float(f.get("t_ms", 0.0)))
        except Exception:  # noqa: BLE001 — skip unreadable frames
            continue
    if len(hists) < 2:
        return []

    def _dist(a: list[int], b: list[int]) -> float:
        return float(sum(abs(x - y) for x, y in zip(a, b)))

    final = hists[-1]
    base = _dist(hists[0], final) or 1.0
    progress = [(t, max(0.0, min(1.0, 1.0 - _dist(h, final) / base))) for t, h in zip(ts, hists)]
    progress[-1] = (progress[-1][0], 1.0)
    return progress


def speed_index_from_progress(progress: list[tuple[float, float]]) -> float | None:
    """Speed Index = ∫(1 − visual_completeness) dt — the average time content is
    visible. Lower is better; rewards early, progressive painting over a late dump."""
    pts = sorted((float(t), max(0.0, min(1.0, float(c)))) for t, c in progress)
    if len(pts) < 2:
        return None
    si = 0.0
    for (t0, c0), (t1, _c1) in zip(pts, pts[1:]):
        si += (t1 - t0) * (1.0 - c0)
    return round(si, 1)


def cadence_from_progress(progress: list[tuple[float, float]]) -> float | None:
    """Largest single jump in visual completeness (0..1). Low = the page filled in
    steadily; high = it stalled then dumped everything at once. Lower is better."""
    pts = sorted((float(t), max(0.0, min(1.0, float(c)))) for t, c in progress)
    if len(pts) < 2:
        return None
    max_jump = 0.0
    prev = 0.0  # start from a blank page
    for _t, c in pts:
        max_jump = max(max_jump, c - prev)
        prev = c
    return round(max_jump, 4)


def _derive_browser(raw: dict, artifact_dir: str | None) -> dict:
    """Per-URL nav/paint/trajectory metrics, averaged across URLs."""
    acc: dict[str, list[float]] = defaultdict(list)
    for u in (raw.get("urls") or {}).values():
        if not isinstance(u, dict) or "nav" not in u:
            continue
        m: dict[str, float | None] = {}
        m.update(compute_navigation_metrics(u.get("nav")))
        m.update(extract_paint_metrics(u.get("paint")))
        m["total_render_ms"] = u.get("total_render_ms")
        progress = visual_progress_from_frames(u.get("filmstrip") or [], artifact_dir)
        m["speed_index_ms"] = speed_index_from_progress(progress)
        m["paint_cadence"] = cadence_from_progress(progress)
        paint = u.get("paint") or {}
        shifts = paint.get("cls_entries")
        m["cls"] = round(sum(float(s) for s in shifts), 4) if shifts is not None else None
        # Perceived load-smoothness metrics (byte-arrival cadence from Resource
        # Timing). Computed from raw, so they backfill over history on re-derive;
        # absent on legacy runs that captured no resource series.
        m.update(
            smoothness_metrics(u.get("nav"), u.get("resources"), paint, u.get("loaf"))
        )
        # Navigation-timing waterfall: additive, independent phase durations + the
        # network-independent paint roll-ups, from the raw nav marks + FCP/LCP.
        m.update(navigation_phases(u.get("nav"), paint))
        for k, v in m.items():
            if v is not None:
                acc[k].append(float(v))
    return {k: _round(mean(v)) for k, v in acc.items() if v}


_DERIVERS = {
    "icmp": _derive_icmp,
    "dns": _derive_dns,
    "tcp": _derive_tcp,
    "tls": _derive_tls,
    "http": _derive_http,
    "browser": _derive_browser,
}


def derive(plugin: str, raw: dict | None, artifact_dir: str | None = None) -> dict:
    """Derive a plugin's scoreable metric dict from its raw observations.

    ``artifact_dir`` is the base path filmstrip frames are stored under (only the
    browser uses it). Returns ``{}`` for unknown plugins or empty raw.
    """
    fn = _DERIVERS.get(plugin)
    if fn is None or not raw:
        return {}
    return {k: v for k, v in fn(raw, artifact_dir).items() if v is not None}
