"""Levers: what moving ONE setting does — how the ring asks, and what it has found.

The pooled crown and the duel ladder both answer *which profile* wins. Neither answers
*which setting* did it: a profile is a bundle of levers, and a bout between two bundles
that differ in four of them is four questions asked at once with one answer. The
observational record has a partial answer (``explore.matched_pairs``: profiles that
happen to differ in exactly one lever), but those pairs were measured on different
nights under different weather, and the field only contains the pairs someone happened
to build.

This module makes the question askable on purpose, and keeps the book.

**Asking** (``lever_variants`` / ``next_variant``, the ring's ``contenders = "levers"``
mode): the belt-holder defends against single-lever variants of *itself* — first the
profiles already in the field that differ from it in exactly one lever (they carry
pooled data; a lever duel matures them), then generated steps the firewall can hold
(the adjacent option on a select, halving/doubling on an unbounded integer, the flip on
a boolean). Every round in such a match is a paired, interleaved, same-weather
measurement of one lever, which is the strongest evidence this platform can produce
about a setting. Levers are round-robined by how much evidence the ledger already holds
for them, so a night spreads its rounds across the levers instead of re-answering one.

**The book** (``lever_ledger``): every decided match on the duel ledger whose two
profiles differ in exactly one lever — seated by this mode or not — is a paired reading
of that lever, oriented as *the effect of moving up* (higher value minus lower), pooled
per lever and per transition: rounds won each way, a sign test over rounds (available
for every record ever written), a signed-rank test over the per-round margins (records
written since the ring kept them), and the per-crown-leg margins so a lever's effect can
be placed on FCP, LCP or network stall. Beside each lever sits its **mechanism
prediction** — what fq_codel theory says the lever should do on a link that is not
saturated, which is the regime a page load lives in — and whether the ring agrees.
A prediction that fails is the interesting row: it says the model of the link is wrong,
which is worth more than a number that confirms it.

Read-only except through the ring; nothing here changes a score.
"""
from __future__ import annotations

import copy
import math
from statistics import median

from sqlalchemy import select

from .logging_config import get_logger
from .models import Run, RunStatus
from .settings_profile import _field_equal, _to_number, environment_signature, fingerprint, summarize
from .shaper_fields import WRITABLE_FIELDS, coerce_value, field as shaper_field, format_display

log = get_logger("levers")

#: Rounds a lever (or transition) needs before the ledger states a direction. Below it
#: the row reads ``thin`` — eight rounds is where a clean sweep first clears p < 0.01.
MIN_ROUNDS = 8
#: Significance bar for the round-level sign test and the margin signed-rank test.
ALPHA = 0.05
#: Generated-step factor for an unbounded integer lever (limit, flows, quantum with no
#: option list): halve and double. The coarsest probe that is still safe on a lever
#: whose scale is unknown; the ring refines from whichever side wins.
STEP_FACTOR = 2.0
#: Levers the ring never generates a step for: a bandwidth is a string with a unit whose
#: legal forms the provider owns, and moving it below line rate changes what the shaper
#: *is* rather than how it schedules. Field siblings still count.
NO_GENERATE = frozenset({"download_bandwidth"})

#: What fq_codel should do on an UNSATURATED link — the regime a page load runs in —
#: lever by lever. ``prediction`` is the sign the ring is expected to find:
#: ``null`` (no effect), ``interior`` (an optimum inside the range, so a local reading
#: can go either way), ``conditional`` (depends on a condition named in the text).
MECHANISM: dict[str, dict] = {
    "quantum": {
        "prediction": "interior",
        "mechanism": (
            "The bytes each flow may send per scheduler turn. Smaller interleaves the flows "
            "more finely — a page's many small fetches get their packets in between a big "
            "one's sooner — at the cost of scheduler work per byte; larger lets one flow "
            "hold the wire for whole packets."
        ),
        "unsaturated": (
            "Page loads are bursts of concurrent flows, so this is the one lever whose "
            "round-robin behaviour is exercised without saturation. Expect an interior "
            "optimum near one to two MTUs: below it the scheduler churns, above it the "
            "interleave coarsens."
        ),
    },
    "target": {
        "prediction": "null",
        "mechanism": (
            "The queue delay CoDel tolerates before it starts dropping or marking. It "
            "acts only once packets have been standing in the queue longer than this."
        ),
        "unsaturated": (
            "With no standing queue nothing waits long enough to be dropped, so the "
            "target is never reached and moving it should change nothing. A measured "
            "effect here means a queue does form during page-load bursts."
        ),
    },
    "interval": {
        "prediction": "null",
        "mechanism": (
            "How long CoDel watches a queue before deciding the delay is persistent, "
            "roughly a worst-case round trip."
        ),
        "unsaturated": (
            "Same as target: no persistent queue, no decision to make. A measured effect "
            "says bursts are queueing for longer than an interval."
        ),
    },
    "limit": {
        "prediction": "null",
        "mechanism": "The hard packet limit of the queue; packets beyond it are dropped outright.",
        "unsaturated": (
            "A page-load burst is far smaller than any sane limit, so it should never be "
            "hit. An effect here would mean the burst overflows the queue — a serious "
            "finding, since tail drops are the worst thing a shaper can do to a load."
        ),
    },
    "flows": {
        "prediction": "null",
        "mechanism": (
            "How many hash buckets flows are spread over. Two flows in one bucket share "
            "a turn, so more buckets means fewer collisions."
        ),
        "unsaturated": (
            "A page opens tens of connections against a thousand buckets, so collisions "
            "are rare; the interleave should not change measurably. A larger effect than "
            "quantum's would be surprising."
        ),
    },
    "ecn": {
        "prediction": "null",
        "mechanism": "Mark packets instead of dropping them when CoDel decides to signal congestion.",
        "unsaturated": (
            "CoDel never signals without a standing queue, so with ECN on or off nothing "
            "is marked or dropped and the setting is inert on a page load."
        ),
    },
    "download_bandwidth": {
        "prediction": "conditional",
        "mechanism": (
            "The rate the shaper pretends the link has. Below the real line rate it makes "
            "itself the bottleneck, which is what lets the AQM see and manage the queue; "
            "at or above it the shaper never sees a queue at all."
        ),
        "unsaturated": (
            "Only matters when it is below line rate. Then a lower figure means more "
            "shaping — fairer interleave under a burst — and less peak throughput, which "
            "big-object pages pay for on LCP."
        ),
    },
}


# ── Classifying a pair of profiles ─────────────────────────────────────────────


def _pipe_map(settings: list[dict] | None) -> dict[str, dict]:
    return {str(p.get("label") or "pipe"): p for p in (settings or []) if isinstance(p, dict)}


def single_lever_diff(a: list[dict] | None, b: list[dict] | None) -> dict | None:
    """The one writable lever two profiles differ in, or None when they differ in none or
    in more than one. Compared numerically (``_field_equal``), so ``"5ms"`` and ``5`` are the
    same value and never a phantom difference."""
    if not a or not b:
        return None
    pa, pb = _pipe_map(a), _pipe_map(b)
    if set(pa) != set(pb):
        return None
    found: dict | None = None
    for label in pa:
        for fkey in WRITABLE_FIELDS:
            va, vb = pa[label].get(fkey), pb[label].get(fkey)
            if va is None and vb is None:
                continue
            if va is None or vb is None or not _field_equal(fkey, va, vb):
                if found is not None:
                    return None
                fld = shaper_field(fkey)
                found = {
                    "pipe": label, "field": fkey,
                    "field_label": fld.label if fld else fkey,
                    "unit": fld.unit if fld else None,
                    "from": va, "to": vb,
                }
    return found


def _numeric(fkey: str, value) -> float | None:
    fld = shaper_field(fkey)
    if fld and fld.kind == "bool":
        if isinstance(value, str):
            return 1.0 if value.strip().lower() in ("1", "true", "yes", "on") else 0.0
        return 1.0 if value else 0.0
    return _to_number(fkey, value)


# ── Asking: single-lever variants of the defender ──────────────────────────────


def _generated_values(fkey: str, current: float, allowed: list[float] | None) -> list[float]:
    """The nearest distinct value(s) the firewall can hold on either side of ``current``."""
    fld = shaper_field(fkey)
    if fld is None or fkey in NO_GENERATE:
        return []
    if fld.kind == "bool":
        return [0.0 if current else 1.0]
    if allowed:
        opts = sorted(set(float(x) for x in allowed))
        below = [x for x in opts if x < current]
        above = [x for x in opts if x > current]
        return [v for v in (below[-1] if below else None, above[0] if above else None) if v is not None]
    default = fld.sweep_default or {}
    step = default.get("step")
    lo, hi = default.get("min"), default.get("max")
    if isinstance(step, (int, float)) and step > 0 and fld.unit:
        # A duration select with no option list on hand: step by the field's own grid.
        cands = [current - float(step), current + float(step)]
    else:
        cands = [current / STEP_FACTOR, current * STEP_FACTOR]
    out: list[float] = []
    for v in cands:
        v = float(coerce_value(fkey, v)) if fld.kind == "int" or fld.unit else v
        if lo is not None and v < float(lo):
            v = float(lo)
        if hi is not None and v > float(hi):
            v = float(hi)
        if v >= 1 and v != current and v not in out:
            out.append(v)
    return out


def _with_value(settings: list[dict], pipe: str, fkey: str, value) -> list[dict]:
    """A deep copy of ``settings`` with one lever moved — every other field, writable or
    not, kept byte for byte so the variant hashes as the firewall will echo it."""
    out = copy.deepcopy(settings)
    for p in out:
        if str(p.get("label") or "pipe") == pipe:
            p[fkey] = coerce_value(fkey, value)
    return out


def lever_variants(
    defender: dict,
    profiles: list[dict],
    baseline: list[dict] | None,
    *,
    allowed: dict[str, list[float]] | None = None,
    history: dict[tuple[str, str], int] | None = None,
) -> list[dict]:
    """Single-lever variants of ``defender``, in the order the ring should seat them.

    Field siblings first (a profile already measured that differs in exactly this lever —
    the duel matures pooled data it already has), then generated steps; levers ordered by
    how many paired rounds the ledger already holds for them (``history``), fewest first,
    so a night's rounds spread across the levers rather than re-answering one. Every
    variant is reachable from ``baseline`` by construction: it is the defender's own
    settings with one writable field moved.
    """
    base_settings = defender.get("settings") or []
    if not base_settings:
        return []
    if baseline and environment_signature(base_settings) != environment_signature(baseline):
        return []
    history = history or {}
    allowed = allowed or {}
    defender_fp = defender.get("fingerprint")
    out: list[dict] = []
    seen_fps: set[str] = {str(defender_fp)} if defender_fp else set()
    for pipe in base_settings:
        label = str(pipe.get("label") or "pipe")
        for fkey in WRITABLE_FIELDS:
            current = pipe.get(fkey)
            if current is None:
                continue
            fld = shaper_field(fkey)
            cur_num = _numeric(fkey, current)
            fought = int(history.get((label, fkey), 0))
            values_taken: set[float] = set()
            # 1. Siblings already in the field.
            for p in profiles:
                if p.get("fingerprint") in seen_fps or not p.get("settings"):
                    continue
                diff = single_lever_diff(base_settings, p["settings"])
                if not diff or diff["pipe"] != label or diff["field"] != fkey:
                    continue
                to_num = _numeric(fkey, diff["to"])
                seen_fps.add(str(p["fingerprint"]))
                if to_num is not None:
                    values_taken.add(to_num)
                out.append({
                    "fingerprint": p["fingerprint"],
                    "profile": p,
                    "lever": {**diff, "from": current},
                    "source": "field",
                    "why": (
                        f"lever: {label} {diff['field_label']} "
                        f"{format_display(fkey, current)} → {format_display(fkey, diff['to'])} "
                        f"(a measured profile, {int(p.get('iterations') or 0)} iterations)"
                    ),
                    "priority": (
                        fought, 0,
                        abs((to_num if to_num is not None else 0.0) - (cur_num or 0.0)),
                    ),
                })
            # 2. Generated steps the firewall can hold.
            if cur_num is None or fkey in NO_GENERATE:
                continue
            for value in _generated_values(fkey, cur_num, allowed.get(fkey)):
                if value in values_taken:
                    continue
                new_value: object = bool(value) if (fld and fld.kind == "bool") else value
                settings = _with_value(base_settings, label, fkey, new_value)
                fp = fingerprint(settings)
                if fp in seen_fps:
                    continue
                seen_fps.add(fp)
                shown = settings[[str(p.get("label") or "pipe") for p in settings].index(label)][fkey]
                out.append({
                    "fingerprint": fp,
                    "profile": {
                        "fingerprint": fp,
                        "label": summarize(settings),
                        "name": None,
                        "settings": settings,
                        "overall": None,
                        "iterations": 0,
                        "generated": True,
                    },
                    "lever": {
                        "pipe": label, "field": fkey,
                        "field_label": fld.label if fld else fkey,
                        "unit": fld.unit if fld else None,
                        "from": current, "to": shown,
                    },
                    "source": "generated",
                    "why": (
                        f"lever: {label} {fld.label if fld else fkey} "
                        f"{format_display(fkey, current)} → {format_display(fkey, shown)} "
                        "(a new step the firewall can hold — nobody has measured it)"
                    ),
                    "priority": (fought, 1, abs(value - cur_num)),
                })
    out.sort(key=lambda v: v["priority"])
    return out


def next_variant(
    variants: list[dict],
    defender_fp: str,
    *,
    fought: set[frozenset[str]] | None = None,
    recently_decided=None,
) -> tuple[str | None, str, dict | None]:
    """The next variant to seat: the first in order not fought this session, preferring one
    not decided within the cooldown — the same two-strength rule ``next_challenger`` uses
    (a session skip is hard; the cooldown only orders)."""
    fought = fought or set()
    pool = [v for v in variants if frozenset((defender_fp, v["fingerprint"])) not in fought]
    if not pool:
        return None, "", None
    if recently_decided is not None:
        for v in pool:
            if not recently_decided(defender_fp, v["fingerprint"]):
                return v["fingerprint"], v["why"], v
        v = pool[0]
        return v["fingerprint"], f"{v['why']} — re-raced (decided within the cooldown)", v
    v = pool[0]
    return v["fingerprint"], v["why"], v


# ── The book ───────────────────────────────────────────────────────────────────


def _settings_lookup(session, fingerprints: set[str]) -> dict[str, list[dict]]:
    """Newest stored settings per fingerprint, one query per 500 keys."""
    out: dict[str, list[dict]] = {}
    fps = [fp for fp in fingerprints if fp]
    for i in range(0, len(fps), 500):
        chunk = fps[i:i + 500]
        rows = session.execute(
            select(Run.settings_fingerprint, Run.settings)
            .where(Run.settings_fingerprint.in_(chunk), Run.settings.is_not(None),
                   Run.status == RunStatus.COMPLETE)
            .order_by(Run.id.desc())
        )
        for fp, settings in rows:
            if fp not in out and isinstance(settings, list):
                out[fp] = settings
    return out


def _binom_two_sided(k: int, n: int) -> float | None:
    """Two-sided exact sign test: P(X ≤ k) and P(X ≥ k) under p = ½, doubled and capped."""
    if n <= 0:
        return None
    lo = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    hi = sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n
    return min(1.0, 2.0 * min(lo, hi))


def _direction(wins_hi: int, wins_lo: int, sign_p: float | None, paired_p: float | None,
               margins_hi: list[float]) -> str:
    rounds = wins_hi + wins_lo
    if rounds < MIN_ROUNDS:
        return "thin"
    med = median(margins_hi) if margins_hi else 0.0
    significant = (sign_p is not None and sign_p < ALPHA) or (paired_p is not None and paired_p < ALPHA)
    if significant and (wins_hi > wins_lo or med > 0) and not (wins_hi < wins_lo and med <= 0):
        return "higher"
    if significant and (wins_lo > wins_hi or med < 0):
        return "lower"
    return "none"


def rounds_by_lever(sessions_data: list[dict], settings_by_fp: dict[str, dict | list]) -> dict[tuple[str, str], int]:
    """``{(pipe, field): rounds}`` the ledger already holds per lever — what orders the
    variants so a night spreads across levers. ``settings_by_fp`` maps a fingerprint to a
    profile dict (with ``settings``) or a bare settings list."""
    out: dict[tuple[str, str], int] = {}

    def _settings(fp: str):
        v = settings_by_fp.get(fp)
        return v.get("settings") if isinstance(v, dict) else v

    for sess in sessions_data:
        for m in sess.get("matchups") or []:
            lever = m.get("lever") or single_lever_diff(_settings(str(m.get("incumbent"))), _settings(str(m.get("challenger"))))
            if not lever:
                continue
            key = (str(lever["pipe"]), str(lever["field"]))
            out[key] = out.get(key, 0) + int(m.get("pairs") or 0)
    return out


def _orient(m: dict, lever: dict) -> dict | None:
    """One matchup as a reading of *moving up*: higher value minus lower."""
    v_from, v_to = _numeric(lever["field"], lever.get("from")), _numeric(lever["field"], lever.get("to"))
    if v_from is None or v_to is None or v_from == v_to:
        return None
    up = v_to > v_from  # the challenger holds the higher value
    sign = 1.0 if up else -1.0
    delta = m.get("median_delta")
    margin = sign * float(delta) if isinstance(delta, (int, float)) else None
    wins_c, wins_i = int(m.get("wins_challenger") or 0), int(m.get("wins_incumbent") or 0)
    crown = {}
    for k, v in (m.get("median_crown_delta") or {}).items():
        if isinstance(v, (int, float)):
            crown[k] = sign * float(v)
    deltas = [sign * float(d) for d in (m.get("deltas") or []) if isinstance(d, (int, float))]
    return {
        "lo": min(v_from, v_to), "hi": max(v_from, v_to),
        "lo_shown": lever.get("from") if up else lever.get("to"),
        "hi_shown": lever.get("to") if up else lever.get("from"),
        "wins_hi": wins_c if up else wins_i,
        "wins_lo": wins_i if up else wins_c,
        "margin": margin,
        "crown": crown,
        "deltas": deltas,
        "verdict": m.get("verdict"),
        "seated_as_lever": bool(m.get("lever")),
    }


def _summary(readings: list[dict]) -> dict:
    from .duel import wilcoxon_p

    wins_hi = sum(r["wins_hi"] for r in readings)
    wins_lo = sum(r["wins_lo"] for r in readings)
    margins = [r["margin"] for r in readings if r["margin"] is not None]
    deltas = [d for r in readings for d in r["deltas"]]
    sign_p = _binom_two_sided(wins_hi, wins_hi + wins_lo)
    paired_p = None
    if len(deltas) >= 5:
        try:
            paired_p = min(1.0, 2.0 * min(wilcoxon_p(deltas, 1), wilcoxon_p(deltas, -1)))
        except Exception:  # noqa: BLE001 — a test statistic must never blank the book
            paired_p = None
    crown: dict[str, float] = {}
    keys = {k for r in readings for k in r["crown"]}
    for k in sorted(keys):
        vals = [r["crown"][k] for r in readings if k in r["crown"]]
        if vals:
            crown[k] = round(median(vals), 2)
    return {
        "matches": len(readings),
        "rounds": wins_hi + wins_lo,
        "wins_higher": wins_hi,
        "wins_lower": wins_lo,
        "median_margin_up": round(median(margins), 2) if margins else None,
        "sign_p": round(sign_p, 4) if sign_p is not None else None,
        "paired_p": round(paired_p, 4) if paired_p is not None else None,
        "paired_rounds": len(deltas),
        "crown_margin_up": crown,
        "direction": _direction(wins_hi, wins_lo, sign_p, paired_p, margins),
        "seated_as_lever": sum(1 for r in readings if r["seated_as_lever"]),
    }


def _agreement(prediction: str, direction: str) -> tuple[str, str]:
    if direction == "thin":
        return "untested", "Too few paired rounds to say — seat it in the ring."
    if prediction == "null":
        if direction == "none":
            return "as_predicted", "No measurable effect, as the mechanism predicts on an unsaturated link."
        return "surprise", (
            f"The ring finds that {direction} helps, where the mechanism predicts no effect — "
            "either a queue forms during page-load bursts after all, or the pairs are confounded."
        )
    if prediction == "interior":
        if direction == "none":
            return "flat_here", "No measurable effect at these values — the defender may already sit near the optimum."
        return "consistent", (
            f"{direction.capitalize()} helps from the defender's value — one side of the predicted "
            "interior optimum. Keep stepping that way until it turns."
        )
    if direction == "none":
        return "as_predicted", "No effect at these values — consistent with the condition not being met."
    return "measured", f"The ring finds that {direction} helps here."


def lever_ledger(session, *, limit_sessions: int = 50) -> dict:
    """Every single-lever match on the duel ledger, pooled per lever and per transition,
    read beside the mechanism prediction for that lever."""
    from .duel import _ledger_sessions, outcome

    sessions = _ledger_sessions(session, limit_sessions)
    fps: set[str] = set()
    for sess in sessions:
        for m in sess.get("matchups") or []:
            if not m.get("lever"):
                fps.add(str(m.get("incumbent") or ""))
                fps.add(str(m.get("challenger") or ""))
    lookup = _settings_lookup(session, fps)

    by_lever: dict[tuple[str, str], dict] = {}
    used = skipped = aborted = 0
    for sess in sessions:
        for m in sess.get("matchups") or []:
            if outcome(m) == "aborted":
                aborted += 1
                continue
            lever = m.get("lever") or single_lever_diff(
                lookup.get(str(m.get("incumbent"))), lookup.get(str(m.get("challenger")))
            )
            if not lever:
                skipped += 1
                continue
            reading = _orient(m, lever)
            if reading is None:
                skipped += 1
                continue
            used += 1
            key = (str(lever["pipe"]), str(lever["field"]))
            slot = by_lever.setdefault(key, {
                "pipe": key[0], "field": key[1],
                "field_label": lever.get("field_label") or key[1],
                "unit": lever.get("unit"),
                "readings": [], "transitions": {},
            })
            slot["readings"].append(reading)
            slot["transitions"].setdefault((reading["lo"], reading["hi"]), []).append(reading)

    levers_out: list[dict] = []
    for (pipe, fkey), slot in by_lever.items():
        summary = _summary(slot["readings"])
        mech = MECHANISM.get(fkey) or {"prediction": "unknown", "mechanism": "", "unsaturated": ""}
        verdict, sentence = _agreement(mech["prediction"], summary["direction"])
        transitions = []
        for (lo, hi), readings in sorted(slot["transitions"].items()):
            t = _summary(readings)
            first = readings[0]
            transitions.append({
                "from": lo, "to": hi,
                "from_shown": format_display(fkey, first["lo_shown"]) if first["lo_shown"] is not None else lo,
                "to_shown": format_display(fkey, first["hi_shown"]) if first["hi_shown"] is not None else hi,
                **t,
            })
        transitions.sort(key=lambda t: (-t["rounds"], t["from"]))
        levers_out.append({
            "pipe": pipe, "field": fkey, "field_label": slot["field_label"], "unit": slot["unit"],
            **summary,
            "prediction": mech["prediction"],
            "mechanism": mech["mechanism"],
            "unsaturated": mech["unsaturated"],
            "agreement": verdict,
            "agreement_why": sentence,
            "transitions": transitions,
        })
    levers_out.sort(key=lambda l: (-l["rounds"], l["pipe"], l["field"]))
    # Levers never fought get a row too — the prediction is the point of the table, and a
    # lever with no evidence is the one the ring should be asked about next.
    seen = {(l["pipe"], l["field"]) for l in levers_out}
    pipes = sorted({p for p, _ in seen}) or ["download"]
    untested = []
    for fkey, mech in MECHANISM.items():
        fld = shaper_field(fkey)
        if not fld or not fld.writable:
            continue
        for pipe in pipes:
            if (pipe, fkey) in seen:
                continue
            untested.append({
                "pipe": pipe, "field": fkey, "field_label": fld.label, "unit": fld.unit,
                "matches": 0, "rounds": 0, "wins_higher": 0, "wins_lower": 0,
                "median_margin_up": None, "sign_p": None, "paired_p": None, "paired_rounds": 0,
                "crown_margin_up": {}, "direction": "thin", "seated_as_lever": 0,
                "prediction": mech["prediction"], "mechanism": mech["mechanism"],
                "unsaturated": mech["unsaturated"],
                "agreement": "untested",
                "agreement_why": "No paired rounds yet — seat it in the ring.",
                "transitions": [],
            })
    return {
        "levers": levers_out + untested,
        "sessions_analyzed": len(sessions),
        "matches_used": used,
        "matches_skipped": skipped,
        "matches_aborted": aborted,
        "min_rounds": MIN_ROUNDS,
        "alpha": ALPHA,
        "note": (
            "Every row is paired, interleaved, same-weather evidence from the duel ledger: matches "
            "between two profiles that differ in exactly one lever, whether the ring seated them for "
            "that lever or they happened to be one apart. Margins are signed as the effect of moving "
            "the lever UP (higher value minus lower), in Overall points."
        ),
    }
