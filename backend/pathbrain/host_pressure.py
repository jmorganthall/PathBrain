"""How loaded was the NAS while a run measured — and was it PathBrain or something else?

Instrument health (:mod:`instrument_health`) infers a slow host from the browser's own
readings: context setup, close and the timing reads run slow, so the machine was slow. It
cannot say *why*. The research export showed a nightly ~5-hour slowdown starting at
07:00 UTC with nothing in PathBrain scheduled then, and "the NAS was busy with its own
job" and "PathBrain's leaked Chromium was eating the box" call for opposite fixes. So
every run now carries the kernel's own account of the window it measured in:

- **Pressure stall information** (``/proc/pressure/{cpu,io,memory}``): the share of the
  run's wall time in which some task on the **host** was stalled waiting for CPU, disk or
  memory — the direct answer to "was the box contended?", and the IO line in particular
  is what a NAS scrub, backup or indexing job shows up as. The container's own cgroup
  pressure is read beside it where the cgroup exposes it (v2), so host-wide contention and
  contention PathBrain itself felt are two numbers.
- **CPU, split by who used it**: the host's busy share of every CPU over the window
  (``/proc/stat``) against the CPU this container consumed (``cpu.stat`` usage on cgroup
  v2, ``cpuacct.usage`` on v1). PathBrain's *share* of the host's busy CPU is the
  attribution: a busy host where PathBrain is a small share was busy with something else.
- **Load average, available memory and swap** at the end of the run, as context.

Counters are read once when a run starts and once when its iterations finish, and only
the **differences** are kept: a cumulative total means nothing without the window it was
read over. Read-only, dependency-free and best-effort throughout — every source degrades
to ``None`` where the file is missing (a dev box, a non-Linux host, a cgroup without the
controller) and nothing here can fail a run. Nothing reads it to grade or gate a run; it
is recorded so a slow stretch can be attributed rather than guessed at.
"""
from __future__ import annotations

import os
import time

from .logging_config import get_logger

log = get_logger(__name__)

_PROC = "/proc"
_CGROUP_V2 = "/sys/fs/cgroup"
_CGROUP_V1_CPUACCT = "/sys/fs/cgroup/cpuacct"

#: PSI resources and the lines each reports. ``full`` (every non-idle task stalled at
#: once) exists for io and memory only.
PSI_LINES: dict[str, tuple[str, ...]] = {
    "cpu": ("some",),
    "io": ("some", "full"),
    "memory": ("some", "full"),
}

#: The host is "busy" past this share of its CPU, or "stalled" past this share of wall
#: time on any PSI line. Used only to phrase the sentence, never to grade.
BUSY_CPU_PCT = 50.0
STALLED_PCT = 10.0
#: Below this share of the host's busy CPU, PathBrain was not what kept the host busy.
SMALL_SHARE = 0.4


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="ascii", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _psi_totals(text: str | None) -> dict[str, int]:
    """``{"some": total_us, "full": total_us}`` from one PSI file's text."""
    out: dict[str, int] = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        for field in parts[1:]:
            if field.startswith("total="):
                try:
                    out[parts[0]] = int(field[6:])
                except ValueError:
                    pass
    return out


def _psi(base: str, suffix: str) -> dict[str, int]:
    """Every PSI line under ``base`` (``{base}/{resource}{suffix}``) as cumulative µs."""
    out: dict[str, int] = {}
    for resource, lines in PSI_LINES.items():
        totals = _psi_totals(_read(f"{base}/{resource}{suffix}"))
        for line in lines:
            if line in totals:
                out[f"{resource}_{line}"] = totals[line]
    return out


def _host_cpu(root: str) -> tuple[int, int] | None:
    """``(busy_ticks, total_ticks)`` across every CPU from ``/proc/stat``'s first line."""
    text = _read(f"{root}/stat")
    if not text:
        return None
    first = text.splitlines()[0].split()
    if not first or first[0] != "cpu":
        return None
    try:
        values = [int(v) for v in first[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    # user nice system idle iowait irq softirq steal [guest guest_nice — already in user]
    values = values[:8]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total - idle, total


def _cgroup_cpu_usec(v2_root: str, v1_root: str) -> int | None:
    """CPU this container has consumed, in µs: cgroup v2 ``cpu.stat`` then v1 ``cpuacct``."""
    text = _read(f"{v2_root}/cpu.stat")
    if text:
        for line in text.splitlines():
            key, _, value = line.partition(" ")
            if key == "usage_usec":
                try:
                    return int(value)
                except ValueError:
                    return None
    text = _read(f"{v1_root}/cpuacct.usage")
    if text:
        try:
            return int(text.strip()) // 1000  # ns → µs
        except ValueError:
            return None
    return None


def _meminfo_mb(root: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in (_read(f"{root}/meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields:
            try:
                out[key] = int(fields[0]) / 1024.0
            except ValueError:
                pass
    return out


def _loadavg(root: str) -> tuple[float | None, float | None]:
    parts = (_read(f"{root}/loadavg") or "").split()
    try:
        return float(parts[0]), float(parts[1])
    except (IndexError, ValueError):
        return None, None


def snapshot(
    *,
    proc_root: str = _PROC,
    cgroup_v2_root: str = _CGROUP_V2,
    cgroup_v1_cpuacct: str = _CGROUP_V1_CPUACCT,
) -> dict:
    """The raw counters at one instant. Never raises."""
    try:
        host_cpu = _host_cpu(proc_root)
        mem = _meminfo_mb(proc_root)
        load1, load5 = _loadavg(proc_root)
        swap_used = None
        if "SwapTotal" in mem and "SwapFree" in mem:
            swap_used = mem["SwapTotal"] - mem["SwapFree"]
        return {
            "t": time.monotonic(),
            "host_psi": _psi(f"{proc_root}/pressure", ""),
            "cgroup_psi": _psi(cgroup_v2_root, ".pressure"),
            "host_cpu": list(host_cpu) if host_cpu else None,
            "clk_tck": os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100,
            "cpus": os.cpu_count() or 1,
            "cgroup_cpu_usec": _cgroup_cpu_usec(cgroup_v2_root, cgroup_v1_cpuacct),
            "load1": load1,
            "load5": load5,
            "mem_available_mb": mem.get("MemAvailable"),
            "swap_used_mb": swap_used,
        }
    except Exception:  # noqa: BLE001 — a reading must never fail a run
        log.debug("Host pressure: snapshot failed", exc_info=True)
        return {"t": time.monotonic()}


def _stall_pct(start: dict, end: dict, wall_us: float) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, v_end in (end or {}).items():
        v_start = (start or {}).get(key)
        if v_start is None or v_end < v_start:
            continue
        out[key] = round(100.0 * (v_end - v_start) / wall_us, 2)
    return out


def summarize(start: dict | None, end: dict | None) -> dict | None:
    """The run's window, as shares and rates. None when the two readings can't be paired."""
    if not start or not end or "t" not in start or "t" not in end:
        return None
    wall_s = end["t"] - start["t"]
    if wall_s <= 0:
        return None
    wall_us = wall_s * 1e6
    cpus = end.get("cpus") or 1
    out: dict = {
        "wall_s": round(wall_s, 1),
        "cpus": cpus,
        "host_psi_pct": _stall_pct(start.get("host_psi"), end.get("host_psi"), wall_us) or None,
        "container_psi_pct": _stall_pct(start.get("cgroup_psi"), end.get("cgroup_psi"), wall_us) or None,
        "load1": end.get("load1"),
        "load5": end.get("load5"),
        "mem_available_mb": round(end["mem_available_mb"], 1) if end.get("mem_available_mb") is not None else None,
        "swap_used_mb": round(end["swap_used_mb"], 1) if end.get("swap_used_mb") is not None else None,
        "swap_growth_mb": (
            round(end["swap_used_mb"] - start["swap_used_mb"], 1)
            if end.get("swap_used_mb") is not None and start.get("swap_used_mb") is not None else None
        ),
    }
    host_busy_s = None
    hc0, hc1 = start.get("host_cpu"), end.get("host_cpu")
    tck = end.get("clk_tck") or 100
    if hc0 and hc1 and hc1[1] > hc0[1]:
        busy = hc1[0] - hc0[0]
        total = hc1[1] - hc0[1]
        out["host_cpu_pct"] = round(100.0 * busy / total, 1)
        host_busy_s = busy / tck
    c0, c1 = start.get("cgroup_cpu_usec"), end.get("cgroup_cpu_usec")
    if c0 is not None and c1 is not None and c1 >= c0:
        container_s = (c1 - c0) / 1e6
        out["container_cpu_cores"] = round(container_s / wall_s, 2)
        if host_busy_s and host_busy_s > 0:
            out["container_share_of_busy"] = round(min(1.0, container_s / host_busy_s), 2)
    out["attribution"] = attribute(out)
    return out


def attribute(s: dict) -> str | None:
    """``host`` (busy or stalled, and mostly not PathBrain), ``pathbrain`` (busy, and mostly
    PathBrain), ``quiet`` or None when the readings can't say."""
    cpu = s.get("host_cpu_pct")
    psi = s.get("host_psi_pct") or {}
    stalled = max(psi.values()) if psi else None
    if cpu is None and stalled is None:
        return None
    busy = (cpu is not None and cpu >= BUSY_CPU_PCT) or (stalled is not None and stalled >= STALLED_PCT)
    if not busy:
        return "quiet"
    share = s.get("container_share_of_busy")
    if share is None:
        return None
    return "pathbrain" if share >= SMALL_SHARE else "host"


def explain(s: dict | None) -> str | None:
    """One sentence with its numbers in it."""
    if not s:
        return None
    psi = s.get("host_psi_pct") or {}
    parts = []
    if s.get("host_cpu_pct") is not None:
        parts.append(f"host CPU {s['host_cpu_pct']:.0f}% busy")
    worst = max(psi.items(), key=lambda kv: kv[1]) if psi else None
    if worst:
        res, line = worst[0].split("_", 1)
        parts.append(f"tasks stalled on {res} ({line}) {worst[1]:.0f}% of the time")
    if s.get("container_share_of_busy") is not None:
        parts.append(f"PathBrain {s['container_share_of_busy'] * 100:.0f}% of the busy CPU")
    if s.get("load1") is not None:
        parts.append(f"load {s['load1']:.1f} on {s.get('cpus')} CPU(s)")
    numbers = ", ".join(parts)
    lead = {
        "host": "The NAS was busy with something other than PathBrain",
        "pathbrain": "The NAS was busy, mostly with PathBrain itself",
        "quiet": "The NAS was quiet",
    }.get(s.get("attribution"), "Host load")
    return f"{lead} while this ran: {numbers}." if numbers else f"{lead} while this ran."
