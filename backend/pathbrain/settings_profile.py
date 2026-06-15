"""Firewall/SQM settings fingerprinting for settings-vs-responsiveness analysis.

A *profile* is a set of FQ-CoDel/shaper parameters. We normalize the discovered
pipes to the scoring-relevant fields, hash them into a stable ``fingerprint`` so
runs sharing a configuration group together, and build a short human ``summary``.
"""
from __future__ import annotations

import hashlib
import json

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
