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

# Fields that define a configuration profile (exclude volatile extras like uuids).
CANON_FIELDS = [
    "download_bandwidth",
    "upload_bandwidth",
    "quantum",
    "limit",
    "target",
    "interval",
    "ecn",
    "flows",
    "queues",
    "scheduler",
]

# Human labels + whether a higher value is intuitively "more"/"bigger", for the
# at-a-glance profile diff. Direction here is purely numeric (did the value go up
# or down); whether up is *good* depends on the resulting score.
FIELD_LABELS: dict[str, str] = {
    "download_bandwidth": "Download bandwidth",
    "upload_bandwidth": "Upload bandwidth",
    "quantum": "Quantum",
    "limit": "Queue limit",
    "target": "CoDel target",
    "interval": "CoDel interval",
    "ecn": "ECN",
    "flows": "Flows",
    "queues": "Queues",
    "scheduler": "Scheduler",
}

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
        out.append(item)
    return out


def fingerprint(normalized: list[dict]) -> str:
    """Stable short hash of the profile-defining fields (order-independent)."""
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


def summarize(normalized: list[dict] | None) -> str:
    """Short, human description of a profile, e.g. 'wan: 900Mbit q1514 t5ms'."""
    if not normalized:
        return "—"
    parts: list[str] = []
    for p in normalized:
        seg: list[str] = []
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
