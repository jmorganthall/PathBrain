"""Write-and-ping: what does one firewall write actually cost the household?

Every other instrument in PathBrain measures the *link*. This one measures **PathBrain**,
because the reload-storm incident exposed that the one thing never on an instrument was
the write path's effect on the network. "The internet blipped" was the only evidence
anyone had, and it cannot distinguish a 200 ms hiccup from a thirty-second outage, nor say
which half of a write caused it.

**A profile switch is two operations, and they are not equally expensive.** Writing the
fields (``setPipe``) edits the firewall's configuration and touches nothing that is
running. Reloading the shaper (``reconfigure``) rebuilds the dummynet queues, and *that*
is what drops flows in flight. Until ``ConfigProvider.reconfigure`` existed they could
only be issued together, so the cost could not be attributed. This probe issues them one
at a time, with ping running throughout, and reports what each cost.

**Two ping targets, never one**, because the failure modes they separate are the whole
diagnosis:

* the **firewall itself** (its LAN address) — if this stops answering, the box is wedged:
  CPU, kernel, a panic. Nothing about writing a config field should ever do this.
* **through** the firewall (a public address) — if the firewall answers but this does not,
  forwarding broke while the box stayed healthy, which is what a queue rebuild looks like.

One target cannot tell those apart, and they call for completely different responses.

**Steps**, each followed by a settle window so the recovery is measured too:

    baseline → set fields (no reload) → reload the shaper → restore fields → reload

The restore is deliberately a full switch: it is the operation the engines actually
perform, so its cost belongs on the same timeline as the decomposed halves.

Every write goes through ``get_provider()`` like any other, so the guard ledgers, paces,
budgets and can refuse it — a diagnostic that bypassed the guard to study the guard's
subject would be the one unsupervised write path in the system. It holds the coordinator
lock (it applies profiles and must not overlap a benchmark) and restores what it changed
in a ``finally``.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import coordinator, firewall_guard
from .database import session_scope
from .logging_config import get_logger
from .models import WriteProbe, WriteProbeStatus
from .providers import get_provider
from .session_runtime import describe_failure
from .settings_profile import _field_equal
from .shaper_fields import FIELD_LABELS, WRITABLE_FIELDS, coerce_value
from .shaper_fields import field as shaper_field
from .stats import spearman

log = get_logger("write_probe")

#: How often each sampler sends. 100 ms so a 300 ms gap is three missed packets rather
#: than a coin toss — the default 1 Hz ping cannot see a blip this instrument exists for.
SAMPLE_INTERVAL_S = 0.1
#: A sample is lost if it does not answer in this long. Deliberately under a second: during
#: an outage the sampler must keep its own clock rather than block, so the timeline stays
#: honest about *when* packets stopped rather than merely how many were missed.
SAMPLE_TIMEOUT_S = 0.9
#: Below this, a run of missed packets is ordinary internet rather than an event worth
#: naming. Two consecutive 100 ms samples can go astray on any link.
MIN_GAP_MS = 250.0
DEFAULT_SETTLE_S = 10.0
DEFAULT_BASELINE_S = 15.0
MAX_SETTLE_S = 120.0
#: A sweep step's settle must OUTLAST the outage it is measuring. ``summarize`` reports the
#: worst gap *inside* the step's window, and a run still lost when the window closes is
#: measured only to the last sample in it — so a 35 s outage read through the single probe's
#: 10 s settle reports 10 s and silently truncates the finding. The link watch measured
#: 33–35 s of silence per reconfigure on this link, so a sweep settles well past that.
SWEEP_SETTLE_S = 45.0
#: A number with a unit the provider owns ("880Mbit"). A bandwidth is stepped by moving the
#: number and keeping the suffix, because the legal forms of that string are the firewall's
#: business and not this module's. (``shaper_fields`` parses the same shape for its own
#: coercion; this is the one place that has to *rebuild* the value, hence its own pattern.)
_NUM_UNIT_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(.*)$")

_state: dict = {"active": False, "id": None, "thread": None, "cancel": False}


def active() -> bool:
    return bool(_state.get("active"))


def cancel() -> bool:
    if not active():
        return False
    _state["cancel"] = True
    log.info("Write probe %s: cancel requested", _state.get("id"))
    return True


class _Sampler:
    """One ping target, sampled on its own thread for the life of the probe.

    Records every send as ``{t, rtt_ms}`` with ``rtt_ms`` None for a loss, so the timeline
    is a real time series rather than a count — the question is *when* packets stopped and
    for how long, which a sent/received tally cannot answer.
    """

    def __init__(self, label: str, target: str) -> None:
        self.label = label
        self.target = target
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"probe-ping-{self.label}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        try:
            from icmplib import ping
        except Exception as exc:  # noqa: BLE001
            self.error = f"icmplib unavailable: {exc}"
            return
        while not self._stop.is_set():
            started = time.time()
            rtt: float | None = None
            try:
                host = ping(self.target, count=1, timeout=SAMPLE_TIMEOUT_S, privileged=False)
                if host.rtts:
                    rtt = round(float(host.rtts[0]), 3)
            except Exception as exc:  # noqa: BLE001 — a failed send is a loss, not a crash
                if self.error is None:
                    self.error = f"{type(exc).__name__}: {exc}"
            self.samples.append({"t": round(started, 3), "rtt_ms": rtt})
            # Pace from the send, so a slow reply does not stretch the series.
            self._stop.wait(max(0.0, SAMPLE_INTERVAL_S - (time.time() - started)))


def summarize(samples: list[dict], start: float, end: float) -> dict:
    """What one window of one target's samples says: loss, the worst continuous gap, RTT.

    ``worst_gap_ms`` is the headline — the longest unbroken run of lost packets, which is
    how long the network was *actually* unusable. A loss percentage cannot distinguish
    twenty scattered drops from two seconds of nothing, and only the second is an outage.
    """
    window = [s for s in samples if start <= s["t"] <= end]
    if not window:
        return {"sent": 0, "lost": 0, "loss_pct": None, "worst_gap_ms": None,
                "rtt_median_ms": None, "rtt_max_ms": None, "gap_started_at": None}
    lost = [s for s in window if s["rtt_ms"] is None]
    rtts = sorted(s["rtt_ms"] for s in window if s["rtt_ms"] is not None)

    worst = 0.0
    worst_at: float | None = None
    run_start: float | None = None
    prev: dict | None = None
    for s in window:
        if s["rtt_ms"] is None:
            if run_start is None:
                run_start = s["t"]
        else:
            if run_start is not None and prev is not None:
                span = (s["t"] - run_start) * 1000.0
                if span > worst:
                    worst, worst_at = span, run_start
            run_start = None
        prev = s
    if run_start is not None:  # still lost at the end of the window
        span = (window[-1]["t"] - run_start) * 1000.0
        if span > worst:
            worst, worst_at = span, run_start

    return {
        "sent": len(window),
        "lost": len(lost),
        "loss_pct": round(100.0 * len(lost) / len(window), 1),
        "worst_gap_ms": round(worst, 1) if worst >= MIN_GAP_MS else 0.0,
        "gap_started_at": worst_at if worst >= MIN_GAP_MS else None,
        "rtt_median_ms": rtts[len(rtts) // 2] if rtts else None,
        "rtt_max_ms": rtts[-1] if rtts else None,
    }


def verdict(steps: list[dict]) -> str:
    """One sentence, with its numbers in it, naming which operation cost what.

    The comparison the whole probe exists to make: did writing the fields hurt, or only the
    reload? And did the firewall itself stop answering, or only traffic through it?
    """
    def worst(step: dict, target: str) -> float:
        return float(((step.get("targets") or {}).get(target) or {}).get("worst_gap_ms") or 0.0)

    named = {s["step"]: s for s in steps}
    fields = named.get("set_fields")
    reload_ = named.get("reload")
    restore = named.get("restore")

    # The restore is a real profile switch and is measured like one — and this used to read
    # only the two halves above it, so a probe whose RESTORE took the box off the network for
    # 33 seconds was reported as "this write was cheap". An instrument that can print the
    # outage and the all-clear on the same screen is worse than none, because the all-clear
    # is the line people act on. Whatever the halves say, a costly restore leads.
    if restore is not None:
        s_box, s_through = worst(restore, "firewall"), worst(restore, "through")
        if s_box > 0 or s_through > 0:
            which = (f"the firewall itself for {s_box / 1000:.1f}s" if s_box > 0
                     else f"traffic through it for {s_through / 1000:.1f}s")
            also = (f" (traffic through it for {s_through / 1000:.1f}s)"
                    if s_box > 0 and s_through > 0 else "")
            return (
                f"Putting the original values back cost more than the change did: it silenced "
                f"{which}{also}. The restore is a full profile switch — the same operation "
                "every duel leg performs — so this is the cost the household actually pays, "
                "whatever the two halves above it read."
            )

    if reload_ is None:
        return "The probe did not reach the shaper reload, so the two halves were not compared."

    f_through = worst(fields, "through") if fields else 0.0
    r_through, r_box = worst(reload_, "through"), worst(reload_, "firewall")
    if r_box > 0:
        return (
            f"The firewall itself stopped answering for {r_box / 1000:.1f}s during the shaper "
            f"reload — the box went away, not just its traffic. Writing the fields cost "
            f"{f_through / 1000:.1f}s. No configuration API should be able to do this; this is "
            "the reading to take to a crash dump."
        )
    if r_through > 0 and f_through <= 0:
        return (
            f"The shaper reload cost {r_through / 1000:.1f}s of through-traffic while the "
            "firewall itself kept answering — the queue rebuild dropped flows, which is "
            "inherent to reconfiguring a live shaper. Writing the fields cost nothing "
            "measurable, so it is the reload, not the write, that the household feels."
        )
    if r_through > 0 and f_through > 0:
        return (
            f"Both halves cost through-traffic: writing the fields {f_through / 1000:.1f}s, "
            f"the reload {r_through / 1000:.1f}s. A field write alone should be free, so "
            "something beyond the queue rebuild is involved."
        )
    return (
        "Neither writing the fields nor reloading the shaper produced a gap above "
        f"{MIN_GAP_MS:.0f}ms on either target. This write was cheap — if the network still "
        "blips in normal use, the cause is a different value, a different pipe, or load."
    )


# ── The per-field sweep: which field's write costs the household the outage? ─────
#
# The probe above answers "the fields or the reload?". It cannot answer "*which* field?",
# and those have different fixes: a reload whose cost is the same whatever changed is
# inherent to reconfiguring a live shaper, while a reload that only hurts when one
# particular field moved is a lead. So the sweep steps each writable field on its own,
# puts it straight back, and measures both.


def step_value(field_key: str, current, options: list[float] | None = None):
    """The smallest write that still *is* a write: ``(new_value, how)``, or None.

    Deliberately **not** ``levers._generated_values``, which halves and doubles. That
    function is hunting a better value and wants a step big enough to move the Overall;
    this one is measuring what a write *costs*, so the ideal step is the one that changes
    the network least while still being a real ``setPipe`` — anything larger risks making
    the link genuinely worse for the settle window, which is the complaint the sweep exists
    to investigate rather than reproduce.

    * a bool is toggled,
    * an option-keyed field (CoDel ``target``/``interval``) moves to the adjacent **valid**
      option, because ``+1`` off that list silently doesn't take and would measure a write
      that never happened,
    * an integer moves ``+1``,
    * a bandwidth moves ``+1`` on its number with the unit preserved.

    None when there is no step to take — no value on record, an unknown field, or a select
    already alone on its list — and the caller reports that as a skip with its reason
    rather than inventing one.
    """
    fld = shaper_field(field_key)
    if fld is None or current is None:
        return None
    if fld.kind == "bool":
        return (not coerce_value(field_key, current), "toggled")
    if fld.kind == "int" or fld.unit:
        cur = coerce_value(field_key, current)
        if not isinstance(cur, int) or isinstance(cur, bool):
            return None
        opts = sorted({int(round(float(o))) for o in (options or [])})
        if opts:
            above = [o for o in opts if o > cur]
            if above:
                return (above[0], f"next option up ({cur} → {above[0]})")
            below = [o for o in opts if o < cur]
            if below:
                return (below[-1], f"next option down ({cur} → {below[-1]})")
            return None
        return (cur + 1, "+1")
    m = _NUM_UNIT_RE.match(str(current))
    if not m:
        return None
    num = float(m.group(1))
    stepped = int(num) + 1 if num.is_integer() else round(num + 1, 3)
    return (f"{stepped}{m.group(2)}", "+1")


#: Fields an automated sweep never touches, whatever the registry says is writable.
#:
#: ``flows`` is the flow-table size. Every other writable field is a parameter the shaper
#: *reads*; this one decides how many queues it allocates, so any change to it — ``1024 →
#: 1025`` included — forces a full flow-table rebuild rather than a re-read. Measured on
#: this link (probe #5): setting it was free, and putting it back took **35.3 s**, timed out
#: the ``apply_many`` call, took the box off the network for 33 s and tripped hands-off. A
#: field that costs a 35-second outage per step is not one a seven-step unattended sweep may
#: include: the sweep would spend its whole budget reproducing the outage it was built to
#: diagnose. Reachable by hand from the single probe, where a person is watching.
NEVER_STEP = frozenset({"flows"})


def sweep_fields() -> list[str]:
    """The fields a sweep may step: the registry's writable set minus :data:`NEVER_STEP`.

    Read from ``shaper_fields`` rather than listed here, so marking a field writable puts it
    in the sweep with no edit — the same rule the Shotgun Sweep follows for sweepable fields.
    """
    return [f for f in WRITABLE_FIELDS if f not in NEVER_STEP]


def plan_sweep(live: dict, pipe_uuid: str | None, fields: list[str] | None = None, *,
               options: dict | None = None, reload: bool = True,
               settle_s: float = SWEEP_SETTLE_S) -> dict:
    """What a sweep would do and what it would cost, before anything is written.

    ``live`` is ``{pipe uuid: field dict}`` as ``_drive`` builds it. Returns the ordered
    steps, the fields skipped and why, and the price: reconfigures (which the guard budgets)
    and wall clock (which the household notices). Pure — no writes, no clock, no provider —
    so the preview endpoint and the engine cannot disagree about what is about to happen.
    """
    pipe_uuid = pipe_uuid or next(iter(live), None)
    pipe = live.get(pipe_uuid) or {}
    allowed = set(sweep_fields())
    wanted = list(fields or sweep_fields())
    steps: list[dict] = []
    skipped: list[dict] = []
    for key in wanted:
        if key not in allowed:
            # Named explicitly by a caller, and still refused: NEVER_STEP is not a default
            # to be overridden by passing the field, because the reason it is on that list
            # does not change with who asked.
            skipped.append({
                "param": key, "label": FIELD_LABELS.get(key, key),
                "why": ("a sweep never steps it — a change forces a full flow-table rebuild, "
                        "measured at 35s of outage per step"
                        if key in NEVER_STEP else "not a writable field"),
            })
            continue
        stepped = step_value(key, pipe.get(key), (options or {}).get(key))
        if stepped is None:
            skipped.append({
                "param": key, "label": FIELD_LABELS.get(key, key),
                "why": ("the firewall reports no value for it"
                        if pipe.get(key) is None else "no smaller step this firewall can hold"),
            })
            continue
        value, how = stepped
        steps.append({
            "pipe_uuid": pipe_uuid, "param": key, "label": FIELD_LABELS.get(key, key),
            "from": pipe.get(key), "to": value, "how": how,
        })
    # Each field costs two writes — step it, put it back — and only a reload is a
    # reconfigure. ``reload=False`` is therefore free of both the guard's hourly budget and
    # its pacing gap, which is what makes the cheap pass worth running first.
    gap_s = float(firewall_guard.config().get("min_reconfigure_gap_s") or 0.0) if reload else 0.0
    reconfigures = len(steps) * 2 if reload else 0
    seconds = len(steps) * 2 * (settle_s + gap_s)
    # Every writable field's proposed step, NEVER_STEP ones included — what the *single*
    # probe should offer when a person picks that field by hand. It is the same rule the
    # sweep runs, computed once here, because the alternative is a second implementation of
    # "what is a sensible value for this field" in the frontend: that is how the value box
    # came to offer 4096 for `ecn`, having simply kept the number left over from `flows`.
    proposals: dict[str, dict] = {}
    for key in WRITABLE_FIELDS:
        stepped = step_value(key, pipe.get(key), (options or {}).get(key))
        if stepped is None:
            continue
        value, how = stepped
        proposals[key] = {
            "param": key, "label": FIELD_LABELS.get(key, key), "from": pipe.get(key),
            "to": value, "how": how, "sweepable": key not in NEVER_STEP,
        }
    return {
        "pipe_uuid": pipe_uuid, "steps": steps, "skipped": skipped, "proposals": proposals,
        "reconfigures": reconfigures, "seconds": round(seconds, 1), "reload": bool(reload),
        "settle_s": settle_s,
    }


def budget_shortfall(reconfigures: int) -> str | None:
    """Why this sweep must not start, or None.

    A sweep that runs out of the guard's hourly budget half way is the worst outcome
    available here: exceeding the cap trips hands-off, hands-off refuses **every** write
    including a restore, and the sweep is then holding a field at a value it cannot put
    back. So the cost is checked against the remaining budget up front and the sweep is
    refused rather than started — the same "don't start a session that can only fail" rule
    the write guard already applies elsewhere. Best-effort: an unreadable ledger returns
    None, because a guess about the budget must not be why a diagnostic is refused.
    """
    if reconfigures <= 0:
        return None
    try:
        cap = int(firewall_guard.config().get("max_reconfigures_per_hour") or 0)
        if cap <= 0:
            return None
        used = firewall_guard.reconfigures_since(
            datetime.now(timezone.utc) - timedelta(hours=1)
        )
    except Exception:  # noqa: BLE001 — never refuse a probe over a failed budget read
        log.debug("write_probe: could not read the reconfigure budget", exc_info=True)
        return None
    left = cap - used
    if reconfigures <= left:
        return None
    return (
        f"This sweep needs {reconfigures} reconfigures and only {max(0, left)} are left in the "
        f"hourly budget ({used} of {cap} used). Starting it would trip hands-off part way "
        "through, which refuses restores too — leaving a field on a value PathBrain could not "
        "put back. Sweep fewer fields, or wait for the hour to roll."
    )


def sweep_verdict(steps: list[dict]) -> str:
    """Which field cost what — and whether that reading is about the field at all.

    One pass gives one sample per field, so "this field is expensive" and "the fourth reload
    of a session is expensive" produce identical tables. The verdict therefore checks the
    gap against the step's **position** before it names a field, and says so when position
    explains it better: a ranking nobody can trust is worse than no ranking, and the reader
    cannot see the confound from the rows.
    """
    measured = [s for s in steps if s.get("param") and not s.get("failed")]
    if not measured:
        return "No field was stepped, so there is nothing to compare."

    def worst(step: dict) -> float:
        t = step.get("targets") or {}
        return max(float((t.get(k) or {}).get("worst_gap_ms") or 0.0) for k in ("firewall", "through")) \
            if t else 0.0

    by_field: dict[str, float] = {}
    for s in measured:
        key = str(s["param"])
        by_field[key] = max(by_field.get(key, 0.0), worst(s))
    if not any(by_field.values()):
        return (
            f"No field's write produced a gap above {MIN_GAP_MS:.0f}ms on either target. "
            "On this evidence the cost is not in any one field."
        )

    rho = spearman([float(s.get("index") or 0) for s in measured], [worst(s) for s in measured])
    ranked = sorted(by_field.items(), key=lambda kv: -kv[1])
    top, top_ms = ranked[0]
    top_label = FIELD_LABELS.get(top, top)
    rest = [f"{FIELD_LABELS.get(k, k)} {v / 1000:.1f}s" for k, v in ranked[1:4]]
    tail = f" Next: {', '.join(rest)}." if rest else ""
    if rho is not None and abs(rho) >= 0.6:
        return (
            f"{top_label} showed the worst gap ({top_ms / 1000:.1f}s), but the gap tracked each "
            f"step's POSITION in the sweep (ρ {rho:+.2f}), not which field moved — so this "
            "ranking is about when the write happened, not what it wrote. Re-run with the "
            f"fields in a different order before believing it.{tail}"
        )
    box = max((float(((s.get('targets') or {}).get('firewall') or {}).get('worst_gap_ms') or 0.0)
               for s in measured), default=0.0)
    lead = (
        f"The firewall itself stopped answering — worst {box / 1000:.1f}s — so this is the box "
        "going away, not just its traffic. "
        if box > 0 else ""
    )
    return (
        f"{lead}{top_label} cost the most: {top_ms / 1000:.1f}s. Position does not explain the "
        f"spread (ρ {rho:+.2f}), so the difference is about which field moved.{tail}"
        if rho is not None else
        f"{lead}{top_label} cost the most: {top_ms / 1000:.1f}s.{tail}"
    )


def firewall_address() -> str | None:
    """The firewall's own address, taken from the provider it is already configured with.

    PathBrain talks to this box constantly; asking a person to retype its address into a
    diagnostic about that same box is asking them for something the application already
    knows. Parsed from ``opnsense_url`` — a hostname is fine, ``icmplib`` resolves it — and
    None for a provider with no address (the mock), where the field stays empty and says so
    rather than offering a default that would ping nothing.
    """
    try:
        from urllib.parse import urlsplit

        from .config import get_settings

        url = (get_settings().opnsense_url or "").strip()
        if not url:
            return None
        host = urlsplit(url if "//" in url else f"//{url}").hostname
        return host or None
    except Exception:  # noqa: BLE001 — a convenience default must never raise
        log.debug("write_probe: could not read the firewall's address", exc_info=True)
        return None


def start(
    changes: list[dict],
    *,
    firewall_target: str | None = None,
    through_target: str = "1.1.1.1",
    baseline_s: float = DEFAULT_BASELINE_S,
    settle_s: float = DEFAULT_SETTLE_S,
) -> int:
    """Launch a write-and-ping probe. Returns the ``WriteProbe`` id.

    ``changes`` is the write to study, in ``plan_apply`` shape. Refused when the guard is
    hands-off — a supervised diagnostic is exactly the case for arming writes deliberately,
    not for a way around the guard.
    """
    if active():
        raise ValueError("A write probe is already running.")
    if not changes:
        raise ValueError("Nothing to write — a probe needs at least one field to change.")
    firewall_target = (firewall_target or "").strip() or firewall_address()
    if not firewall_target:
        raise ValueError(
            "No firewall address to ping — PathBrain has none configured (set "
            "PATHBRAIN_OPNSENSE_URL), so give one explicitly."
        )
    blocked = firewall_guard.blocked_reason("write_probe")
    if blocked:
        raise ValueError(blocked)
    settle_s = max(1.0, min(float(settle_s), MAX_SETTLE_S))
    baseline_s = max(1.0, min(float(baseline_s), MAX_SETTLE_S))

    with session_scope() as session:
        probe = WriteProbe(
            status=WriteProbeStatus.RUNNING,
            changes=[dict(c) for c in changes],
            firewall_target=firewall_target,
            through_target=through_target,
            stage="Starting",
        )
        session.add(probe)
        session.flush()
        probe_id = probe.id

    _state.update({"active": True, "id": probe_id, "cancel": False})
    t = threading.Thread(target=_drive, args=(probe_id, changes, firewall_target, through_target,
                                              baseline_s, settle_s),
                         name=f"write-probe-{probe_id}", daemon=True)
    _state["thread"] = t
    t.start()
    return probe_id


def start_sweep(
    pipe_uuid: str | None = None,
    fields: list[str] | None = None,
    *,
    reload: bool = True,
    firewall_target: str | None = None,
    through_target: str = "1.1.1.1",
    baseline_s: float = DEFAULT_BASELINE_S,
    settle_s: float = SWEEP_SETTLE_S,
) -> int:
    """Step each writable field in turn, put it straight back, and measure both.

    The cost is checked against the guard's remaining hourly budget *before* the first write
    (see :func:`budget_shortfall`), because a sweep stopped half way by the budget cannot
    restore the field it is holding.
    """
    if active():
        raise ValueError("A write probe is already running.")
    firewall_target = (firewall_target or "").strip() or firewall_address()
    if not firewall_target:
        raise ValueError(
            "No firewall address to ping — PathBrain has none configured (set "
            "PATHBRAIN_OPNSENSE_URL), so give one explicitly."
        )
    blocked = firewall_guard.blocked_reason("write_probe")
    if blocked:
        raise ValueError(blocked)
    settle_s = max(1.0, min(float(settle_s), MAX_SETTLE_S))
    baseline_s = max(1.0, min(float(baseline_s), MAX_SETTLE_S))

    provider = get_provider()
    live = {(c.extra or {}).get("uuid"): c.to_dict() for c in provider.discover()}
    if not live:
        raise ValueError("The firewall reports no pipes, so there is nothing to sweep.")
    plan = plan_sweep(live, pipe_uuid, fields, options=provider.field_options(),
                      reload=reload, settle_s=settle_s)
    if not plan["steps"]:
        why = "; ".join(f"{s['label']}: {s['why']}" for s in plan["skipped"][:4])
        raise ValueError(
            "No field on this pipe can be stepped, so there is nothing to measure"
            + (f" — {why}." if why else ".")
        )
    shortfall = budget_shortfall(plan["reconfigures"])
    if shortfall:
        raise ValueError(shortfall)

    with session_scope() as session:
        probe = WriteProbe(
            status=WriteProbeStatus.RUNNING,
            mode="sweep",
            changes=[dict(s) for s in plan["steps"]],
            firewall_target=firewall_target,
            through_target=through_target,
            stage="Starting",
        )
        session.add(probe)
        session.flush()
        probe_id = probe.id

    _state.update({"active": True, "id": probe_id, "cancel": False})
    t = threading.Thread(
        target=_drive_sweep,
        args=(probe_id, plan, firewall_target, through_target, baseline_s, settle_s),
        name=f"write-sweep-{probe_id}", daemon=True)
    _state["thread"] = t
    t.start()
    return probe_id


def _drive_sweep(probe_id: int, plan: dict, firewall_target: str, through_target: str,
                 baseline_s: float, settle_s: float) -> None:  # noqa: C901 — one lifecycle
    provider = None
    samplers: dict[str, _Sampler] = {}
    steps: list[dict] = []
    err: str | None = None
    status = WriteProbeStatus.COMPLETE
    reload = bool(plan.get("reload", True))
    #: The field currently stepped away from its original value, if any. At most one at a
    #: time by construction — every step is reverted before the next begins — so a failure
    #: can always name exactly what is still moved and what it should be.
    outstanding: dict | None = None

    try:
        provider = get_provider()
        samplers = {
            "firewall": _Sampler("firewall", firewall_target),
            "through": _Sampler("through", through_target),
        }
        for s in samplers.values():
            s.start()
        with coordinator.hold(f"write_probe#{probe_id}", abort=lambda: bool(_state.get("cancel"))):
            base = _run_step(probe_id, steps, samplers, "baseline",
                             "Baseline — nothing is being written", None, baseline_s)
            refusal = _baseline_refusal(base, samplers)
            if refusal:
                raise ValueError(refusal)

            total = len(plan["steps"])
            for i, planned in enumerate(plan["steps"], start=1):
                if _state.get("cancel"):
                    break
                label = planned["label"]
                change = {"pipe_uuid": planned["pipe_uuid"], "param": planned["param"],
                          "value": planned["to"]}
                back = {"pipe_uuid": planned["pipe_uuid"], "param": planned["param"],
                        "value": planned["from"]}
                tag = {"index": i, "param": planned["param"], "label": label,
                       "pipe_uuid": planned["pipe_uuid"], "from": planned["from"],
                       "to": planned["to"], "how": planned["how"]}

                outstanding = dict(back)
                step = _run_step(
                    probe_id, steps, samplers, "field_set",
                    f"{i}/{total} · {label}: {planned['how']}"
                    + ("" if reload else " (no shaper reload)"),
                    lambda c=change: provider.apply_many([c], reload=reload),
                    settle_s, {**tag, "phase": "set"})
                if step.get("failed"):
                    # The write did not take, so nothing is outstanding — but a failure here
                    # is usually the guard or the firewall, and pressing on would spend the
                    # rest of the budget discovering the same thing N more times.
                    outstanding = None
                    raise ValueError(
                        f"Stepping {label} failed, so the sweep stopped there: {step['failed']}"
                    )
                # Always put it back before the next field moves, so exactly one field is
                # ever away from its original value — and measure the restore too, since it
                # is a real write and the second sample for this field is free.
                revert = _run_step(
                    probe_id, steps, samplers, "field_revert",
                    f"{i}/{total} · {label}: back to {planned['from']}",
                    lambda c=back: provider.apply_many([c], reload=reload),
                    settle_s, {**tag, "phase": "revert", "to": planned["from"],
                               "from": planned["to"]})
                if revert.get("failed"):
                    raise ValueError(
                        f"Could not put {label} back to {planned['from']}: {revert['failed']}"
                    )
                outstanding = None
    except Exception as exc:  # noqa: BLE001
        err = describe_failure(exc)
        status = WriteProbeStatus.FAILED
        log.warning("Write sweep %s failed: %s", probe_id, err)
    finally:
        if outstanding is not None:
            # One last attempt, outside the loop's own error handling, and if it does not
            # work the reason says exactly which field is still moved and to what. A sweep
            # that leaves the firewall changed without saying so is the one outcome this
            # module may never produce.
            try:
                with coordinator.hold(f"write_probe#{probe_id}-restore"):
                    _run_step(probe_id, steps, samplers, "field_revert",
                              f"Restoring {outstanding['param']}",
                              lambda: provider.apply_many([outstanding], reload=True),
                              settle_s, {"param": outstanding["param"], "phase": "revert"})
            except Exception as exc:  # noqa: BLE001
                restore_err = describe_failure(exc)
                log.error("Write sweep %s: RESTORE FAILED — %s", probe_id, restore_err)
                err = (f"{err + ' | ' if err else ''}THE FIREWALL IS STILL CHANGED: "
                       f"{outstanding['param']} on pipe {outstanding.get('pipe_uuid')} should be "
                       f"{outstanding['value']!r} and could not be put back ({restore_err}). "
                       "Set it by hand.")
                status = WriteProbeStatus.FAILED
        for s in samplers.values():
            try:
                s.stop()
            except Exception:  # noqa: BLE001 — never let cleanup hide the result
                log.debug("Write sweep %s: a sampler would not stop", probe_id, exc_info=True)
        if _state.get("cancel") and status is WriteProbeStatus.COMPLETE:
            status = WriteProbeStatus.CANCELLED
        ping_errors = {k: s.error for k, s in samplers.items() if s.error}
        try:
            with session_scope() as session:
                p = session.get(WriteProbe, probe_id)
                if p is not None:
                    p.status = status
                    p.steps = list(steps)
                    p.samples = {k: list(s.samples) for k, s in samplers.items()}
                    p.verdict = sweep_verdict(steps) if steps else None
                    p.error = err or (
                        "; ".join(f"{k}: {v}" for k, v in ping_errors.items()) or None
                    )
                    if p.status is WriteProbeStatus.FAILED and not p.error:
                        p.error = "Failed before any step, with no reason recorded — a bug."
                    p.finished_at = datetime.now(timezone.utc)
                    p.stage = "Done" if status is WriteProbeStatus.COMPLETE else str(status.value)
        except Exception:  # noqa: BLE001
            log.exception("Write sweep %s: could not record the result", probe_id)
        _state.update({"active": False, "id": None, "thread": None, "cancel": False})


def _stage(probe_id: int, stage: str) -> None:
    log.info("Write probe %s: %s", probe_id, stage)
    try:
        with session_scope() as session:
            p = session.get(WriteProbe, probe_id)
            if p is not None:
                p.stage = stage
    except Exception:  # noqa: BLE001 — a status write must never break the probe
        log.debug("Write probe %s: could not persist stage", probe_id, exc_info=True)


def _publish(probe_id: int, steps: list[dict], samplers: dict) -> None:
    """Write the timeline so far, so the page can watch it build rather than wait."""
    try:
        with session_scope() as session:
            p = session.get(WriteProbe, probe_id)
            if p is not None:
                p.steps = list(steps)
                p.samples = {k: list(s.samples) for k, s in samplers.items()}
    except Exception:  # noqa: BLE001
        log.debug("Write probe %s: could not publish the timeline", probe_id, exc_info=True)


def _run_step(probe_id: int, steps: list[dict], samplers: dict, name: str, label: str,
              action, seconds: float, extra: dict | None = None) -> dict:
    """One operation, then a settle window — both inside the measured span, because how long
    the network takes to *recover* is as much the cost as the drop itself.

    Module-level rather than a closure so the single probe and the sweep measure a step the
    same way by construction: two definitions would agree until one of them was edited.
    """
    _stage(probe_id, label)
    t0 = time.time()
    failed: str | None = None
    firewall_guard.take_wait_ms()  # clear anything a previous step left on this thread
    if action is not None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 — a failed write is a result, not a crash
            failed = describe_failure(exc)
            log.warning("Write probe %s: %s failed — %s", probe_id, name, failed)
    acted = time.time()
    # The guard paces reconfigures, and that wait is inside the span just timed. It is
    # PathBrain's own rate limiting, not the firewall being slow — reporting them as one
    # number sent a reader after a five-second "firewall cost" that was a five-second
    # wait PathBrain chose, so the two are separated here.
    paced_ms = firewall_guard.take_wait_ms()
    # Settle with the samplers still running; cancel cuts it short.
    deadline = acted + seconds
    while time.time() < deadline and not _state.get("cancel"):
        time.sleep(0.2)
    t1 = time.time()
    step = {
        "step": name, "label": label,
        "started_at": round(t0, 3), "acted_at": round(acted, 3), "ended_at": round(t1, 3),
        "action_ms": round(max(0.0, (acted - t0) * 1000.0 - paced_ms), 1),
        "paced_ms": round(paced_ms, 1),
        "failed": failed,
        "targets": {k: summarize(s.samples, t0, t1) for k, s in samplers.items()},
        **(extra or {}),
    }
    steps.append(step)
    _publish(probe_id, steps, samplers)
    return step


def _baseline_refusal(base: dict, samplers: dict) -> str | None:
    """Why this probe must not write anything, judged from the baseline window alone.

    Never write the firewall for a measurement that cannot be taken. If the baseline got no
    replies at all, the ping is broken — ICMP blocked from the container, a wrong address, a
    host that does not answer — and every step after it would read as total loss and be
    reported as a catastrophic result. That false positive is worse than no probe, and it
    would have cost a real write to produce.
    """
    targets = base.get("targets") or {}
    dead = [name for name, t in targets.items()
            if (t.get("sent") or 0) > 0 and (t.get("lost") or 0) == (t.get("sent") or 0)]
    why_ping = "; ".join(f"{k}: {v}" for k, v in
                         ((k, s.error) for k, s in samplers.items()) if v)
    if dead:
        return (
            f"No ping replies from {', '.join(dead)} before anything was written, so there is "
            "nothing to measure against — nothing was applied to the firewall. Check the "
            "address, and that the container can send ICMP"
            + (f". The sampler said: {why_ping}" if why_ping else ".")
        )
    if not any((t.get("sent") or 0) for t in targets.values()):
        return ("The ping sampler produced no packets at all, so nothing was applied"
                + (f": {why_ping}" if why_ping else "."))
    return None


def _drive(probe_id: int, changes: list[dict], firewall_target: str, through_target: str,
           baseline_s: float, settle_s: float) -> None:  # noqa: C901 — one linear lifecycle
    # Everything that can fail lives inside the try below, including building the provider
    # and the samplers. Constructing them out here meant an exception escaped the thread
    # entirely: the row stayed RUNNING forever with no reason, and because the module flag
    # was never cleared, every later probe was refused as "already running". A diagnostic
    # that dies without saying so is the failure this whole module exists to stop.
    provider = None
    samplers: dict[str, _Sampler] = {}
    steps: list[dict] = []
    err: str | None = None
    status = WriteProbeStatus.COMPLETE
    before: list[dict] = []
    wrote = False

    def run_step(name: str, label: str, action, seconds: float) -> dict:
        return _run_step(probe_id, steps, samplers, name, label, action, seconds)

    try:
        provider = get_provider()
        samplers = {
            "firewall": _Sampler("firewall", firewall_target),
            "through": _Sampler("through", through_target),
        }
        for s in samplers.values():
            s.start()
        with coordinator.hold(f"write_probe#{probe_id}", abort=lambda: bool(_state.get("cancel"))):
            # What the firewall is on now, so the restore puts back exactly that and the
            # probe's own writes are the only thing that moved.
            live = {(c.extra or {}).get("uuid"): c.to_dict() for c in provider.discover()}
            first = next(iter(live.values()), {})
            for ch in changes:
                cur = live.get(ch.get("pipe_uuid"), first)
                before.append({"pipe_uuid": ch.get("pipe_uuid"), "param": ch.get("param"),
                               "value": cur.get(str(ch.get("param")))})
            already = all(
                _field_equal(str(c["param"]), b.get("value"), c.get("value"))
                for c, b in zip(changes, before)
            )
            if already:
                raise ValueError(
                    "The firewall is already on these values, so there is nothing to write "
                    "and nothing to measure. Pick a value it is not currently on."
                )

            base = run_step("baseline", "Baseline — nothing is being written", None, baseline_s)
            refusal = _baseline_refusal(base, samplers)
            if refusal:
                raise ValueError(refusal)
            if not _state.get("cancel"):
                wrote = True
                run_step("set_fields", "Writing the fields (no shaper reload)",
                         lambda: provider.apply_many(changes, reload=False), settle_s)
            if not _state.get("cancel"):
                run_step("reload", "Reloading the shaper", provider.reconfigure, settle_s)
    except Exception as exc:  # noqa: BLE001
        err = describe_failure(exc)
        status = WriteProbeStatus.FAILED
        log.warning("Write probe %s failed: %s", probe_id, err)
    finally:
        # Always put it back, and measure that too: the restore is a real profile switch,
        # so its cost belongs on the same timeline as the halves above.
        if wrote and before:
            try:
                with coordinator.hold(f"write_probe#{probe_id}-restore"):
                    run_step("restore", "Restoring the original values (a full switch)",
                             lambda: provider.apply_many(before, reload=True), settle_s)
            except Exception as exc:  # noqa: BLE001
                restore_err = describe_failure(exc)
                log.error("Write probe %s: RESTORE FAILED — %s", probe_id, restore_err)
                err = f"{err + ' | ' if err else ''}Restore failed: {restore_err}"
                status = WriteProbeStatus.FAILED
        for s in samplers.values():
            try:
                s.stop()
            except Exception:  # noqa: BLE001 — never let cleanup hide the result
                log.debug("Write probe %s: a sampler would not stop", probe_id, exc_info=True)
        if _state.get("cancel") and status is WriteProbeStatus.COMPLETE:
            status = WriteProbeStatus.CANCELLED
        ping_errors = {k: s.error for k, s in samplers.items() if s.error}
        try:
            with session_scope() as session:
                p = session.get(WriteProbe, probe_id)
                if p is not None:
                    p.status = status
                    p.steps = list(steps)
                    p.samples = {k: list(s.samples) for k, s in samplers.items()}
                    p.verdict = verdict(steps) if steps else None
                    # A failed probe ALWAYS carries a reason. "It failed" with an empty
                    # error is the unfalsifiable result this module was written against.
                    p.error = err or (
                        "; ".join(f"{k}: {v}" for k, v in ping_errors.items()) or None
                    )
                    if p.status is WriteProbeStatus.FAILED and not p.error:
                        p.error = "Failed before any step, with no reason recorded — a bug."
                    p.finished_at = datetime.now(timezone.utc)
                    p.stage = "Done" if status is WriteProbeStatus.COMPLETE else str(status.value)
        except Exception:  # noqa: BLE001
            log.exception("Write probe %s: could not record the result", probe_id)
        _state.update({"active": False, "id": None, "thread": None, "cancel": False})


def current() -> dict | None:
    pid = _state.get("id")
    return get(int(pid)) if pid else None


def get(probe_id: int) -> dict | None:
    with session_scope() as session:
        p = session.get(WriteProbe, probe_id)
        return _serialize(p) if p is not None else None


def recent(limit: int = 10) -> list[dict]:
    with session_scope() as session:
        rows = session.scalars(
            select(WriteProbe).order_by(WriteProbe.id.desc()).limit(limit)
        ).all()
        return [_serialize(p, with_samples=False) for p in rows]


def _serialize(p: WriteProbe, with_samples: bool = True) -> dict:
    out = {
        "id": p.id,
        "status": p.status.value if hasattr(p.status, "value") else str(p.status),
        "stage": p.stage,
        # Rows written before the sweep existed carry no mode and are all single probes.
        "mode": p.mode or "single",
        "changes": p.changes or [],
        "firewall_target": p.firewall_target,
        "through_target": p.through_target,
        "steps": p.steps or [],
        "verdict": p.verdict,
        "error": p.error,
        "started_at": p.started_at.isoformat() if p.started_at else None,
        "finished_at": p.finished_at.isoformat() if p.finished_at else None,
    }
    if with_samples:
        out["samples"] = p.samples or {}
    return out


__all__ = ["active", "budget_shortfall", "cancel", "current", "firewall_address", "get",
           "plan_sweep", "recent", "start", "start_sweep", "step_value", "summarize",
           "sweep_fields", "sweep_verdict", "verdict"]
