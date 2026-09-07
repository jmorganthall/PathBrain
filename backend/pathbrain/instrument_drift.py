"""Instrument drift: when a run gets longer, did the *measurement* get slower — and does it
move a graded number?

The Dashboard's "Avg iteration" tile is a wall clock: what one suite iteration cost, priced
from the freshest runs. When it climbs (33 s → 40 s → 49 s over a couple of weeks) it is
asking a question it cannot answer on its own, and the question has two very different
answers with opposite consequences:

* **The run got bigger.** An iteration is not a fixed unit of work. Under the
  methodology-only scope every iteration measures the browser (the 2-of-N cap is lifted),
  a duel round runs three browser iterations a side, a site publish can add pages, and a
  raised idle cap adds seconds per page. Every one of those lengthens the tile's number
  without touching a single graded value — the page's own clock is what the crown reads,
  and the page does not know how many other pages the run loaded before it.

* **The measurement got slower.** A host under pressure — leaked Chromium trees, a NAS
  swapping — makes *the browser itself* slower, and that lands inside the graded numbers:
  FCP and LCP both contain the render phase, so a slower machine grades every profile
  worse over time, and a profile measured mostly early in history beats one measured
  mostly late for no reason the network knows about. That is the "best drops to 65th over
  time" failure class, and it is the only one of the two that corrupts grading.

Telling them apart needs three clocks per run, all already on record, none of which
requires decoding a raw blob:

1. the **suite iteration** (``Run.per_iteration_ms``, the tile's number);
2. the **browser iteration** (the browser result's ``duration_ms``: goto → idle → timing
   reads, over every page) — mix-independent, since it is one browser pass whatever else
   the suite ran;
3. the **page's own clock** (``load_event_ms`` from Navigation Timing, the window every
   crown metric lives inside) — pages-independent, since it is a per-page mean.

Their differences are the parts of a run *nothing grades*: ``total_render_ms −
load_event_ms`` is the post-load idle wait, and ``browser − pages × total_render`` is the
time outside page loads (context setup, the timing reads, the synthetic INP interaction,
the close). And the ledger's **client-role** metrics — ``nav_render`` (responseEnd →
first paint: parse and layout, CPU-bound), ``inp``, ``cls`` — are shaping-immune by
construction, so they are the detector: if *those* trend upward the machine is degrading,
whatever the link is doing. The network phases (DNS/TCP/TLS/request/response) are the
control on the other side — if they moved and render did not, the link or the profile mix
changed and the instrument is fine.

Deliberately **bounded by the question**: a window of ``days`` sampled evenly to at most
``limit`` runs, read as scalars through JSON paths rather than materialized rows. A
verdict is only ever a sentence with its numbers in it; the cohort table is there so the
reader can see the same thing the sentence claims.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median

from sqlalchemy import and_, func, select

from .interpret import DERIVATION_VERSION
from .models import BenchmarkResult, Run, RunStatus, ScoreResult
from .stats import spearman

DEFAULT_DAYS = 14
MAX_DAYS = 120
DEFAULT_LIMIT = 3000
MAX_LIMIT = 20000
# Below this many runs in the window no trend is worth a sentence.
MIN_SAMPLES = 12
# |z| = |ρ|·√(n−1) at or above this ≈ two-sided p<0.05 — the same flag ``drift.py`` uses.
DRIFT_Z = 1.96
# ...and the shift between the window's first and last third must also be *material*,
# because with hundreds of runs a ρ of 0.1 is "significant" and means nothing to a grade.
MATERIAL_SHIFT_PCT = 10.0
# Windows this short or shorter are bucketed by hour rather than by day.
HOURLY_AT_OR_BELOW_DAYS = 3
_CHUNK = 500

# Every quantity trended, with the smallest absolute change that counts as a change at all
# (so a 3 ms wobble on a 20 ms render phase is not a finding) and the family the verdict
# reads it under.
QUANTITIES: dict[str, dict] = {
    "per_iteration_ms": {"label": "Suite iteration", "unit": "ms", "floor": 1000.0, "family": "wall"},
    "browser_wall_ms": {"label": "Browser iteration", "unit": "ms", "floor": 1000.0, "family": "wall"},
    "page_wall_ms": {"label": "Page, open → idle", "unit": "ms", "floor": 200.0, "family": "wall"},
    "page_clock_ms": {"label": "Page load, own clock", "unit": "ms", "floor": 100.0, "family": "page"},
    "idle_wait_ms": {"label": "Post-load idle wait", "unit": "ms", "floor": 200.0, "family": "wall"},
    "overhead_ms": {"label": "Outside page loads", "unit": "ms", "floor": 500.0, "family": "wall"},
    "nav_render_ms": {"label": "Render to first paint", "unit": "ms", "floor": 20.0, "family": "client"},
    "inp_ms": {"label": "Input delay (INP)", "unit": "ms", "floor": 20.0, "family": "client"},
    "cls": {"label": "Layout shift (CLS)", "unit": "", "floor": 0.02, "family": "client"},
    "nav_network_ms": {"label": "Network phases", "unit": "ms", "floor": 20.0, "family": "network"},
    "fcp_ms": {"label": "FCP", "unit": "ms", "floor": 20.0, "family": "crown"},
    "lcp_ms": {"label": "LCP", "unit": "ms", "floor": 20.0, "family": "crown"},
    "network_stall_all_ms": {"label": "Network stall (all)", "unit": "ms", "floor": 10.0, "family": "crown"},
    "browser_share": {"label": "Browser iterations per suite iteration", "unit": "", "floor": 0.1, "family": "mix"},
    "pages": {"label": "Pages per browser iteration", "unit": "", "floor": 0.5, "family": "mix"},
}
CLIENT_KEYS = ("nav_render_ms", "inp_ms", "cls")
CROWN_KEYS = ("fcp_ms", "lcp_ms", "network_stall_all_ms")
_NAV_NETWORK = ("nav_dns_ms", "nav_tcp_ms", "nav_tls_ms", "nav_request_ms", "nav_response_ms")


@dataclass
class RunSample:
    """One completed run's clocks and readings, as scalars."""

    id: int
    at: datetime
    kind: str
    iterations: int
    methodology_version: str | None = None
    methodology_only: bool | None = None
    per_iteration_ms: float | None = None
    browser_wall_ms: float | None = None
    browser_samples: int | None = None
    pages: int | None = None
    idle_cap_s: float | None = None
    page_wall_ms: float | None = None   # total_render_ms: goto → idle/timeout, mean over pages
    page_clock_ms: float | None = None  # load_event_ms: the page's own clock
    fcp_ms: float | None = None
    lcp_ms: float | None = None
    network_stall_all_ms: float | None = None
    nav_render_ms: float | None = None
    inp_ms: float | None = None
    cls: float | None = None
    nav_network_ms: float | None = None

    @property
    def browser_share(self) -> float | None:
        """Browser iterations per suite iteration — 1.0 when every iteration measured the crown,
        0.4 for the old 2-of-5 cap. The mix term that moves the tile's number most."""
        if self.browser_samples is None or not self.iterations:
            return None
        return round(min(1.0, self.browser_samples / self.iterations), 3)

    @property
    def idle_wait_ms(self) -> float | None:
        if self.page_wall_ms is None or self.page_clock_ms is None:
            return None
        return round(max(0.0, self.page_wall_ms - self.page_clock_ms), 3)

    @property
    def overhead_ms(self) -> float | None:
        """Browser time outside page loads: context setup, the timing reads, the INP
        interaction, the close. Needs the page count — the per-page wall is a mean."""
        if self.browser_wall_ms is None or self.page_wall_ms is None or not self.pages:
            return None
        return round(max(0.0, self.browser_wall_ms - self.pages * self.page_wall_ms), 3)

    def value(self, key: str) -> float | None:
        v = getattr(self, key, None)
        if isinstance(v, bool) or v is None:
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if math.isfinite(f) else None


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _f(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def run_kind(label: str | None, job_group: str | None) -> str:
    """Which engine produced a run, from the two fields every engine stamps."""
    g = job_group or ""
    for prefix, kind in (
        ("duel-", "duel"), ("profile_test-", "test"), ("current_test-", "current"),
        ("baseline_test-", "baseline"), ("run-series-", "manual"),
    ):
        if g.startswith(prefix):
            return kind
    lab = (label or "").lower()
    for prefix, kind in (
        ("duel", "duel"), ("race", "race"), ("refresh", "refresh"), ("scheduled", "monitoring"),
        ("test-current", "current"), ("test", "test"), ("explore", "explore"), ("apply", "apply"),
        ("baseline", "baseline"), ("sweep", "sweep"), ("exp", "experiment"),
    ):
        if lab.startswith(prefix):
            return kind
    return "manual"


# ── Trend statistic ──────────────────────────────────────────────────────────────────


def trend(points: list[tuple[float, float]], *, floor: float = 0.0) -> dict | None:
    """Does a quantity move with time across the window?

    ``points`` are ``(seconds, value)``. Returns ``{n, rho, z, early, late, delta, shift_pct,
    direction, drifts}`` — ``early``/``late`` are the medians of the first and last third of
    the window (robust to one hung run), ``drifts`` needs **both** a significant rank
    correlation and a material shift (≥ ``MATERIAL_SHIFT_PCT`` of ``early`` and ≥ ``floor``
    absolute), because on a few hundred runs a tiny ρ clears p<0.05 and says nothing a
    grade would notice. None below ``MIN_SAMPLES``.
    """
    pts = sorted((t, v) for t, v in points if v is not None and math.isfinite(v))
    n = len(pts)
    if n < MIN_SAMPLES:
        return None
    rho = spearman([p[0] for p in pts], [p[1] for p in pts])
    third = max(1, n // 3)
    early = median(v for _, v in pts[:third])
    late = median(v for _, v in pts[-third:])
    delta = late - early
    shift_pct = (delta / early * 100.0) if early else None
    z = (rho * math.sqrt(n - 1)) if rho is not None else 0.0
    material = abs(delta) >= floor and (early == 0 or abs(shift_pct or 0.0) >= MATERIAL_SHIFT_PCT)
    return {
        "n": n,
        "rho": round(rho, 3) if rho is not None else None,
        "z": round(z, 2),
        "early": round(early, 3),
        "late": round(late, 3),
        "delta": round(delta, 3),
        "shift_pct": round(shift_pct, 1) if shift_pct is not None else None,
        "direction": "up" if delta > 0 else ("down" if delta < 0 else "flat"),
        "drifts": bool(rho is not None and abs(z) >= DRIFT_Z and material),
    }


def _up(t: dict | None) -> bool:
    return bool(t and t["drifts"] and t["direction"] == "up")


def _moved(t: dict | None) -> bool:
    return bool(t and t["drifts"])


# ── Cohorts + verdict (pure) ─────────────────────────────────────────────────────────


def _bucket_key(at: datetime, bucket: str) -> str:
    return at.strftime("%Y-%m-%d %H:00") if bucket == "hour" else at.strftime("%Y-%m-%d")


def cohorts(samples: list[RunSample], *, bucket: str) -> list[dict]:
    """Per-day (or per-hour) medians of every quantity, plus the run mix that day."""
    groups: dict[str, list[RunSample]] = defaultdict(list)
    for s in samples:
        groups[_bucket_key(s.at, bucket)].append(s)
    out: list[dict] = []
    for key in sorted(groups):
        rows = groups[key]
        medians: dict[str, float | None] = {}
        for q in QUANTITIES:
            vals = [v for v in (s.value(q) for s in rows) if v is not None]
            medians[q] = round(median(vals), 3) if vals else None
        scoped = [s.methodology_only for s in rows if s.methodology_only is not None]
        out.append(
            {
                "key": key,
                "runs": len(rows),
                "iterations": sum(s.iterations for s in rows),
                "kinds": dict(Counter(s.kind for s in rows)),
                "methodology_versions": dict(Counter(s.methodology_version or "?" for s in rows)),
                "methodology_only_share": round(sum(1 for x in scoped if x) / len(scoped), 3) if scoped else None,
                "medians": medians,
            }
        )
    return out


def _fmt(key: str, v: float | None) -> str:
    if v is None:
        return "—"
    unit = QUANTITIES[key]["unit"]
    if unit == "ms":
        return f"{v / 1000:.1f} s" if abs(v) >= 1000 else f"{v:.0f} ms"
    return f"{v:.2f}" if abs(v) < 10 else f"{v:.0f}"


def _shift(key: str, t: dict) -> str:
    pct = f" ({t['shift_pct']:+.0f}%)" if t.get("shift_pct") is not None else ""
    return f"{_fmt(key, t['early'])} → {_fmt(key, t['late'])}{pct}"


def _label(key: str) -> str:
    return QUANTITIES[key]["label"]


def findings(trends: dict[str, dict | None]) -> tuple[str, bool, list[dict]]:
    """Read the trends into a verdict.

    Returns ``(verdict, grading_at_risk, findings)``. ``verdict`` is one of ``instrument``
    (the machine got slower and the crown reads it), ``unattributed`` (pages got slower by
    their own clock and neither render nor the network phases explain it), ``network``
    (the link or the profile mix changed; the instrument is fine), ``overhead`` (the browser
    spends longer outside page loads), ``idle`` (the post-load wait grew), ``mix`` (the
    suite iteration got bigger, the browser's own time didn't), ``stable``. Each finding
    is a sentence with its numbers in it, and a ``severity`` the page renders.
    """
    out: list[dict] = []
    t = trends
    client_up = [k for k in CLIENT_KEYS if _up(t.get(k))]
    crown_up = [k for k in CROWN_KEYS if _up(t.get(k))]
    clock_up = _up(t.get("page_clock_ms"))
    net_up = _up(t.get("nav_network_ms"))
    idle_up = _up(t.get("idle_wait_ms"))
    overhead_up = _up(t.get("overhead_ms"))
    wall_up = _up(t.get("browser_wall_ms"))
    page_wall_up = _up(t.get("page_wall_ms"))
    iter_up = _up(t.get("per_iteration_ms"))
    mix_moved = [k for k in ("browser_share", "pages") if _moved(t.get(k))]

    verdict = "stable"
    at_risk = False

    if client_up:
        verdict, at_risk = "instrument", True
        parts = "; ".join(f"{_label(k).lower()} {_shift(k, t[k])}" for k in client_up)
        out.append(
            {
                "key": "instrument",
                "severity": "bad",
                "text": (
                    f"The client-side readings rose: {parts}. These are shaping-immune — the network "
                    "cannot move parse, layout or input handling — so the machine running Chromium got "
                    "slower. FCP and LCP both contain the render phase, so the crown is being graded on "
                    "a slower instrument, and profiles measured mostly late in this window are "
                    "penalized for it. Check processes below for leaked browsers, then re-measure "
                    "the leaders (Re-run profiles) once the host is healthy."
                ),
            }
        )
    if clock_up or crown_up:
        crown_parts = "; ".join(f"{_label(k)} {_shift(k, t[k])}" for k in crown_up) or None
        clock_part = _shift("page_clock_ms", t["page_clock_ms"]) if clock_up else None
        if net_up and not client_up:
            if verdict == "stable":
                verdict = "network"
            out.append(
                {
                    "key": "network",
                    "severity": "info",
                    "text": (
                        f"Pages got slower by their own clock"
                        + (f" ({clock_part})" if clock_part else "")
                        + (f"; crown legs: {crown_parts}" if crown_parts else "")
                        + f", and the network phases moved with them ({_shift('nav_network_ms', t['nav_network_ms'])}) "
                        "while render did not. That is the link, the weather or which profiles were "
                        "on the firewall — a real change in what is measured, not in how. The instrument is fine."
                    ),
                }
            )
        elif not client_up:
            if verdict == "stable":
                verdict = "unattributed"
            out.append(
                {
                    "key": "unattributed",
                    "severity": "warn",
                    "text": (
                        "Pages got slower by their own clock"
                        + (f" ({clock_part})" if clock_part else "")
                        + (f"; crown legs: {crown_parts}" if crown_parts else "")
                        + ", but neither the render phase nor the network phases moved enough to explain "
                        "it. The usual cause is the pages themselves changing (heavier composition, a new "
                        "site list) — compare the collection shape on a profile's Data-integrity card."
                    ),
                }
            )
    if idle_up:
        if verdict == "stable":
            verdict = "idle"
        out.append(
            {
                "key": "idle",
                "severity": "warn",
                "text": (
                    f"The post-load idle wait grew ({_shift('idle_wait_ms', t['idle_wait_ms'])}): pages stopped "
                    "going quiet after load, or the cap was raised. It lengthens every run and can only move a "
                    "graded number when a page paints its largest element after load — the Idle-wait audit says "
                    "whether yours ever do."
                ),
            }
        )
    if overhead_up or (wall_up and not page_wall_up and not idle_up):
        if verdict == "stable":
            verdict = "overhead"
        key = "overhead_ms" if overhead_up else "browser_wall_ms"
        out.append(
            {
                "key": "overhead",
                "severity": "warn",
                "text": (
                    f"The browser spends longer outside the page loads ({_label(key).lower()} "
                    f"{_shift(key, t[key])}) while the per-page time is flat: context setup, the timing reads and "
                    "the close are what got slower. Nothing graded lives there, but it is the same machine that "
                    "renders the pages — a leaked Chromium or a swapping host shows up here first."
                ),
            }
        )
    if iter_up and not wall_up and not page_wall_up:
        if verdict == "stable":
            verdict = "mix"
        mix_parts = "; ".join(f"{_label(k).lower()} {_shift(k, t[k])}" for k in mix_moved)
        out.append(
            {
                "key": "mix",
                "severity": "ok",
                "text": (
                    f"The suite iteration got longer ({_shift('per_iteration_ms', t['per_iteration_ms'])}) while a "
                    f"browser iteration did not ({_shift('browser_wall_ms', t['browser_wall_ms']) if t.get('browser_wall_ms') else 'no change'}). "
                    + (
                        f"What changed is what an iteration contains: {mix_parts}. "
                        if mix_parts
                        else "What changed is what an iteration contains — the plugins or the caps in the run's config. "
                    )
                    + "The methodology-only scope lifts the browser's 2-of-N cap so every iteration measures the "
                    "crown, and a duel round runs three browser iterations a side. More work per iteration, the "
                    "same work per page: no graded number moved."
                ),
            }
        )
    if not out:
        out.append(
            {
                "key": "stable",
                "severity": "ok",
                "text": "Nothing trended materially with time in this window: the browser's per-page time, the "
                "client-side readings and the network phases are all where they started.",
            }
        )
    return verdict, at_risk, out


def _headline(verdict: str, at_risk: bool, runs: int) -> str:
    if runs < MIN_SAMPLES:
        return f"Only {runs} run(s) in the window — widen it; no trend is readable below {MIN_SAMPLES}."
    return {
        "instrument": "Grading is at risk: the machine got slower and the crown metrics contain it.",
        "unattributed": "Pages got slower by their own clock and nothing on record explains it; grading follows the pages.",
        "network": "The link or the profile mix changed; the instrument is consistent.",
        "overhead": "Runs got longer outside the page loads; no graded number moved — watch the host.",
        "idle": "Runs got longer in the post-load idle wait; graded numbers unaffected unless a page paints late.",
        "mix": "Runs got longer because an iteration now measures more; no graded number moved.",
        "stable": "No drift: the instrument is measuring the same way it did at the start of the window.",
    }.get(verdict, "No verdict.")


def assess(
    samples: list[RunSample],
    *,
    days: int,
    bucket: str | None = None,
    processes: dict | None = None,
    stale_derivations: int | None = None,
    now: datetime | None = None,
) -> dict:
    """The pure core over loaded samples: trends, cohorts, findings, verdict."""
    now = now or datetime.now(timezone.utc)
    bucket = bucket or ("hour" if days <= HOURLY_AT_OR_BELOW_DAYS else "day")
    ordered = sorted(samples, key=lambda s: s.at)
    epoch = ordered[0].at if ordered else now
    trends: dict[str, dict | None] = {}
    for q, spec in QUANTITIES.items():
        pts = [((s.at - epoch).total_seconds(), s.value(q)) for s in ordered]
        trends[q] = trend([(t, v) for t, v in pts if v is not None], floor=spec["floor"])
    verdict, at_risk, found = findings(trends) if len(ordered) >= MIN_SAMPLES else ("insufficient", False, [])
    procs = dict(processes or {})
    leaked = 0
    if procs.get("available"):
        # One driver is the live measurement; anything past it is an orphaned tree, and a
        # stray or a zombie is a leak whichever way you count.
        leaked = max(0, int(procs.get("drivers") or 0) - 1) + int(procs.get("stray_chrome") or 0) + int(procs.get("zombies") or 0)
        if leaked:
            found.append(
                {
                    "key": "processes",
                    "severity": "warn",
                    "text": (
                        f"Right now: {procs.get('drivers', 0)} browser driver tree(s), {procs.get('chrome', 0)} Chrome "
                        f"processes, {procs.get('zombies', 0)} zombies, {procs.get('stray_chrome', 0)} stray. "
                        "More than one driver between measurements is leaked Chromium competing with the "
                        "next measurement for the same CPU — the live cause the trends above would be showing."
                    ),
                }
            )
    if stale_derivations:
        found.append(
            {
                "key": "derivation",
                "severity": "warn",
                "text": (
                    f"{stale_derivations} run(s) in the window carry metrics derived under an older formula "
                    f"than the current {DERIVATION_VERSION}. Their stored values are not like-for-like with "
                    "the rest — re-derive history (Methodology page) before trusting a cross-window comparison."
                ),
            }
        )
    return {
        "window": {
            "days": days,
            "bucket": bucket,
            "runs": len(ordered),
            "from": ordered[0].at.isoformat() if ordered else None,
            "to": ordered[-1].at.isoformat() if ordered else None,
            "min_samples": MIN_SAMPLES,
        },
        "verdict": verdict,
        "grading_at_risk": at_risk,
        "headline": _headline(verdict, at_risk, len(ordered)),
        "findings": found,
        "trends": trends,
        "quantities": {k: {"label": v["label"], "unit": v["unit"], "family": v["family"]} for k, v in QUANTITIES.items()},
        "cohorts": cohorts(ordered, bucket=bucket),
        "processes": procs or None,
        "leaked_processes": leaked,
        "stale_derivations": stale_derivations,
        "derivation_version": DERIVATION_VERSION,
    }


# ── Loading (bounded) ────────────────────────────────────────────────────────────────


def _stride(ids: list[int], limit: int) -> list[int]:
    """Every k-th id so the sample spans the whole window evenly instead of only its end."""
    if len(ids) <= limit:
        return ids
    step = math.ceil(len(ids) / limit)
    return ids[::step]


def load_samples(session, *, days: int = DEFAULT_DAYS, limit: int = DEFAULT_LIMIT, now: datetime | None = None) -> list[RunSample]:
    """Completed runs in the window as scalar samples — JSON paths, no blob decoding."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).replace(tzinfo=None)  # stored naive-UTC
    ids = list(
        session.scalars(
            select(Run.id).where(Run.status == RunStatus.COMPLETE, Run.created_at >= since).order_by(Run.id)
        )
    )
    ids = _stride(ids, max(1, limit))
    if not ids:
        return []
    sqlite = session.get_bind().dialect.name == "sqlite"
    metrics = BenchmarkResult.metrics
    cfg = Run.config_used
    pages_col = func.json_array_length(cfg["browser"]["urls"]) if sqlite else func.null()
    browser = and_(BenchmarkResult.run_id == Run.id, BenchmarkResult.plugin == "browser")
    out: list[RunSample] = []
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i : i + _CHUNK]
        q = (
            select(
                Run.id, Run.finished_at, Run.created_at, Run.label, Run.job_group, Run.iterations_completed,
                Run.iterations, Run.per_iteration_ms, Run.methodology_version,
                cfg["measurement"]["applied"]["methodology_only"].as_boolean(),
                cfg["browser"]["networkidle_timeout_s"].as_float(),
                pages_col,
                BenchmarkResult.duration_ms,
                BenchmarkResult.details["samples"].as_integer(),
                metrics["total_render_ms"].as_float(),
                metrics["load_event_ms"].as_float(),
                metrics["fcp_ms"].as_float(),
                metrics["lcp_ms"].as_float(),
                metrics["network_stall_all_ms"].as_float(),
                metrics["nav_render_ms"].as_float(),
                metrics["inp_ms"].as_float(),
                metrics["cls"].as_float(),
                *[metrics[k].as_float() for k in _NAV_NETWORK],
            )
            .outerjoin(BenchmarkResult, browser)
            .where(Run.id.in_(chunk))
        )
        for row in session.execute(q).all():
            (
                rid, finished, created, label, group, done, planned, per_ms, mver, scoped, idle_cap, pages,
                wall, samples, page_wall, page_clock, fcp, lcp, nsa, render, inp, cls, *nav,
            ) = row
            nav_vals = [_f(v) for v in nav]
            out.append(
                RunSample(
                    id=rid,
                    at=_as_utc(finished) or _as_utc(created) or now,
                    kind=run_kind(label, group),
                    iterations=int(done or planned or 1),
                    methodology_version=mver,
                    methodology_only=bool(scoped) if scoped is not None else None,
                    per_iteration_ms=_f(per_ms),
                    browser_wall_ms=_f(wall),
                    browser_samples=int(samples) if samples is not None else None,
                    pages=int(pages) if pages else None,
                    idle_cap_s=_f(idle_cap),
                    page_wall_ms=_f(page_wall),
                    page_clock_ms=_f(page_clock),
                    fcp_ms=_f(fcp),
                    lcp_ms=_f(lcp),
                    network_stall_all_ms=_f(nsa),
                    nav_render_ms=_f(render),
                    inp_ms=_f(inp),
                    cls=_f(cls),
                    nav_network_ms=round(sum(v for v in nav_vals if v is not None), 3) if any(v is not None for v in nav_vals) else None,
                )
            )
    return out


def stale_derivation_count(session, run_ids: list[int]) -> int:
    """How many of these runs' cached metrics were derived under an older formula."""
    total = 0
    for i in range(0, len(run_ids), _CHUNK):
        chunk = run_ids[i : i + _CHUNK]
        total += int(
            session.scalar(
                select(func.count()).select_from(ScoreResult).where(
                    ScoreResult.run_id.in_(chunk),
                    ScoreResult.derivation_version.is_not(None),
                    ScoreResult.derivation_version != DERIVATION_VERSION,
                )
            )
            or 0
        )
    return total


def instrument_drift(session, *, days: int = DEFAULT_DAYS, limit: int = DEFAULT_LIMIT, now: datetime | None = None) -> dict:
    """The audit off the database: load, count the stale derivations, snapshot the host, assess."""
    from . import browser_procs

    days = max(1, min(int(days), MAX_DAYS))
    limit = max(MIN_SAMPLES, min(int(limit), MAX_LIMIT))
    samples = load_samples(session, days=days, limit=limit, now=now)
    stale = stale_derivation_count(session, [s.id for s in samples]) if samples else 0
    try:
        procs = browser_procs.snapshot()
    except Exception:  # noqa: BLE001 — the host readout is a bonus, never the failure
        procs = None
    return assess(samples, days=days, processes=procs, stale_derivations=stale, now=now)
