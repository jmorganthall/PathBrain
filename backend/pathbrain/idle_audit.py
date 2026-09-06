"""Idle-wait audit: what the browser's post-load ``networkidle`` settle actually buys.

After every page load the browser plugin waits for the network to go idle, capped at
``browser.networkidle_timeout_s`` (5 s by default). The smoothness instrument is bounded to
``loadEventEnd`` on purpose (``interpret.smoothness.resources_within_load``), so nothing that
arrives during that wait can move longest-stall or network_stall_all. The only crown metric
the wait can still move is **LCP**, and only when a page paints its largest element *after*
the load event. So whether the cap can be shortened is an empirical question about the
sites being measured — and every stored browser raw already holds the answer: per load,
``paint.lcp`` and ``nav.loadEventEnd`` say whether LCP landed after load and by how much,
and ``total_render_ms - loadEventEnd`` says how long the wait actually lasted.

This module reads that off history (read-only, no score changes) and recommends the
smallest cap that would have caught every observed late LCP with margin. It is a
*diagnostic*: the cap is a config value the user changes deliberately, because a cap
below the observed lag would change a crown metric — and that would be a methodology
decision, not a performance one.
"""
from __future__ import annotations

import math
from statistics import mean, median

from sqlalchemy import select

from .models import BenchmarkResult, Run, RunStatus
from .raw_access import browser_url_observations

# Margin added over the worst observed late-LCP lag when recommending a cap.
SAFETY_MARGIN_MS = 500.0
MIN_CAP_S = 1.0


def _f(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _p95(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def audit_observations(observations: list[tuple[str, dict]], *, current_cap_s: float | None = None) -> dict:
    """Pure core over ``(url, per-URL browser observation)`` pairs."""
    per_site: dict[str, dict] = {}
    for url, obs in observations:
        nav = obs.get("nav") or {}
        paint = obs.get("paint") or {}
        load_end = _f(nav.get("loadEventEnd"))
        lcp = _f(paint.get("lcp"))
        render = _f(obs.get("total_render_ms"))
        site = per_site.setdefault(url, {"loads": 0, "with_lcp": 0, "lcp_after_load": 0, "lags_ms": [], "idle_waits_ms": []})
        site["loads"] += 1
        if lcp is not None and lcp > 0:
            site["with_lcp"] += 1
            if load_end is not None and load_end > 0 and lcp > load_end:
                site["lcp_after_load"] += 1
                site["lags_ms"].append(lcp - load_end)
        if render is not None and load_end is not None and load_end > 0 and render > load_end:
            site["idle_waits_ms"].append(render - load_end)

    sites = []
    worst_lag = 0.0
    idle_all: list[float] = []
    for url, d in per_site.items():
        lags = d["lags_ms"]
        max_lag = max(lags) if lags else 0.0
        worst_lag = max(worst_lag, max_lag)
        idle_all.extend(d["idle_waits_ms"])
        sites.append({
            "url": url,
            "loads": d["loads"],
            "with_lcp": d["with_lcp"],
            "lcp_after_load": d["lcp_after_load"],
            "lcp_after_load_share": round(d["lcp_after_load"] / d["loads"], 3) if d["loads"] else None,
            "max_lag_ms": round(max_lag, 1) if lags else 0.0,
            "p95_lag_ms": round(_p95(lags), 1) if lags else 0.0,
            "median_idle_wait_ms": round(median(d["idle_waits_ms"]), 1) if d["idle_waits_ms"] else None,
            "mean_idle_wait_ms": round(mean(d["idle_waits_ms"]), 1) if d["idle_waits_ms"] else None,
        })
    sites.sort(key=lambda x: x["url"])
    recommended = max(MIN_CAP_S, math.ceil((worst_lag + SAFETY_MARGIN_MS) / 1000.0)) if per_site else None
    if recommended is not None and current_cap_s is not None:
        recommended = min(recommended, float(current_cap_s))
    mean_idle = round(mean(idle_all), 1) if idle_all else None
    loads = sum(d["loads"] for d in per_site.values())
    return {
        "loads": loads,
        "sites": sites,
        "worst_lcp_after_load_ms": round(worst_lag, 1),
        "mean_idle_wait_ms": mean_idle,
        "current_networkidle_timeout_s": current_cap_s,
        "recommended_networkidle_timeout_s": recommended,
        # Rough per-load saving if the cap were lowered to the recommendation: only the
        # waits that ran past the recommended cap would shorten.
        "estimated_saving_ms_per_load": (
            round(mean([max(0.0, w - recommended * 1000.0) for w in idle_all]), 1)
            if idle_all and recommended is not None else None
        ),
        "verdict": (
            None if not per_site else
            "no load painted its largest element after the load event: the idle wait never moved LCP"
            if worst_lag == 0 else
            f"LCP landed after the load event in some loads (worst lag {round(worst_lag)} ms): keep the cap at or above the recommendation"
        ),
    }


def idle_wait_audit(session, *, limit: int = 200, current_cap_s: float | None = None) -> dict:
    """Audit the most recent ``limit`` completed runs' browser raw."""
    rows = session.execute(
        select(BenchmarkResult.run_id, BenchmarkResult.raw)
        .join(Run, Run.id == BenchmarkResult.run_id)
        .where(BenchmarkResult.plugin == "browser", BenchmarkResult.success.is_(True), Run.status == RunStatus.COMPLETE)
        .order_by(Run.id.desc())
        .limit(max(1, min(int(limit), 2000)))
    ).all()
    observations: list[tuple[str, dict]] = []
    for _run_id, raw in rows:
        for _i, url, obs in browser_url_observations(raw):
            if "nav" in obs:
                observations.append((url, obs))
    out = audit_observations(observations, current_cap_s=current_cap_s)
    out["runs"] = len(rows)
    return out
