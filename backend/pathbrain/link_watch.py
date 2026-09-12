"""Link watch — a continuous ping beside every firewall write, so the ledger says which
write cost the household what.

``write_probe`` answers "what does *a* write cost?" on demand, under supervision, on a
value you choose. That is the right instrument for a controlled experiment and the wrong
one for the question actually being asked, which is **"which of the writes PathBrain
already makes is the one that breaks the firewall?"** — a question about writes that have
already happened, on a link nobody was watching at the time, at 03:00 while a duel ladder
ran. You cannot answer it by running a probe, because the probe is not the write that hurt.

So the probe's instrument runs **all the time**, beside the writes the engines make
anyway, and every ledger row gains what it cost:

* two targets, the same two and for the same reason (see :mod:`write_probe`) — the
  firewall's own address, which going silent means the *box* is wedged, and a public
  address *through* it, which going silent alone means forwarding broke while the box
  stayed healthy. One target cannot separate those and they call for opposite responses.
* a rolling buffer of the last :data:`RETAIN_S`, so the window around a write is already
  on record by the time the write finishes — the measurement cannot be "started" after
  the event it is measuring.
* the headline is the **worst continuous gap**, never a loss rate: twenty scattered drops
  and two seconds of nothing are the same percentage and only the second is an outage.

**The gaps nobody wrote for are the control, and they are recorded too.** An instrument
that only looked at write windows would attribute every ISP hiccup to PathBrain — it can
only ever find what it is pointed at. The sweep therefore walks the *whole* settled
timeline and files each gap either against the write in flight at the time
(``write_id``) or as unattributed. "Six gaps last night, five of them with no write within
seconds" and "six gaps, every one during a reconfigure" are opposite findings, and a
watcher that only sampled around writes reports them identically.

Attribution is deliberately **a window, not a cause**: a gap that starts within
[write − :data:`PRE_S`, write's return + :data:`POST_S`] is recorded against that write
because it is contemporaneous, which is evidence and not proof. The recovery window is
part of the cost on purpose — how long the network takes to come *back* is as much what
the household feels as the drop.

Read-only throughout: this module sends ICMP echoes and writes its own rows. It never
touches the firewall, so it needs no guard, no coordinator lock and no arming.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .database import session_scope
from .logging_config import get_logger
from .models import FirewallWrite, LinkGap

log = get_logger("link_watch")

#: A sample is lost if it does not answer in this long. Under a second on purpose: during
#: an outage the sampler must keep its own clock rather than block, so the timeline stays
#: honest about *when* packets stopped rather than merely how many went missing.
SAMPLE_TIMEOUT_S = 0.9
#: Below this, a run of missed packets is ordinary internet rather than an event worth
#: naming. Matches ``write_probe.MIN_GAP_MS`` so the two instruments agree on what a gap is.
MIN_GAP_MS = 250.0
#: How much history each target keeps. A write's window must already be in the buffer when
#: the write lands, and a restart-and-look-later is not a use case: 15 minutes is plenty to
#: cover the slowest write plus its recovery, and costs a few thousand tuples.
RETAIN_S = 900.0
#: Seconds before a write's first packet that still count as its window — a reconfigure's
#: effect can land while the HTTP call is still being answered.
PRE_S = 3.0
#: Seconds after a write returns that still count as its window. Recovery is part of the
#: cost, and a queue rebuild's damage outlives the API call that caused it.
POST_S = 12.0
#: How often the sweep looks for gaps nobody wrote for.
SWEEP_S = 30.0
#: Default send rate per target. 5 Hz resolves a 250 ms gap as one or two missed packets
#: while costing the link ten packets a second across both targets — continuous, so it is
#: deliberately gentler than the on-demand probe's 10 Hz.
DEFAULT_HZ = 5.0

_lock = threading.Lock()
_state: dict = {
    "running": False,
    "targets": {},          # label -> _Target
    "worker": None,
    "stop": None,
    "pending": [],          # windows awaiting attribution
    "recent_windows": [],   # windows already scored, so the sweep cannot re-file them
    "swept_to": None,       # wall clock the sweep has settled up to
    "error": None,
    "started_at": None,
    "hz": DEFAULT_HZ,
}


class _Target:
    """One ping target, sampled on its own thread for as long as the watch runs.

    The buffer is a ``deque`` trimmed by age rather than by count, so the retention is a
    promise about *time* ("the last fifteen minutes are on record") which is what the
    attribution window needs — a count-bounded buffer silently holds less history exactly
    when the rate is raised.
    """

    def __init__(self, label: str, address: str, hz: float) -> None:
        self.label = label
        self.address = address
        self.interval = 1.0 / max(0.5, float(hz))
        self.samples: deque[tuple[float, float | None]] = deque()
        self.error: str | None = None
        self.sent = 0
        self.lost = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"link-watch-{self.label}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        try:
            from icmplib import ping
        except Exception as exc:  # noqa: BLE001
            self.error = f"icmplib unavailable: {exc}"
            log.warning("link_watch: %s cannot sample — %s", self.label, self.error)
            return
        while not self._stop.is_set():
            started = time.time()
            rtt: float | None = None
            try:
                host = ping(self.address, count=1, timeout=SAMPLE_TIMEOUT_S, privileged=False)
                if host.rtts:
                    rtt = round(float(host.rtts[0]), 3)
            except Exception as exc:  # noqa: BLE001 — a failed send is a loss, not a crash
                if self.error is None:
                    self.error = f"{type(exc).__name__}: {exc}"
                    log.warning("link_watch: %s ping failed — %s", self.label, self.error)
            with self._guard:
                self.samples.append((started, rtt))
                self.sent += 1
                if rtt is None:
                    self.lost += 1
                cutoff = started - RETAIN_S
                while self.samples and self.samples[0][0] < cutoff:
                    self.samples.popleft()
            # Pace from the send, so a slow reply does not stretch the series.
            self._stop.wait(max(0.0, self.interval - (time.time() - started)))

    def window(self, start: float, end: float) -> list[tuple[float, float | None]]:
        with self._guard:
            return [s for s in self.samples if start <= s[0] <= end]


def find_gaps(samples: list[tuple[float, float | None]]) -> list[dict]:
    """Every run of consecutive losses at or above :data:`MIN_GAP_MS`, oldest first.

    A run is measured from its first missed send to the next *answered* one, so the
    duration is how long the target was actually unreachable rather than how many packets
    were skipped. A run still open at the end of the samples is measured to the last send —
    it may be longer, and reporting the part we watched is the honest floor.
    """
    gaps: list[dict] = []
    run_start: float | None = None
    for t, rtt in samples:
        if rtt is None:
            if run_start is None:
                run_start = t
            continue
        if run_start is not None:
            span = (t - run_start) * 1000.0
            if span >= MIN_GAP_MS:
                gaps.append({"started_at": run_start, "duration_ms": round(span, 1), "open": False})
            run_start = None
    if run_start is not None and samples:
        span = (samples[-1][0] - run_start) * 1000.0
        if span >= MIN_GAP_MS:
            gaps.append({"started_at": run_start, "duration_ms": round(span, 1), "open": True})
    return gaps


def summarize(samples: list[tuple[float, float | None]]) -> dict:
    """One target over one window: how much was lost, the worst continuous gap, the RTTs."""
    if not samples:
        return {"sent": 0, "lost": 0, "loss_pct": None, "worst_gap_ms": None,
                "gaps": [], "rtt_median_ms": None, "rtt_max_ms": None}
    rtts = sorted(r for _, r in samples if r is not None)
    gaps = find_gaps(samples)
    lost = sum(1 for _, r in samples if r is None)
    return {
        "sent": len(samples),
        "lost": lost,
        "loss_pct": round(100.0 * lost / len(samples), 1),
        "worst_gap_ms": max((g["duration_ms"] for g in gaps), default=0.0),
        "gaps": gaps,
        "rtt_median_ms": rtts[len(rtts) // 2] if rtts else None,
        "rtt_max_ms": rtts[-1] if rtts else None,
    }


def verdict(targets: dict) -> str:
    """One sentence for a write's window, with its numbers in it.

    Leads with the box going silent whenever it did, because "the firewall stopped
    answering" and "traffic through the firewall stopped" are different failures with
    different responses, and the first is the one no configuration API should ever cause.
    """
    box = float((targets.get("firewall") or {}).get("worst_gap_ms") or 0.0)
    through = float((targets.get("through") or {}).get("worst_gap_ms") or 0.0)
    if box > 0 and through > 0:
        return (f"The firewall itself stopped answering for {box / 1000:.1f}s during this write "
                f"(and traffic through it for {through / 1000:.1f}s) — the box went away, not just "
                "its traffic.")
    if box > 0:
        return (f"The firewall's own address went quiet for {box / 1000:.1f}s during this write "
                "while traffic through it kept flowing — the box was busy, forwarding was not.")
    if through > 0:
        return (f"Traffic through the firewall stopped for {through / 1000:.1f}s during this write "
                "while the box itself kept answering — the queue rebuild dropped flows in flight.")
    return "No gap above the threshold on either target: this write cost nothing measurable."


# --------------------------------------------------------------------------- lifecycle

def config() -> dict:
    from .config_store import get_config

    try:
        with session_scope() as s:
            cfg = dict((get_config(s) or {}).get("firewall") or {})
    except Exception:  # noqa: BLE001 — the watch must never be why startup fails
        log.debug("link_watch: could not read config; using defaults", exc_info=True)
        cfg = {}
    return {
        "enabled": bool(cfg.get("watch_enabled", True)),
        "hz": float(cfg.get("watch_hz") or DEFAULT_HZ),
        "through_target": str(cfg.get("watch_through_target") or "1.1.1.1"),
    }


def running() -> bool:
    return bool(_state.get("running"))


def start(*, firewall_target: str | None = None, through_target: str | None = None,
          hz: float | None = None) -> dict:
    """Begin sampling. Idempotent; never raises — a watcher that cannot start must not be
    able to stop the application that does the actual work."""
    with _lock:
        if _state["running"]:
            return status()
        cfg = config()
        if firewall_target is None:
            from .write_probe import firewall_address

            firewall_target = firewall_address()
        through = through_target or cfg["through_target"]
        rate = float(hz or cfg["hz"])
        targets: dict[str, _Target] = {}
        if firewall_target:
            targets["firewall"] = _Target("firewall", firewall_target, rate)
        if through:
            targets["through"] = _Target("through", through, rate)
        if not targets:
            _state["error"] = "no ping targets: the provider has no address and no through-target is set"
            log.warning("link_watch: %s", _state["error"])
            return status()
        for t in targets.values():
            t.start()
        _state.update({
            "running": True, "targets": targets, "error": None, "hz": rate,
            "started_at": time.time(), "pending": [], "recent_windows": [],
            "swept_to": time.time(),
            "stop": threading.Event(),
        })
        _state["worker"] = threading.Thread(target=_work, name="link-watch", daemon=True)
        _state["worker"].start()
        log.info("link_watch: watching %s at %.1f Hz",
                 ", ".join(f"{k}={v.address}" for k, v in targets.items()), rate)
        return status()


def stop() -> None:
    with _lock:
        if not _state["running"]:
            return
        _state["running"] = False
        ev = _state.get("stop")
        targets = list(_state["targets"].values())
    if ev is not None:
        ev.set()
    for t in targets:
        t.stop()
    log.info("link_watch: stopped")


def status() -> dict:
    """What the watch is doing right now, plus the last minute as a live reading."""
    targets = _state.get("targets") or {}
    now = time.time()
    out = {
        "running": bool(_state.get("running")),
        "error": _state.get("error"),
        "hz": _state.get("hz"),
        "retain_s": RETAIN_S,
        "min_gap_ms": MIN_GAP_MS,
        "window": {"pre_s": PRE_S, "post_s": POST_S},
        "started_at": _state.get("started_at"),
        "targets": {},
    }
    for label, t in targets.items():
        recent = summarize(t.window(now - 60.0, now))
        recent.pop("gaps", None)
        out["targets"][label] = {
            "address": t.address, "error": t.error,
            "sent_total": t.sent, "lost_total": t.lost,
            "last_minute": recent,
        }
    return out


# ------------------------------------------------------------------- write attribution

def note_write(write_id: int | None, op: str, started_at: float, ended_at: float,
               owner: str | None = None) -> None:
    """Register the window one firewall write occupied. Never raises.

    Called from the write path itself, so it does exactly two things: appends a tuple, and
    returns. Everything expensive — waiting out the recovery window, reading the samples,
    updating the row — happens on the worker, because a write must never be slowed by the
    instrument watching it.
    """
    try:
        if not _state.get("running"):
            return
        _state["pending"].append({
            "write_id": write_id, "op": op, "owner": owner,
            "start": float(started_at) - PRE_S, "end": float(ended_at) + POST_S,
            "due": float(ended_at) + POST_S,
        })
    except Exception:  # noqa: BLE001
        log.debug("link_watch: could not register a write window", exc_info=True)


def _work() -> None:
    ev: threading.Event = _state["stop"]
    last_sweep = time.time()
    while not ev.is_set():
        try:
            _settle_due()
            if time.time() - last_sweep >= SWEEP_S:
                _sweep()
                last_sweep = time.time()
        except Exception:  # noqa: BLE001 — the watcher outlives its own bugs
            log.debug("link_watch: worker pass failed", exc_info=True)
        ev.wait(1.0)


def _settle_due() -> None:
    """Score every write whose recovery window has elapsed."""
    now = time.time()
    pending = _state.get("pending") or []
    due = [p for p in pending if p["due"] <= now]
    if not due:
        return
    _state["pending"] = [p for p in pending if p["due"] > now]
    for p in due:
        _score_write(p)


def _score_write(window: dict) -> None:
    # Remember the span even after the write leaves ``pending``: the sweep runs behind the
    # settling by design, so without this a gap could be filed twice — once against its
    # write and once as unattributed, which reads as two outages and inverts the control.
    recent = _state.setdefault("recent_windows", [])
    recent.append((window["start"], window["end"]))
    cutoff = time.time() - RETAIN_S
    _state["recent_windows"] = [w for w in recent if w[1] >= cutoff]

    targets = _state.get("targets") or {}
    per_target = {label: summarize(t.window(window["start"], window["end"]))
                  for label, t in targets.items()}
    box = float((per_target.get("firewall") or {}).get("worst_gap_ms") or 0.0)
    through = float((per_target.get("through") or {}).get("worst_gap_ms") or 0.0)
    worst = max(box, through)
    sentence = verdict(per_target)

    if window.get("write_id"):
        try:
            with session_scope() as s:
                row = s.get(FirewallWrite, int(window["write_id"]))
                if row is not None:
                    row.gap_ms = worst
                    row.box_gap_ms = box
                    row.through_gap_ms = through
                    row.watch = {
                        "window": {"start": round(window["start"], 3), "end": round(window["end"], 3)},
                        "targets": per_target,
                        "verdict": sentence,
                    }
        except Exception:  # noqa: BLE001
            log.debug("link_watch: could not attach the window to write %s", window.get("write_id"),
                      exc_info=True)

    for label, summary in per_target.items():
        for gap in summary.get("gaps") or []:
            _record_gap(label, gap, write_id=window.get("write_id"), op=window.get("op"),
                        owner=window.get("owner"))
    if worst > 0:
        log.warning("link_watch: %s (write %s) — %s", window.get("op"), window.get("write_id"), sentence)


def _sweep() -> None:
    """File every gap in the settled timeline that no write window covers.

    This is the control group, and it is the whole reason the reading means anything: an
    instrument that only looked where the writes are would blame PathBrain for the ISP.
    Bounded to the region older than the longest recovery window, so a gap is never filed
    as unattributed while a write that would claim it is still pending.
    """
    targets = _state.get("targets") or {}
    if not targets:
        return
    now = time.time()
    settled_to = now - POST_S - 1.0
    since = float(_state.get("swept_to") or (settled_to - SWEEP_S))
    if settled_to <= since:
        return
    _state["swept_to"] = settled_to
    claimed = [(p["start"], p["end"]) for p in list(_state.get("pending") or [])]
    claimed += list(_state.get("recent_windows") or [])
    for label, t in targets.items():
        # Read a little either side so a gap straddling the boundary is seen whole, then
        # keep only the ones that *start* in the swept region — so it is filed exactly once.
        for gap in find_gaps(t.window(since - POST_S, settled_to + POST_S)):
            start = gap["started_at"]
            if not (since <= start < settled_to):
                continue
            if any(a <= start <= b for a, b in claimed):
                continue
            _record_gap(label, gap, write_id=None, op=None, owner=None)


def _record_gap(target: str, gap: dict, *, write_id: int | None, op: str | None,
                owner: str | None) -> None:
    try:
        at = datetime.fromtimestamp(gap["started_at"], tz=timezone.utc).replace(tzinfo=None)
        with session_scope() as s:
            # Idempotent by (target, second): the sweep and a write window can both see the
            # same gap, and one event recorded twice reads as two outages.
            dup = s.scalars(
                select(LinkGap).where(
                    LinkGap.target == target,
                    LinkGap.at >= at - timedelta(seconds=1),
                    LinkGap.at <= at + timedelta(seconds=1),
                ).limit(1)
            ).first()
            if dup is not None:
                if write_id and not dup.write_id:
                    dup.write_id, dup.op, dup.owner = write_id, op, owner
                if float(gap["duration_ms"]) > float(dup.duration_ms or 0):
                    dup.duration_ms = float(gap["duration_ms"])
                return
            s.add(LinkGap(at=at, target=target, duration_ms=float(gap["duration_ms"]),
                          write_id=write_id, op=op, owner=owner))
    except Exception:  # noqa: BLE001
        log.debug("link_watch: could not record a gap", exc_info=True)


def recent_gaps(limit: int = 100) -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(select(LinkGap).order_by(LinkGap.id.desc()).limit(limit)).all()
        return [{
            "id": r.id,
            "at": r.at.replace(tzinfo=timezone.utc).isoformat() if r.at else None,
            "target": r.target,
            "duration_ms": r.duration_ms,
            "write_id": r.write_id,
            "op": r.op,
            "owner": r.owner,
        } for r in rows]


def gap_summary(hours: float = 24.0) -> dict:
    """The one reading that answers the question: of the gaps seen, how many had a write in
    flight? A count on its own says the link is unstable; the split says whose fault it is."""
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    with session_scope() as s:
        rows = s.scalars(select(LinkGap).where(LinkGap.at >= since)).all()
        attributed = [r for r in rows if r.write_id]
        return {
            "hours": hours,
            "gaps": len(rows),
            "during_a_write": len(attributed),
            "unattributed": len(rows) - len(attributed),
            "worst_ms": max((float(r.duration_ms or 0) for r in rows), default=0.0),
            "worst_during_a_write_ms": max((float(r.duration_ms or 0) for r in attributed), default=0.0),
        }
