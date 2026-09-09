"""The app polices its own footprint — in code, not in a host limit.

The instrument-drift audit showed the browser's host-side phases (context setup, context
close, the timing reads) growing four- to nine-fold over a day with the network phases
flat: the machine PathBrain measures *from* was getting slower, steadily, and every graded
number that includes render slid with it. A ``mem_limit`` on the container would only
turn that slide into an OOM kill. What is wanted is for the app to notice that it is the
thing eating the host and to back off before the host notices.

So this module reads what the kernel already exposes — the container's cgroup memory
usage and limit, the host's available memory, the one-minute load against the CPU count —
and turns it into a **pressure level** (``ok`` / ``high`` / ``critical``) with the numbers
behind it. Two readers act on it:

- ``GET /api/health/pipeline`` reports it, so "the NAS is struggling" is a number on a
  page instead of a feeling.
- The scheduler watchdog calls :func:`relieve` every tick. At ``high`` it reaps stray
  Chromium, asks the browser plugin to recycle its Chromium at the next seam and drops
  the field memo (tens of MB per key, rebuilt on demand). At ``critical`` it also holds
  this tick's *scheduled* monitoring run back — one tick, retried next tick, so external
  pressure can never starve measurement for good, while a run that would tip a swapping
  host over is not started on top of it.

Dependency-free (``/proc`` and ``/sys/fs/cgroup`` reads), never raises, and every
reading degrades to ``None`` where a file is missing (a dev box outside a cgroup, a
non-Linux host) rather than inventing a number.
"""
from __future__ import annotations

import os
import time
from typing import Callable

from .logging_config import get_logger

log = get_logger(__name__)

#: Memory share of the container's own limit (or of the host's total when the container
#: has no limit) at which the app starts backing off, and at which it holds scheduled work.
HIGH_MEMORY_PCT = 80.0
CRITICAL_MEMORY_PCT = 92.0
#: One-minute load per CPU at which the app treats the host as saturated.
HIGH_LOAD_PER_CPU = 2.0
CRITICAL_LOAD_PER_CPU = 4.0

_CGROUP_V2 = "/sys/fs/cgroup"
_CGROUP_V1_MEM = "/sys/fs/cgroup/memory"
_PROC = "/proc"

_state: dict = {
    "last": None,          # the last reading
    "relieved_at": 0.0,    # monotonic time of the last relief action
    "actions": 0,          # relief actions taken since start
    "held_ticks": 0,       # scheduled runs held back since start
}


def _read_int(path: str) -> int | None:
    try:
        with open(path, "rb") as fh:
            text = fh.read().strip()
    except OSError:
        return None
    if not text or text == b"max":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _meminfo(root: str = _PROC) -> dict[str, int]:
    """``/proc/meminfo`` as ``{key: kB}``, empty when unreadable."""
    out: dict[str, int] = {}
    try:
        with open(f"{root}/meminfo", "rb") as fh:
            for line in fh:
                key, _, rest = line.partition(b":")
                fields = rest.split()
                if fields:
                    try:
                        out[key.decode()] = int(fields[0])
                    except ValueError:
                        continue
    except OSError:
        return {}
    return out


def _loadavg(root: str = _PROC) -> float | None:
    try:
        with open(f"{root}/loadavg", "rb") as fh:
            return float(fh.read().split()[0])
    except (OSError, IndexError, ValueError):
        return None


def _cgroup_memory(v2_root: str = _CGROUP_V2, v1_root: str = _CGROUP_V1_MEM) -> tuple[int | None, int | None]:
    """``(used_bytes, limit_bytes)`` for this cgroup: v2 first, then v1. ``limit`` is None
    when the cgroup is unlimited (``max``) or unreadable."""
    used = _read_int(f"{v2_root}/memory.current")
    if used is not None:
        return used, _read_int(f"{v2_root}/memory.max")
    used = _read_int(f"{v1_root}/memory.usage_in_bytes")
    if used is not None:
        limit = _read_int(f"{v1_root}/memory.limit_in_bytes")
        # v1 reports "unlimited" as a huge number rather than a word.
        if limit is not None and limit > (1 << 60):
            limit = None
        return used, limit
    return None, None


def pressure(
    *,
    proc_root: str = _PROC,
    cgroup_v2_root: str = _CGROUP_V2,
    cgroup_v1_root: str = _CGROUP_V1_MEM,
    cpus: int | None = None,
) -> dict:
    """The current reading: memory used against the tightest limit that applies, the
    host's available memory, the load per CPU, and the level those imply."""
    used, limit = _cgroup_memory(cgroup_v2_root, cgroup_v1_root)
    info = _meminfo(proc_root)
    total_kb = info.get("MemTotal")
    avail_kb = info.get("MemAvailable")
    cpus = cpus or os.cpu_count() or 1
    load1 = _loadavg(proc_root)

    # The memory yardstick: the container's own limit when it has one (the app's share of
    # the host is what it agreed to), else the host's total (the app IS the host's tenant).
    mem_pct: float | None = None
    basis = None
    if used is not None and limit:
        mem_pct = 100.0 * used / limit
        basis = "cgroup_limit"
    elif total_kb and avail_kb is not None:
        mem_pct = 100.0 * (total_kb - avail_kb) / total_kb
        basis = "host_total"
    load_per_cpu = (load1 / cpus) if load1 is not None else None

    level = "ok"
    reasons: list[str] = []
    if mem_pct is not None:
        if mem_pct >= CRITICAL_MEMORY_PCT:
            level = "critical"
            reasons.append(f"memory {mem_pct:.0f}% of {basis.replace('_', ' ')}")
        elif mem_pct >= HIGH_MEMORY_PCT:
            level = "high"
            reasons.append(f"memory {mem_pct:.0f}% of {basis.replace('_', ' ')}")
    if load_per_cpu is not None:
        if load_per_cpu >= CRITICAL_LOAD_PER_CPU:
            level = "critical"
            reasons.append(f"load {load1:.1f} on {cpus} CPU(s)")
        elif load_per_cpu >= HIGH_LOAD_PER_CPU and level == "ok":
            level = "high"
            reasons.append(f"load {load1:.1f} on {cpus} CPU(s)")
    reading = {
        "level": level,
        "reasons": reasons,
        "memory_used_mb": round(used / 1048576.0, 1) if used is not None else None,
        "memory_limit_mb": round(limit / 1048576.0, 1) if limit else None,
        "memory_pct": round(mem_pct, 1) if mem_pct is not None else None,
        "memory_basis": basis,
        "host_total_mb": round(total_kb / 1024.0, 1) if total_kb else None,
        "host_available_mb": round(avail_kb / 1024.0, 1) if avail_kb is not None else None,
        "load1": load1,
        "cpus": cpus,
        "load_per_cpu": round(load_per_cpu, 2) if load_per_cpu is not None else None,
        "actions": _state["actions"],
        "held_ticks": _state["held_ticks"],
    }
    _state["last"] = reading
    return reading


def last() -> dict | None:
    """The most recent reading, without taking a new one."""
    return _state["last"]


def relieve(
    reading: dict | None = None,
    *,
    reap: Callable[[], dict] | None = None,
    recycle: Callable[[], None] | None = None,
    drop_caches: Callable[[], None] | None = None,
    now: Callable[[], float] = time.monotonic,
    min_interval_s: float = 60.0,
) -> dict:
    """Act on a reading. Returns ``{level, acted, hold_scheduled}``.

    ``hold_scheduled`` is True when the caller should not start a scheduled run this
    tick. Relief actions (reap, recycle, drop caches) run at most once per
    ``min_interval_s`` so a tick loop does not thrash them; the hold is re-evaluated
    every tick from the fresh reading, never latched.
    """
    reading = reading or pressure()
    level = reading.get("level", "ok")
    acted = False
    if level in ("high", "critical") and now() - _state["relieved_at"] >= min_interval_s:
        _state["relieved_at"] = now()
        _state["actions"] += 1
        acted = True
        log.warning(
            "Resource pressure %s (%s): reaping stray Chromium, recycling the browser at "
            "its next seam, dropping the field memo",
            level, "; ".join(reading.get("reasons") or []) or "no detail",
        )
        for label, fn in (("reap", reap), ("recycle", recycle), ("drop_caches", drop_caches)):
            if fn is None:
                continue
            try:
                fn()
            except Exception:  # noqa: BLE001 — relief must never be the failure
                log.debug("resource_guard: %s failed", label, exc_info=True)
    hold = level == "critical"
    if hold:
        _state["held_ticks"] += 1
    return {"level": level, "acted": acted, "hold_scheduled": hold}


def default_relievers() -> dict[str, Callable]:
    """The real relief actions, resolved lazily so this module imports nothing heavy."""
    def _reap() -> dict:
        from . import browser_procs

        return browser_procs.reap_orphans()

    def _recycle() -> None:
        from .plugins import get_plugin

        plugin = get_plugin("browser")
        if plugin is not None and hasattr(plugin, "request_recycle"):
            plugin.request_recycle("resource pressure")

    def _drop() -> None:
        from .api.routes_settings import invalidate_profiles_cache

        invalidate_profiles_cache()

    return {"reap": _reap, "recycle": _recycle, "drop_caches": _drop}


__all__ = [
    "CRITICAL_LOAD_PER_CPU",
    "CRITICAL_MEMORY_PCT",
    "HIGH_LOAD_PER_CPU",
    "HIGH_MEMORY_PCT",
    "default_relievers",
    "last",
    "pressure",
    "relieve",
]
