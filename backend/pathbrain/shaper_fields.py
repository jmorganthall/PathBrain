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

import re
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
    # A sensible starting range for the sweep UI ({enabled, min, max, step}) so the
    # Shotgun Sweep page can render a control per sweepable field with no hardcoding.
    sweep_default: dict | None = None


# Declared once, in canonical (display + fingerprint) order. ``writable=False`` means
# ``apply()`` can't drive the field: ``upload_bandwidth`` because OPNsense pipes are
# directional via rules (always None on read), ``queues``/``scheduler`` because they're
# structural pipe properties with no setPipe mapping.
SHAPER_FIELDS: list[ShaperField] = [
    ShaperField("download_bandwidth", "Download bandwidth", writable=True),
    ShaperField("upload_bandwidth", "Upload bandwidth"),
    ShaperField("quantum", "Quantum", kind="int", writable=True, sweepable=True,
                sweep_default={"enabled": True, "min": 300, "max": 10000, "step": 757}),
    ShaperField("limit", "Queue limit", kind="int", writable=True),
    ShaperField("target", "CoDel target", writable=True, sweepable=True, unit="ms",
                sweep_default={"enabled": False, "min": 3, "max": 8, "step": 1}),
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


def format_value(field_key: str, n: float) -> int:
    """The provider-ready **wire** value for a numeric field: a bare int. The firewall's
    duration fields (CoDel ``target``/``interval``) are select options keyed by the bare number
    (OPNsense stores/echoes ``"3"``, not ``"3ms"``) — so the *value we write* must be that bare
    number. The ``"ms"`` unit is a **display** concern only (see ``format_display``). Writing
    ``"3ms"`` to a field keyed ``"3"`` silently doesn't take, which is exactly the "apply didn't
    happen" failure it used to cause."""
    return int(round(n))


def format_display(field_key: str, value) -> str:
    """Human-facing rendering of a field value with its unit (e.g. ``target`` 5 → ``"5ms"``) —
    for labels and summaries. The wire value stays unit-less (see ``format_value``)."""
    f = _BY_KEY.get(field_key)
    if value is None:
        return "—"
    s = str(value)
    if f and f.unit and not s.endswith(f.unit):
        return f"{s}{f.unit}"
    return s


_LEADING_NUM_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)")


def coerce_value(field_key: str, value):
    """Canonicalize an *externally supplied* field value (an AI suggestion, a hand-typed
    override) into the exact provider-ready form the firewall reports back on read — so a
    value round-trips to the same ``fingerprint``. Without this, ``target: "5ms"`` (AI) vs
    ``target: 5`` (discover) hash differently and the profile-test verify wrongly concludes
    "could not reach the target".

    - bool fields → ``bool``
    - int fields → ``int`` (rounded; accepts ``"3000"`` / ``3000.0``)
    - unit fields (target/interval) → bare ``int`` — the firewall's duration selects are keyed
      by the bare number (``"3"``), NOT ``"3ms"``; accepts ``"5ms"``/``5``/``"5"`` all → ``5``
    - everything else (bandwidth strings, scheduler names) → passthrough unchanged

    Unparseable numeric input is passed through untouched (the caller's diff/apply still
    sees it) rather than raising, so a malformed suggestion degrades instead of 500-ing."""
    if value is None:
        return None
    f = _BY_KEY.get(field_key)
    if f is None:
        return value
    if f.kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    if f.kind == "int" or f.unit:
        if isinstance(value, bool):  # bool is an int subclass — don't treat True as 1 here
            return value
        num: float | None = None
        if isinstance(value, (int, float)):
            num = float(value)
        else:
            m = _LEADING_NUM_RE.match(str(value))
            if m:
                num = float(m.group(1))
        if num is None:
            return value  # can't parse — leave as-is
        return format_value(field_key, num)
    return value


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
