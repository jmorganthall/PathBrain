"""Was the MACHINE healthy when this run was measured? — the instrument's own gate.

Every other comparability check asks whether a run measured *the same thing*: the same
sites, the same browser client, every declared page loaded, the metrics the crown needs.
None of them asks whether the thing doing the measuring was working, and that is a real
failure mode with a real cost. When the host degrades — leaked Chromium, a swapping NAS,
a session wedged against the pipeline — the browser itself gets slower, and that lands
**inside FCP and LCP through the render phase**. The link was fine; the numbers are worse;
the profile that happened to be on the firewall wears the penalty in its pooled median
forever, because the crown pools all history with no window and no weighting.

Reported as *"profiles showing bad results that weren't actually bad"* after a stretch of
NAS trouble. The runs are on the record and they count. This module is the architectural
answer: the instrument's health is **part of comparability**, so a run measured on a sick
machine is quarantined by the same one predicate every scored view already filters
through — the crown, the standings, the rollup, Explore, the weather cohorts.

**What it reads.** Only quantities the *shaper cannot move*, so a slow reading is the
machine and never the link (:data:`HOST_QUANTITIES`):

* the browser plugin's own host-side phases — context setup, context close, the timing
  reads — which touch no network at all, and
* ``nav_render_ms``, the ledger's client-role metric: render to first paint, the term
  that sits inside the crown's own FCP and LCP.

**How it grades.** Not a percentile of the field. The whole problem is that the field's
recent history *is* the contaminated stretch, so ranking a bad run against mostly-bad runs
calls it normal. Instead each quantity is read as a **ratio against a robust baseline**
(:data:`BASELINE_PERCENTILE`, the 25th percentile of recent history) — "what this machine
does when it is well". A p25 is unmoved until more than three quarters of history is bad,
where a median gives up at half; and a ratio is absolute, so the verdict does not drift as
the field grows. The run's reading is the **median ratio** across the quantities it has,
so one noisy phase cannot condemn a run and one flattering one cannot rescue it.

**What it decides.** ``healthy`` / ``strained`` / ``degraded`` against two configured
ratios. Only ``degraded`` quarantines. ``strained`` is recorded and shown and changes
nothing — the same flag-and-steer discipline the weather stamp follows, because a run
that was a little slow is evidence with a caveat, not a broken measurement.

**Why the record heals.** The readings were always being written (the browser has timed
its phases per iteration since the drift audit shipped), so a run's health is derivable
from what is already on disk. It is computed at capture and stamped on ``Run
.instrument_health``; a run without the stamp has it computed and **written back** the
first time it is graded — so one re-grade under the current methodology retroactively
stamps and re-quarantines the bad stretch. Nothing needs re-measuring.

Best-effort by rule: an unreadable baseline, a run with too few host readings, or the
gate switched off all degrade to "no opinion" (``None``), never to a quarantine. A guess
about the machine must never cost a real measurement.
"""
from __future__ import annotations

from statistics import median

from .logging_config import get_logger

log = get_logger(__name__)

#: The host-side quantities, each mapped to where it is stored on a run's **browser**
#: result. ``phase`` reads ``details["phases"][key]``; ``metric`` reads ``metrics[key]``.
#: Every one is chosen because the shaper cannot move it: the phases touch no network,
#: and ``nav_render_ms`` is the ledger's client role — shaping-immune by construction,
#: and the one that lands inside the crown's FCP and LCP.
HOST_QUANTITIES: dict[str, dict] = {
    "context_ms": {"where": "phase", "label": "Context setup"},
    "close_ms": {"where": "phase", "label": "Context close"},
    "reads_ms": {"where": "phase", "label": "Timing reads + interaction"},
    "nav_render_ms": {"where": "metric", "label": "Render to first paint"},
}

#: The percentile of recent history that stands for "this machine, healthy". Deliberately
#: low: the distribution being measured against is the one that may be contaminated, and a
#: p25 is unmoved until more than three quarters of it is bad.
BASELINE_PERCENTILE = 25.0

#: A quantity's baseline needs at least this many readings before it is believed. Below it
#: the quantity is dropped rather than given a made-up reference.
MIN_BASELINE_READINGS = 20

#: A reading at or below this many ms is at the floor of what can be timed — a 3 ms close
#: against a 1 ms baseline is a 3× ratio and means nothing. Quantities whose baseline sits
#: under the floor are dropped from the comparison entirely.
FLOOR_MS = 25.0

HEALTHY, STRAINED, DEGRADED = "healthy", "strained", "degraded"


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolated percentile of a sorted list (the ``_overall_se`` convention)."""
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = (pct / 100.0) * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def readings_from_parts(phases: dict | None, metrics: dict | None) -> dict[str, float]:
    """The host-side readings of one run, from its browser result's parts. Pure.

    Split from the ORM so the same extraction serves capture (live ``PluginResult``
    aggregates), re-grade (stored rows) and the audit (scalars pulled in SQL) — one
    definition of what "the host's readings" are, rather than three that agree until
    one is edited.
    """
    out: dict[str, float] = {}
    for key, spec in HOST_QUANTITIES.items():
        src = phases if spec["where"] == "phase" else metrics
        value = (src or {}).get(key)
        if isinstance(value, (int, float)) and value > 0:
            out[key] = float(value)
    return out


def readings_from_results(results) -> dict[str, float]:
    """The host-side readings from a run's stored ``BenchmarkResult`` rows (browser only)."""
    for res in results or []:
        if getattr(res, "plugin", None) != "browser":
            continue
        details = getattr(res, "details", None) or {}
        return readings_from_parts(details.get("phases"), getattr(res, "metrics", None))
    return {}


class Baseline:
    """What this machine does when it is well — one robust reference per quantity.

    Frozen, so a pass grades many runs against one reference rather than re-deriving a
    moving target per run (and so the same reference is auditable: every verdict says
    which numbers it was measured against).
    """

    __slots__ = ("values", "samples")

    def __init__(self, values: dict[str, float], samples: int = 0) -> None:
        # A baseline at the timing floor is not a reference, it is rounding noise: a
        # ratio against 2 ms says nothing about the host. Dropped rather than trusted.
        self.values = {k: v for k, v in values.items() if v >= FLOOR_MS}
        self.samples = samples

    def __len__(self) -> int:
        return len(self.values)

    def ratios(self, readings: dict[str, float]) -> dict[str, float]:
        """Per-quantity ``reading / healthy baseline`` for the quantities both have."""
        return {
            k: round(readings[k] / base, 3)
            for k, base in self.values.items()
            if k in readings and base > 0
        }


def build_baseline(samples: list[dict[str, float]]) -> Baseline:
    """A :class:`Baseline` from a set of runs' host readings (see :data:`BASELINE_PERCENTILE`)."""
    dists: dict[str, list[float]] = {}
    for reading in samples:
        for key, value in reading.items():
            dists.setdefault(key, []).append(value)
    values = {
        key: _percentile(sorted(vals), BASELINE_PERCENTILE)
        for key, vals in dists.items()
        if len(vals) >= MIN_BASELINE_READINGS
    }
    return Baseline(values, samples=len(samples))


def assess(
    readings: dict[str, float],
    baseline: Baseline,
    *,
    strained_ratio: float,
    degraded_ratio: float,
    min_quantities: int,
) -> dict | None:
    """One run's health: ``{verdict, ratio, ratios, readings, baseline, quantities}``.

    ``None`` — no opinion — when the baseline is unusable or the run carries fewer than
    ``min_quantities`` comparable readings. A missing opinion never quarantines: the gate
    is here to remove measurements taken on a machine we can *show* was sick, and "we
    could not tell" is not that.
    """
    ratios = baseline.ratios(readings)
    if len(ratios) < max(1, int(min_quantities)):
        return None
    ratio = round(median(ratios.values()), 3)
    if ratio >= degraded_ratio:
        verdict = DEGRADED
    elif ratio >= strained_ratio:
        verdict = STRAINED
    else:
        verdict = HEALTHY
    return {
        "verdict": verdict,
        "ratio": ratio,
        "ratios": ratios,
        "readings": {k: round(v, 1) for k, v in readings.items()},
        "baseline": {k: round(v, 1) for k, v in baseline.values.items() if k in ratios},
        "quantities": len(ratios),
        "baseline_samples": baseline.samples,
    }


def explain(health: dict | None) -> str | None:
    """The verdict as a sentence with its numbers in it, for the run's own page."""
    if not health:
        return None
    ratio = health.get("ratio")
    worst = max(health.get("ratios") or {}, key=lambda k: health["ratios"][k], default=None)
    verdict = health.get("verdict")
    if worst is None or ratio is None:
        return None
    label = (HOST_QUANTITIES.get(worst) or {}).get("label", worst)
    got = (health.get("readings") or {}).get(worst)
    base = (health.get("baseline") or {}).get(worst)
    detail = (
        f"{label.lower()} took {got:.0f} ms against {base:.0f} ms when this machine is well"
        if got is not None and base is not None
        else f"{label.lower()} ran {health['ratios'][worst]:.1f}× its healthy time"
    )
    if verdict == DEGRADED:
        return (
            f"The machine was {ratio:.1f}× slower than healthy on its own host-side work "
            f"({detail}), so this run's render-bound metrics measure the host rather than "
            "the link. Quarantined: not counted toward any profile's standing."
        )
    if verdict == STRAINED:
        return (
            f"The machine ran {ratio:.1f}× its healthy host-side time ({detail}) — enough "
            "to note, not enough to discard. This run still counts."
        )
    return f"The machine was healthy ({ratio:.1f}× its own baseline)."


# ── Reading history: the baseline, memoized on a cheap stamp ──────────────────

#: How many recent runs the baseline is built from. Bounded by the *question* ("what does
#: this machine do lately?"), never by all of time — the rule `profile_aggregates` states.
DEFAULT_BASELINE_RUNS = 400

_CACHE: dict = {"stamp": None, "baseline": None}


def config_block(config: dict | None) -> dict:
    """The ``instrument`` config with its defaults applied."""
    block = ((config or {}).get("instrument") or {})
    return {
        "enabled": bool(block.get("enabled", True)),
        "strained_ratio": float(block.get("strained_ratio", 1.8) or 1.8),
        "degraded_ratio": float(block.get("degraded_ratio", 3.0) or 3.0),
        "min_quantities": int(block.get("min_quantities", 2) or 2),
        "baseline_runs": int(block.get("baseline_runs", DEFAULT_BASELINE_RUNS) or DEFAULT_BASELINE_RUNS),
    }


def _sample_history(session, limit: int) -> list[dict[str, float]]:
    """The newest ``limit`` browser results' host readings, as scalars.

    Read through JSON paths rather than by loading the rows: ``details`` carries the
    per-iteration metric cache and ``metrics`` the whole registry, and the baseline needs
    four numbers. Best-effort — a backend without ``json_extract`` returns nothing and the
    gate simply has no opinion.
    """
    from sqlalchemy import text

    paths = [
        (key, f"$.phases.{key}" if spec["where"] == "phase" else None)
        for key, spec in HOST_QUANTITIES.items()
    ]
    cols = ", ".join(
        f"json_extract(br.{'details' if path else 'metrics'}, "
        f"'{path or f'$.{key}'}') AS q_{key}"
        for key, path in paths
    )
    sql = text(
        f"SELECT {cols} FROM benchmark_results br "
        "JOIN runs r ON r.id = br.run_id "
        "WHERE br.plugin = 'browser' AND br.success = 1 AND r.status = 'COMPLETE' "
        "ORDER BY br.run_id DESC LIMIT :limit"
    )
    try:
        rows = session.execute(sql, {"limit": int(limit)}).all()
    except Exception:  # noqa: BLE001 — a baseline read must never break scoring
        log.debug("Instrument health: could not sample host readings", exc_info=True)
        return []
    keys = [key for key, _ in paths]
    out: list[dict[str, float]] = []
    for row in rows:
        reading = {
            key: float(value)
            for key, value in zip(keys, row)
            if isinstance(value, (int, float)) and value > 0
        }
        if reading:
            out.append(reading)
    return out


def _stamp(session) -> tuple:
    """The cheap identity of the baseline's input: newest browser result id + row count."""
    from sqlalchemy import func, select

    from .models import BenchmarkResult

    row = session.execute(
        select(func.max(BenchmarkResult.id), func.count(BenchmarkResult.id)).where(
            BenchmarkResult.plugin == "browser"
        )
    ).first()
    return tuple(row or (None, 0))


def baseline(session, *, limit: int | None = None) -> Baseline:
    """The current healthy-machine baseline, memoized on the cheap stamp of its input.

    A hit can never be stale (the stamp is the identity of what it was built from), and a
    miss costs one scalar-only query — so the gate is affordable on the re-grade's
    per-run path, which is where the record actually heals.
    """
    limit = int(limit or DEFAULT_BASELINE_RUNS)
    try:
        stamp = (*_stamp(session), limit)
    except Exception:  # noqa: BLE001
        return Baseline({})
    if _CACHE["stamp"] == stamp and _CACHE["baseline"] is not None:
        return _CACHE["baseline"]
    built = build_baseline(_sample_history(session, limit))
    _CACHE.update({"stamp": stamp, "baseline": built})
    return built


def invalidate() -> None:
    """Drop the memoized baseline (config changed, or a wholesale rewrite)."""
    _CACHE.update({"stamp": None, "baseline": None})


def assess_run(session, run, config: dict | None = None) -> dict | None:
    """One run's health, from whatever is on disk — and **stamped back onto the run**.

    This is the seam that heals the record. The readings have been written since the
    browser began timing its phases, so a run measured during a bad stretch can be graded
    now even though nothing knew to ask then: the first time it is scored, its health is
    computed and persisted, and the comparability gate quarantines it. One re-grade, and
    the whole stretch stops counting — with no re-measurement, exactly as a new crown
    metric re-derives from stored raw.

    Returns the health block (or ``None`` for no opinion). Best-effort throughout: the
    caller is the scoring path, and a verdict about the machine must never be the reason
    a run fails to score.
    """
    try:
        stored = getattr(run, "instrument_health", None)
        if isinstance(stored, dict) and stored.get("verdict"):
            return stored
        cfg = config_block(config)
        if not cfg["enabled"]:
            return None
        readings = readings_from_results(getattr(run, "results", None))
        if not readings:
            return None
        health = assess(
            readings,
            baseline(session, limit=cfg["baseline_runs"]),
            strained_ratio=cfg["strained_ratio"],
            degraded_ratio=cfg["degraded_ratio"],
            min_quantities=cfg["min_quantities"],
        )
        if health is not None:
            try:
                run.instrument_health = health
            except Exception:  # noqa: BLE001 — grading matters more than the stamp
                log.debug("Instrument health: could not stamp run %s", getattr(run, "id", "?"), exc_info=True)
        return health
    except Exception:  # noqa: BLE001
        log.debug("Instrument health: assessment failed", exc_info=True)
        return None


def is_degraded(health: dict | None) -> bool:
    return bool(health) and health.get("verdict") == DEGRADED


__all__ = [
    "BASELINE_PERCENTILE",
    "Baseline",
    "DEGRADED",
    "HEALTHY",
    "HOST_QUANTITIES",
    "STRAINED",
    "assess",
    "assess_run",
    "baseline",
    "build_baseline",
    "config_block",
    "explain",
    "invalidate",
    "is_degraded",
    "readings_from_parts",
    "readings_from_results",
]
