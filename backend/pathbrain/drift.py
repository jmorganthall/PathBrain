"""Campaign drift: how much each metric trends with wall-clock time.

The four-bucket ledger predicts *which* metrics are weather-immune, and this measures
it. For every metric it takes the Spearman rank correlation between the metric's
per-run value and the run's ``created_at`` over all completed history — the same
"campaign drift" reading used to show that absolute stall metrics drift while ratio
shape statistics don't.

The reading that matters right now: **does ``jank_fraction`` (the new ratio form)
land near ρ≈0 like ``delivery_gini`` / ``cadence_cov``, while ``stall_time`` (absolute)
sits well away from 0?** If so, stall enters the crown *raw* — no weather lens needed.
If jank still carries material drift, its fixed 200 ms bar is leaking weather and we
either relativize the bar or keep a light lens. This is the receipts step before the
crown is designed around it.

``ρ`` here is magnitude-blind and scale-free, so a monotone real trend (network genuinely
changing) and pure recurring weather both show up as drift — it does not separate the
two. It answers "is this metric time-stationary?", which is the property a raw-rankable
metric needs; a drifting metric must be judged against a contemporaneous baseline.

Run it **after a re-derive**, so display-only metrics (``jank_fraction``, the nav
waterfall) are backfilled into ``BenchmarkResult.metrics`` from stored raw:

    POST /api/score/rederive     then     GET /api/metrics/drift
    # headless:  python -m pathbrain.drift
"""
from __future__ import annotations

import math
from collections import defaultdict

from sqlalchemy import select

from .database import session_scope
from .metrics import all_metric_sources, role_of
from .models import Run, RunStatus
from .stats import spearman

# Below this many (value, time) points a ρ is too noisy to read.
MIN_SAMPLES = 8
# |z| = |ρ|·√(n−1) (Fisher's large-sample approximation) above this ≈ two-sided p<0.05:
# the metric trends with time beyond chance. Used only to flag, not to over-claim a p.
DRIFT_Z = 1.96


def metric_time_drift(session, *, min_samples: int = MIN_SAMPLES) -> list[dict]:
    """Per-metric Spearman ρ of value vs ``created_at`` across completed runs.

    Returns one row per metric with enough samples, each ``{key, role, n, rho, abs_rho,
    z, drifts}`` — sorted by ``abs_rho`` descending (biggest drifters first). ``role`` is
    the ledger bucket, so you can read drift *by* bucket at a glance (ratio-S should sit
    near 0; absolute-S and the setup-N phases should not). ``drifts`` is the |z|≥1.96 flag.

    Reads per-run values from ``BenchmarkResult.metrics`` (every metric the derivation
    emits, incl. display-only ones), so re-derive first to include metrics added after a
    run was collected.
    """
    metric_src = all_metric_sources()
    runs = list(
        session.scalars(
            select(Run).where(Run.status == RunStatus.COMPLETE).order_by(Run.created_at)
        )
    )

    epoch = None
    series: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for run in runs:
        if run.created_at is None:
            continue
        if epoch is None:
            epoch = run.created_at
        # Seconds since the earliest run — a monotone time axis. Spearman only uses its
        # rank order, so the unit/scale is irrelevant; datetime subtraction avoids tz issues.
        t = (run.created_at - epoch).total_seconds()
        results_by_plugin = {r.plugin: (r.metrics or {}) for r in run.results}
        for key, (plugin, source_key) in metric_src.items():
            v = results_by_plugin.get(plugin, {}).get(source_key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                series[key].append((t, float(v)))

    out: list[dict] = []
    for key, pts in series.items():
        n = len(pts)
        if n < min_samples:
            continue
        rho = spearman([p[0] for p in pts], [p[1] for p in pts])
        if rho is None:  # constant column (a flat metric) — no trend to report
            continue
        z = rho * math.sqrt(n - 1)
        out.append(
            {
                "key": key,
                "role": role_of(key),
                "n": n,
                "rho": round(rho, 3),
                "abs_rho": round(abs(rho), 3),
                "z": round(z, 2),
                "drifts": abs(z) >= DRIFT_Z,
            }
        )
    out.sort(key=lambda d: d["abs_rho"], reverse=True)
    return out


def _print_report(rows: list[dict]) -> None:
    if not rows:
        print("No metric had enough samples. Re-derive history first (POST /api/score/rederive).")
        return
    print(f"{'metric':22s} {'role':4s} {'n':>4s} {'rho':>7s} {'|z|':>6s}  drift")
    print("-" * 52)
    for r in rows:
        flag = "DRIFTS" if r["drifts"] else "stable"
        print(f"{r['key']:22s} {r['role'] or '-':4s} {r['n']:>4d} {r['rho']:>7.3f} {abs(r['z']):>6.2f}  {flag}")
    pair = {r["key"]: r for r in rows}
    if "jank_fraction" in pair and "stall_time" in pair:
        j, s = pair["jank_fraction"], pair["stall_time"]
        print(
            f"\nstall pair — absolute stall_time ρ={s['rho']:+.3f} ({'drifts' if s['drifts'] else 'stable'}) "
            f"vs ratio jank_fraction ρ={j['rho']:+.3f} ({'drifts' if j['drifts'] else 'stable'})."
        )
        if not j["drifts"] and s["drifts"]:
            print("→ jank is time-stationary where stall_time drifts: it can be ranked raw, no weather lens.")


if __name__ == "__main__":  # headless: python -m pathbrain.drift
    with session_scope() as _s:
        _print_report(metric_time_drift(_s))
