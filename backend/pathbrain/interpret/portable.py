"""Raw observations → metrics for the **portable (away) test**.

The portable test is what a plain browser tab on *any* device can measure: a synthetic
resource waterfall of public CDN objects, a streamed download, and a burst of warm
round trips. A page served by PathBrain can never read the load timing of google.com
or github.com (same-origin policy), so this is a different instrument from the
Chromium plugin — deliberately **never** graded on the methodology's Overall scale. Its
only comparison is "vs home": the same device, the same recipe, at home.

Everything here is a pure function over the stored raw, so a run re-derives at any time
and — the property the "vs home" comparison depends on — can be re-derived over a
**restricted resource set** (``include_ids``): when an away network blocked one origin,
the comparison drops that origin from *both* sides instead of averaging over the
survivors (the same rule ``runner.missing_pages`` applies to a failed page).

Raw shape (one document per run, built by the page — ``frontend/src/utils/portableTest.ts``)::

    {"iterations": [
       {"waterfall": {"resources": [
            {"id": "doc", "url": "...", "bytes": 13188, "ok": true, "error": null,
             "t_start": 12.3, "t_end": 98.1,
             "entry": {  # the Resource Timing entry, when the page could match one
               "startTime": .., "fetchStart": .., "domainLookupStart": .., "domainLookupEnd": ..,
               "connectStart": .., "secureConnectionStart": .., "connectEnd": ..,
               "requestStart": .., "responseStart": .., "responseEnd": ..,
               "transferSize": .., "encodedBodySize": .., "nextHopProtocol": ".."}}, ...]},
        "stream": {"url": "...", "ok": true, "start": .., "end": .., "bytes": .., "partial": false,
                   "chunks": [{"t": ms_since_start, "bytes": n}, ...]},
        "rtt":    {"url": "...", "samples_ms": [..]}}
    ]}

All times are ``performance.now()`` milliseconds on the device's own clock; only
differences are ever used.
"""
from __future__ import annotations

from statistics import median, pstdev
from urllib.parse import urlsplit

from .smoothness import (
    byte_earliness,
    cadence_cov,
    delivery_gini,
    longest_stall,
    stall_energy,
)

# Bump when a formula here changes: it is folded into the instrument version, so home
# references derived under an older formula stop being admitted as comparable.
PORTABLE_DERIVATION_VERSION = "portable-derive-v1"

# Metric catalog for the portable instrument: key → (label, unit, lower_is_better).
# Kept here (not in ``metrics.py``) on purpose — these are NOT methodology metrics and must
# never enter the crown, the axes or the per-run Score.
PORTABLE_METRICS: dict[str, tuple[str, str, bool]] = {
    "first_complete_ms": ("First resource complete", "ms", True),
    "largest_complete_ms": ("Largest resource complete", "ms", True),
    "last_complete_ms": ("Waterfall complete", "ms", True),
    "longest_stall_ms": ("Longest stall", "ms", True),
    "stall_energy_ms": ("Stall energy", "ms", True),
    "cadence_cov": ("Cadence CoV", "", True),
    "delivery_gini": ("Delivery Gini", "", True),
    "byte_earliness_ms": ("Byte earliness", "ms", True),
    "rtt_ms": ("Round trip (warm)", "ms", True),
    "jitter_ms": ("Round-trip jitter", "ms", True),
    "throughput_mbps": ("Stream throughput", "Mbit/s", False),
    "stream_ms_per_mb": ("Stream time per MB", "ms", True),
    "stream_longest_stall_ms": ("Stream longest stall", "ms", True),
    "stream_cadence_cov": ("Stream cadence CoV", "", True),
    # Burst fairness — the round-robin mechanism itself, read off the waterfall's concurrent
    # downloads (see ``_burst_metrics``). "Higher is better" on the interleave index: 1 means
    # a small object moved at the large flow's pace beside it; below 1 it waited behind the
    # bulk; above 1 the small flow was favoured (fq_codel's new-flow priority).
    "interleave_index": ("Burst interleave (small ÷ large flow pace)", "", False),
    "bulk_share": ("Bulk share while ≥2 flows in flight", "", True),
    "small_under_large_ms": ("Small object under a large one", "ms", True),
}

#: The burst-fairness keys, for readers that want just the mechanism.
BURST_METRICS = ("interleave_index", "bulk_share", "small_under_large_ms")

# Per-origin connection-setup phases, from the first *new* connection an iteration opened
# to that origin. Only origins that send ``Timing-Allow-Origin`` expose them.
ORIGIN_PHASES = ("dns_ms", "tcp_ms", "tls_ms", "ttfb_ms", "download_ms")

# Metrics whose value is dominated by connection setup — DNS, TCP and TLS on the first fetch
# to each origin. Two runs compare on them only when they opened their connections the same
# way: a phone's long-lived tab holds warm sockets to the recipe's origins from ordinary use,
# while a fresh browser context pays every handshake, and the difference (tens of ms per
# origin, chained down the waterfall) is exactly the size of the "lead" the first real
# cross-device reading showed. ``warmth`` measures it per run; the compare reads it.
SETUP_BOUND = ("first_complete_ms", "largest_complete_ms", "last_complete_ms", "byte_earliness_ms")


def _f(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _origin(url: str | None) -> str:
    try:
        parts = urlsplit(url or "")
        return parts.hostname or ""
    except Exception:  # noqa: BLE001 — a malformed URL is just an unnamed origin
        return ""


def _median(vals: list[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(median(vals), 3) if vals else None


# ── one iteration ────────────────────────────────────────────────────────────


def _resource_end(r: dict) -> float | None:
    """Completion time of a resource: the Resource Timing ``responseEnd`` when the page
    matched an entry, else the page's own wall-clock ``t_end`` (same clock, coarser)."""
    entry = r.get("entry") or {}
    end = _f(entry.get("responseEnd"))
    if end is not None and end > 0:
        return end
    return _f(r.get("t_end"))


def _resource_start(r: dict) -> float | None:
    entry = r.get("entry") or {}
    start = _f(entry.get("startTime"))
    if start is not None and start > 0:
        return start
    return _f(r.get("t_start"))


def _usable(resources: list, include_ids: set[str] | None) -> list[dict]:
    out = []
    for r in resources or []:
        if not isinstance(r, dict) or not r.get("ok"):
            continue
        if include_ids is not None and r.get("id") not in include_ids:
            continue
        if _resource_end(r) is None:
            continue
        out.append(r)
    return out


# ── burst fairness: the round-robin mechanism, measured ─────────────────────────
#
# fq_codel's everyday effect on an UNSATURATED link is not queue management — nothing
# stands in a queue long enough for CoDel to act — it is the scheduler interleaving the
# flows of a page-load burst: a font's few packets get onto the wire between a bundle's
# many, instead of behind them. That mechanism is exactly what a real page cannot expose:
# a page's resource sizes are unknown (cross-origin entries report 0 bytes without
# Timing-Allow-Origin), its overlap structure changes with every deploy, and its bytes are
# opaque. The portable recipe fixes all three — known sizes, TAO'd origins, a fixed
# dependency chain in which small objects (fonts, small libraries) are fetched WHILE large
# ones (bundles) are in flight — so the fairness of the interleave can be read directly:
#
# * ``interleave_index`` — for each small object downloaded mostly beside a large one, its
#   byte rate over its own download window divided by the large flow's over its window,
#   median over such pairs. 1 = it moved at the large flow's pace (a fair share); well below
#   1 = it waited behind the bulk (FIFO behaviour); above 1 = small/new flows were favoured
#   (fq_codel's new-flow priority). The large flow's rate is its whole-window average, which
#   includes stretches with fewer competitors, so the reading is biased slightly below 1
#   even under perfect fairness — the same bias for every profile on the same recipe, so
#   the comparison across profiles (and against SQM off) is what to read.
# * ``bulk_share`` — over every stretch in which two or more downloads were in flight, the
#   share of the bytes that went to the largest active flow (each flow's bytes in a
#   stretch taken at its own average rate), byte-weighted. Two equal flows read 0.5; one
#   flow hogging the wire reads toward 1.
# * ``small_under_large_ms`` — the median download time of those small objects while a
#   large one was in flight: the felt cost, in ms, on this device.
#
# Additive, so ``PORTABLE_DERIVATION_VERSION`` is deliberately NOT bumped: it is part of
# the instrument identity, and bumping it would stop every home run on record from being
# admitted as a reference — for a metric older raws re-derive perfectly well.
SMALL_BYTES = 32_000
LARGE_BYTES = 100_000
MIN_OVERLAP_SHARE = 0.5


def _download_window(r: dict) -> tuple[float, float] | None:
    """The body-download window (responseStart → responseEnd) of a TAO'd entry, or None."""
    entry = r.get("entry") or {}
    rs, re_ = _f(entry.get("responseStart")), _f(entry.get("responseEnd"))
    if rs is None or re_ is None or rs <= 0 or re_ <= rs:
        return None
    return rs, re_


def _burst_metrics(waterfall: dict | None, include_ids: set[str] | None) -> dict:
    res = _usable((waterfall or {}).get("resources") or [], include_ids)
    windows: list[tuple[str, float, float, float]] = []
    for r in res:
        w = _download_window(r)
        nbytes = _f(r.get("bytes")) or 0.0
        if w is None or nbytes <= 0:
            continue
        windows.append((str(r.get("id")), w[0], w[1], nbytes))
    if len(windows) < 2:
        return {}
    rate = {rid: nbytes / (re_ - rs) for rid, rs, re_, nbytes in windows}
    smalls = [w for w in windows if w[3] <= SMALL_BYTES]
    larges = [w for w in windows if w[3] >= LARGE_BYTES]
    ratios: list[float] = []
    durations: list[float] = []
    for sid, srs, sre, _sb in smalls:
        s_dur = sre - srs
        best: tuple[float, str] | None = None
        for lid, lrs, lre, _lb in larges:
            overlap = min(sre, lre) - max(srs, lrs)
            if overlap <= 0 or overlap < MIN_OVERLAP_SHARE * s_dur:
                continue
            if best is None or overlap > best[0]:
                best = (overlap, lid)
        if best is None or rate[best[1]] <= 0:
            continue
        ratios.append(rate[sid] / rate[best[1]])
        durations.append(s_dur)
    out: dict[str, float] = {}
    if ratios:
        out["interleave_index"] = round(median(ratios), 3)
        out["small_under_large_ms"] = round(median(durations), 3)
    events = sorted({t for _id, rs, re_, _b in windows for t in (rs, re_)})
    hog = total = 0.0
    for a, b in zip(events, events[1:]):
        active = [w for w in windows if w[1] <= a and w[2] >= b]
        if len(active) < 2:
            continue
        by = [rate[w[0]] * (b - a) for w in active]
        tot = sum(by)
        if tot <= 0:
            continue
        hog += max(by)
        total += tot
    if total > 0:
        out["bulk_share"] = round(hog / total, 3)
    return out


def _waterfall_metrics(waterfall: dict | None, include_ids: set[str] | None) -> dict:
    res = _usable((waterfall or {}).get("resources") or [], include_ids)
    if not res:
        return {}
    starts = [s for s in (_resource_start(r) for r in res) if s is not None]
    t0 = min(starts) if starts else None
    if t0 is None:
        return {}
    ends = sorted(_resource_end(r) for r in res)
    out: dict[str, float | None] = {
        "first_complete_ms": round(ends[0] - t0, 3),
        "last_complete_ms": round(ends[-1] - t0, 3),
    }
    largest = max(res, key=lambda r: _f(r.get("bytes")) or 0.0)
    if (_f(largest.get("bytes")) or 0.0) > 0:
        out["largest_complete_ms"] = round(_resource_end(largest) - t0, 3)
    # The byte-arrival smoothness math is shared with the page-load instrument: the
    # completion series is the same object (sorted responseEnd), so the shape statistics
    # mean the same thing. Sizes come from the recipe (opaque cross-origin entries report
    # transferSize 0), so the byte-weighted metrics see real bytes.
    pseudo = [
        {"responseEnd": _resource_end(r), "transferSize": _f(r.get("bytes")) or 0.0} for r in res
    ]
    out["longest_stall_ms"] = longest_stall(ends)
    out["stall_energy_ms"] = stall_energy(ends)
    out["cadence_cov"] = cadence_cov(ends)
    out["byte_earliness_ms"] = byte_earliness(pseudo, t0)
    out["delivery_gini"] = delivery_gini(pseudo, t0, ends[-1])
    return {k: v for k, v in out.items() if v is not None}


def _stream_metrics(stream: dict | None) -> dict:
    s = stream or {}
    if not s.get("ok") and not s.get("partial"):
        return {}
    start, end, nbytes = _f(s.get("start")), _f(s.get("end")), _f(s.get("bytes"))
    out: dict[str, float | None] = {}
    if start is not None and end is not None and end > start and nbytes and nbytes > 0:
        secs = (end - start) / 1000.0
        mbps = (nbytes * 8.0) / secs / 1_000_000.0
        out["throughput_mbps"] = round(mbps, 3)
        out["stream_ms_per_mb"] = round((end - start) / (nbytes / 1_000_000.0), 3)
    chunks = [c for c in (s.get("chunks") or []) if isinstance(c, dict)]
    times = sorted(t for t in (_f(c.get("t")) for c in chunks) if t is not None)
    if len(times) >= 3:
        out["stream_longest_stall_ms"] = longest_stall(times)
        out["stream_cadence_cov"] = cadence_cov(times)
    return {k: v for k, v in out.items() if v is not None}


def _rtt_metrics(rtt: dict | None) -> dict:
    samples = [v for v in (_f(x) for x in ((rtt or {}).get("samples_ms") or [])) if v is not None and v >= 0]
    if not samples:
        return {}
    out = {"rtt_ms": round(median(samples), 3)}
    if len(samples) > 1:
        out["jitter_ms"] = round(pstdev(samples), 3)
    return out


def _origin_phases(waterfall: dict | None, include_ids: set[str] | None) -> dict[str, dict]:
    """Per-origin setup phases from the first NEW connection to each origin.

    A reused connection reports ``connectStart == connectEnd`` — that is connection reuse,
    not a 0 ms handshake — so setup phases are only read off entries that actually opened
    a connection. TTFB/download come from the first usable entry regardless."""
    out: dict[str, dict] = {}
    res = sorted(_usable((waterfall or {}).get("resources") or [], include_ids), key=lambda r: _resource_start(r) or 0.0)
    for r in res:
        entry = r.get("entry") or {}
        origin = _origin(r.get("url"))
        if not origin:
            continue
        rs, rq, re_ = _f(entry.get("responseStart")), _f(entry.get("requestStart")), _f(entry.get("responseEnd"))
        if rs is None or rs <= 0 or rq is None or rq <= 0:
            continue  # no Timing-Allow-Origin: the phases are zeroed, nothing to read
        slot = out.setdefault(origin, {})
        if "ttfb_ms" not in slot:
            slot["ttfb_ms"] = round(rs - rq, 3)
            if re_ is not None and re_ >= rs:
                slot["download_ms"] = round(re_ - rs, 3)
        cs, ce = _f(entry.get("connectStart")), _f(entry.get("connectEnd"))
        if "tcp_ms" in slot or cs is None or ce is None or ce <= cs:
            continue  # already have a cold connection, or this one was reused
        dls, dle = _f(entry.get("domainLookupStart")), _f(entry.get("domainLookupEnd"))
        if dls is not None and dle is not None and dle >= dls:
            slot["dns_ms"] = round(dle - dls, 3)
        sec = _f(entry.get("secureConnectionStart")) or 0.0
        if sec > 0 and ce >= sec:
            slot["tls_ms"] = round(ce - sec, 3)
            slot["tcp_ms"] = round(sec - cs, 3)
        else:
            slot["tls_ms"] = 0.0
            slot["tcp_ms"] = round(ce - cs, 3)
    return out


def _protocol(entry: dict | None) -> str:
    p = str((entry or {}).get("nextHopProtocol") or "").strip().lower()
    if not p:
        return "unknown"
    if p.startswith("h3") or "quic" in p:
        return "h3"
    if p == "h2":
        return "h2"
    if p.startswith("http/1"):
        return "http/1.1"
    return p


def warmth(raw: dict | None) -> dict:
    """How warm the run's connections were, and over what transport.

    Over every completed resource whose origin sends ``Timing-Allow-Origin`` (only those
    expose the connect phases): the share whose entry **reused** a connection — the Resource
    Timing spec reports ``connectEnd <= connectStart`` for a reused socket, never a 0 ms
    handshake — and the ``nextHopProtocol`` mix (h2 over TCP, h3 over QUIC, http/1.1). The
    two together say whether a run's setup-bound metrics can be compared with another run's:
    a warm h3 tab and a cold h2 context are two instruments."""
    tao = reused = 0
    protocols: dict[str, int] = {}
    for it in (raw or {}).get("iterations") or []:
        res = ((it or {}).get("waterfall") or {}).get("resources") or []
        for r in _usable(res, None):
            entry = r.get("entry") or {}
            rq = _f(entry.get("requestStart"))
            if rq is None or rq <= 0:
                continue  # no Timing-Allow-Origin: the connect phases are zeroed, unreadable
            tao += 1
            cs, ce = _f(entry.get("connectStart")), _f(entry.get("connectEnd"))
            if cs is not None and ce is not None and ce <= cs:
                reused += 1
            proto = _protocol(entry)
            protocols[proto] = protocols.get(proto, 0) + 1
    known = {k: v for k, v in protocols.items() if k != "unknown"}
    dominant = max(known, key=known.get) if known else None
    return {
        "tao_resources": tao,
        "reused": reused,
        "reused_share": round(reused / tao, 3) if tao else None,
        "protocols": protocols,
        "protocol": dominant,
    }


# ── the run ──────────────────────────────────────────────────────────────────


def coverage(raw: dict | None) -> dict:
    """Which recipe resources succeeded in **every** iteration, which failed, and the
    origins involved — the input to the pairwise "compare only what both sides have" rule."""
    ok_all: set[str] | None = None
    failed: dict[str, str] = {}
    origins: set[str] = set()
    iterations = (raw or {}).get("iterations") or []
    for it in iterations:
        res = ((it or {}).get("waterfall") or {}).get("resources") or []
        ok_here = set()
        for r in res:
            if not isinstance(r, dict) or not r.get("id"):
                continue
            origins.add(_origin(r.get("url")))
            if r.get("ok") and _resource_end(r) is not None:
                ok_here.add(r["id"])
            else:
                failed[r["id"]] = str(r.get("error") or "failed")
        ok_all = ok_here if ok_all is None else (ok_all & ok_here)
    return {
        "resources_ok": sorted(ok_all or set()),
        "resources_failed": failed,
        "origins": sorted(o for o in origins if o),
        "iterations": len(iterations),
        # Stored with the coverage so a run's warmth is on its row without a migration.
        "warmth": warmth(raw),
        # This derivation knew the burst metrics. A row without the flag was derived before
        # they existed, so a reader can backfill it from raw; a row WITH the flag and no
        # burst metric genuinely had no overlapping downloads to read.
        "burst": True,
    }


def derive_portable(raw: dict | None, include_ids: set[str] | list[str] | None = None) -> dict:
    """Derive the portable metric set from a run's raw.

    Returns ``{"metrics": {...}, "per_origin": {origin: {phase: ms}}, "coverage": {...},
    "warmth": {...}}`` (``warmth`` is also inside ``coverage``).
    Metrics are medians over the iterations that produced them. ``include_ids`` restricts
    the waterfall to that resource subset (for pairwise comparison); stream and RTT are
    independent of it."""
    ids = set(include_ids) if include_ids is not None else None
    iterations = (raw or {}).get("iterations") or []
    per_iter: list[dict] = []
    per_origin_iters: list[dict[str, dict]] = []
    for it in iterations:
        it = it or {}
        m: dict[str, float] = {}
        m.update(_waterfall_metrics(it.get("waterfall"), ids))
        m.update(_burst_metrics(it.get("waterfall"), ids))
        m.update(_stream_metrics(it.get("stream")))
        m.update(_rtt_metrics(it.get("rtt")))
        per_iter.append(m)
        per_origin_iters.append(_origin_phases(it.get("waterfall"), ids))
    metrics: dict[str, float] = {}
    for key in PORTABLE_METRICS:
        vals = [m[key] for m in per_iter if key in m]
        med = _median(vals)
        if med is not None:
            metrics[key] = med
    per_origin: dict[str, dict] = {}
    for origin in sorted({o for po in per_origin_iters for o in po}):
        slot: dict[str, float] = {}
        for phase in ORIGIN_PHASES:
            vals = [po[origin][phase] for po in per_origin_iters if origin in po and phase in po[origin]]
            med = _median(vals)
            if med is not None:
                slot[phase] = med
        if slot:
            per_origin[origin] = slot
    cov = coverage(raw)
    return {"metrics": metrics, "per_origin": per_origin, "coverage": cov, "warmth": cov["warmth"]}


def iteration_count(raw: dict | None) -> int:
    return len((raw or {}).get("iterations") or [])


__all__ = [
    "BURST_METRICS",
    "ORIGIN_PHASES",
    "PORTABLE_DERIVATION_VERSION",
    "PORTABLE_METRICS",
    "coverage",
    "derive_portable",
    "iteration_count",
]
