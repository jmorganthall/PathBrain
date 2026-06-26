"""Offline calibration tools (kept out of the hot path).

Currently: fitting the perceived-time weight ratio to subjective smoothness
ratings — the rigorous version of judging "smooth vs chunky" by eye.
"""
from __future__ import annotations

from .smoothness_calibration import fit_perceived_weights

__all__ = ["fit_perceived_weights"]
