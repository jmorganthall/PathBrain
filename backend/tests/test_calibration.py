"""Tests for the perceived-time weight calibration harness (offline tool)."""
from __future__ import annotations

from pathbrain.calibration.smoothness_calibration import (
    Load,
    Sample,
    fit_perceived_weights,
    loads_from_browser_raw,
)
from pathbrain.interpret.smoothness import completion_series


def _smooth_load():
    events = completion_series(
        [{"responseEnd": t, "transferSize": 1000} for t in range(100, 900, 100)]
    )
    return Load(events=events, start=0.0, end=900.0)


def _chunky_load():
    events = completion_series(
        [{"responseEnd": t, "transferSize": 1000} for t in (50, 60, 70, 760, 800)]
    )
    return Load(events=events, start=0.0, end=900.0)


def test_fit_prefers_a_ratio_that_tracks_ratings():
    # Smooth loads rated high, chunky loads rated low: perceived time should
    # correlate negatively with rating, and the fit should surface that.
    samples = [
        Sample(rating=9.0, loads=[_smooth_load()]),
        Sample(rating=8.0, loads=[_smooth_load()]),
        Sample(rating=2.0, loads=[_chunky_load()]),
        Sample(rating=3.0, loads=[_chunky_load()]),
    ]
    result = fit_perceived_weights(samples)
    assert result["best"] is not None
    assert result["best"]["correlation"] < 0  # lower perceived ⇒ higher rating
    # The winning ratio penalizes stalls (>1): equal weights make perceived time
    # equal the real window for every load, which can't track ratings at all.
    assert result["best"]["ratio"] > 1.0
    flat = next(r for r in result["table"] if r["ratio"] == 1.0)
    assert flat["correlation"] is None


def test_fit_handles_degenerate_input():
    # Single sample → correlation undefined → no best, but no crash.
    result = fit_perceived_weights([Sample(rating=5.0, loads=[_smooth_load()])])
    assert result["best"] is None
    assert result["samples"] == 1


def test_loads_from_browser_raw_reduces_iterations():
    raw = {
        "iterations": [
            {
                "urls": {
                    "u": {
                        "nav": {"responseStart": 50.0, "loadEventEnd": 900.0},
                        "paint": {"fcp": 120.0},
                        "resources": [{"responseEnd": 300.0, "transferSize": 1000}],
                    }
                }
            }
        ]
    }
    loads = loads_from_browser_raw(raw)
    assert len(loads) == 1
    assert loads[0].start == 50.0 and loads[0].end == 900.0
    assert loads[0].events  # completion series populated
