"""The **portable (away) test**: recipe, score, and the "vs home" comparison.

PathBrain's real instrument is a headless Chromium on the home network loading real
pages. Away from home — a hotel, a client site, a relative's Wi-Fi — the question is
"this isn't home, but how did it stand up?", from whatever device is in hand. A browser
tab cannot load google.com and read its paint timing (same-origin policy), so the
portable test is a **different instrument**: a synthetic resource waterfall of public CDN
objects, a streamed download, and warm round trips, all measured with Resource Timing
from a plain page (``frontend/src/pages/Away.tsx`` drives it; ``interpret/portable.py``
derives the metrics).

Two rules make the comparison honest, and both are enforced here rather than left to
the reader:

* **Only directly comparable data is a reference.** A home reference for an away run is
  drawn from runs that match on every stamp — the same **device** (a device id the page
  keeps in local storage; a phone compares to itself, never to a laptop and never to the
  server's Chromium), the same **instrument version** (a hash of the recipe + the derive
  formula — change the recipe and old home data stops being admitted, exactly as a site
  publish quarantines old runs), one **home profile** (the firewall fingerprint stamped
  on each home run; the reference names it), the nearest **time cell** with enough runs
  (same weekday & hour → same hour any day → any time, the fallback ladder ``trends``
  uses, and the readout says which rung), and **pairwise coverage** (a resource that
  failed away is dropped from *both* sides by re-deriving from raw over the common set,
  never averaged over the survivors).
* **Never on the crown's scale.** Portable runs live in their own table
  (``PortableRun``), score on their own small rubric (``PORTABLE_RUBRIC``), and are never
  joined to ``runs``/``scores`` — so nothing here can touch the pooled crown, the duel,
  the trends baseline or the weather pass.

A **home** run is one the user marks as taken at home; the server stamps it with the
firewall profile in effect (best-effort — the provider may be unreachable, and a home
run with no stamp is still a home run, just one that can't be filtered by profile).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy import func, select

from .interpret.portable import (
    PORTABLE_DERIVATION_VERSION,
    PORTABLE_METRICS,
    derive_portable,
    iteration_count,
)
from .logging_config import get_logger
from .models import BenchmarkResult, PortableRun
from .raw_access import stored_iterations
from .scoring.engine import compute_score

log = get_logger("portable")

# ── the recipe ───────────────────────────────────────────────────────────────

# Default resources: every URL below was verified to send ``Timing-Allow-Origin: *`` and
# ``Access-Control-Allow-Origin: *`` (so a page can read its connection phases and stream
# its body), with the byte size recorded from the response's Content-Length. Sizes are
# part of the recipe on purpose: opaque/cross-origin entries report ``transferSize`` 0,
# so the byte-weighted smoothness metrics read the recipe's size instead. ``after`` is
# the id this resource is discovered from (a dependency chain, like a real page's
# stylesheet → font or script → data fetch); ``null`` starts with the document.
DEFAULT_RESOURCES: list[dict] = [
    {"id": "doc", "url": "https://ajax.googleapis.com/ajax/libs/webfont/1.6.26/webfont.js", "bytes": 13188, "after": None},
    {"id": "font-a", "url": "https://fonts.gstatic.com/s/roboto/v30/KFOmCnqEu92Fr1Mu4mxK.woff2", "bytes": 15744, "after": "doc"},
    {"id": "lib-a", "url": "https://ajax.googleapis.com/ajax/libs/jquery/3.7.1/jquery.min.js", "bytes": 87533, "after": "doc"},
    {"id": "lib-b", "url": "https://ajax.googleapis.com/ajax/libs/hammerjs/2.0.8/hammer.min.js", "bytes": 20765, "after": "doc"},
    {"id": "lib-c", "url": "https://ajax.googleapis.com/ajax/libs/mootools/1.6.0/mootools.min.js", "bytes": 127518, "after": "doc"},
    {"id": "lib-d", "url": "https://ajax.googleapis.com/ajax/libs/angularjs/1.8.2/angular.min.js", "bytes": 177366, "after": "doc"},
    {"id": "hero", "url": "https://ajax.googleapis.com/ajax/libs/shaka-player/4.3.4/shaka-player.compiled.js", "bytes": 434452, "after": "doc"},
    {"id": "font-b", "url": "https://fonts.gstatic.com/s/opensans/v34/memSYaGs126MiZpBA-UvWbX2vVnXBbObj2OVZyOOSr4dVJWUgsjZ0B4gaVI.woff2", "bytes": 16740, "after": "lib-a"},
    {"id": "lib-e", "url": "https://ajax.googleapis.com/ajax/libs/jqueryui/1.13.2/jquery-ui.min.js", "bytes": 255084, "after": "lib-a"},
    {"id": "data", "url": "https://ajax.googleapis.com/ajax/libs/d3js/7.8.5/d3.min.js", "bytes": 279633, "after": "lib-d"},
]
DEFAULT_STREAM: dict = {
    "url": "https://ajax.googleapis.com/ajax/libs/model-viewer/3.5.0/model-viewer.min.js",
    "bytes": 935194,
    # A slow link still produces a reading: the page stops reading after this many seconds
    # and the metrics are computed over the bytes that arrived (``partial`` on the raw).
    "max_seconds": 15,
}
DEFAULT_RTT: dict = {
    "url": "https://ajax.googleapis.com/ajax/libs/webfont/1.6.26/webfont.js",
    "samples": 8,
}

DEFAULT_CONFIG: dict = {
    # Waterfall repetitions per run (the stream + round trips run once per iteration too).
    "iterations": 2,
    # Home runs from the same device (and matching stamps) required before a "vs home"
    # delta is shown — below it the page asks for more home runs rather than guessing.
    "min_home_runs": 5,
    # Home/away is DETECTED, not declared (see ``decide_home``): the page asks this service
    # for the device's public egress address and the server asks it for its own; equal means
    # the device's traffic leaves through the tuned firewall — which is what "home" means for
    # shaping. Any JSON endpoint answering ``{"ip": "..."}`` with CORS ``*`` works.
    "ip_lookup_url": "https://api.ipify.org?format=json",
    # The same question over IPv6 (a v6-only service, so the answer is the v6 egress). A
    # dual-stack device may reach the v4 service over v4 and still have a v6 address, and a
    # v6-only or CGNAT'd network may make v4 useless — so BOTH families are asked and either
    # may decide (``decide_home``). Best-effort: a v4-only network simply fails this one.
    "ip_lookup_url_v6": "https://api6.ipify.org?format=json",
    # IPv6 hosts on one network never share an *address* (every host gets its own), they
    # share a *prefix*: the delegated /64 (or a VLAN's /64 out of a wider delegation). Two
    # v6 addresses within this prefix length count as the same network. 64 is the safe
    # default — wider (56) would also match a neighbour on an ISP that delegates /64s.
    "home_ipv6_prefix": 64,
    # The home WAN address(es), when you would rather state them than have the server look
    # them up (a server whose own egress isn't the home WAN — say a NAS behind its own
    # tunnel). One or two addresses, IPv4 and/or IPv6, separated by a comma or space.
    "home_ip": "",
    "resources": DEFAULT_RESOURCES,
    "stream": DEFAULT_STREAM,
    "rtt": DEFAULT_RTT,
}


def portable_config(cfg: dict | None) -> dict:
    """The effective ``portable`` config section (defaults under whatever is stored)."""
    stored = (cfg or {}).get("portable") or {}
    out = dict(DEFAULT_CONFIG)
    for k, v in stored.items():
        if v is not None:
            out[k] = v
    return out


def recipe(cfg: dict | None) -> dict:
    """What the page runs: the resource waterfall, the stream, the round-trip probe, the
    iteration count, and the **instrument version** those hash to."""
    pc = portable_config(cfg)
    resources = [
        {
            "id": str(r.get("id")),
            "url": str(r.get("url")),
            "bytes": int(r.get("bytes") or 0),
            "after": r.get("after"),
            "mode": r.get("mode") or "cors",
        }
        for r in (pc.get("resources") or [])
        if r.get("id") and r.get("url")
    ]
    stream = dict(pc.get("stream") or {})
    rtt = dict(pc.get("rtt") or {})
    body = {"resources": resources, "stream": stream, "rtt": rtt}
    return {
        **body,
        "iterations": int(pc.get("iterations") or 2),
        "min_home_runs": int(pc.get("min_home_runs") or 5),
        "instrument_version": instrument_version(body),
        "derivation_version": PORTABLE_DERIVATION_VERSION,
    }


def instrument_version(recipe_body: dict) -> str:
    """Stable id of *what was measured and how it was derived*. Two runs are comparable
    only when this matches — the portable analogue of ``site_set``/``client_set``."""
    canon = {
        "resources": [
            {"id": r["id"], "url": r["url"], "bytes": r.get("bytes"), "after": r.get("after"), "mode": r.get("mode", "cors")}
            for r in recipe_body.get("resources") or []
        ],
        "stream": {k: recipe_body.get("stream", {}).get(k) for k in ("url", "bytes", "max_seconds")},
        "rtt": {k: recipe_body.get("rtt", {}).get(k) for k in ("url", "samples")},
        "derive": PORTABLE_DERIVATION_VERSION,
    }
    payload = json.dumps(canon, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


# ── the portable score ───────────────────────────────────────────────────────

# Its own small rubric on the shared perception-calibrated log curve (``compute_score``).
# Thresholds are reasoned defaults for "a good home link" vs "a bad hotel", NOT calibrated
# against the methodology — this number is for reading an away run against a home run on
# the same instrument, never against the crown's Overall. Weights sum to 100.
PORTABLE_RUBRIC: dict[str, dict] = {
    "first_complete_ms": {"weight": 20, "best": 60.0, "worst": 1500.0},
    "largest_complete_ms": {"weight": 20, "best": 200.0, "worst": 4000.0},
    "last_complete_ms": {"weight": 10, "best": 400.0, "worst": 8000.0},
    "longest_stall_ms": {"weight": 15, "best": 25.0, "worst": 2000.0},
    "stall_energy_ms": {"weight": 10, "best": 50.0, "worst": 3000.0},
    "cadence_cov": {"weight": 5, "best": 0.2, "worst": 2.5},
    "rtt_ms": {"weight": 10, "best": 5.0, "worst": 300.0},
    "jitter_ms": {"weight": 5, "best": 0.5, "worst": 50.0},
    # Time to move one megabyte (lower is better) rather than Mbit/s, so it rides the same
    # log curve as everything else: 40 ms/MB ≈ 200 Mbit/s, 4000 ms/MB ≈ 2 Mbit/s.
    "stream_ms_per_mb": {"weight": 5, "best": 40.0, "worst": 4000.0},
}
_SOURCES = {k: ("portable", k) for k in PORTABLE_RUBRIC}


def score_metrics(metrics: dict) -> tuple[float | None, dict[str, float]]:
    """The portable score (0–100) + per-metric subscores. ``None`` with no scorable metric."""
    breakdown = compute_score(
        {"portable": metrics or {}},
        weights={k: v["weight"] for k, v in PORTABLE_RUBRIC.items()},
        thresholds={k: {"best": v["best"], "worst": v["worst"]} for k, v in PORTABLE_RUBRIC.items()},
        metric_sources=_SOURCES,
    )
    if not breakdown.subscores:
        return None, {}
    return breakdown.sops, breakdown.subscores


# ── home detection ───────────────────────────────────────────────────────────

HOME_IP_TTL_S = 3600.0
FAMILIES = ("v4", "v6")
_home_cache: dict = {"v4": None, "v6": None, "checked_at": None, "errors": {}}


def is_public_ip(ip: str | None) -> bool:
    """A globally routable address — i.e. what a device looks like from the internet. Private
    (RFC1918), loopback, link-local, ULA and carrier-grade NAT (100.64/10, what Tailscale hands
    out) all read False: a request arriving from one of those says "LAN or tunnel", not where
    the device's internet traffic actually leaves."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    return addr.is_global


def address_family(ip: str | None) -> str | None:
    """``"v4"`` / ``"v6"`` for a public address, else None."""
    if not is_public_ip(ip):
        return None
    return "v6" if ipaddress.ip_address(ip.strip()).version == 6 else "v4"


def split_families(*ips: str | None) -> dict[str, str | None]:
    """File each public address under its actual family (first per family wins) — the
    caller need not know which family a lookup answered with (a dual-stack service may
    answer v4 to a v6 question, and a plain-text service says nothing about itself)."""
    out: dict[str, str | None] = {"v4": None, "v6": None}
    for raw in ips:
        for token in str(raw or "").replace(",", " ").split():
            fam = address_family(token)
            if fam and out[fam] is None:
                out[fam] = token.strip()
    return out


def _lookup_egress(url: str, timeout: float = 5.0) -> str | None:
    """Ask ``url`` (a ``{"ip": ...}`` JSON service, or plain text) what address we come from."""
    req = urllib.request.Request(url, headers={"User-Agent": "PathBrain/portable"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — https, configured
        body = resp.read(4096).decode("utf-8", "replace").strip()
    try:
        ip = str(json.loads(body).get("ip") or "").strip()
    except (ValueError, AttributeError):
        ip = body
    return ip if is_public_ip(ip) else None


def home_addresses(cfg: dict | None, *, now: float | None = None) -> dict:
    """The home network's public addresses, per family —
    ``{"v4", "v6", "source", "checked_at", "errors": {family: why}}``.

    ``portable.home_ip`` in config wins (``source="config"``; one or two addresses); otherwise
    the server asks each lookup service for its own egress (``source="lookup"``, cached
    ``HOME_IP_TTL_S``): PathBrain runs at home, so its egress *is* the home WAN. Best-effort per
    family — a v4-only home simply has no v6 — and detection falls back to the user's own
    choice only when no family can be compared."""
    pc = portable_config(cfg)
    stated = str(pc.get("home_ip") or "").strip()
    if stated:
        fams = split_families(stated)
        errors = {} if (fams["v4"] or fams["v6"]) else {"config": f"configured home_ip {stated!r} holds no public address"}
        return {**fams, "source": "config", "checked_at": None, "errors": errors}
    t = time.time() if now is None else now
    c = _home_cache
    if c["checked_at"] is not None and t - c["checked_at"] < HOME_IP_TTL_S and (c["v4"] or c["v6"] or c["errors"]):
        return {"v4": c["v4"], "v6": c["v6"], "source": "lookup" if (c["v4"] or c["v6"]) else None,
                "checked_at": c["checked_at"], "errors": dict(c["errors"])}
    found: dict[str, str | None] = {"v4": None, "v6": None}
    errors: dict[str, str] = {}
    for fam, key in (("v4", "ip_lookup_url"), ("v6", "ip_lookup_url_v6")):
        url = str(pc.get(key) or "").strip()
        if not url:
            errors[fam] = f"no {key} configured"
            continue
        try:
            ip = _lookup_egress(url)
        except Exception as exc:  # noqa: BLE001 — best-effort, reported not raised
            errors[fam] = f"{type(exc).__name__}: {exc}"
            log.info("Portable: home %s lookup via %s failed: %s", fam, url, exc)
            continue
        got = address_family(ip)
        if got is None:
            errors[fam] = "lookup returned no public address"
        elif found[got] is None:
            found[got] = ip  # filed under the family it actually is
    c.update({"v4": found["v4"], "v6": found["v6"], "checked_at": t, "errors": errors})
    return {**found, "source": "lookup" if (found["v4"] or found["v6"]) else None, "checked_at": t, "errors": errors}


def reset_home_ip_cache() -> None:
    _home_cache.update({"v4": None, "v6": None, "checked_at": None, "errors": {}})


def same_v6_network(a: str, b: str, prefix: int) -> bool:
    try:
        na = ipaddress.ip_network(f"{a.strip()}/{int(prefix)}", strict=False)
        nb = ipaddress.ip_network(f"{b.strip()}/{int(prefix)}", strict=False)
    except ValueError:
        return False
    return na == nb


def decide_home(
    is_home: bool | None,
    egress: dict | str | None,
    home: dict | str | None,
    *,
    v6_prefix: int = 64,
) -> tuple[bool, str]:
    """Is this run at home? ``(is_home, detection)`` with detection ``"manual"`` / ``"ip4"`` /
    ``"ip6"``.

    Detected by address whenever a family is known on both sides: the device's public egress
    equal to the home WAN (IPv4), or within the home prefix (IPv6 — hosts on one network share
    a prefix, never an address), means its traffic leaves through the tuned firewall — the
    definition that matters for shaping (a phone on cellular on the living-room couch is *not*
    home for this purpose, and this gets that right where a person's answer wouldn't). **Either
    family matching means home**: an IPv4 mismatch alone is not proof of away, because a
    carrier-grade NAT can hand different flows different public v4 addresses while the v6
    prefix stays the home's. An explicit ``is_home`` is an override and wins. With no family
    comparable on both sides, raise — never guess a stamp the whole comparison keys on."""
    if is_home is not None:
        return bool(is_home), "manual"
    e = split_families(egress) if not isinstance(egress, dict) else split_families(egress.get("v4"), egress.get("v6"))
    h = split_families(home) if not isinstance(home, dict) else split_families(home.get("v4"), home.get("v6"))
    compared: list[tuple[str, bool]] = []
    if e["v4"] and h["v4"]:
        compared.append(("ip4", e["v4"] == h["v4"]))
    if e["v6"] and h["v6"]:
        compared.append(("ip6", same_v6_network(e["v6"], h["v6"], v6_prefix)))
    for fam, matched in compared:
        if matched:
            return True, fam
    if compared:
        return False, compared[0][0]
    raise ValueError(
        "could not tell home from away: no address family is known on both the device and the "
        "home side (the IP lookup may be blocked on this network, or one side is IPv4-only and "
        "the other IPv6-only) — choose Home or Away yourself"
    )


#: How many recent away runs the venue recall scans. Portable runs are a handful a week, not
#: the benchmark table, so this covers a long history for one indexed query — and the label
#: worth suggesting is a recent one anyway.
VENUE_RECALL_SCAN = 500


def recall_venue(
    session,
    egress: dict | str | None,
    *,
    v6_prefix: int = 64,
    device_id: str | None = None,
) -> dict | None:
    """The venue label this network was given last time someone tested from it.

    Being somewhere you have tested before is the common case — the same hotel, the same
    office, the same café — and asking for the label from scratch every time is both busywork
    and how one place ends up recorded under three spellings, which silently splits its
    history. The address is the thing that identifies a network, so the address is what the
    label is recalled by: IPv4 compared exactly, IPv6 by **prefix**, the same rule
    ``decide_home`` uses, because hosts on one network share a prefix and never an address.

    Matching is deliberately **not** restricted to one device — a laptop should inherit the
    name a phone gave the hotel — but the asking device's own label wins when it has one,
    since that is the spelling that reader is used to. Home runs are skipped: they carry no
    venue by construction.

    Returns ``{venue, matched_on, last_seen, runs, from_this_device}`` or ``None`` — a
    *suggestion*, never a decision. The caller pre-fills it and the user can type over it.
    """
    from .models import PortableRun

    want = split_families(egress) if not isinstance(egress, dict) else split_families(
        egress.get("v4"), egress.get("v6")
    )
    if not (want["v4"] or want["v6"]):
        return None

    rows = (
        session.query(PortableRun)
        .filter(
            PortableRun.is_home.is_(False),
            PortableRun.venue.isnot(None),
            PortableRun.venue != "",
            PortableRun.egress_ip.isnot(None),
        )
        .order_by(PortableRun.id.desc())
        .limit(VENUE_RECALL_SCAN)
        .all()
    )

    matches: list[tuple[PortableRun, str]] = []
    for row in rows:
        seen = split_families(row.egress_ip)
        if want["v4"] and seen["v4"] and want["v4"] == seen["v4"]:
            matches.append((row, "ip4"))
        elif want["v6"] and seen["v6"] and same_v6_network(want["v6"], seen["v6"], v6_prefix):
            matches.append((row, "ip6"))
    if not matches:
        return None

    # This device's own last label for the place, else the most recent anyone gave it.
    mine = [m for m in matches if device_id and m[0].device_id == device_id]
    row, matched_on = (mine or matches)[0]
    return {
        "venue": row.venue,
        "matched_on": matched_on,
        "last_seen": row.created_at.isoformat() if row.created_at else None,
        # How much agreement there is behind the suggestion, so the page can say "you have
        # tested here 4 times" rather than implying a single stray label is established.
        "runs": sum(1 for m in matches if (m[0].venue or "").strip() == (row.venue or "").strip()),
        "from_this_device": bool(mine),
    }


def describe_addresses(fams: dict | None) -> str | None:
    """``"203.0.113.7 / 2001:db8::1"`` — one string for a row's ``egress_ip``/``home_ip``."""
    if not fams:
        return None
    parts = [fams.get(f) for f in FAMILIES if fams.get(f)]
    return " / ".join(parts) if parts else None


# ── ingest ───────────────────────────────────────────────────────────────────


def home_stamp() -> tuple[str | None, str | None]:
    """The firewall profile in effect right now — ``(fingerprint, summary)`` — for stamping
    a home run. Best-effort: an unreachable provider yields ``(None, None)`` and the run is
    still recorded as home (it just can't be filtered by profile)."""
    try:
        from .providers import get_provider
        from .settings_profile import fingerprint, normalize, summarize

        normalized = normalize(get_provider().discover())
        return fingerprint(normalized), summarize(normalized)
    except Exception as exc:  # noqa: BLE001 — stamping must never fail an upload
        log.warning("Portable home run: could not stamp firewall profile: %s", exc)
        return None, None


def build_run(payload: dict, current_version: str, *, home: dict | None = None, v6_prefix: int = 64) -> PortableRun:
    """Derive + score an uploaded raw document into a ``PortableRun`` row (not added to a
    session). ``home`` is the home WAN address for detection (see ``decide_home``). Raises
    ``ValueError`` when the upload's instrument version isn't the current recipe's — a stale
    page must not file runs that nothing can compare against — or when home/away can't be
    told and wasn't stated."""
    version = str(payload.get("instrument_version") or "")
    if version != current_version:
        raise ValueError(
            f"instrument version {version or '(none)'} is not the current recipe "
            f"({current_version}); reload the page and run again"
        )
    raw = payload.get("raw") or {}
    if iteration_count(raw) == 0:
        raise ValueError("the upload carries no iterations")
    derived = derive_portable(raw)
    score, subscores = score_metrics(derived["metrics"])
    egress = split_families(payload.get("egress_ip"), payload.get("egress_ip_v6"))
    home_fams = split_families(home.get("v4"), home.get("v6")) if home else {"v4": None, "v6": None}
    is_home, detection = decide_home(payload.get("is_home"), egress, home_fams, v6_prefix=v6_prefix)
    fp = summary = None
    if is_home:
        fp, summary = home_stamp()
    tz = payload.get("tz_offset_minutes")
    try:
        tz = int(tz) if tz is not None else None
    except (TypeError, ValueError):
        tz = None
    return PortableRun(
        device_id=str(payload.get("device_id") or "")[:64],
        device_label=(str(payload.get("device_label"))[:120] if payload.get("device_label") else None),
        venue=(str(payload.get("venue"))[:120] if payload.get("venue") else None),
        is_home=is_home,
        home_detection=detection,
        egress_ip=describe_addresses(egress),
        home_ip=describe_addresses(home_fams),
        instrument_version=version,
        client=payload.get("client") or {},
        tz_offset_minutes=tz,
        settings_fingerprint=fp,
        settings_summary=summary,
        raw=raw,
        metrics=derived["metrics"],
        per_origin=derived["per_origin"],
        coverage=derived["coverage"],
        score=score,
        subscores=subscores,
        notes=(str(payload.get("notes"))[:500] if payload.get("notes") else None),
    )


# ── the "vs home" comparison ─────────────────────────────────────────────────

RUNGS = (
    ("same_weekday_hour", "same weekday and hour"),
    ("same_hour", "same hour of day"),
    ("any_time", "any time"),
)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local(run: PortableRun) -> datetime | None:
    """The run's wall clock on the *device* (each run carries its own UTC offset), which is
    what "same hour" has to mean for a phone that crosses time zones."""
    dt = _as_utc(run.created_at)
    if dt is None:
        return None
    return dt + timedelta(minutes=int(run.tz_offset_minutes or 0))


def _matches_rung(rung: str, a: datetime | None, b: datetime | None) -> bool:
    if rung == "any_time":
        return True
    if a is None or b is None:
        return False
    if rung == "same_hour":
        return a.hour == b.hour
    return a.hour == b.hour and a.weekday() == b.weekday()


def _quartiles(vals: list[float]) -> tuple[float | None, float | None]:
    if not vals:
        return None, None
    s = sorted(vals)
    n = len(s)

    def q(p: float) -> float:
        pos = p * (n - 1)
        lo, hi = int(pos), min(int(pos) + 1, n - 1)
        return s[lo] + (s[hi] - s[lo]) * (pos - lo)

    return round(q(0.25), 3), round(q(0.75), 3)


def home_candidates(session, run: PortableRun) -> list[PortableRun]:
    """Every stored home run that matches the run's hard stamps: same device, same
    instrument version, marked home, not the run itself. Profile and time are chosen
    among these by :func:`compare`."""
    if not run.device_id:
        return []
    rows = session.scalars(
        select(PortableRun)
        .where(
            PortableRun.device_id == run.device_id,
            PortableRun.instrument_version == run.instrument_version,
            PortableRun.is_home.is_(True),
            PortableRun.id != run.id,
        )
        .order_by(PortableRun.created_at.desc())
    ).all()
    return list(rows)


def _pick_profile(candidates: list[PortableRun], min_runs: int, crown_fp: str | None) -> str | None:
    """One home profile for the reference: the pooled crown when it has enough home runs
    on this device, else the profile with the most. ``None`` when no profile reaches the
    bar (then unstamped/mixed home runs are used and the readout says so)."""
    counts: dict[str, int] = {}
    for r in candidates:
        if r.settings_fingerprint:
            counts[r.settings_fingerprint] = counts.get(r.settings_fingerprint, 0) + 1
    if crown_fp and counts.get(crown_fp, 0) >= min_runs:
        return crown_fp
    best = max(counts.items(), key=lambda kv: kv[1], default=None)
    if best and best[1] >= min_runs:
        return best[0]
    return None


# The device id PathBrain files its own plugin's readings under. A *different device* from
# any phone (wired, always Chromium), so it is never pooled with a phone's home runs — it
# is the second reference, shown beside the same-device one and labelled.
SERVER_DEVICE_ID = "pathbrain-server"
SERVER_DEVICE_LABEL = "PathBrain (wired)"
SERVER_SAMPLE_LIMIT = 500
SERVER_NOTE = (
    "measured by PathBrain itself on its wired connection — expect a few ms better round trip "
    "and jitter than a Wi-Fi device sees at home, so read small differences with that in mind"
)


def record_server_run(session, run, results: list) -> PortableRun | None:
    """File a completed run's ``portable`` plugin readings as ONE home sample from
    ``SERVER_DEVICE_ID`` (called by the runner after the result rows are added; same
    session, caller commits). The raw stays in the run's ``BenchmarkResult`` — the row
    points at it (``source_run_id``) rather than copying it. Every iteration must carry the
    same instrument version, else nothing is filed (a recipe change mid-run). Returns the
    row, or None when there was nothing usable."""
    iterations = []
    versions: set[str] = set()
    client: dict = {}
    for r in results or []:
        raw = getattr(r, "raw", None)
        if not getattr(r, "success", False) or not isinstance(raw, dict):
            continue
        it = raw.get("iteration")
        if isinstance(it, dict) and it.get("waterfall"):
            iterations.append(it)
            versions.add(str(raw.get("instrument_version") or ""))
            client = client or (raw.get("client") or {})
    if not iterations:
        return None
    if len(versions) != 1 or not next(iter(versions)):
        log.warning("Run %s: portable iterations span instrument versions %s; not filed", run.id, sorted(versions))
        return None
    derived = derive_portable({"iterations": iterations})
    score, subscores = score_metrics(derived["metrics"])
    summary = None
    if run.settings:
        try:
            from .settings_profile import summarize

            summary = summarize(run.settings)
        except Exception:  # noqa: BLE001 — cosmetic
            summary = None
    local = datetime.now().astimezone().utcoffset()
    row = PortableRun(
        created_at=run.created_at,
        device_id=SERVER_DEVICE_ID,
        device_label=SERVER_DEVICE_LABEL,
        venue=None,
        is_home=True,
        home_detection="server",
        egress_ip=None,
        home_ip=None,
        instrument_version=next(iter(versions)),
        client=client,
        tz_offset_minutes=int(local.total_seconds() // 60) if local is not None else None,
        settings_fingerprint=run.settings_fingerprint,
        settings_summary=summary,
        raw=None,
        source_run_id=run.id,
        metrics=derived["metrics"],
        per_origin=derived["per_origin"],
        coverage=derived["coverage"],
        score=score,
        subscores=subscores,
        notes=f"filed from run #{run.id}",
    )
    session.add(row)
    return row


def server_candidates(session, run: PortableRun) -> list[PortableRun]:
    """PathBrain's own recent readings on this instrument version (newest first, capped)."""
    rows = session.scalars(
        select(PortableRun)
        .where(
            PortableRun.device_id == SERVER_DEVICE_ID,
            PortableRun.instrument_version == run.instrument_version,
            PortableRun.is_home.is_(True),
            PortableRun.id != run.id,
        )
        .order_by(PortableRun.created_at.desc())
        .limit(SERVER_SAMPLE_LIMIT)
    ).all()
    return list(rows)


def _load_raws(session, rows: list[PortableRun]) -> dict[int, dict]:
    """Each row's raw document: its own, or — for a server sample — rebuilt from the source
    run's ``BenchmarkResult`` (one batched query)."""
    out: dict[int, dict] = {}
    by_source: dict[int, list[PortableRun]] = {}
    for r in rows:
        if r.raw:
            out[r.id] = r.raw
        elif r.source_run_id:
            by_source.setdefault(int(r.source_run_id), []).append(r)
    if by_source:
        ids = list(by_source)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for run_id, raw in session.execute(
                select(BenchmarkResult.run_id, BenchmarkResult.raw)
                .where(BenchmarkResult.run_id.in_(chunk), BenchmarkResult.plugin == "portable")
            ).all():
                its = [it["iteration"] for it in stored_iterations(raw) if isinstance(it.get("iteration"), dict)]
                for r in by_source.get(int(run_id), []):
                    out[r.id] = {"iterations": its}
    return out


def _reference(
    session,
    run: PortableRun,
    candidates: list[PortableRun],
    *,
    min_runs: int,
    crown_fingerprint: str | None,
    kind: str,
) -> dict:
    """One "vs home" block against one pool of home samples (``kind`` = ``device`` — the same
    device's own home runs — or ``server`` — PathBrain's wired readings)."""
    prov: dict = {
        "reference": kind,
        "reference_label": "this device at home" if kind == "device" else SERVER_DEVICE_LABEL,
        "device_id": run.device_id if kind == "device" else SERVER_DEVICE_ID,
        "instrument_version": run.instrument_version,
        "min_home_runs": min_runs,
        "home_runs_on_device": len(candidates),
    }
    if kind == "server":
        prov["note"] = SERVER_NOTE
    if len(candidates) < min_runs:
        who = "from this device" if kind == "device" else "by PathBrain itself"
        return {
            "available": False,
            "reason": (
                f"only {len(candidates)} comparable home run(s) {who} on this instrument version; "
                f"{min_runs} needed."
                + (" Run the test at home a few more times." if kind == "device" else
                   " They accrue with every monitoring run once the portable plugin is on.")
            ),
            "provenance": prov,
        }

    profile_fp = _pick_profile(candidates, min_runs, crown_fingerprint)
    pool = [r for r in candidates if r.settings_fingerprint == profile_fp] if profile_fp else candidates
    prov["profile"] = None
    if profile_fp:
        sample = next(r for r in pool if r.settings_fingerprint == profile_fp)
        prov["profile"] = {"fingerprint": profile_fp, "summary": sample.settings_summary}
    else:
        prov["profile_note"] = "home runs pooled across profiles (no single profile has enough)"

    here = _local(run)
    rung_key, rung_label, cell = RUNGS[-1][0], RUNGS[-1][1], pool
    for key, label in RUNGS:
        chosen = [r for r in pool if _matches_rung(key, here, _local(r))]
        if len(chosen) >= min_runs:
            rung_key, rung_label, cell = key, label, chosen
            break
    prov["time_rung"] = rung_key
    prov["time_rung_label"] = rung_label

    away_ok = set((run.coverage or {}).get("resources_ok") or [])
    tally: dict[str, int] = {}
    for r in cell:
        for rid in (r.coverage or {}).get("resources_ok") or []:
            tally[rid] = tally.get(rid, 0) + 1
    common = {rid for rid, n in tally.items() if n >= 0.8 * len(cell)} & away_ok
    kept = [r for r in cell if common <= set((r.coverage or {}).get("resources_ok") or [])]
    dropped_resources = sorted(set(tally) | set((run.coverage or {}).get("resources_failed") or {})) if common else []
    dropped_resources = [rid for rid in dropped_resources if rid not in common]
    prov["common_resources"] = sorted(common)
    prov["dropped_resources"] = dropped_resources
    prov["home_runs_used"] = len(kept)
    prov["home_runs_dropped"] = len(cell) - len(kept)
    if not common or len(kept) < min_runs:
        return {
            "available": False,
            "reason": "the run and the home runs share too few completed resources to compare",
            "provenance": prov,
        }

    raws = _load_raws(session, [run, *kept])
    away_raw = raws.get(run.id)
    if not away_raw:
        return {"available": False, "reason": "this run's raw observations are no longer available", "provenance": prov}
    kept = [r for r in kept if raws.get(r.id)]
    prov["home_runs_used"] = len(kept)
    if len(kept) < min_runs:
        return {"available": False, "reason": "too few home runs still have their raw observations", "provenance": prov}
    away = derive_portable(away_raw, include_ids=common)
    homes = [derive_portable(raws[r.id], include_ids=common) for r in kept]

    def _delta(a: float | None, vals: list[float], lower_better: bool) -> dict | None:
        vals = [v for v in vals if v is not None]
        if a is None or not vals:
            return None
        med = round(median(vals), 3)
        p25, p75 = _quartiles(vals)
        delta = round(a - med, 3)
        pct = round(delta / med * 100.0, 1) if med else None
        # The verdict is read against home's OWN run-to-run spread (its IQR), not against
        # the median alone: inside the band is "within" what home itself does from run to
        # run; only a value past the band's bad edge is "worse", past its good edge "better".
        if lower_better:
            verdict = "worse" if (p75 is not None and a > p75) else ("better" if (p25 is not None and a < p25) else "within")
        else:
            verdict = "worse" if (p25 is not None and a < p25) else ("better" if (p75 is not None and a > p75) else "within")
        return {
            "away": a, "home_median": med, "home_p25": p25, "home_p75": p75,
            "delta": delta, "pct": pct, "n": len(vals),
            "lower_is_better": lower_better,
            "verdict": verdict,
        }

    metrics_out: dict[str, dict] = {}
    for key, (_label, _unit, lower_better) in PORTABLE_METRICS.items():
        d = _delta(away["metrics"].get(key), [h["metrics"].get(key) for h in homes], lower_better)
        if d is not None:
            metrics_out[key] = d

    per_origin_out: dict[str, dict] = {}
    for origin, phases in away["per_origin"].items():
        slot = {}
        for phase, val in phases.items():
            d = _delta(val, [h["per_origin"].get(origin, {}).get(phase) for h in homes], True)
            if d is not None:
                slot[phase] = d
        if slot:
            per_origin_out[origin] = slot

    away_score, _ = score_metrics(away["metrics"])
    home_scores = [sc for sc, _ in (score_metrics(h["metrics"]) for h in homes) if sc is not None]
    score_block = None
    if away_score is not None and home_scores:
        hm = round(median(home_scores), 1)
        p25, p75 = _quartiles(home_scores)
        score_block = {
            "away": away_score, "home_median": hm, "home_p25": p25, "home_p75": p75,
            "delta": round(away_score - hm, 1), "n": len(home_scores),
        }
    return {
        "available": True,
        "reason": None,
        "provenance": prov,
        "metrics": metrics_out,
        "per_origin": per_origin_out,
        "score": score_block,
    }


def compare(session, run: PortableRun, cfg: dict | None, *, crown_fingerprint: str | None = None) -> dict:
    """The "vs home" block for one run — **two references, never merged**.

    ``references.device`` compares against the same device's own home runs (the reference
    that removes the device difference); ``references.server`` against PathBrain's own
    wired readings (``SERVER_DEVICE_ID`` — always accruing, per profile and hour, but a
    different device, and labelled so). ``headline`` names the one the top-level fields
    mirror: the device reference when it is available, else the server one. Each block is
    ``{"available", "reason", "provenance", "metrics", "per_origin", "score"}`` — metrics as
    ``{key: {"away", "home_median", "home_p25", "home_p75", "delta", "pct", "n",
    "lower_is_better", "verdict"}}``. A home run is compared against the *other* home
    runs the same way — "is home itself where it was?". A server run compares only to the
    other server runs (its ``device`` reference)."""
    pc = portable_config(cfg)
    min_runs = int(pc.get("min_home_runs") or 5)
    device = _reference(session, run, home_candidates(session, run), min_runs=min_runs,
                        crown_fingerprint=crown_fingerprint, kind="device")
    server = None
    if run.device_id != SERVER_DEVICE_ID:
        server = _reference(session, run, server_candidates(session, run), min_runs=min_runs,
                            crown_fingerprint=crown_fingerprint, kind="server")
    headline = "device" if device["available"] else ("server" if server and server["available"] else None)
    top = server if headline == "server" else device
    return {**top, "headline": headline, "references": {"device": device, "server": server}}


# ── listing ──────────────────────────────────────────────────────────────────


def serialize_run(run: PortableRun, *, include_raw: bool = False) -> dict:
    created = _as_utc(run.created_at)
    out = {
        "id": run.id,
        "created_at": created.isoformat() if created else None,
        "device_id": run.device_id,
        "device_label": run.device_label,
        "venue": run.venue,
        "is_home": bool(run.is_home),
        "home_detection": run.home_detection,
        "egress_ip": run.egress_ip,
        "home_ip": run.home_ip,
        "instrument_version": run.instrument_version,
        "client": run.client or {},
        "tz_offset_minutes": run.tz_offset_minutes,
        "settings_fingerprint": run.settings_fingerprint,
        "settings_summary": run.settings_summary,
        "metrics": run.metrics or {},
        "per_origin": run.per_origin or {},
        "coverage": run.coverage or {},
        "score": run.score,
        "subscores": run.subscores or {},
        "notes": run.notes,
        "source_run_id": run.source_run_id,
        "iterations": iteration_count(run.raw) or int((run.coverage or {}).get("iterations") or 0),
    }
    if include_raw:
        out["raw"] = run.raw
    return out


def devices(session) -> list[dict]:
    """Every device that has uploaded a run: id, label (newest non-empty), home/away counts,
    last seen."""
    totals = session.execute(
        select(PortableRun.device_id, func.count(PortableRun.id), func.max(PortableRun.created_at))
        .group_by(PortableRun.device_id)
    ).all()
    homes = dict(
        session.execute(
            select(PortableRun.device_id, func.count(PortableRun.id))
            .where(PortableRun.is_home.is_(True))
            .group_by(PortableRun.device_id)
        ).all()
    )
    out = []
    for device_id, total, last in totals:
        label = session.scalars(
            select(PortableRun.device_label)
            .where(PortableRun.device_id == device_id, PortableRun.device_label.is_not(None))
            .order_by(PortableRun.id.desc())
            .limit(1)
        ).first()
        last_utc = _as_utc(last)
        home = int(homes.get(device_id, 0) or 0)
        out.append({
            "device_id": device_id,
            "label": label,
            "runs": int(total or 0),
            "home_runs": home,
            "away_runs": int(total or 0) - home,
            "last_seen": last_utc.isoformat() if last_utc else None,
        })
    out.sort(key=lambda d: d["last_seen"] or "", reverse=True)
    return out


def metric_catalog() -> list[dict]:
    return [
        {"key": k, "label": label, "unit": unit, "lower_is_better": lower, "scored": k in PORTABLE_RUBRIC}
        for k, (label, unit, lower) in PORTABLE_METRICS.items()
    ]
