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

Every write goes through ``get_provider()`` like any other, so it is ledgered and can be
refused — a diagnostic that bypassed the write path to study the write path would be the
one unsupervised writer in the system. It holds the coordinator lock (it applies profiles
and must not overlap a benchmark) and restores what it changed in a ``finally``.

**Scope, deliberately narrowed.** This module also carried an automated per-field sweep:
step every writable field in turn, revert each, measure the gap after each step. It did its
job — the flow table, and nothing else, was expensive — and that field is no longer writable
at all. A one-time survey whose only re-run means writing to every field of a live
production firewall is not a thing to keep lying around, so it was removed; the probe below,
aimed at one field on a value a person chooses while they watch, is the surviving form of
the same question and the reusable one.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import coordinator
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


# ── Choosing the value: the smallest write that is still a write ─────────────────
#
# An automated **per-field sweep** lived here — it stepped every writable field on its own,
# put each straight back, and measured both, to answer "*which* field's write costs the
# outage?" when the probe above could only answer "the fields or the reload?". It answered:
# the flow table, and only the flow table, which is no longer writable.
#
# It is gone, and the removal is the point. Its question was a one-time survey, but the only
# way to re-ask it was to write to every field of a live production firewall in sequence —
# the most invasive operation the product had. For a field that comes under suspicion later,
# the single probe answers the same question for that one field, on a value a person picks,
# while they watch. What is kept here is the part both needed: how to choose that value.


def step_value(field_key: str, current, options: list[float] | None = None):
    """The smallest write that still *is* a write: ``(new_value, how)``, or None.

    Deliberately **not** ``levers._generated_values``, which halves and doubles. That
    function is hunting a better value and wants a step big enough to move the Overall;
    this one is measuring what a write *costs*, so the ideal step is the one that changes
    the network least while still being a real ``setPipe`` — anything larger risks making
    the link genuinely worse for the settle window, which is the complaint the probe exists
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


#: Fields this module never proposes stepping, whatever the registry says is writable.
#:
#: ``flows`` is the flow-table size, and this list is where its cost was first written down:
#: every other writable field is a parameter the shaper *reads*, while this one decides how
#: many queues it allocates, so any change to it — ``1024 -> 1025`` included — forces a full
#: flow-table rebuild rather than a re-read. Measured here (probe #5): setting it was free,
#: putting it back took **35.3 s**, timed out the ``apply_many`` call and took the box off
#: the network for 33 s.
#:
#: The ledger then showed the same cost on every *ordinary* write that carried the field —
#: duel legs, not probes — so the decision moved to where it belongs: ``flows`` is no longer
#: a writable field at all (``shaper_fields``), which takes it out of ``WRITABLE_FIELDS``
#: and out of the proposals below on its own. This stays as the second lock and as the
#: record of why, and it is what makes the refusal *say something*: a caller naming the
#: field gets the flow-table reason rather than a bare "not writable". The registry is the
#: decision; the guard refuses the write; this refuses to suggest it.
NEVER_STEP = frozenset({"flows"})


def field_proposals(live: dict, pipe_uuid: str | None, *, options: dict | None = None) -> dict:
    """Each writable field's smallest real step, for the probe's value box. Writes nothing.

    Computed here rather than in the frontend because "what is a sensible next value for
    this field" is the firewall's answer, not a component's: a hardcoded list is how the
    value box came to offer ``ecn`` a value of **4096**, having kept the number left over
    from ``flows``.

    This is what remains of ``plan_sweep``, which also priced and ordered a pass that
    stepped *every* field of a live firewall in turn. That pass is gone — its question
    ("which field's write is expensive?") is answered, and re-asking it meant writing to
    everything. One field at a time, chosen by a person, is the surviving form.
    """
    pipe_uuid = pipe_uuid or next(iter(live), None)
    pipe = live.get(pipe_uuid) or {}
    proposals: dict[str, dict] = {}
    for key in WRITABLE_FIELDS:
        stepped = step_value(key, pipe.get(key), (options or {}).get(key))
        if stepped is None:
            continue
        value, how = stepped
        proposals[key] = {
            "param": key, "label": FIELD_LABELS.get(key, key), "from": pipe.get(key),
            "to": value, "how": how, "steppable": key not in NEVER_STEP,
        }
    return {"pipe_uuid": pipe_uuid, "proposals": proposals}



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

    ``changes`` is the write to study, in ``plan_apply`` shape. Guarded like any other
    write — ledgered, watched, and refused if it names a field PathBrain never writes.
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

    Module-level rather than a closure: it was shared with the per-field sweep so the two
    could not drift, and it stays module-level so the next caller inherits the same shape.
    """
    _stage(probe_id, label)
    t0 = time.time()
    failed: str | None = None
    if action is not None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 — a failed write is a result, not a crash
            failed = describe_failure(exc)
            log.warning("Write probe %s: %s failed — %s", probe_id, name, failed)
    acted = time.time()
    # ``action_ms`` is the firewall's own cost, full stop. It used to have PathBrain's
    # pacing wait subtracted out of it, because the guard slept inside this span and a
    # five-second gap read as a five-second firewall. Nothing sleeps here now.
    # Settle with the samplers still running; cancel cuts it short.
    deadline = acted + seconds
    while time.time() < deadline and not _state.get("cancel"):
        time.sleep(0.2)
    t1 = time.time()
    step = {
        "step": name, "label": label,
        "started_at": round(t0, 3), "acted_at": round(acted, 3), "ended_at": round(t1, 3),
        "action_ms": round(max(0.0, (acted - t0) * 1000.0), 1),
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
        # Rows written before, during and after the per-field sweep's lifetime are all
        # read the same way; a swept row keeps its mode and renders as the history it is.
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


__all__ = ["active", "cancel", "current", "field_proposals", "firewall_address", "get",
           "recent", "start", "step_value", "summarize", "verdict"]
