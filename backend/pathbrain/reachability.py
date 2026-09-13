"""Which measured profiles can the firewall still be driven to — and what would bring the
rest back?

Every other audit on the Methodology page asks whether a *measurement* is sound. This one
asks whether a profile is still **raceable**, which is a different question and became a
live one the day ``flows`` stopped being writable: a profile that differs from the live
firewall in a field PathBrain never writes cannot be applied, so the duel, the challenger
race and the heirs card leave it alone. That is correct behaviour and it is invisible —
the profile simply stops appearing, with nothing anywhere saying why, and a ledger of
hundreds silently becomes a ladder of a dozen.

Two causes, one consequence, and the card names which:

* **``field``** — the profile differs in a non-writable shaper field (``flows``,
  ``scheduler``, ``queues``, ``upload_bandwidth``). ``settings_profile.unreachable_fields``
  is the primitive; this module only groups its answers.
* **``pipe``** — the planner cannot address the pipe at all (no live match, no uuid), so
  even its *writable* differences are dropped (``settings_profile.unwritable_diffs``).

**It reports, and it never prescribes.** An earlier cut of this module grouped the
unreachable profiles by "the change that would bring them back" and led with it — *setting
the Download pipe's flow table to 512 restores 87 profiles*. That reads as useful and it is
the one thing this card must never say. The field is non-writable **because writing it took
the household off the network for about thirty seconds every time**, so a card that works
out the most valuable flow-table change and puts it on screen has re-created the hazard as
a recommendation: no button, but the same instruction, arrived at automatically and phrased
as a decision worth making. The value of a lost profile is never worth an outage the
platform has already decided not to cause, and a suggestion PathBrain computes is one it is
responsible for.

So the grouping stays and the **framing does not**: profiles are grouped by *what they were
measured at*, as a statement about the past, priced in profiles and in the iterations of
evidence they carry — because a hundred thin profiles and six well-measured ones are not the
same loss. No target value is presented as an action, nothing is ranked by "what to fix
first", and the verdict names the honest recourse, which is that those profiles are out of
the running and the field goes on without them.

Read-only throughout: one cached stored-profile list, one rollup read for the Overalls,
one ledger read for the ring records. Nothing here changes a score, a setting or a profile,
and nothing here asks anyone else to.
"""
from __future__ import annotations

from sqlalchemy import func, select

from .logging_config import get_logger
from .models import Run, RunStatus
from .settings_profile import (
    FIELD_LABELS,
    describe_unreachable,
    fingerprint,
    normalize,
    summarize,
    unreachable_fields,
    unwritable_diffs,
)

log = get_logger(__name__)

#: Unreachable profiles listed in full on the card. The grouped ``measured_at`` carries the whole
#: count, so the list is a sample to recognise them by, not the record.
PROFILE_LIMIT = 200


def _change_key(diffs: list[dict]) -> tuple:
    """The identity of a required-change set: the pipe, field and target value of each.

    Profiles needing the *same* change are one decision, so they group. Deliberately keyed
    on the value too — two profiles measured at ``flows`` 512 and 2048 are two different
    groups, and merging them would describe neither.
    """
    return tuple(sorted(
        (str(d.get("label")), str(d.get("field")), str(d.get("to")))
        for d in diffs
    ))


def _describe(diffs: list[dict]) -> str:
    """What this group was measured at, as a statement about the past.

    Deliberately *"was measured at X; the firewall is on Y"* and never *"change Y to X"*:
    the same two numbers, and only one of them is an instruction to go and cause an outage.
    """
    parts = [
        f"{d.get('label')} · {d.get('field_label')} {d.get('to')} "
        f"(the firewall is on {d.get('from')})"
        for d in diffs
    ]
    return "; ".join(parts)


def audit(session, live, *, limit: int = PROFILE_LIMIT) -> dict:
    """Which stored profiles the live firewall cannot be driven to, and what would fix it.

    ``live`` is ``provider.discover()``'s list — the audit is *relative to the firewall as
    it stands now*, which is the only sense in which "reachable" means anything: change the
    firewall's flow table and a different set of profiles becomes raceable.
    """
    from . import duel as duel_mod
    from . import refresh as refresh_mod

    live_norm = normalize(live)
    profiles = refresh_mod.list_profiles(session)
    fps = [p["fingerprint"] for p in profiles]
    live_fp = fingerprint(live_norm)

    try:
        overalls = duel_mod._pooled_overalls(session, fps)
    except Exception:  # noqa: BLE001 — a grade is context, never the reason the card fails
        log.debug("Reachability audit: could not read pooled overalls", exc_info=True)
        overalls = {}

    # Measurement *spent*, read from the runs themselves rather than from the scored rollup.
    # The rollup counts only what the current methodology can grade, so a profile whose runs
    # were quarantined by a publish would report zero and sort to the bottom — understating
    # exactly the loss this card exists to price. One indexed GROUP BY.
    spent = _measurement_spent(session)

    # The ring's own record, so "I have 14 rounds on a profile I can no longer race" is
    # visible. Bounded by the matchup ledger (not by history), and best-effort like the rest.
    try:
        ratings = duel_mod.ledger_ratings(session)
    except Exception:  # noqa: BLE001
        log.debug("Reachability audit: could not read the ring ledger", exc_info=True)
        ratings = {}

    rows: list[dict] = []
    reachable = 0
    for p in profiles:
        settings = p.get("settings") or []
        field_diffs = unreachable_fields(settings, live)
        pipe_diffs = unwritable_diffs(settings, live)
        if not field_diffs and not pipe_diffs:
            reachable += 1
            continue
        overall, _graded_iterations = overalls.get(p["fingerprint"], (None, 0))
        runs, iterations = spent.get(p["fingerprint"], (0, 0))
        rating = ratings.get(p["fingerprint"]) or {}
        rows.append({
            "fingerprint": p["fingerprint"],
            "name": p.get("name"),
            "label": p.get("label") or summarize(settings),
            "overall": overall,
            "runs": int(runs or 0),
            "iterations": int(iterations or 0),
            "is_live": p["fingerprint"] == live_fp,
            # Which of the two causes, and every difference behind it.
            "cause": "field" if field_diffs else "pipe",
            "diffs": [
                {k: d.get(k) for k in ("label", "field", "field_label", "from", "to")}
                for d in (field_diffs or pipe_diffs)
            ],
            "reason": (field_diffs or pipe_diffs)[0].get("reason"),
            # Head-to-head evidence that can no longer be extended.
            "ring_pairs": int(rating.get("rating_pairs") or 0) or None,
        })

    rows.sort(key=lambda r: (-(r["iterations"] or 0), r["overall"] is None, -(r["overall"] or 0.0)))

    # Which field explains how much. One line each, because "flows explains 87 of the 92"
    # is the whole finding when it is true, and the ranked list is how you see that it is.
    by_field: dict[str, dict] = {}
    for r in rows:
        for d in r["diffs"]:
            key = str(d.get("field"))
            entry = by_field.setdefault(key, {
                "field": key,
                "field_label": FIELD_LABELS.get(key, key),
                "profiles": 0,
                "iterations": 0,
            })
            entry["profiles"] += 1
            entry["iterations"] += r["iterations"]

    # Grouped by what each set of profiles was measured at — a description of the record,
    # never a change to make. Only ``field`` causes group: a pipe the planner cannot address
    # is not characterised by a value.
    groups: dict[tuple, dict] = {}
    for r in rows:
        if r["cause"] != "field":
            continue
        key = _change_key(r["diffs"])
        g = groups.setdefault(key, {
            "changes": r["diffs"],
            "describe": _describe(r["diffs"]),
            "profiles": 0,
            "iterations": 0,
            "best_overall": None,
            "examples": [],
        })
        g["profiles"] += 1
        g["iterations"] += r["iterations"]
        if r["overall"] is not None and (g["best_overall"] is None or r["overall"] > g["best_overall"]):
            g["best_overall"] = r["overall"]
        if len(g["examples"]) < 3:
            g["examples"].append({"fingerprint": r["fingerprint"], "name": r["name"],
                                  "label": r["label"], "overall": r["overall"]})
    measured_at = sorted(groups.values(), key=lambda g: (-g["iterations"], -g["profiles"]))

    return {
        "live": {
            "fingerprint": live_fp,
            "label": summarize(live_norm),
            "non_writable": _live_non_writable(live_norm),
        },
        "checked": len(profiles),
        "reachable": reachable,
        "unreachable": len(rows),
        "iterations_unreachable": sum(r["iterations"] for r in rows),
        "by_field": sorted(by_field.values(), key=lambda e: (-e["iterations"], -e["profiles"])),
        "measured_at": measured_at,
        "profiles": rows[:max(1, int(limit))],
        "truncated": max(0, len(rows) - max(1, int(limit))),
        "verdict": _verdict(len(profiles), reachable, len(rows), measured_at,
                            sum(r["iterations"] for r in rows)),
    }


def _measurement_spent(session) -> dict[str, tuple[int, int]]:
    """``{fingerprint: (runs, iterations)}`` over every completed run — what was actually
    spent measuring each profile, graded or not."""
    rows = session.execute(
        select(
            Run.settings_fingerprint,
            func.count(Run.id),
            func.coalesce(func.sum(Run.iterations), 0),
        )
        .where(Run.status == RunStatus.COMPLETE, Run.settings_fingerprint.is_not(None))
        .group_by(Run.settings_fingerprint)
    ).all()
    return {fp: (int(runs or 0), int(iters or 0)) for fp, runs, iters in rows}


def _live_non_writable(live_norm: list[dict]) -> list[dict]:
    """What the firewall is currently on, for the fields nothing may write — the values every
    raceable profile has to match."""
    from .shaper_fields import NON_WRITABLE_FIELDS

    out: list[dict] = []
    for pipe in live_norm or []:
        for key in NON_WRITABLE_FIELDS:
            value = pipe.get(key)
            if value is None:
                continue
            out.append({
                "label": pipe.get("label") or "pipe",
                "field": key,
                "field_label": FIELD_LABELS.get(key, key),
                "value": value,
            })
    return out


def _verdict(checked: int, reachable: int, unreachable: int, groups: list[dict],
             iterations: int) -> str:
    """One sentence with its numbers in it — a reading, never an instruction.

    It deliberately stops short of naming a firewall change that would restore these
    profiles, even though the grouping above makes that change trivial to compute. The
    field is not writable because writing it took the link down for about half a minute
    every time, and a recommendation the platform works out and puts on screen is a
    recommendation the platform is responsible for. So the recourse this names is the one
    that costs nothing: they are out of the running, and the field goes on without them.
    """
    if not checked:
        return "No stored profiles yet — nothing to check."
    if not unreachable:
        return (
            f"All {checked} measured profiles can be applied to the firewall as it stands. "
            "Nothing is out of reach."
        )
    share = round(100.0 * unreachable / checked)
    lead = (
        f"{unreachable} of {checked} measured profiles ({share}%) cannot be applied to the "
        f"firewall as it stands, so the duel, the challenger race and the heirs card skip "
        f"them — {iterations} iterations of measurement that can no longer be extended or "
        f"re-raced."
    )
    if not groups:
        return (
            lead + " Their pipes themselves differ from the live ones, so no value explains it."
        )
    return (
        f"{lead} They were measured on a firewall set up differently from this one — "
        f"{len(groups)} distinct setup(s) — and PathBrain neither changes those fields nor "
        "recommends changing them, because writing them is what took the link down. Treat "
        "these profiles as out of the running: anything worth having among them can be "
        "measured again as a reachable profile."
    )


# ── The per-profile view: what this profile actually IS, and can it exist? ────────────
#
# Profile Detail led with a call sign, a grade and a bout tape and never showed the
# settings themselves — the one thing a profile *is*. The closest it came was the
# technical summary in the subtitle ("wan: 900Mbit q1514 t5ms"), which is a lossy
# one-liner: it names a few fields, in an order nobody can scan, with no units, no
# per-pipe split, and no way to see what it differs from.
#
# This is served from the server rather than rendered from the settings blob the page
# already holds, for one reason: **which fields are writable is the registry's answer, not
# the frontend's.** A hardcoded list in a component is the drift that put a value of 4096
# on the ECN field in the write probe, and it would put "can this profile exist?" in two
# places that disagree the day the registry changes again. The Methodology card and this
# one run the same ``unreachable_fields`` primitive, so a profile the audit flags and a
# profile this card calls unreachable are the same set by construction.


def profile_view(session, fingerprint_: str, live) -> dict | None:
    """One profile's settings, per pipe and per field, against the live firewall.

    Each field carries what this profile runs, what the firewall runs now, whether they
    differ, and whether PathBrain may write it — which together answer the only two
    questions a reader has: *what is this profile?* and *can I be put on it?*

    ``None`` when no run ever captured settings for that fingerprint.
    """
    from .api.routes_settings import _profile_settings
    from .shaper_fields import SHAPER_FIELDS, format_display

    settings = _profile_settings(session, fingerprint_)
    if not settings:
        return None

    live_norm = normalize(live)
    live_by_label = {p.get("label"): p for p in live_norm}
    # Positional fallback, the same rule ``_match_live_pipes`` applies, so a pipe whose
    # label was renamed still lines up rather than reading as "not on the firewall".
    pipes: list[dict] = []
    for i, pipe in enumerate(settings):
        live_pipe = live_by_label.get(pipe.get("label"))
        if live_pipe is None and len(settings) == len(live_norm):
            live_pipe = live_norm[i]
        fields = []
        for f in SHAPER_FIELDS:
            value = pipe.get(f.key)
            live_value = (live_pipe or {}).get(f.key)
            fields.append({
                "field": f.key,
                "label": f.label,
                "kind": f.kind,
                "unit": f.unit,
                "writable": f.writable,
                "value": value,
                "live": live_value,
                # Formatted once, here, by the registry's own formatter: a CoDel target is
                # stored as the bare option key the firewall echoes, so a frontend appending
                # the unit itself renders "5msms" for one profile and "5ms" for the next.
                "display": format_display(f.key, value),
                "live_display": format_display(f.key, live_value),
                # Deliberately not a raw ``!=``: the firewall echoes a CoDel target back as
                # the bare option key, so "5ms" and 5 are the same value written twice.
                "differs": value is not None and not _same(f.key, value, live_value),
            })
        pipes.append({
            "label": pipe.get("label") or f"pipe {i + 1}",
            "on_firewall": live_pipe is not None,
            "fields": fields,
        })

    blocked = unreachable_fields(settings, live)
    dropped = unwritable_diffs(settings, live)
    return {
        "fingerprint": fingerprint_,
        "label": summarize(settings),
        # ``settings`` is already normalized (it is what was stored); only the provider's
        # configs need normalizing.
        "is_live": fingerprint(settings) == fingerprint(live_norm),
        "pipes": pipes,
        "blocked": [
            {k: d.get(k) for k in ("label", "field", "field_label", "from", "to")}
            for d in blocked
        ],
        "dropped": [
            {k: d.get(k) for k in ("label", "field", "field_label", "from", "to", "reason")}
            for d in dropped
        ],
        "can_exist": not blocked and not dropped,
        "verdict": _profile_verdict(blocked, dropped),
    }


def _same(field_key: str, a, b) -> bool:
    """Numeric-aware equality, via the one comparison the planner uses."""
    from .settings_profile import _field_equal

    try:
        return bool(_field_equal(field_key, a, b))
    except Exception:  # noqa: BLE001
        return a == b


def _profile_verdict(blocked: list[dict], dropped: list[dict]) -> str:
    if not blocked and not dropped:
        return (
            "The firewall can be put on this profile: every difference from the live "
            "settings is a field PathBrain can write."
        )
    if blocked:
        return (
            f"The firewall cannot be put on this profile — {describe_unreachable(blocked)}. "
            "That field is recorded on every run and never written — writing it is what took "
            "the link down — so this profile is out of the running: the duel, the challenger "
            "race and the heirs card skip it. Nothing needs doing about it."
        )
    names = ", ".join(sorted({str(d.get("field_label")) for d in dropped}))
    return (
        f"The firewall cannot be fully driven to this profile: {names} sits on a pipe the "
        "planner cannot address, so those differences would be dropped silently."
    )
