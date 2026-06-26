"""Interpretation layer: turn raw observations into metric values.

Plugins are pure sensors that store raw observations; *everything* that interprets
them — aggregation across targets, statistics like jitter, derived metrics like
Speed Index and transfer speed, and (downstream) scoring — lives here and is
versioned + re-runnable, so a new metric or a changed formula can be applied to
history without re-collecting (see ``derive.DERIVATION_VERSION``).
"""
from __future__ import annotations

from .derive import DERIVATION_VERSION, derive

__all__ = ["DERIVATION_VERSION", "derive"]
