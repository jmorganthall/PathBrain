"""Process-tree accounting and orphan reaping for the browser probe.

PathBrain launches exactly one Chromium at a time, through exactly one code path
(:class:`~pathbrain.plugins.benchmark_browser.BrowserBenchmark`), on exactly one thread
(``probes``). Everything about the design says a leak is impossible, and the host said
otherwise: 224 node drivers, 887 Chrome processes, 13 GiB of node and 6.6 GiB of Chrome,
against 593 MiB free on a 32 GiB NAS.

Two things make that possible and this module is the answer to both.

**Leaks are inevitable in one direction.** ``probes`` deliberately abandons a wedged
worker rather than blocking the pipeline behind it, and an abandoned worker's Chromium
cannot be closed — closing it means calling into the wedged browser from a thread that
does not own it. That trade is right, but on its own it is *unbounded*: every stall
permanently costs a process tree, and nothing ever collects them. Reaping turns an
unbounded leak into a bounded one.

**A leak is invisible from inside the app.** A dropped Playwright handle raises nothing,
logs nothing and changes no number PathBrain reports — the only place it shows up is the
host's memory, hours later, as an OOM kill. So the counts are read directly from
``/proc`` and reported on the health endpoint: node processes, Chrome processes, zombies,
resident memory. "It seems to be leaking" becomes a number.

Deliberately dependency-free (no ``psutil``) and read from ``/proc`` by hand, matching
the rest of the codebase: it degrades to empty results on any platform without ``/proc``,
so nothing here can be the reason a measurement fails.

Safety: every function is scoped to **descendants of this process**. A container gives us
our own PID namespace, but the scoping is what makes the reaper correct rather than
merely contained — it can only ever kill something PathBrain itself started.
"""
from __future__ import annotations

import os
import signal
from typing import Iterable

from .logging_config import get_logger

log = get_logger("browser_procs")

_PROC = "/proc"

#: A Playwright driver is ``node .../playwright/cli.js run-driver`` (or ``run-server``).
#: Matching on ``playwright`` in the command line is what distinguishes our driver from
#: any other node process that might one day share the container.
_DRIVER_MARKERS = ("playwright",)
_NODE_NAMES = ("node", "nodejs")
_CHROME_MARKERS = ("chrome", "chromium", "headless_shell")


def available() -> bool:
    """True when this platform exposes ``/proc`` (Linux — i.e. the container)."""
    return os.path.isdir(_PROC)


def _pids() -> list[int]:
    if not available():
        return []
    out = []
    try:
        for name in os.listdir(_PROC):
            if name.isdigit():
                out.append(int(name))
    except OSError:
        return []
    return out


def _stat(pid: int) -> tuple[str, str, int] | None:
    """``(comm, state, ppid)`` for ``pid``, or None if it is gone.

    ``comm`` is parenthesised in ``/proc/pid/stat`` and may itself contain spaces and
    parentheses, so the fields after it are found from the LAST ``)`` rather than by
    splitting the whole line.
    """
    try:
        with open(f"{_PROC}/{pid}/stat", "rb") as fh:
            raw = fh.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return None
    close = raw.rfind(")")
    open_paren = raw.find("(")
    if close == -1 or open_paren == -1:
        return None
    comm = raw[open_paren + 1 : close]
    rest = raw[close + 2 :].split()
    if len(rest) < 2:
        return None
    try:
        return comm, rest[0], int(rest[1])
    except (TypeError, ValueError):
        return None


def _cmdline(pid: int) -> str:
    try:
        with open(f"{_PROC}/{pid}/cmdline", "rb") as fh:
            return fh.read().decode("utf-8", "replace").replace("\x00", " ").strip()
    except OSError:
        return ""


def _rss_kb(pid: int) -> int:
    """Resident set size in KiB, from ``statm`` (pages) — 0 when unreadable."""
    try:
        with open(f"{_PROC}/{pid}/statm", "rb") as fh:
            fields = fh.read().split()
        return int(fields[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
    except (OSError, IndexError, ValueError):
        return 0


def _children_index() -> dict[int, list[int]]:
    index: dict[int, list[int]] = {}
    for pid in _pids():
        st = _stat(pid)
        if st is None:
            continue
        index.setdefault(st[2], []).append(pid)
    return index


def descendants(pid: int, index: dict[int, list[int]] | None = None) -> list[int]:
    """Every process under ``pid`` (breadth-first, excluding ``pid`` itself)."""
    index = index if index is not None else _children_index()
    out: list[int] = []
    frontier = list(index.get(pid, ()))
    seen: set[int] = set()
    while frontier:
        child = frontier.pop()
        if child in seen:
            continue
        seen.add(child)
        out.append(child)
        frontier.extend(index.get(child, ()))
    return out


def _kind(comm: str, cmdline: str) -> str:
    lowered = f"{comm} {cmdline}".lower()
    if any(marker in lowered for marker in _CHROME_MARKERS):
        return "chrome"
    if comm in _NODE_NAMES or " node " in f" {lowered} ":
        return "node"
    return "other"


def driver_pids() -> list[int]:
    """Direct children of this process that are Playwright node drivers.

    Playwright spawns its driver as an immediate child of the Python process, so this is
    the complete set of browser trees we own — one per live *or leaked* Playwright.
    """
    if not available():
        return []
    me = os.getpid()
    out: list[int] = []
    for pid in _children_index().get(me, ()):
        st = _stat(pid)
        if st is None:
            continue
        comm, _state, _ppid = st
        cmdline = _cmdline(pid)
        lowered = f"{comm} {cmdline}".lower()
        if comm in _NODE_NAMES and any(m in lowered for m in _DRIVER_MARKERS):
            out.append(pid)
    return out


def snapshot() -> dict:
    """Counts and resident memory for the browser process trees this process owns.

    Reported on ``/api/health/pipeline``. ``drivers`` is the number that matters: it should
    be 0 between runs and 1 during one. Anything else is leaked, and ``drivers`` minus the
    live one is exactly how many trees are orphaned.
    """
    if not available():
        return {"available": False}
    me = os.getpid()
    index = _children_index()
    node = chrome = zombies = 0
    rss_kb = 0
    for pid in descendants(me, index):
        st = _stat(pid)
        if st is None:
            continue
        comm, state, _ppid = st
        if state == "Z":
            zombies += 1
            continue  # a zombie holds no memory and has no useful cmdline
        kind = _kind(comm, _cmdline(pid))
        if kind == "chrome":
            chrome += 1
        elif kind == "node":
            node += 1
        rss_kb += _rss_kb(pid)
    return {
        "available": True,
        "pid": me,
        "self_rss_mb": round(_rss_kb(me) / 1024.0, 1),
        "drivers": len(driver_pids()),
        "node": node,
        "chrome": chrome,
        "zombies": zombies,
        "children_rss_mb": round(rss_kb / 1024.0, 1),
    }


def kill_tree(pid: int, *, index: dict[int, list[int]] | None = None) -> int:
    """SIGKILL ``pid`` and everything under it. Returns how many signals landed.

    Children are killed before the parent so the driver cannot re-parent or respawn on
    its way out, and SIGKILL rather than SIGTERM because the trees this is used on are by
    definition ones that stopped answering.
    """
    index = index if index is not None else _children_index()
    targets = descendants(pid, index) + [pid]
    killed = 0
    for target in targets:
        try:
            os.kill(target, signal.SIGKILL)
            killed += 1
        except ProcessLookupError:
            pass  # already gone — the normal case for a tree that closed cleanly
        except PermissionError:
            log.warning("Not permitted to kill pid %s", target)
    return killed


def reap_orphans(keep: Iterable[int] = ()) -> dict:
    """Kill every Playwright driver tree we own except the ones in ``keep``.

    Called where the caller provably holds no browser it still intends to use: before
    launching a fresh one, and after a close that could not be completed. Any driver still
    alive at that point was dropped by ``abandon()`` or survived a failed ``stop()`` — it
    is unreachable from Python and will never be closed by anything, so the only way it
    goes away is this.

    Returns ``{"reaped": n, "pids": [...]}``; never raises.
    """
    if not available():
        return {"reaped": 0, "pids": []}
    keep_set = {int(p) for p in keep if p}
    try:
        index = _children_index()
        orphans = [pid for pid in driver_pids() if pid not in keep_set]
        for pid in orphans:
            kill_tree(pid, index=index)
    except Exception:  # noqa: BLE001 — reaping must never be why a measurement fails
        log.warning("Orphan reap failed", exc_info=True)
        return {"reaped": 0, "pids": []}
    if orphans:
        log.warning(
            "Reaped %s orphaned Playwright driver tree(s): %s. These were browsers "
            "abandoned by a wedged probe or left behind by a close that could not "
            "complete — unreachable from Python, so nothing else would ever free them.",
            len(orphans), orphans,
        )
    return {"reaped": len(orphans), "pids": orphans}
