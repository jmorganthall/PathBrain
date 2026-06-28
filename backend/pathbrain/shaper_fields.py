"""Single source of truth for the SQM / FQ-CoDel shaper field model.

Every shaper field PathBrain knows about is declared **once** here, with its four facets:
its identity (does it define a profile?), whether the standard provider's ``apply()`` can
**write** it, its display label, and whether it's **sweepable**. The settings/profile layer,
the providers, and the sweep/experiment engines all *derive* from this instead of re-listing
field names — so the facets can't drift out of sync. That drift (the profile fingerprint
covering fields ``apply()`` can't write, with nothing owning the relationship) is exactly
what produced the "valid but unappliable profile" bug that aborted the challenger race.

Mirrors the ``metrics.py`` registry pattern: declare once, derive everywhere. Adding a
shaper field is a single entry here.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShaperField:
    """One shaper parameter and what PathBrain may do with it."""

    key: str                 # normalized field name used everywhere in PathBrain
    label: str               # human label (UI / profile diffs)
    kind: str = "str"        # value's nature: "int" | "str" | "bool"
    identity: bool = True    # part of a profile's identity (the fingerprint)
    writable: bool = False   # the standard provider's apply() can write it
    sweepable: bool = False  # offered as a sweep/experiment parameter
    unit: str | None = None  # value unit/suffix, e.g. "ms" for the CoDel target


# Declared once, in canonical (display + fingerprint) order. ``writable=False`` means
# ``apply()`` can't drive the field: ``upload_bandwidth`` because OPNsense pipes are
# directional via rules (always None on read), ``queues``/``scheduler`` because they're
# structural pipe properties with no setPipe mapping.
SHAPER_FIELDS: list[ShaperField] = [
    ShaperField("download_bandwidth", "Download bandwidth", writable=True),
    ShaperField("upload_bandwidth", "Upload bandwidth"),
    ShaperField("quantum", "Quantum", kind="int", writable=True, sweepable=True),
    ShaperField("limit", "Queue limit", kind="int", writable=True),
    ShaperField("target", "CoDel target", writable=True, sweepable=True, unit="ms"),
    ShaperField("interval", "CoDel interval", writable=True, unit="ms"),
    ShaperField("ecn", "ECN", kind="bool", writable=True),
    ShaperField("flows", "Flows", kind="int", writable=True),
    ShaperField("queues", "Queues", kind="int"),
    ShaperField("scheduler", "Scheduler"),
]

_BY_KEY: dict[str, ShaperField] = {f.key: f for f in SHAPER_FIELDS}


def field(key: str) -> ShaperField | None:
    """The ShaperField for a key (or None)."""
    return _BY_KEY.get(key)


# Derived views — the names the rest of the codebase consumes. Keeping them as derived
# constants (not re-typed literals) is the whole point: change a facet above and every
# consumer updates with it.
CANON_FIELDS: list[str] = [f.key for f in SHAPER_FIELDS if f.identity]
FIELD_LABELS: dict[str, str] = {f.key: f.label for f in SHAPER_FIELDS}
WRITABLE_FIELDS: list[str] = [f.key for f in SHAPER_FIELDS if f.writable]
NON_WRITABLE_FIELDS: list[str] = [f.key for f in SHAPER_FIELDS if f.identity and not f.writable]
SWEEPABLE_FIELDS: list[str] = [f.key for f in SHAPER_FIELDS if f.sweepable]


# ── Invariants ───────────────────────────────────────────────────────────────
# Enforced at import (a bad edit fails fast) and asserted again in tests (fails CI).
# These are the relationships that used to live only in comments.

# If we can write a field, it must define the profile — otherwise applying it wouldn't
# change the profile's identity. (The *converse* is allowed and expected: identity fields
# like scheduler/queues that we can't write — those drive the reachability check.)
assert set(WRITABLE_FIELDS) <= set(CANON_FIELDS), (
    "writable shaper fields must be identity fields"
)
# You can't sweep a field you can't apply.
assert set(SWEEPABLE_FIELDS) <= set(WRITABLE_FIELDS), (
    "sweepable shaper fields must be writable"
)
