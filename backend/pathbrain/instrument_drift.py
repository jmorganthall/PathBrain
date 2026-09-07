"""Instrument drift: when a run gets longer, did the *measurement* get slower — and does it
move a graded number?

The Dashboard's "Avg iteration" tile is a wall clock: what one suite iteration cost, priced
from the freshest runs. When it climbs (33 s → 40 s → 49 s over a couple of weeks) it is
asking a question it cannot answer on its own, and the question has two very different
answers with opposite consequences:

* **The run got bigger.** An iteration is not a fixed unit of work. Under the
  methodology-only scope every iteration measures the browser (the 2-of-N cap is lifted),
  a duel round runs three browser iterations a side, a publish adds pages, a raised idle cap
  adds seconds per page. Every one of those lengthens the tile's number without touching a
  single graded value — the page's own clock is what the crown reads, and the page does not
  know how many other pages the run loaded before it.

* **The measurement got slower.** A host under pressure — leaked Chromium trees, a NAS
  swapping — makes *the browser itself* slower, and that lands inside the graded numbers:
  FCP and LCP both contain the render phase, so a slower machine grades every profile
  worse over time. That is the "best drops to 65th over time" failure class, and it is the
  one of the two that corrupts grading.

Telling them apart needs three clocks per run, all already on record, none of which
requires decoding a raw blob:

1. the **suite iteration** (``Run.per_iteration_ms``, the tile's number);
2. the **browser iteration** (the browser result's ``duration_ms``) — mix-independent;
3. the **page's own clock** (``load_event_ms`` from Navigation Timing, the window every
   crown metric lives inside) — pages-independent.

Their differences are the parts of a run *nothing grades*: ``total_render_ms −
load_event_ms`` is the post-load idle wait, and ``browser − pages × total_render`` is the
time outside page loads — which the browser plugin now also times **by phase** (context
setup, navigation, idle wait, timing reads, close; ``details.phases``) so that number is
attributable rather than a residual. The ledger's **client-role** metrics — ``nav_render``,
``inp``, ``cls`` — are shaping-immune by construction, so they are the detector: if *those*
trend upward the machine is degrading. The network phases are the control on the other side.

**A step is not a drift, and a first-third-versus-last-third comparison hides one.** The
audit's first reading of a real fortnight showed every page metric flat to within noise for
twelve days and then doubling in a day — the real-browser client (``--headless=new``, a
desktop user agent, 1920×1080) taking effect, so every site served the page it serves a
person — while the diluted trend read "+6%". So the audit now (a) trends **within the
methodology version currently in force** (the thing a rubric change is *supposed* to move
is not drift), (b) finds the largest **day-over-day step** in every quantity and names the
day, and (c) reads each step against what changed that day: the browser **client** each
cohort measured as (``details.client``), and whether the version that took effect
**declares** that client. That last check is the one that matters for grading: the client is
part of the methodology's collection precisely so a publish quarantines runs measured as a
different client — but a code-shipped version carries the *prior* collection forward, so a
client that changed underneath one is **pooled** with what came before, and every profile
measured under the old client holds an unearned lead. The finding says so, and what to press.

Deliberately **bounded by the question**: a window of ``days`` sampled evenly to at most
``limit`` runs, read as scalars through JSON paths. A verdict is only ever a sentence with
its numbers in it; the cohort table is there so the reader can see what the sentence claims.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
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
# Below this many runs in the window (or in the version in force) no trend is worth a sentence.
MIN_SAMPLES = 12
# |z| = |ρ|·√(n−1) at or above this ≈ two-sided p<0.05 — the same flag ``drift.py`` uses.
DRIFT_Z = 1.96
# ...and the shift between the window's first and last third must also be *material*,
# because with hundreds of runs a ρ of 0.1 is "significant" and means nothing to a grade.
MATERIAL_SHIFT_PCT = 10.0
# A day-over-day change this large (and past the quantity's floor) is a step worth naming.
STEP_PCT = 50.0
# Both cohorts on either side of a step must hold at least this many runs.
STEP_MIN_RUNS = 10
# A cohort where at least this share of runs had no successful browser iteration is flagged.
FAILED_SHARE = 0.5
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
    "idle_wait_ms": {"label": "Post-load idle wait", "unit": "ms", "floor": 200.0, "family": "wall"},
    "overhead_ms": {"label": "Outside page loads", "unit": "ms", "floor": 500.0, "family": "wall"},
    "phase_context_ms": {"label": "Context setup", "unit": "ms", "floor": 200.0, "family": "phase"},
    "phase_goto_ms": {"label": "Navigation (open → load)", "unit": "ms", "floor": 200.0, "family": "phase"},
    "phase_idle_ms": {"label": "Idle wait (measured)", "unit": "ms", "floor": 200.0, "family": "phase"},
    "phase_reads_ms": {"label": "Timing reads + interaction", "unit": "ms", "floor": 200.0, "family": "phase"},
    "phase_warm_ms": {"label": "Warm repeat load", "unit": "ms", "floor": 200.0, "family": "phase"},
    "phase_close_ms": {"label": "Context close", "unit": "ms", "floor": 200.0, "family": "phase"},
    "page_clock_ms": {"label": "Page load, own clock", "unit": "ms", "floor": 100.0, "family": "page"},
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
# A step in any of these is a step in what the crown reads — the instrument, not the schedule.
INSTRUMENT_KEYS = CLIENT_KEYS + CROWN_KEYS + ("page_clock_ms", "nav_network_ms")
PHASE_KEYS = ("context_ms", "goto_ms", "idle_ms", "reads_ms", "warm_ms", "close_ms")
_NAV_NETWORK = ("nav_dns_ms", "nav_tcp_ms", "nav_tls_ms", "nav_request_ms", "nav_response_ms")
UNRECORDED_CLIENT = "unrecorded (before the client was stamped)"


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
    # The client the pages were loaded as (``details.client`` → :func:`client_label`).
    client: str | None = None
    # The browser plugin's own per-phase totals for the iteration (``details.phases``).
    phases: dict | None = field(default=None, repr=False)

    @property
    def browser_share(self) -> float | None:
        """Browser iterations per suite iteration — 1.0 when every iteration measured the crown,
        0.4 for the old 2-of-5 cap. The mix term that moves the tile's number most."""
        if self.browser_samples is None or not self.iterations:
            return None
        return round(min(1.0, self.browser_samples / self.iterations), 3)

    @property
    def browser_failed(self) -> bool:
        """The run had a browser row and not one successful browser iteration."""
        return self.browser_samples == 0

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
        if key.startswith("phase_"):
            v = (self.phases or {}).get(key[len("phase_"):])
        else:
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


def _json(text) -> dict | None:
    if isinstance(text, dict):
        return text
    if not text:
        return None
    try:
        v = json.loads(text)
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, dict) else None


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


def client_label(client: dict | None) -> str:
    """The browser client as one readable line — headless mode, viewport, Chromium major, the
    kind of user agent, automation hiding — so two cohorts measured as different clients read
    as different at a glance. A Chromium major bump is a client change too (a base-image
    upgrade renders differently), which is why it is part of the label."""
    if not client:
        return UNRECORDED_CLIENT
    mode = client.get("headless_mode") or ("headless shell" if client.get("headless", True) else "headed")
    vp = client.get("viewport") or {}
    size = f"{vp.get('width')}×{vp.get('height')}" if vp.get("width") and vp.get("height") else "default viewport"
    ver = str(client.get("chromium_version") or "")
    major = ver.split(".")[0] if ver else "?"
    ua = str(client.get("user_agent") or "")
    ua_kind = "headless UA" if "HeadlessChrome" in ua else ("desktop UA" if ua else "default UA")
    auto = "automation hidden" if client.get("hide_automation") else "automation visible"
    return f"{mode} · {size} · Chrome {major} · {ua_kind} · {auto}"


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


# ── Cohorts, steps ───────────────────────────────────────────────────────────────────


def _bucket_key(at: datetime, bucket: str) -> str:
    return at.strftime("%Y-%m-%d %H:00") if bucket == "hour" else at.strftime("%Y-%m-%d")


def _dominant(counter: Counter) -> str | None:
    return counter.most_common(1)[0][0] if counter else None


def cohorts(samples: list[RunSample], *, bucket: str) -> list[dict]:
    """Per-day (or per-hour) medians of every quantity, plus what that cohort measured as:
    the run mix, the methodology version, the browser client, and how often the browser
    failed outright."""
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
        with_browser = [s for s in rows if s.browser_samples is not None]
        versions = Counter(s.methodology_version or "?" for s in rows)
        clients = Counter(s.client for s in with_browser if s.client)
        out.append(
            {
                "key": key,
                "runs": len(rows),
                "iterations": sum(s.iterations for s in rows),
                "kinds": dict(Counter(s.kind for s in rows)),
                "methodology_versions": dict(versions),
                "version": _dominant(versions),
                "clients": dict(clients),
                "client": _dominant(clients),
                "methodology_only_share": round(sum(1 for x in scoped if x) / len(scoped), 3) if scoped else None,
                "browser_failed_share": (
                    round(sum(1 for s in with_browser if s.browser_failed) / len(with_browser), 3) if with_browser else None
                ),
                "medians": medians,
            }
        )
    return out


def steps(cohorts_: list[dict]) -> list[dict]:
    """The largest day-over-day change in each quantity, when it is a step worth naming.

    Compares consecutive cohorts that both hold ≥ ``STEP_MIN_RUNS`` runs; a step is a change
    ≥ ``STEP_PCT`` of the earlier median and ≥ the quantity's floor. Each step says which
    day it landed on and what else changed at that boundary — the methodology version and
    the browser client — so the reader (and :func:`findings`) can tell a publish from a
    degradation."""
    out: list[dict] = []
    for q, spec in QUANTITIES.items():
        best: dict | None = None
        for prev, cur in zip(cohorts_, cohorts_[1:]):
            if prev["runs"] < STEP_MIN_RUNS or cur["runs"] < STEP_MIN_RUNS:
                continue
            a, b = prev["medians"].get(q), cur["medians"].get(q)
            if a is None or b is None:
                continue
            delta = b - a
            if abs(delta) < spec["floor"]:
                continue
            rel = (delta / a * 100.0) if a else None
            if a and abs(rel) < STEP_PCT:
                continue
            size = abs(rel) if rel is not None else float("inf")
            if best is None or size > best["_size"]:
                best = {
                    "_size": size,
                    "key": q,
                    "at": cur["key"],
                    "from_cohort": prev["key"],
                    "before": a,
                    "after": b,
                    "shift_pct": round(rel, 1) if rel is not None else None,
                    "direction": "up" if delta > 0 else "down",
                    "version_from": prev.get("version"),
                    "version_to": cur.get("version"),
                    "version_changed": prev.get("version") != cur.get("version"),
                    "client_from": prev.get("client"),
                    "client_to": cur.get("client"),
                    "client_changed": bool(prev.get("client") and cur.get("client") and prev["client"] != cur["client"]),
                }
        if best is not None:
            best.pop("_size")
            out.append(best)
    out.sort(key=lambda s: (s["at"], -(abs(s["shift_pct"]) if s["shift_pct"] is not None else float("inf"))))
    return out


# ── Findings + verdict (pure) ────────────────────────────────────────────────────────


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


def _step_text(s: dict) -> str:
    pct = f" ({s['shift_pct']:+.0f}%)" if s.get("shift_pct") is not None else ""
    return f"{_label(s['key'])} {_fmt(s['key'], s['before'])} → {_fmt(s['key'], s['after'])}{pct}"


def _label(key: str) -> str:
    return QUANTITIES[key]["label"]


def _fmt_ms(v: float) -> str:
    return f"{v / 1000:.1f} s" if abs(v) >= 1000 else f"{v:.0f} ms"


def _boundary_findings(steps_: list[dict], version_clients: dict[str, bool]) -> list[dict]:
    """One finding per day on which an instrument-sensitive quantity stepped, read against
    what changed at that boundary. The decision table, in order:

    * the **client changed** and the version that took effect **declares** a client → the
      instrument changed by design; the publish quarantines the earlier runs (info);
    * the client changed and the version declares **none** → the earlier runs are **pooled**
      with the later ones under one version, and every profile measured before that day
      holds an unearned lead: grading at risk, and the fix is one button (bad);
    * only the version changed → a new rubric took effect; whether it quarantines depends on
      what it declares (info, hedged);
    * neither → something outside the record moved the instrument that day (warn).
    """
    by_day: dict[str, list[dict]] = defaultdict(list)
    for s in steps_:
        if s["key"] in INSTRUMENT_KEYS:
            by_day[s["at"]].append(s)
    out: list[dict] = []
    for day in sorted(by_day):
        group = by_day[day]
        lead = max(group, key=lambda s: abs(s["shift_pct"]) if s["shift_pct"] is not None else float("inf"))
        moved = "; ".join(_step_text(s) for s in sorted(group, key=lambda s: -(abs(s["shift_pct"]) if s["shift_pct"] is not None else float("inf"))))
        v_from, v_to = lead.get("version_from"), lead.get("version_to")
        declares = bool(version_clients.get(v_to or "", False))
        if lead["client_changed"] and declares:
            out.append({
                "key": "published", "severity": "info", "at": day,
                "text": (
                    f"On {day} the instrument changed by design: the browser client went from "
                    f"“{lead['client_from']}” to “{lead['client_to']}” and {moved}. The version in force "
                    f"({v_to}) declares that client, so runs measured as the earlier one are quarantined "
                    "under it and the crown compares only like with like."
                ),
            })
        elif lead["client_changed"]:
            out.append({
                "key": "pooled_instruments", "severity": "bad", "at": day,
                "text": (
                    f"On {day} the browser client changed — “{lead['client_from']}” → “{lead['client_to']}” — "
                    f"and {moved}. The methodology in force ({v_to}) declares NO client, so runs measured as "
                    "two different clients are pooled under one version: every profile measured before that "
                    "day holds an unearned lead on FCP and LCP. Fix: Methodology page → “Publish sites + "
                    "client as a new version”, then re-grade; the earlier runs quarantine and the field "
                    "rebuilds from the prior version's order."
                ),
            })
        elif lead["version_changed"]:
            out.append({
                "key": "published", "severity": "info", "at": day,
                "text": (
                    f"On {day} the methodology changed ({v_from} → {v_to}) and {moved}. A version published "
                    "from the Methodology page quarantines the runs before it; a code-shipped adoption "
                    "carries the previous collection forward and does not. Check that version's “Sites "
                    "measured” card if the step is not one the rubric was meant to make."
                ),
            })
        else:
            out.append({
                "key": "unexplained_step", "severity": "warn", "at": day,
                "text": (
                    f"On {day} {moved}, with the same methodology ({v_to}) and the same recorded client. "
                    "Something outside the record moved the instrument that day — a deploy, the sites "
                    "themselves, or the host. Check what changed on that date before trusting a comparison "
                    "that spans it."
                ),
            })
    return out


def findings(
    trends: dict[str, dict | None],
    *,
    steps_: list[dict] | None = None,
    cohorts_: list[dict] | None = None,
    version_clients: dict[str, bool] | None = None,
    pages: float | None = None,
) -> tuple[str, bool, list[dict]]:
    """Read the trends and steps into a verdict.

    Returns ``(verdict, grading_at_risk, findings)``. Grading verdicts come first —
    ``instrument`` (client readings drifted up within the version in force),
    ``pooled_instruments`` (a client change with no publish), ``unexplained_step`` — then the
    measurement verdicts (``unattributed``, ``network``), then the wall-clock ones ranked by
    how many seconds they add to a browser iteration (``overhead``/``idle``/``mix``), then
    ``published`` (a step the rubric was meant to make) and ``stable``. Every finding is a
    sentence with its numbers in it; wall-clock findings also carry ``impact_ms``.
    """
    out: list[dict] = []
    t = trends
    steps_ = steps_ or []
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

    # A client-role reading that rose across a STEP is explained by the step (a publish, a
    # pooled client, a deploy) — the more specific reading, and the one that names the day.
    # A gradual host degradation shows no step, so it still reaches the `instrument` finding.
    stepped_client = any(x["key"] in CLIENT_KEYS for x in steps_)
    if client_up and not stepped_client:
        parts = "; ".join(f"{_label(k).lower()} {_shift(k, t[k])}" for k in client_up)
        out.append({
            "key": "instrument", "severity": "bad",
            "text": (
                f"The client-side readings rose within the version in force: {parts}. These are "
                "shaping-immune — the network cannot move parse, layout or input handling — so the machine "
                "running Chromium got slower. FCP and LCP both contain the render phase, so the crown is "
                "being graded on a slower instrument, and profiles measured mostly late in this window are "
                "penalized for it. Check processes below for leaked browsers, then re-measure the leaders "
                "(Re-run profiles) once the host is healthy."
            ),
        })
    out.extend(_boundary_findings(steps_, version_clients or {}))
    if (clock_up or crown_up) and not client_up and not any(x["key"] in INSTRUMENT_KEYS for x in steps_):
        crown_parts = "; ".join(f"{_label(k)} {_shift(k, t[k])}" for k in crown_up) or None
        clock_part = _shift("page_clock_ms", t["page_clock_ms"]) if clock_up else None
        if net_up:
            out.append({
                "key": "network", "severity": "info",
                "text": (
                    "Pages got slower by their own clock"
                    + (f" ({clock_part})" if clock_part else "")
                    + (f"; crown legs: {crown_parts}" if crown_parts else "")
                    + f", and the network phases moved with them ({_shift('nav_network_ms', t['nav_network_ms'])}) "
                    "while render did not. That is the link, the weather or which profiles were on the "
                    "firewall — a real change in what is measured, not in how. The instrument is fine."
                ),
            })
        else:
            out.append({
                "key": "unattributed", "severity": "warn",
                "text": (
                    "Pages got slower by their own clock"
                    + (f" ({clock_part})" if clock_part else "")
                    + (f"; crown legs: {crown_parts}" if crown_parts else "")
                    + ", but neither the render phase nor the network phases moved enough to explain it. "
                    "The usual cause is the pages themselves changing (heavier composition, a new site "
                    "list) — compare the collection shape on a profile's Data-integrity card."
                ),
            })

    # Wall-clock findings, each priced in seconds per browser iteration so the headline can
    # name the biggest contributor rather than whichever check happened to run first.
    wall: list[dict] = []
    if idle_up:
        per_page = t["idle_wait_ms"]["delta"]
        impact = per_page * (pages or 1.0)
        wall.append({
            "key": "idle", "severity": "warn", "impact_ms": round(impact, 1),
            "text": (
                f"The post-load idle wait grew ({_shift('idle_wait_ms', t['idle_wait_ms'])} per page, "
                f"≈ {_fmt_ms(impact)} per browser iteration over {pages:g} pages): pages stopped going quiet "
                "after load, or the cap was raised. It lengthens every run and can only move a graded number "
                "when a page paints its largest element after load — the Idle-wait audit says whether yours "
                "ever do, and names the smallest safe cap."
                if pages else
                f"The post-load idle wait grew ({_shift('idle_wait_ms', t['idle_wait_ms'])} per page): pages "
                "stopped going quiet after load, or the cap was raised. It lengthens every run and can only "
                "move a graded number when a page paints its largest element after load — the Idle-wait audit "
                "says whether yours ever do."
            ),
        })
    if overhead_up or (wall_up and not page_wall_up and not idle_up):
        key = "overhead_ms" if overhead_up else "browser_wall_ms"
        phase_bits = [
            f"{_label(k).lower()} {_shift(k, t[k])}"
            for k in ("phase_context_ms", "phase_goto_ms", "phase_reads_ms", "phase_close_ms")
            if _up(t.get(k))
        ]
        wall.append({
            "key": "overhead", "severity": "warn", "impact_ms": round(t[key]["delta"], 1),
            "text": (
                f"The browser spends longer outside the page loads ({_label(key).lower()} {_shift(key, t[key])}) "
                "while the per-page time is flat. "
                + (f"Measured by phase: {'; '.join(phase_bits)}. " if phase_bits else
                   "Context setup, the timing reads and the close are what got slower (runs after this "
                   "audit's phase timers landed will say which). ")
                + "Nothing graded lives there, but it is the same machine that renders the pages — a leaked "
                "Chromium or a swapping host shows up here first."
            ),
        })
    if iter_up and not wall_up and not page_wall_up:
        mix_parts = "; ".join(f"{_label(k).lower()} {_shift(k, t[k])}" for k in mix_moved)
        wall.append({
            "key": "mix", "severity": "ok", "impact_ms": round(t["per_iteration_ms"]["delta"], 1),
            "text": (
                f"The suite iteration got longer ({_shift('per_iteration_ms', t['per_iteration_ms'])}) while a "
                f"browser iteration did not ({_shift('browser_wall_ms', t['browser_wall_ms']) if t.get('browser_wall_ms') else 'no change'}). "
                + (f"What changed is what an iteration contains: {mix_parts}. " if mix_parts
                   else "What changed is what an iteration contains — the plugins or the caps in the run's config. ")
                + "The methodology-only scope lifts the browser's 2-of-N cap so every iteration measures the "
                "crown, and a duel round runs three browser iterations a side. More work per iteration, the "
                "same work per page: no graded number moved."
            ),
        })
    wall.sort(key=lambda f: -abs(f["impact_ms"]))
    out.extend(wall)

    # Days on which the browser failed outright on most runs: no crown metrics, quarantined
    # legs, and a verdict from those days resting on nothing.
    failed_days = [c for c in (cohorts_ or []) if (c.get("browser_failed_share") or 0) >= FAILED_SHARE and c["runs"] >= STEP_MIN_RUNS]
    if failed_days:
        days = ", ".join(f"{c['key']} ({c['runs']} runs, {c['browser_failed_share']:.0%})" for c in failed_days)
        out.append({
            "key": "browser_failed", "severity": "warn",
            "text": (
                f"On {days} the median run had no successful browser iteration. Those legs produced no crown "
                "metrics and were quarantined as incomparable, so nothing they measured entered the standings — "
                "but any duel verdict from those days rests on the few legs that did load. The usual cause was "
                "a wedged browser; the process readout below says whether it is still happening."
            ),
        })

    if not out:
        out.append({
            "key": "stable", "severity": "ok",
            "text": "Nothing trended materially with time in this window: the browser's per-page time, the "
            "client-side readings and the network phases are all where they started.",
        })

    order = ["instrument", "pooled_instruments", "unexplained_step", "unattributed", "network"]
    keys = [f["key"] for f in out]
    verdict = next((k for k in order if k in keys), None)
    if verdict is None:
        verdict = wall[0]["key"] if wall else ("published" if "published" in keys else ("browser_failed" if "browser_failed" in keys else "stable"))
    at_risk = verdict in {"instrument", "pooled_instruments"}
    return verdict, at_risk, out


def _headline(verdict: str, found: list[dict], scope: dict, runs: int) -> str:
    if runs < MIN_SAMPLES:
        return f"Only {runs} run(s) in the window — widen it; no trend is readable below {MIN_SAMPLES}."
    version = scope.get("methodology")
    within = f" within {version}" if scope.get("within_version") and version else ""
    wall = [f for f in found if f.get("impact_ms") is not None]
    longer = ""
    if wall:
        top = ", ".join(f"{_label({'idle': 'idle_wait_ms', 'overhead': 'overhead_ms', 'mix': 'per_iteration_ms'}[f['key']]).lower()} +{_fmt_ms(abs(f['impact_ms']))}" for f in wall[:2])
        longer = f" Runs got longer: {top} per browser iteration."
    by_key = {f["key"]: f for f in found}
    if verdict == "instrument":
        return f"Grading is at risk: the machine got slower{within} and the crown metrics contain it."
    if verdict == "pooled_instruments":
        return (
            f"Grading is at risk: the browser client changed on {by_key['pooled_instruments'].get('at')} with no "
            "methodology publish, so runs measured as two different clients are pooled under one version."
        )
    if verdict == "unexplained_step":
        return f"The instrument stepped on {by_key['unexplained_step'].get('at')} and nothing on record explains it; grading spans two instruments until that day is understood."
    if verdict == "unattributed":
        return f"Pages got slower by their own clock{within} and nothing on record explains it; grading follows the pages."
    if verdict == "network":
        return f"The link or the profile mix changed{within}; the instrument is consistent." + longer
    if verdict in {"overhead", "idle", "mix"}:
        pub = f" The instrument changed by design on {by_key['published'].get('at')}." if "published" in by_key else ""
        return f"No graded number drifted{within}.{longer}{pub}"
    if verdict == "published":
        return f"The instrument changed by design on {by_key['published'].get('at')}; nothing drifted{within} since."
    if verdict == "browser_failed":
        return "The instrument is consistent, but on some days the browser failed on most runs — see below."
    return f"No drift{within}: the instrument is measuring the same way it did at the start of the window."


def assess(
    samples: list[RunSample],
    *,
    days: int,
    bucket: str | None = None,
    processes: dict | None = None,
    stale_derivations: int | None = None,
    version_clients: dict[str, bool] | None = None,
    now: datetime | None = None,
) -> dict:
    """The pure core over loaded samples: cohorts, steps, within-version trends, findings,
    verdict. ``version_clients`` maps each methodology version to whether it declares a
    browser client (the one fact that tells a publish from a pooled instrument)."""
    now = now or datetime.now(timezone.utc)
    bucket = bucket or ("hour" if days <= HOURLY_AT_OR_BELOW_DAYS else "day")
    ordered = sorted(samples, key=lambda s: s.at)
    coh = cohorts(ordered, bucket=bucket)
    stp = steps(coh)

    # Trend within the version in force: what a rubric change is meant to move is not drift.
    current = ordered[-1].methodology_version if ordered else None
    within = [s for s in ordered if s.methodology_version == current] if current else []
    if len(within) >= MIN_SAMPLES:
        scope_samples, scope = within, {"methodology": current, "runs": len(within), "within_version": True}
    else:
        scope_samples, scope = ordered, {"methodology": current, "runs": len(ordered), "within_version": False}
    epoch = scope_samples[0].at if scope_samples else now
    trends: dict[str, dict | None] = {}
    for q, spec in QUANTITIES.items():
        pts = [((s.at - epoch).total_seconds(), s.value(q)) for s in scope_samples]
        trends[q] = trend([(t, v) for t, v in pts if v is not None], floor=spec["floor"])
    pages_vals = [v for v in (s.value("pages") for s in scope_samples) if v]
    pages = median(pages_vals) if pages_vals else None

    if len(ordered) >= MIN_SAMPLES:
        verdict, at_risk, found = findings(trends, steps_=stp, cohorts_=coh, version_clients=version_clients, pages=pages)
    else:
        verdict, at_risk, found = "insufficient", False, []
    procs = dict(processes or {})
    leaked = 0
    if procs.get("available"):
        # One driver is the live measurement; anything past it is an orphaned tree, and a
        # stray or a zombie is a leak whichever way you count.
        leaked = max(0, int(procs.get("drivers") or 0) - 1) + int(procs.get("stray_chrome") or 0) + int(procs.get("zombies") or 0)
        if leaked:
            found.append({
                "key": "processes", "severity": "warn",
                "text": (
                    f"Right now: {procs.get('drivers', 0)} browser driver tree(s), {procs.get('chrome', 0)} Chrome "
                    f"processes, {procs.get('zombies', 0)} zombies, {procs.get('stray_chrome', 0)} stray. More than "
                    "one driver between measurements is leaked Chromium competing with the next measurement for "
                    "the same CPU — the live cause the trends above would be showing."
                ),
            })
    if stale_derivations:
        found.append({
            "key": "derivation", "severity": "warn",
            "text": (
                f"{stale_derivations} run(s) in the window carry metrics derived under an older version than the "
                f"current {DERIVATION_VERSION}. If the newer version only added metrics (the usual case), nothing "
                "is wrong; if it changed a formula, re-derive history (Methodology page) before trusting a "
                "cross-window comparison."
            ),
        })
    return {
        "window": {
            "days": days,
            "bucket": bucket,
            "runs": len(ordered),
            "from": ordered[0].at.isoformat() if ordered else None,
            "to": ordered[-1].at.isoformat() if ordered else None,
            "min_samples": MIN_SAMPLES,
        },
        "scope": scope,
        "verdict": verdict,
        "grading_at_risk": at_risk,
        "headline": _headline(verdict, found, scope, len(ordered)),
        "findings": found,
        "trends": trends,
        "steps": stp,
        "quantities": {k: {"label": v["label"], "unit": v["unit"], "family": v["family"]} for k, v in QUANTITIES.items()},
        "cohorts": coh,
        "version_clients": dict(version_clients or {}),
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
    details = BenchmarkResult.details
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
                details["samples"].as_integer(),
                details["client"].as_string(),
                details["phases"].as_string(),
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
                wall, samples, client_json, phases_json, page_wall, page_clock, fcp, lcp, nsa, render, inp, cls, *nav,
            ) = row
            nav_vals = [_f(v) for v in nav]
            phases = _json(phases_json)
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
                    # A browser row with no client block is a run from before the client was
                    # stamped — a real, distinguishable client of its own for the step test.
                    client=client_label(_json(client_json)) if samples is not None else None,
                    phases={k: _f(v) for k, v in phases.items() if _f(v) is not None} if phases else None,
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


def declared_clients(session, versions: list[str]) -> dict[str, bool]:
    """Per methodology version: does its frozen definition declare a browser client? The
    one fact that tells a publish (earlier runs quarantined) from a pooled instrument."""
    from .methodology import definition_client_set
    from .models import Methodology

    out: dict[str, bool] = {}
    for v in versions:
        if not v:
            continue
        row = session.get(Methodology, v)
        out[v] = bool(row is not None and definition_client_set(row.definition or {}))
    return out


def instrument_drift(session, *, days: int = DEFAULT_DAYS, limit: int = DEFAULT_LIMIT, now: datetime | None = None) -> dict:
    """The audit off the database: load, count the stale derivations, look up what each
    version declares, snapshot the host, assess."""
    from . import browser_procs

    days = max(1, min(int(days), MAX_DAYS))
    limit = max(MIN_SAMPLES, min(int(limit), MAX_LIMIT))
    samples = load_samples(session, days=days, limit=limit, now=now)
    stale = stale_derivation_count(session, [s.id for s in samples]) if samples else 0
    versions = sorted({s.methodology_version for s in samples if s.methodology_version})
    version_clients = declared_clients(session, versions) if versions else {}
    try:
        procs = browser_procs.snapshot()
    except Exception:  # noqa: BLE001 — the host readout is a bonus, never the failure
        procs = None
    return assess(samples, days=days, processes=procs, stale_derivations=stale, version_clients=version_clients, now=now)
