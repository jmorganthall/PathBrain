"""Perceived-time weight calibration (§6 of the PRD).

Josh is the one perceiving "smooth vs chunky", so the perceived-time weights
should be *fit to him*, not guessed. This harness:

  1. collects a batch of loads (from stored raw observations),
  2. takes a subjective 1–10 rating per run,
  3. fits the balance knob ``w_unoccupied / w_occupied`` so ``perceivedTime`` best
     predicts the ratings (a lower perceived time should track a *higher* rating),

and prints the calibrated weight set to persist alongside records. This is exactly
what locates the knee where "smoother but slower" stops being worth it.

It's deliberately separable from the metric pipeline — an occasional offline tool,
not part of collection or scoring. Run it as a module:

    python -m pathbrain.calibration.smoothness_calibration ratings.json

where ``ratings.json`` maps run id -> subjective rating, e.g. ``{"12": 8, "15": 3}``.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass

from ..interpret.smoothness import PERCEIVED_DEFAULTS, completion_series, perceived_time

# Candidate w_unoccupied/w_occupied ratios to search (w_occupied is fixed at 1.0;
# only the ratio matters for ranking loads against each other).
DEFAULT_RATIOS = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0]


@dataclass
class Load:
    """One page load reduced to what perceived_time needs."""

    events: list[float]
    start: float
    end: float


@dataclass
class Sample:
    """A subjectively-rated run and its loads."""

    rating: float
    loads: list[Load]


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation, or None if undefined (constant input / <2 points)."""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / (sxx**0.5 * syy**0.5)


def _sample_perceived(load: Load, w_unoccupied: float, w_occupied: float) -> float | None:
    return perceived_time(
        load.events, load.start, load.end,
        slice_ms=PERCEIVED_DEFAULTS["slice_ms"],
        w_occupied=w_occupied, w_unoccupied=w_unoccupied,
    )


def fit_perceived_weights(
    samples: list[Sample],
    *,
    ratios: list[float] | None = None,
    w_occupied: float = 1.0,
) -> dict:
    """Fit the ``w_unoccupied/w_occupied`` ratio to subjective ratings.

    For each candidate ratio, computes the mean perceived time per rated run and
    correlates it against the ratings. The best ratio is the one whose perceived
    time most *negatively* correlates with rating (lower perceived ⇒ higher rating).
    Returns the best weights, the per-ratio correlation table, and the sample count.
    """
    ratios = ratios or DEFAULT_RATIOS
    table: list[dict] = []
    for ratio in ratios:
        w_un = w_occupied * ratio
        xs: list[float] = []
        ys: list[float] = []
        for s in samples:
            pts = [
                p for p in (_sample_perceived(ld, w_un, w_occupied) for ld in s.loads)
                if p is not None
            ]
            if not pts:
                continue
            xs.append(sum(pts) / len(pts))
            ys.append(s.rating)
        corr = _pearson(xs, ys)
        table.append({"ratio": ratio, "w_unoccupied": w_un, "correlation": corr, "n": len(xs)})

    scored = [r for r in table if r["correlation"] is not None]
    best = min(scored, key=lambda r: r["correlation"]) if scored else None
    return {
        "w_occupied": w_occupied,
        "best": best,
        "table": table,
        "samples": len(samples),
        "note": "Lower perceived time should track a higher rating, so the best ratio "
        "has the most negative correlation.",
    }


def loads_from_browser_raw(browser_raw: dict | None) -> list[Load]:
    """Reduce a browser BenchmarkResult.raw to per-(iteration, URL) loads."""
    loads: list[Load] = []
    for it in (browser_raw or {}).get("iterations") or []:
        for u in ((it or {}).get("urls") or {}).values():
            if not isinstance(u, dict) or "nav" not in u:
                continue
            nav = u.get("nav") or {}
            paint = u.get("paint") or {}
            events = completion_series(u.get("resources"), fcp=paint.get("fcp"))
            start = nav.get("responseStart") or paint.get("fcp")
            end = nav.get("loadEventEnd")
            if start is None or end is None or not events:
                continue
            loads.append(Load(events=events, start=float(start), end=float(end)))
    return loads


def _collect_samples_from_db(ratings: dict[int, float]) -> list[Sample]:
    """Build rated samples from stored browser raw for the given run ids."""
    from sqlalchemy import select

    from ..database import session_scope
    from ..models import BenchmarkResult

    samples: list[Sample] = []
    with session_scope() as session:
        for run_id, rating in ratings.items():
            br = session.scalars(
                select(BenchmarkResult).where(
                    BenchmarkResult.run_id == run_id, BenchmarkResult.plugin == "browser"
                )
            ).first()
            if br is None:
                continue
            loads = loads_from_browser_raw(br.raw)
            if loads:
                samples.append(Sample(rating=float(rating), loads=loads))
    return samples


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m pathbrain.calibration.smoothness_calibration ratings.json")
        return 2
    with open(argv[0], encoding="utf-8") as fh:
        ratings = {int(k): float(v) for k, v in json.load(fh).items()}
    samples = _collect_samples_from_db(ratings)
    if not samples:
        print("No rated runs with browser raw found. Nothing to fit.")
        return 1
    result = fit_perceived_weights(samples)
    print(json.dumps(result, indent=2))
    best = result["best"]
    if best:
        print(
            f"\nCalibrated weights: w_occupied={result['w_occupied']}, "
            f"w_unoccupied={best['w_unoccupied']} "
            f"(ratio {best['ratio']}, r={best['correlation']:.3f}, n={best['n']})"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
