"""How often the crown changes: the observations it is counted from, and the count."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class ChurnEntry:
    """One observed crown. ``previous`` None means tracking began here (not a change);
    ``label`` None means the source kept no label (the Verdict history keeps none)."""

    at: datetime
    fingerprint: str | None
    previous: str | None
    label: str | None


@dataclass(frozen=True)
class Churn:
    """Crown-churn statistics (`crown_follower.stats`). Every None means "not enough
    history yet" for that number: no observation, no completed reign, or under a day."""

    basis: Literal["verdict", "pooled"]
    tracked_since: str | None
    total_changes: int
    changes_24h: int
    changes_7d: int
    changes_30d: int
    changes_per_day: float | None
    distinct_crowns_30d: int
    last_change_at: str | None
    current_crown_fingerprint: str | None
    current_crown_label: str | None
    current_reign_hours: float | None
    mean_reign_hours: float | None
    median_reign_hours: float | None
