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
import json
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
from .models import PortableRun
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


def build_run(payload: dict, current_version: str) -> PortableRun:
    """Derive + score an uploaded raw document into a ``PortableRun`` row (not added to a
    session). Raises ``ValueError`` when the upload's instrument version isn't the current
    recipe's — a stale page must not file runs that nothing can compare against."""
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
    is_home = bool(payload.get("is_home"))
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


def compare(session, run: PortableRun, cfg: dict | None, *, crown_fingerprint: str | None = None) -> dict:
    """The "vs home" block for one run.

    Returns ``{"available": bool, "reason": str | None, "provenance": {...},
    "metrics": {key: {"away", "home_median", "home_p25", "home_p75", "delta", "pct",
    "lower_is_better", "n"}}, "score": {"away", "home_median", "delta"},
    "per_origin": {origin: {phase: {...same shape...}}}}``. A home run (``is_home``) is
    compared against the *other* home runs the same way — "is home itself where it was?".
    """
    pc = portable_config(cfg)
    min_runs = int(pc.get("min_home_runs") or 5)
    candidates = home_candidates(session, run)
    prov: dict = {
        "device_id": run.device_id,
        "instrument_version": run.instrument_version,
        "min_home_runs": min_runs,
        "home_runs_on_device": len(candidates),
    }
    if len(candidates) < min_runs:
        return {
            "available": False,
            "reason": (
                f"only {len(candidates)} comparable home run(s) from this device on this "
                f"instrument version; {min_runs} needed. Run the test at home a few more times."
            ),
            "provenance": prov,
        }

    # Profile: one named home profile when it has enough runs; otherwise every home run.
    profile_fp = _pick_profile(candidates, min_runs, crown_fingerprint)
    pool = [r for r in candidates if r.settings_fingerprint == profile_fp] if profile_fp else candidates
    prov["profile"] = None
    if profile_fp:
        sample = next(r for r in pool if r.settings_fingerprint == profile_fp)
        prov["profile"] = {"fingerprint": profile_fp, "summary": sample.settings_summary}
    else:
        prov["profile_note"] = "home runs pooled across profiles (no single profile has enough)"

    # Time cell: the first rung with enough runs.
    here = _local(run)
    rung_key, rung_label, cell = RUNGS[-1][0], RUNGS[-1][1], pool
    for key, label in RUNGS:
        chosen = [r for r in pool if _matches_rung(key, here, _local(r))]
        if len(chosen) >= min_runs:
            rung_key, rung_label, cell = key, label, chosen
            break
    prov["time_rung"] = rung_key
    prov["time_rung_label"] = rung_label

    # Pairwise coverage: resources the away run completed AND ≥80% of the home cell did.
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
            "reason": "the away run and the home runs share too few completed resources to compare",
            "provenance": prov,
        }

    # Re-derive both sides over the common resource set — from raw, so the restriction is
    # real (a stored aggregate can't be un-averaged).
    away = derive_portable(run.raw, include_ids=common)
    homes = [derive_portable(r.raw, include_ids=common) for r in kept]

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
    home_scores = [s for s, _ in (score_metrics(h["metrics"]) for h in homes) if s is not None]
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
        "iterations": iteration_count(run.raw),
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
