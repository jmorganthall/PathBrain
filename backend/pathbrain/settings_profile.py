"""Firewall/SQM settings fingerprinting for settings-vs-responsiveness analysis.

A *profile* is a set of FQ-CoDel/shaper parameters. We normalize the discovered
pipes to the scoring-relevant fields, hash them into a stable ``fingerprint`` so
runs sharing a configuration group together, and build a short human ``summary``.
"""
from __future__ import annotations

import hashlib
import json
import re

from .providers.base import FqCodelConfig

# The shaper field model — identity (CANON_FIELDS), labels, and which fields apply() can
# write (WRITABLE_PARAMS) / can't (NON_WRITABLE_FIELDS) — is owned by ``shaper_fields`` so
# fingerprinting, the providers, and the sweep/experiment engines all share one definition.
# These names are re-exported here for the existing call sites.
from .shaper_fields import (  # noqa: F401 — re-exported for back-compat
    CANON_FIELDS,
    FIELD_LABELS,
    NON_WRITABLE_FIELDS,
    WRITABLE_FIELDS,
    field as _shaper_field,
)

# Back-compat alias: the writable subset was historically ``WRITABLE_PARAMS`` here.
WRITABLE_PARAMS = WRITABLE_FIELDS

# Bandwidth unit -> Mbit, so "1Gbit" and "880Mbit" compare numerically.
_BW_UNITS = {"kbit": 1e-3, "mbit": 1.0, "gbit": 1000.0, "bit": 1e-6}
_NUM_RE = re.compile(r"^\s*([\d.]+)\s*([a-zA-Z]*)")


def normalize(configs: list[FqCodelConfig]) -> list[dict]:
    """Reduce discovered pipes to canonical, comparable dicts (+ a label)."""
    out: list[dict] = []
    for cfg in configs:
        d = cfg.to_dict()
        extra = d.get("extra") or {}
        item = {k: d.get(k) for k in CANON_FIELDS}
        item["label"] = extra.get("description") or extra.get("pipe") or extra.get("direction")
        # Whether the pipe's shaping is switched on. Not a CANON_FIELDS identity value (it
        # never re-keys a normal profile — see fingerprint()), but carried so the baseline
        # "SQM off" test's runs form their own profile and the UI can label them.
        item["enabled"] = extra.get("enabled")
        out.append(item)
    return out


SQM_OFF_FINGERPRINT = hashlib.sha1(b"\x00sqm_off").hexdigest()[:12]


def fingerprint(normalized: list[dict]) -> str:
    """Stable short hash of the profile-defining fields (order-independent).

    **SQM off collapses to one profile.** When any pipe is switched off the shaper parameters
    don't apply — the link is unshaped regardless of what quantum/target/… the firewall still
    echoes back — so *every* "SQM off" run hashes to the single canonical ``SQM_OFF_FINGERPRINT``,
    no matter which pipe is off or what the (inert) field values are. That way the nightly
    baseline test's runs all aggregate into one "SQM off" profile instead of splintering into a
    new profile every time the underlying shaper values differ.

    ``enabled`` is deliberately not a ``CANON_FIELDS`` identity value, and an ordinary all-enabled
    profile still hashes byte-for-byte as it always has (no history re-key) — only SQM-off runs
    change fingerprint. Re-key existing SQM-off history with ``POST /api/settings/refingerprint``."""
    if any(p.get("enabled") is False for p in normalized):
        return SQM_OFF_FINGERPRINT
    core = [{k: p.get(k) for k in CANON_FIELDS} for p in normalized]
    core.sort(key=lambda x: json.dumps(x, sort_keys=True, default=str))
    blob = json.dumps(core, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _to_number(field: str, value) -> float | None:
    """Best-effort numeric value for a shaper field, for direction comparison.

    Bandwidth strings are normalized to Mbit; durations like ``"5ms"`` yield their
    leading number (units are consistent within a field). Booleans map to 0/1.
    Returns ``None`` for values that aren't meaningfully ordered (e.g. scheduler
    names), which the diff then reports as a plain change rather than up/down.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUM_RE.match(str(value))
    if not match:
        return None
    num = float(match.group(1))
    if field in ("download_bandwidth", "upload_bandwidth"):
        return num * _BW_UNITS.get(match.group(2).lower(), 1.0)
    return num


def diff_profiles(from_norm: list[dict] | None, to_norm: list[dict] | None) -> list[dict]:
    """Field-level differences going *from* one profile *to* another.

    Pipes are matched by their label (falling back to position). Each returned
    change is ``{pipe, field, field_label, from_value, to_value, direction}``
    where ``direction`` is ``"higher"``/``"lower"`` (numeric) or ``"changed"``
    (non-orderable). Powers the "what the best profile changed" diff and, later,
    experiment suggestions ("target went 10ms→5ms; try 3ms next").
    """
    from_list = from_norm or []
    to_list = to_norm or []
    from_by_label = {(p.get("label") or f"pipe{i}"): p for i, p in enumerate(from_list)}

    changes: list[dict] = []
    for i, tp in enumerate(to_list):
        label = tp.get("label") or f"pipe{i}"
        fp = from_by_label.get(label)
        if fp is None and i < len(from_list):
            fp = from_list[i]
        fp = fp or {}
        for field in CANON_FIELDS:
            fv, tv = fp.get(field), tp.get(field)
            if fv == tv:
                continue
            fn, tn = _to_number(field, fv), _to_number(field, tv)
            if fn is not None and tn is not None and fn != tn:
                direction = "higher" if tn > fn else "lower"
            else:
                direction = "changed"
            changes.append(
                {
                    "pipe": label,
                    "field": field,
                    "field_label": FIELD_LABELS.get(field, field),
                    "from_value": fv,
                    "to_value": tv,
                    "direction": direction,
                }
            )
    return changes


def environment_signature(normalized: list[dict] | None) -> str:
    """Stable hash of only the **non-writable** profile fields — the environmental state
    PathBrain can't change via ``apply()``. Two profiles with the same signature are
    mutually reachable (applying the writable codel/bandwidth params drives one to the
    other); a different signature means the target is unreachable from here. Used to keep
    the challenger race from selecting a profile it can never apply to."""
    core = [{k: p.get(k) for k in NON_WRITABLE_FIELDS} for p in (normalized or [])]
    core.sort(key=lambda x: json.dumps(x, sort_keys=True, default=str))
    blob = json.dumps(core, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def _same_value(a, b) -> bool:
    """Loosely equal? Compares scalars by normalized string so '5ms' == '5ms' and
    1514 == '1514' don't read as changes."""
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if a is None or b is None:
        return a is b
    return str(a).strip().lower() == str(b).strip().lower()


def _field_equal(param: str, a, b) -> bool:
    """Whether two values of a shaper field are the *same setting*, comparing **numerically**
    for numeric/unit fields so ``"3ms"`` == ``"3"`` == ``3`` (the firewall echoes a duration
    select back as the bare number, while a profile / AI value may carry the ``ms`` unit).
    Falls back to loose string equality for non-numeric fields (scheduler names, etc.)."""
    fa, fb = _to_number(param, a), _to_number(param, b)
    if fa is not None and fb is not None:
        return fa == fb
    return _same_value(a, b)


def _wire_value(param: str, desired):
    """The exact value to hand ``provider.apply()`` for a field. Numeric/unit fields become a
    bare int — the firewall's duration selects are keyed by the bare number (``"3"``, not
    ``"3ms"``), so writing the unit-suffixed string silently doesn't take. ecn → 1/0."""
    if param == "ecn":
        return 1 if desired else 0
    f = _shaper_field(param)
    if f is not None and (f.kind == "int" or f.unit):
        n = _to_number(param, desired)
        if n is not None:
            return int(round(n))
    return desired


def plan_apply(target: list[dict] | None, live: list[FqCodelConfig]) -> tuple[list[dict], list[str]]:
    """Plan the writes to make the live firewall match a target profile.

    Matches each target pipe to a live pipe by label (falling back to position when
    the pipe counts line up), then for every writable field that differs emits a
    change ``{pipe_uuid, param, value, label, field, from, to}`` ready for
    ``provider.apply()``. Fields already at the target value are skipped, so an
    apply is a no-op when the firewall is already on this profile. Returns
    ``(changes, warnings)``; warnings flag target pipes with no live match or uuid.
    """
    target_list = target or []
    warnings: list[str] = []
    by_label: dict = {}
    for cfg in live:
        extra = cfg.extra or {}
        lbl = extra.get("description") or extra.get("pipe") or extra.get("direction")
        by_label.setdefault(lbl, cfg)

    changes: list[dict] = []
    for i, pipe in enumerate(target_list):
        label = pipe.get("label")
        match = by_label.get(label)
        if match is None and len(target_list) == len(live):
            match = live[i]  # positional fallback when the topology lines up
        if match is None:
            warnings.append(f"No live pipe matches '{label or 'pipe'}' — skipped")
            continue
        uuid = (match.extra or {}).get("uuid")
        if not uuid:
            warnings.append(f"Live pipe '{label or 'pipe'}' has no uuid — skipped")
            continue
        current = match.to_dict()
        for param in WRITABLE_PARAMS:
            desired = pipe.get(param)
            if desired is None or _field_equal(param, current.get(param), desired):
                continue
            value = _wire_value(param, desired)
            changes.append(
                {
                    "pipe_uuid": uuid,
                    "param": param,
                    "value": value,
                    "label": label or "pipe",
                    "field": param,
                    "field_label": FIELD_LABELS.get(param, param),
                    "from": current.get(param),
                    "to": desired,
                }
            )
    return changes, warnings


def summarize(normalized: list[dict] | None) -> str:
    """Short, human description of a profile, e.g. 'wan: 900Mbit q1514 t5ms'."""
    if not normalized:
        return "—"
    parts: list[str] = []
    for p in normalized:
        seg: list[str] = []
        # SQM off: the shaper params don't apply, so don't tack on inert quantum/target values —
        # a collapsed "SQM off" profile reads cleanly regardless of which run's settings back it.
        if p.get("enabled") is False:
            parts.append(f"{p.get('label') or 'pipe'}: SQM off")
            continue
        if p.get("download_bandwidth"):
            seg.append(str(p["download_bandwidth"]))
        if p.get("quantum") is not None:
            seg.append(f"q{p['quantum']}")
        if p.get("target"):
            seg.append(f"t{p['target']}")
        if p.get("interval"):
            seg.append(f"i{p['interval']}")
        if p.get("ecn") is not None:
            seg.append("ecn" if p["ecn"] else "noecn")
        label = p.get("label") or "pipe"
        parts.append(f"{label}: {' '.join(seg)}".strip())
    return " | ".join(parts)
