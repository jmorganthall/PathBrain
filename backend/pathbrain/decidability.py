"""**What can this ladder actually settle tonight?**

Every other part of the duel is machinery for *answering* a question: the SPRT walk, the
Wilcoxon signed-rank test, the streak rule, the Bradley-Terry fit, the lineal belt. Nothing
anywhere asked whether the question was **answerable** — ``contender_order`` picks *who*
fights and never *whether this bout can conclude anything* — so the ladder spent its nights
on pairs it had no power to separate and recorded the result as a draw, which reads
identically to "these two are equal".

The reported shape of that: 845 matches across 50 sessions, 342 decisive — 59.5% of every
night producing no result, a belt held through 57 defences of which ~51 were draws, and
three verdicts (belt, ring #1, pooled crown) naming three different profiles with no way to
converge. None of that is the adjudication failing. It is the adjudication **correctly
reporting that it was handed an unanswerable question**, over and over, with nothing on
screen saying so.

So this module gives the ring a sense of its own resolving power, and the vocabulary to
refuse.

**The resolving power is MEASURED, not modelled** (``round_noise`` / ``resolving_power``).
Every match record already carries ``deltas`` — the per-round margins, challenger minus
reference — written so the lever ledger could run a paired test over the real evidence. The
*spread of successive margins within one matchup* is a direct sample of round noise: a
match's true edge is a constant across its own rounds, so the difference between two
consecutive rounds cancels the edge exactly and leaves only measurement error. Pooling that
across matchups gives the ring's noise in Overall points, off data already on disk,
recomputed as conditions change.

The estimator is deliberately **within-matchup**, not "the spread of drawn matches". The
obvious version — measure noise from the matches the ring called a draw — selects on small
observed spread and so reports the noise as smaller than it is, which is the one direction
of error that matters here: it would tell the ladder it can resolve differences it cannot.
Working within a matchup is unbiased whatever that match's true edge was, and validated
against a known σ in ``test_decidability`` rather than asserted.

**Two different refusals, and they are not the same fact** (``decidable``):

* ``cannot_differ`` — a *structural* fact about the two profiles, needing no statistics:
  they differ in nothing, or only in fields PathBrain never writes (so the bout cannot even
  be applied — see ``shaper_fields``), or by a lever step so small against the range the
  field has actually measured that no instrument would see it (``q5799`` vs ``q5800`` on an
  880 Mbit link — a variant-generation artifact, not a contender). Racing these is spending
  a night to learn nothing, and it is knowable before the first round.
* ``below_resolution`` — a fact about the *instrument*: the best prior on this pair's gap is
  smaller than the smallest margin the ring can currently call. The bout is real and the
  answer may well exist; this ladder, at this ``max_pairs`` and this noise, cannot reach it.

Both are reported, neither is silent, and the difference matters: the first is fixed by not
generating such variants, the second by more rounds, a bigger difference, or accepting that
the question stays open.

**What this module never does is withhold a crown.** A profile that is better by 0.000001 is
better, full stop — the pooled crown is an argmax with no floor (``_select_crown``) and
nothing here changes that. Ranking is free: it is already-collected data, sorted. What is
expensive is *demonstrating* an ordering, and that is the only thing being rationed. The
verdict rule and the measurement budget are separate questions and this module touches only
the second.

Strictly read-only: it reads the duel ledger, the pooled field and the shaper registry, and
returns numbers and sentences. No firewall write, no coordinator lock, no new table.
"""
from __future__ import annotations

import math

from .levers import _measured_span, single_lever_diff
from .logging_config import get_logger
from .shaper_fields import (
    NON_WRITABLE_FIELDS,
    WRITABLE_FIELDS,
    field as shaper_field,
    format_display,
)
from .settings_profile import _field_equal

log = get_logger(__name__)

# ── Measuring the ring's own noise ────────────────────────────────────────────────────

#: A matchup contributes to the noise estimate only with at least this many margins. Below
#: it a within-matchup dispersion is itself mostly noise, and pooling those widens the
#: estimate rather than sharpening it.
MIN_MATCHUP_DELTAS = 4

#: And the pooled estimate needs this many contributing matchups before it is reported at
#: all. One bad night's dispersion is not the ring's resolving power.
MIN_NOISE_MATCHUPS = 5

#: MAD → σ for a normal distribution. The dispersion is taken robustly (median absolute
#: deviation) rather than as a standard deviation because a single failed leg produces one
#: enormous margin, and an SD would hand that one round the whole estimate.
MAD_TO_SIGMA = 1.4826

#: Consecutive margins differ by ``√2 · σ`` when each carries noise ``σ``, so the spread of
#: successive differences is divided by this to recover one round's noise.
SUCCESSIVE_TO_SIGMA = math.sqrt(2.0)

#: How many standard errors a margin must clear to be callable. 2.0 matches
#: ``duel.tie_sigma`` and ``correlation.crown_tie_sigma``, so "the ring can resolve this"
#: and "the crown calls this clearly better" mean the same thing by the same bar.
DETECT_SIGMA = 2.0

#: Past this, ``rounds_to_resolve`` reports None rather than a number nobody would run. A
#: figure like "1,400 rounds" is information; "2.3 million" is a way of saying never.
MAX_USEFUL_ROUNDS = 5000


def _median(values: list[float]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _deltas_of(matchup: dict) -> list[float]:
    """The per-round margins on a match record, as floats, skipping anything unreadable."""
    out: list[float] = []
    for raw in (matchup or {}).get("deltas") or []:
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(val):
            out.append(val)
    return out


def round_noise(sessions_data: list[dict]) -> dict | None:
    """The ring's per-round measurement noise, in Overall points, measured from the ledger.

    The estimator is the spread of **successive margins within a matchup** — the classic
    mean-successive-difference idea, taken robustly. Consecutive rounds of one match share
    its true edge, so the difference between them cancels the edge exactly and leaves
    ``√2 · σ`` of pure measurement error.

    Two reasons this beats the obvious alternative of centring each matchup on its own
    median. It needs **no centre**, so it does not lose a degree of freedom to estimating
    one from the four-to-thirty margins a matchup actually has — measured against a known
    σ of 1.0 the centred version reads 0.73, which is exactly the wrong direction to be
    wrong in, since it would tell the ladder it can resolve differences it cannot. And it
    is far less sensitive to **drift**: a match running for hours across changing
    conditions has a wandering centre, and deviations about one median book the whole
    wander as noise where a successive difference sees only one round of it (measured, at
    0.5 points of drift per round: 5% inflation against the centred version's 41%). Less
    sensitive, not immune — drift still enters, and that residue is conservative, which is
    the right direction here.

    Returns ``{sigma, matchups, rounds, method}`` or None when the ledger does not yet hold
    enough rounds to say. None means *we cannot tell*, and every caller treats it as such:
    nothing is refused for being below a resolution nobody has measured.
    """
    steps: list[float] = []
    matchups = 0
    rounds = 0
    for sess in sessions_data or []:
        for m in sess.get("matchups") or []:
            deltas = _deltas_of(m)
            if len(deltas) < MIN_MATCHUP_DELTAS:
                continue
            matchups += 1
            rounds += len(deltas)
            steps.extend(abs(b - a) for a, b in zip(deltas, deltas[1:]))

    if matchups < MIN_NOISE_MATCHUPS or not steps:
        return None
    spread = _median(steps)
    if spread is None or spread <= 0:
        return None
    return {
        "sigma": round(spread * MAD_TO_SIGMA / SUCCESSIVE_TO_SIGMA, 3),
        "matchups": matchups,
        "rounds": rounds,
        "method": "robust successive-difference within matchups",
    }


def rounds_to_resolve(margin: float, sigma: float) -> int | None:
    """How many rounds a margin of this size needs before the ring could call it.

    ``margin`` is in Overall points and ``sigma`` is one round's noise, so the standard
    error of the mean margin over n rounds is ``sigma/√n`` and the margin is callable once
    it clears ``DETECT_SIGMA`` of that. None past ``MAX_USEFUL_ROUNDS``, which is the honest
    rendering of "not by measuring".
    """
    if not margin or margin <= 0 or not sigma or sigma <= 0:
        return None
    needed = math.ceil((DETECT_SIGMA * sigma / float(margin)) ** 2)
    if needed <= 0 or needed > MAX_USEFUL_ROUNDS:
        return None
    return int(needed)


def resolving_power(sessions_data: list[dict], max_pairs: int) -> dict:
    """The smallest margin this ladder can currently call, and what it rests on.

    ``{sigma, max_pairs, min_margin, noise, verdict}`` — ``min_margin`` in Overall points is
    the headline: a bout whose true edge is smaller than this cannot reach a verdict inside
    its round cap however long the night is, so the ring will record a draw and the reader
    will read "equal".
    """
    noise = round_noise(sessions_data)
    cap = max(1, int(max_pairs or 1))
    if not noise:
        return {
            "sigma": None,
            "max_pairs": cap,
            "min_margin": None,
            "noise": None,
            "verdict": (
                "Not enough rounds on the ledger to measure this ladder's own noise, so "
                "nothing is being refused for being too small to see."
            ),
        }
    sigma = float(noise["sigma"])
    floor = DETECT_SIGMA * sigma / math.sqrt(cap)
    return {
        "sigma": sigma,
        "max_pairs": cap,
        "min_margin": round(floor, 3),
        "noise": noise,
        "verdict": (
            f"One round carries ±{sigma:.2f} Overall points (measured over {noise['rounds']} "
            f"rounds in {noise['matchups']} matches). Over the {cap}-round cap that resolves "
            f"a margin of {floor:.2f} points — a real difference smaller than that cannot "
            f"reach a verdict here, however long the ladder runs."
        ),
    }


# ── Can these two profiles differ at all? ─────────────────────────────────────────────

#: A single-lever difference smaller than this fraction of the range that lever has actually
#: been run at, across the whole field, is treated as immaterial. Observed: two profiles one
#: quantum apart (``q5799`` vs ``q5800``) against a measured quantum span of 800–10814 —
#: one part in ten thousand of the range, which is a rounding artifact of some earlier
#: generated variant rather than a setting anybody chose. Deliberately a fraction of the
#: MEASURED span rather than an absolute step: what counts as a meaningful move differs by
#: three orders of magnitude between ``quantum`` and ``target``, and the field's own range
#: is the only scale that knows which is which.
IMMATERIAL_SPAN_FRACTION = 0.01


def _writable_diff_count(a: list[dict] | None, b: list[dict] | None) -> int | None:
    """How many writable fields differ across the pipes, or None when the pipe sets differ
    (which is a structural mismatch this module has nothing useful to say about)."""
    if not a or not b:
        return None
    pa = {str(p.get("label") or "pipe"): p for p in a}
    pb = {str(p.get("label") or "pipe"): p for p in b}
    if set(pa) != set(pb):
        return None
    count = 0
    for label in pa:
        for fkey in WRITABLE_FIELDS:
            va, vb = pa[label].get(fkey), pb[label].get(fkey)
            if va is None and vb is None:
                continue
            if va is None or vb is None or not _field_equal(fkey, va, vb):
                count += 1
    return count


def _unwritable_diffs(a: list[dict] | None, b: list[dict] | None) -> list[str]:
    """The non-writable fields these two profiles differ in — each one a reason the bout
    cannot be applied, since ``plan_apply`` emits only writable fields and
    ``firewall_guard.before_write`` refuses the rest."""
    if not a or not b:
        return []
    pa = {str(p.get("label") or "pipe"): p for p in a}
    pb = {str(p.get("label") or "pipe"): p for p in b}
    out: list[str] = []
    for label in set(pa) & set(pb):
        for fkey in NON_WRITABLE_FIELDS:
            va, vb = pa[label].get(fkey), pb[label].get(fkey)
            if va is None and vb is None:
                continue
            if va is None or vb is None or not _field_equal(fkey, va, vb):
                fld = shaper_field(fkey)
                out.append(f"{label} · {fld.label if fld else fkey}")
    return sorted(out)


def _immaterial_step(lever: dict, profiles: list[dict]) -> str | None:
    """A sentence when this single-lever move is too small against the field's own measured
    range to be worth a night, else None. Bools and anything with no measured span are never
    immaterial — a flag has no range and an unmeasured lever has no basis for the claim."""
    fkey = str(lever.get("field") or "")
    fld = shaper_field(fkey)
    if fld and fld.kind == "bool":
        return None
    try:
        lo_v, hi_v = float(lever.get("from")), float(lever.get("to"))
    except (TypeError, ValueError):
        return None
    span = _measured_span(profiles or [], str(lever.get("pipe") or ""), fkey)
    if not span:
        return None
    width = abs(span[1] - span[0])
    if width <= 0:
        return None
    gap = abs(hi_v - lo_v)
    if gap <= 0 or gap >= width * IMMATERIAL_SPAN_FRACTION:
        return None
    return (
        f"{lever.get('field_label') or fkey} moves "
        f"{format_display(fkey, lever.get('from'))} → {format_display(fkey, lever.get('to'))}, "
        f"which is {gap / width * 100:.2f}% of the range this lever has been measured over "
        f"({format_display(fkey, span[0])}–{format_display(fkey, span[1])})"
    )


def cannot_differ(
    a_settings: list[dict] | None,
    b_settings: list[dict] | None,
    *,
    profiles: list[dict] | None = None,
) -> str | None:
    """Why these two profiles cannot produce a different measurement, or None.

    Structural only — no statistics and no reference to how well anything was measured, so
    the answer is the same today and in a year. Three causes, in the order they are cheap
    to establish:

    1. **Nothing differs** in any writable field: the same profile twice.
    2. **Only non-writable fields differ**: the firewall cannot be driven from one to the
       other at all, so one side of the bout would be measured under the other's name.
    3. **One lever, moved immaterially**: a difference so small against the range the field
       has actually run that lever over that no instrument would resolve it.
    """
    writable = _writable_diff_count(a_settings, b_settings)
    unwritable = _unwritable_diffs(a_settings, b_settings)

    if writable == 0:
        if unwritable:
            return (
                "they differ only in fields PathBrain never writes ("
                + ", ".join(unwritable)
                + "), so the firewall cannot be driven from one to the other"
            )
        return "they are the same profile in every writable field"

    if writable is None:
        return None  # different pipe sets — not this module's call to make

    if unwritable:
        return (
            "they differ in fields PathBrain never writes ("
            + ", ".join(unwritable)
            + "), so whichever profile was asked for, the firewall would settle on the other"
        )

    lever = single_lever_diff(a_settings, b_settings)
    if lever:
        immaterial = _immaterial_step(lever, profiles or [])
        if immaterial:
            return f"the only difference is immaterial — {immaterial}"
    return None


# ── What do we already believe about this pair's gap? ─────────────────────────────────


def _direct_margin(sessions_data: list[dict], a_fp: str, b_fp: str) -> dict | None:
    """Every round these two have already fought, pooled and signed from ``a``'s side.

    The strongest evidence available about this specific pair: paired, interleaved,
    same-weather rounds on exactly these two profiles. When it exists, nothing else is
    consulted.
    """
    deltas: list[float] = []
    for sess in sessions_data or []:
        for m in sess.get("matchups") or []:
            inc, cha = str(m.get("incumbent") or ""), str(m.get("challenger") or "")
            if {inc, cha} != {a_fp, b_fp}:
                continue
            own = _deltas_of(m)  # challenger − incumbent
            if not own:
                continue
            deltas.extend(own if cha == a_fp else [-d for d in own])
    if not deltas:
        return None
    margin = _median(deltas)
    if margin is None:
        return None
    return {
        "margin": round(abs(margin), 3),
        "signed": round(margin, 3),
        "rounds": len(deltas),
        "source": "direct",
        "why": f"{len(deltas)} round(s) already fought between them",
    }


def expected_margin(
    a_fp: str,
    b_fp: str,
    *,
    sessions_data: list[dict] | None = None,
    overalls: dict[str, float] | None = None,
) -> dict | None:
    """The best prior on how far apart these two are, in Overall points, with provenance.

    A ladder of evidence, strongest first — the same discipline ``explore._predict`` applies
    to pricing a move:

    1. **Direct rounds** — they have met; use what happened.
    2. **Pooled Overall** — they have not met; the difference of their pooled medians is a
       confounded but real estimate, and confounded-but-real is what a prior is for.

    None when neither is available, which is honest rather than zero: "we have no idea how
    far apart these are" is a reason to race them, not a reason to refuse.
    """
    direct = _direct_margin(sessions_data or [], a_fp, b_fp)
    if direct:
        return direct
    pool = overalls or {}
    a_o, b_o = pool.get(a_fp), pool.get(b_fp)
    if a_o is None or b_o is None:
        return None
    gap = float(a_o) - float(b_o)
    return {
        "margin": round(abs(gap), 3),
        "signed": round(gap, 3),
        "rounds": 0,
        "source": "pooled",
        "why": "never met — the gap between their pooled Overalls",
    }


# ── The verdict ───────────────────────────────────────────────────────────────────────

YES = "yes"
BELOW_RESOLUTION = "below_resolution"
CANNOT_DIFFER = "cannot_differ"
UNKNOWN = "unknown"


def decidable(
    a_fp: str,
    b_fp: str,
    *,
    power: dict,
    settings_by_fp: dict[str, list[dict] | None] | None = None,
    sessions_data: list[dict] | None = None,
    overalls: dict[str, float] | None = None,
    profiles: list[dict] | None = None,
) -> dict:
    """Can a bout between these two conclude anything? ``{verdict, why, margin, rounds_needed}``.

    ``UNKNOWN`` is a real answer and is never a refusal: with no measured resolving power,
    or no prior on the gap, the honest position is that the bout might settle something and
    the only way to find out is to run it. Refusing on an absence of evidence is how a
    ladder stops racing exactly the pairs nobody has looked at yet.
    """
    settings = settings_by_fp or {}
    blocked = cannot_differ(settings.get(a_fp), settings.get(b_fp), profiles=profiles)
    if blocked:
        return {
            "verdict": CANNOT_DIFFER,
            "why": blocked,
            "margin": None,
            "rounds_needed": None,
        }

    prior = expected_margin(a_fp, b_fp, sessions_data=sessions_data, overalls=overalls)
    floor = power.get("min_margin") if power else None
    sigma = power.get("sigma") if power else None

    if prior is None or floor is None:
        return {
            "verdict": UNKNOWN,
            "why": (
                "nothing measured says how far apart these are"
                if prior is None
                else "this ladder's resolving power has not been measured yet"
            ),
            "margin": prior.get("margin") if prior else None,
            "rounds_needed": None,
            "source": (prior or {}).get("source"),
        }

    margin = float(prior["margin"])
    needed = rounds_to_resolve(margin, float(sigma)) if sigma else None
    if margin < float(floor):
        return {
            "verdict": BELOW_RESOLUTION,
            "why": (
                f"the best estimate of the gap is {margin:.2f} points "
                f"({prior['why']}) and this ladder resolves {float(floor):.2f}"
                + (f" — it would take about {needed} rounds" if needed else "")
            ),
            "margin": round(margin, 3),
            "rounds_needed": needed,
            "source": prior["source"],
        }
    return {
        "verdict": YES,
        "why": (
            f"the gap looks like {margin:.2f} points ({prior['why']}), above the "
            f"{float(floor):.2f} this ladder resolves"
        ),
        "margin": round(margin, 3),
        "rounds_needed": needed,
        "source": prior["source"],
    }


#: Verdicts that mean "do not spend a night on this". ``UNKNOWN`` is deliberately absent.
REFUSED = (CANNOT_DIFFER,)


def plan(
    incumbent_fp: str,
    candidates: list[str],
    *,
    power: dict,
    settings_by_fp: dict[str, list[dict] | None] | None = None,
    sessions_data: list[dict] | None = None,
    overalls: dict[str, float] | None = None,
    profiles: list[dict] | None = None,
) -> dict:
    """What tonight's card can and cannot settle, against one defender.

    Returns ``{power, entries, worth_racing, refused, unknown, below, verdict}``. Pure: the
    caller supplies the field, the ledger and the defender, and nothing here reads the
    database or changes a score.
    """
    entries: list[dict] = []
    for fp in candidates:
        if fp == incumbent_fp:
            continue
        verdict = decidable(
            fp, incumbent_fp,
            power=power, settings_by_fp=settings_by_fp,
            sessions_data=sessions_data, overalls=overalls, profiles=profiles,
        )
        entries.append({"fingerprint": fp, **verdict})

    worth = [e for e in entries if e["verdict"] == YES]
    unknown = [e for e in entries if e["verdict"] == UNKNOWN]
    below = [e for e in entries if e["verdict"] == BELOW_RESOLUTION]
    refused = [e for e in entries if e["verdict"] == CANNOT_DIFFER]

    floor = (power or {}).get("min_margin")
    if floor is None:
        verdict = (
            f"{len(entries)} bout(s) queued. This ladder's own noise has not been measured "
            f"yet, so nothing is being held back — but {len(refused)} pair(s) cannot differ "
            f"at all and are out."
            if refused else
            f"{len(entries)} bout(s) queued, and this ladder's own noise has not been "
            f"measured yet, so nothing is being held back."
        )
    else:
        verdict = (
            f"Tonight this ladder can settle a margin of {float(floor):.2f} Overall points. "
            f"Of {len(entries)} queued bout(s), {len(worth)} look big enough to call, "
            f"{len(unknown)} are unmeasured (race them — that is how you find out), "
            f"{len(below)} sit below what it can resolve, and {len(refused)} cannot differ "
            f"at all."
        )

    return {
        "power": power,
        "entries": entries,
        "worth_racing": [e["fingerprint"] for e in worth],
        "unknown": [e["fingerprint"] for e in unknown],
        "below": [e["fingerprint"] for e in below],
        "refused": [e["fingerprint"] for e in refused],
        "verdict": verdict,
    }


__all__ = [
    "BELOW_RESOLUTION",
    "CANNOT_DIFFER",
    "DETECT_SIGMA",
    "IMMATERIAL_SPAN_FRACTION",
    "REFUSED",
    "UNKNOWN",
    "YES",
    "cannot_differ",
    "decidable",
    "expected_margin",
    "plan",
    "round_noise",
    "resolving_power",
    "rounds_to_resolve",
]
