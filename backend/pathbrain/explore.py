"""The exploration landscape: what the shaper's parameter space looks like, where the
holes are, and which untested profiles are worth measuring next.

Every other engine in PathBrain *judges profiles that already exist*. The sweep enumerates
a grid, the race promotes the under-sampled, the duel adjudicates head to head — but none
of them answers "what haven't we tried that might beat everything we have?". With a
hundred-odd profiles varying several fields at once, that question isn't answerable by
eye: the levers interact, the coverage is lumpy, and the interesting values are the ones
nobody happened to pick.

So this module reads the measured field and produces four things:

* **Axes** — every writable lever, kept **per pipe** (the Download and Upload legs are
  separate knobs and behave differently), with what's actually been tested on it.
* **Response curves** — median Overall at each tested value of each axis, plus the
  Spearman ρ. The curve matters more than the correlation: a lever with ρ≈0 can still
  have a clear sweet spot in the middle, which a single monotonic number cannot express.
* **Interactions** — for each pair of axes, whether the better half of one depends on
  which half of the other you're in (a 2×2 contrast). This is the "does the best download
  quantum depend on the upload quantum?" question, and it is invisible to any per-lever view.
* **Candidates** — concrete untested profiles, ranked by an upper confidence bound:
  a local prediction of how they'd score, plus how uncertain that prediction is. "Likely
  to possibly beat everything we have" is exactly UCB: somewhere we expect to do well
  *or* somewhere we know so little that it could surprise us.

Deliberately **deterministic and dependency-free** — no numpy, no fitted black box. Every
number here can be recomputed from the profile table and explained in a sentence, which is
the same standard the crown and the duel ratings are held to. The prediction is a
distance-weighted average of nearby measured profiles (Nadaraya–Watson with a Gaussian
kernel over the normalized lever space) and the uncertainty is the field's own spread
shrunk by how much evidence sits nearby.

**Read-only.** Nothing here writes the firewall or starts a run; it proposes, and the
existing ``POST /api/settings/test-settings`` path is what measures a proposal. That
separation is deliberate: today the page is a standalone "what should I try next?" tool,
and the seam it leaves is the overnight module — a scheduler that alternates *explore*
(measure the candidates this module proposes) with *adjudicate* (duel the survivors), so
the field grows toward the optimum instead of only being re-ranked.
"""
from __future__ import annotations

import math

from .logging_config import get_logger
from .settings_profile import _to_number
from .shaper_fields import WRITABLE_FIELDS, field as shaper_field
from .stats import spearman

log = get_logger("explore")

# A lever needs this many distinct tested values before a response curve says anything.
MIN_DISTINCT_VALUES = 3
# …and this many profiles before we correlate it at all.
MIN_POINTS = 4

# Kernel bandwidth in normalized ([0,1] per axis) lever space. 0.25 means profiles within
# a quarter of the tested range on each axis dominate a prediction — local enough to
# follow a real gradient, wide enough that a sparse field still predicts something.
KERNEL_BANDWIDTH = 0.25
# How much unexplored territory is worth. The candidate score is
# ``predicted + EXPLORATION_WEIGHT * uncertainty``; at 1.0 a point one standard deviation
# more uncertain is as attractive as one predicted a full deviation better. Higher explores
# harder, lower exploits what's already known.
EXPLORATION_WEIGHT = 1.0
# A 2x2 contrast has to clear both an absolute floor (Overall points) and this share of
# the spread between the four corners before it's called an interaction. Without both, every
# pair of levers reads as interacting on rounding noise — worse than saying nothing, because
# it invites choosing two levers together for no reason.
INTERACTION_MIN_CONTRAST = 1.0
INTERACTION_MIN_SHARE = 0.15
# Parents to branch candidates from, and how many candidates to return by default.
CANDIDATE_PARENTS = 5
DEFAULT_SUGGESTIONS = 3


def _axis_key(pipe: str, fkey: str) -> str:
    return f"{pipe}::{fkey}"


def _numeric_axes(profiles: list[dict]) -> dict[str, dict]:
    """Every (pipe, writable field) that carries a *numeric, varying* value.

    Constant levers are dropped: a field every profile shares is not a lever, it's a
    constant, and showing it as an axis with one tested value is noise on every panel.
    """
    values: dict[str, list[float]] = {}
    meta: dict[str, dict] = {}
    for p in profiles:
        for pipe in p.get("settings") or []:
            label = pipe.get("label") or "pipe"
            for fkey in WRITABLE_FIELDS:
                x = _to_number(fkey, pipe.get(fkey))
                if x is None:
                    continue
                key = _axis_key(label, fkey)
                values.setdefault(key, []).append(float(x))
                if key not in meta:
                    fld = shaper_field(fkey)
                    meta[key] = {
                        "key": key,
                        "pipe": label,
                        "field": fkey,
                        "field_label": fld.label if fld else fkey,
                        "unit": (fld.unit if fld else None),
                        "sweepable": bool(fld.sweepable) if fld else False,
                    }
    axes: dict[str, dict] = {}
    for key, vals in values.items():
        distinct = sorted(set(vals))
        if len(distinct) < 2:
            continue  # constant — not a lever
        axes[key] = {
            **meta[key],
            "values": distinct,
            "min": distinct[0],
            "max": distinct[-1],
            "distinct": len(distinct),
            "measured": len(vals),
        }
    return axes


def _coords(profile: dict, axes: dict[str, dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for pipe in profile.get("settings") or []:
        label = pipe.get("label") or "pipe"
        for fkey in WRITABLE_FIELDS:
            key = _axis_key(label, fkey)
            if key not in axes:
                continue
            x = _to_number(fkey, pipe.get(fkey))
            if x is not None:
                out[key] = float(x)
    return out


def _normalize(value: float, axis: dict) -> float:
    span = axis["max"] - axis["min"]
    return 0.0 if span <= 0 else (value - axis["min"]) / span


def _distance(a: dict[str, float], b: dict[str, float], axes: dict[str, dict]) -> float | None:
    """Normalized Euclidean distance over the axes both points share.

    Averaged over the shared axes rather than summed, so a profile missing a pipe isn't
    made to look artificially close (fewer terms) or far (more terms) than its neighbours.
    """
    shared = [k for k in a if k in b and k in axes]
    if not shared:
        return None
    total = sum((_normalize(a[k], axes[k]) - _normalize(b[k], axes[k])) ** 2 for k in shared)
    return math.sqrt(total / len(shared))


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _stdev(vals: list[float]) -> float:
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / n
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))


def _response_curves(points: list[dict], axes: dict[str, dict]) -> list[dict]:
    """Median Overall at each tested value of each axis, plus the rank correlation.

    The curve is the point. A lever can be flat on ρ and still have an obvious sweet spot
    — a single monotonic coefficient cannot say "best in the middle", and a sweet spot both
    extremes miss is exactly the kind of thing a hundred profiles chosen by hand will hide.
    """
    out: list[dict] = []
    for key, axis in axes.items():
        xs: list[float] = []
        ys: list[float] = []
        by_value: dict[float, list[float]] = {}
        for pt in points:
            x = pt["coords"].get(key)
            if x is None:
                continue
            xs.append(x)
            ys.append(pt["overall"])
            by_value.setdefault(x, []).append(pt["overall"])
        if len(xs) < MIN_POINTS or len(set(xs)) < 2:
            continue
        curve = [
            {
                "value": v,
                "overall": round(_median(vs), 2),
                "best": round(max(vs), 2),
                "profiles": len(vs),
            }
            for v, vs in sorted(by_value.items())
        ]
        rho = spearman(xs, ys) if len(set(xs)) >= MIN_DISTINCT_VALUES else None
        # The value with the best median outcome, and whether it sits at an edge of what
        # has been tested — an edge-best is the field telling you to look further out.
        best = max(curve, key=lambda c: c["overall"])
        at_edge = best["value"] in (axis["min"], axis["max"])
        out.append({
            **{k: axis[k] for k in ("key", "pipe", "field", "field_label", "unit", "sweepable")},
            "curve": curve,
            "spearman": round(rho, 3) if rho is not None else None,
            "best_value": best["value"],
            "best_overall": best["overall"],
            "best_at_edge": at_edge,
            "shape": _curve_shape(curve, rho),
        })
    out.sort(key=lambda r: -(abs(r["spearman"] or 0.0)))
    return out


def _curve_shape(curve: list[dict], rho: float | None) -> str:
    """A one-word reading of the response curve: what the page shows before the numbers."""
    if len(curve) < 3:
        return "too few values"
    best_i = max(range(len(curve)), key=lambda i: curve[i]["overall"])
    spread = max(c["overall"] for c in curve) - min(c["overall"] for c in curve)
    if spread < 0.5:
        return "flat"
    # An interior peak beats the correlation, and has to be checked first. A lever whose
    # far end is disastrous carries a strong monotonic rho even when its best value is in
    # the middle — reading that as "lower is better" would send you to the low end, which
    # the curve itself says is worse than the peak you already found.
    if 0 < best_i < len(curve) - 1:
        return "sweet spot"
    if rho is not None and abs(rho) >= 0.4:
        return "higher is better" if rho > 0 else "lower is better"
    return "unclear"


def _interactions(points: list[dict], axes: dict[str, dict], limit: int = 8) -> list[dict]:
    """Does the better half of one lever depend on which half of another you're in?

    Split each axis at its median, take the median Overall in each of the four cells, and
    read the interaction contrast ``(HH − LH) − (HL − LL)``. A large contrast means the
    two levers have to be chosen together — which no per-lever view can show, and which is
    the whole reason to look at the Download and Upload legs side by side.
    """
    keys = list(axes)
    out: list[dict] = []
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            cells: dict[tuple[bool, bool], list[float]] = {}
            va = [pt["coords"][a] for pt in points if a in pt["coords"]]
            vb = [pt["coords"][b] for pt in points if b in pt["coords"]]
            if len(set(va)) < 2 or len(set(vb)) < 2:
                continue
            mid_a, mid_b = _median(va), _median(vb)
            for pt in points:
                if a not in pt["coords"] or b not in pt["coords"]:
                    continue
                cells.setdefault(
                    (pt["coords"][a] > mid_a, pt["coords"][b] > mid_b), []
                ).append(pt["overall"])
            if len(cells) < 4 or any(len(v) < 2 for v in cells.values()):
                continue
            med = {k: _median(v) for k, v in cells.items()}
            contrast = (med[(True, True)] - med[(False, True)]) - (
                med[(True, False)] - med[(False, False)]
            )
            cell_values = list(med.values())
            # "They interact" has to mean something. A contrast has to clear both an
            # absolute floor and a share of the spread between the four corners, or every
            # pair of levers reads as interacting on rounding noise — which is worse than
            # saying nothing, because it invites choosing two levers together for no reason.
            corner_spread = max(cell_values) - min(cell_values)
            interacts = abs(contrast) >= max(
                INTERACTION_MIN_CONTRAST, INTERACTION_MIN_SHARE * corner_spread
            )
            out.append({
                "a": a,
                "b": b,
                "a_label": f"{axes[a]['pipe']} {axes[a]['field_label']}",
                "b_label": f"{axes[b]['pipe']} {axes[b]['field_label']}",
                "a_split": mid_a,
                "b_split": mid_b,
                "cells": [
                    {
                        "a_high": ah,
                        "b_high": bh,
                        "overall": round(med[(ah, bh)], 2),
                        "profiles": len(cells[(ah, bh)]),
                    }
                    for ah in (False, True)
                    for bh in (False, True)
                ],
                "contrast": round(contrast, 2),
                "corner_spread": round(corner_spread, 2),
                "interacts": interacts,
                "summary": _interaction_summary(
                    f"{axes[a]['pipe']} {axes[a]['field_label']}",
                    f"{axes[b]['pipe']} {axes[b]['field_label']}",
                    contrast,
                    interacts,
                ),
            })
    # Only pairs that genuinely interact are worth a panel; the rest are the default case
    # (pick each lever on its own) and saying so for every pair is noise.
    out = [r for r in out if r["interacts"]]
    out.sort(key=lambda r: -abs(r["contrast"]))
    return out[:limit]


def _interaction_summary(a_label: str, b_label: str, contrast: float, interacts: bool) -> str:
    if not interacts:
        return f"{a_label} and {b_label} act independently — pick each on its own."
    better = "higher" if contrast > 0 else "lower"
    return (
        f"They interact: {a_label} wants to be {better} when {b_label} is high than when "
        f"it's low, so the two have to be chosen together."
    )


def _gaps(points: list[dict], axes: dict[str, dict], curves: list[dict]) -> list[dict]:
    """Where the field has no measurements — the holes worth filling.

    Two kinds, and they need different action. A **gap** is a wide untested interval
    between two values that *have* been tested, so the answer is bracketed and one run
    resolves it. An **edge** is the best-performing value sitting at the end of the tested
    range, where the answer isn't bracketed at all and the field is telling you to look
    further out.
    """
    by_key = {c["key"]: c for c in curves}
    out: list[dict] = []
    for key, axis in axes.items():
        vals = axis["values"]
        curve = by_key.get(key)
        span = axis["max"] - axis["min"]
        if span <= 0:
            continue
        widest = max(
            ((lo, hi) for lo, hi in zip(vals, vals[1:])),
            key=lambda pair: pair[1] - pair[0],
            default=None,
        )
        if widest is not None and (widest[1] - widest[0]) / span >= 0.15:
            lo, hi = widest
            out.append({
                "key": key,
                "pipe": axis["pipe"],
                "field": axis["field"],
                "field_label": axis["field_label"],
                "unit": axis["unit"],
                "kind": "gap",
                "from": lo,
                "to": hi,
                "suggest": round((lo + hi) / 2.0, 3),
                "width_fraction": round((hi - lo) / span, 3),
                "detail": (
                    f"Nothing measured between {lo:g} and {hi:g} — "
                    f"{round((hi - lo) / span * 100)}% of the tested range is a blank."
                ),
            })
        if curve and curve["best_at_edge"]:
            at_max = curve["best_value"] == axis["max"]
            step = _step_for(axis)
            beyond = axis["max"] + step if at_max else max(0.0, axis["min"] - step)
            out.append({
                "key": key,
                "pipe": axis["pipe"],
                "field": axis["field"],
                "field_label": axis["field_label"],
                "unit": axis["unit"],
                "kind": "edge",
                "from": axis["min"],
                "to": axis["max"],
                "suggest": round(beyond, 3),
                "width_fraction": None,
                "detail": (
                    f"The best value tested ({curve['best_value']:g}) is the "
                    f"{'highest' if at_max else 'lowest'} one tried — the optimum may lie "
                    f"beyond it, and nothing out there has been measured."
                ),
            })
    out.sort(key=lambda g: -(g["width_fraction"] or 1.0))
    return out


def _step_for(axis: dict) -> float:
    """A sensible step past the edge of what's been tested: the field's own sweep step if
    it declares one, otherwise a fifth of the range already explored."""
    fld = shaper_field(axis["field"])
    default = (fld.sweep_default or {}) if fld else {}
    step = default.get("step")
    if isinstance(step, (int, float)) and step > 0:
        return float(step)
    return max((axis["max"] - axis["min"]) / 5.0, 1.0)


def _uncertainty(coords: dict[str, float], points: list[dict], axes: dict[str, dict]) -> tuple[float, float]:
    """``(uncertainty, distance to the nearest measured profile)`` for an untested point.

    The field's own spread, divided by ``√(1 + effective neighbours)`` under a Gaussian
    kernel over the normalized lever space. A point sitting among many similar measured
    profiles is pinned down; one in open space carries the full spread of the field, which
    is the honest statement that we have no idea — and the whole reason to go and look.
    """
    spread = _stdev([p["overall"] for p in points]) or 1.0
    weight = 0.0
    nearest = float("inf")
    for p in points:
        d = _distance(coords, p["coords"], axes)
        if d is None:
            continue
        nearest = min(nearest, d)
        weight += math.exp(-((d / KERNEL_BANDWIDTH) ** 2))
    return (spread / math.sqrt(1.0 + weight), nearest if nearest < float("inf") else 1.0)


def _curve_at(curve: dict | None, value: float) -> float | None:
    """The response curve's Overall at a value, interpolated between measured points.

    Beyond either end the curve is **clamped, not extrapolated**. Continuing a trend past
    the last thing anyone measured is exactly the kind of confident guess that sends a
    night of runs somewhere useless; the honest position out there is "we don't know", and
    that belongs in the uncertainty term — which is what makes the candidate attractive in
    the first place — not in the prediction.
    """
    if not curve or not curve.get("curve"):
        return None
    pts = curve["curve"]
    if value <= pts[0]["value"]:
        return pts[0]["overall"]
    if value >= pts[-1]["value"]:
        return pts[-1]["overall"]
    for lo, hi in zip(pts, pts[1:]):
        if lo["value"] <= value <= hi["value"]:
            span = hi["value"] - lo["value"]
            if span <= 0:
                return lo["overall"]
            t = (value - lo["value"]) / span
            return lo["overall"] + t * (hi["overall"] - lo["overall"])
    return None


def _predict(parent: dict, changes: dict[str, float], by_key: dict[str, dict]) -> float:
    """What a candidate would score, anchored on the profile it was branched from.

    Not a global regression: the candidate *is* "this measured profile with one lever
    moved", so the prediction should be that profile's measured Overall plus what the
    response curve says about moving that lever. A smoothed global average can never
    exceed the best value it was fitted on, so it drags every candidate branched from the
    winner back toward the middle of the field and reports the leader's own neighbourhood
    as unpromising — the one place worth looking hardest.
    """
    predicted = parent["overall"]
    for key, value in changes.items():
        curve = by_key.get(key)
        before = _curve_at(curve, parent["coords"][key])
        after = _curve_at(curve, value)
        if before is not None and after is not None:
            predicted += after - before
    return predicted


def _candidate_values(axis: dict, curve: dict | None) -> list[tuple[float, str]]:
    """The values worth trying on one axis, each with the reason it's interesting.

    Three moves, which is what a person does by hand with a table of results:

    * **Fill a hole** — the midpoint of any untested interval wide enough to hide a
      different answer. The result is bracketed on both sides, so one run settles it.
    * **Refine around the best** — halfway between the best value measured and each of its
      neighbours. Coverage is coarse, so the true optimum is usually *near* the winner
      rather than at it, and nobody ever picks these values by hand.
    * **Step past an edge** — when the best value is the highest (or lowest) anyone tried,
      the optimum isn't bracketed at all and the only way to find out is to go further.
    """
    vals = axis["values"]
    span = axis["max"] - axis["min"]
    if span <= 0:
        return []
    out: list[tuple[float, str]] = []

    for lo, hi in zip(vals, vals[1:]):
        if (hi - lo) / span >= 0.15:
            out.append(((lo + hi) / 2.0, f"fills the untested gap {lo:g}–{hi:g}"))

    if curve:
        best = curve["best_value"]
        if curve["best_at_edge"]:
            step = _step_for(axis)
            beyond = best + step if best == axis["max"] else best - step
            if beyond > 0:
                out.append((beyond, f"steps past {best:g}, the best value tested and the end of the range"))
        else:
            i = vals.index(best) if best in vals else -1
            for j in (i - 1, i + 1):
                if 0 <= j < len(vals) and i >= 0:
                    out.append((
                        (best + vals[j]) / 2.0,
                        f"refines between {best:g} (the best measured) and {vals[j]:g}",
                    ))

    # Coerce to the field's own granularity — an int lever has no use for 4213.5 — and drop
    # anything that lands back on a value already tested here.
    fld = shaper_field(axis["field"])
    tested = set(vals)
    cleaned: list[tuple[float, str]] = []
    seen: set[float] = set()
    for v, why in out:
        val = float(round(v)) if not fld or fld.kind != "str" else float(v)
        if val <= 0 or val in seen or val in tested:
            continue
        seen.add(val)
        cleaned.append((val, why))
    return cleaned


def _settings_for(parent: dict, changes: dict[str, float], axes: dict[str, dict]) -> list[dict]:
    """A per-pipe override list for ``POST /settings/test-settings`` — only the changed
    writable fields, keyed by pipe label, so the rest of the live profile is left alone."""
    by_pipe: dict[str, dict] = {}
    for key, value in changes.items():
        axis = axes[key]
        pipe = by_pipe.setdefault(axis["pipe"], {"label": axis["pipe"]})
        fld = shaper_field(axis["field"])
        pipe[axis["field"]] = int(round(value)) if (fld and fld.kind == "int") else value
    return list(by_pipe.values())


def _candidates(
    points: list[dict],
    axes: dict[str, dict],
    curves: list[dict],
    gaps: list[dict],
    limit: int,
) -> list[dict]:
    """Untested profiles worth measuring, best first.

    Built by branching from the profiles that already score well — change one lever (or
    two) to a value that is interesting *for a reason we can state*: it fills the widest
    hole on that axis, it steps past an edge the field is pushing against, or it's the best
    value anyone has measured on that axis but this profile isn't using. That keeps every
    proposal explainable ("Speedy Sloth, but with the download quantum nobody has tried"),
    which a raw optimizer sampling the space cannot do.

    They're then ranked by an upper confidence bound — predicted score plus what we don't
    know — because the question isn't "where do we expect to do well?" but "where might we
    do better than anything so far?", and those are different questions.
    """
    by_key = {c["key"]: c for c in curves}
    best_measured = max(p["overall"] for p in points)
    measured_coords = {tuple(sorted(p["coords"].items())) for p in points}
    parents = sorted(points, key=lambda p: (-p["overall"], -p["iterations"]))[:CANDIDATE_PARENTS]

    # (axis, value, why) worth trying, computed once per axis rather than per parent.
    moves: list[tuple[str, float, str]] = [
        (key, value, why)
        for key, axis in axes.items()
        for value, why in _candidate_values(axis, by_key.get(key))
    ]

    seen: set[tuple] = set()
    out: list[dict] = []

    def _emit(parent: dict, changes: dict[str, tuple[float, str]]) -> None:
        coords = dict(parent["coords"])
        coords.update({k: v for k, (v, _) in changes.items()})
        sig = tuple(sorted(coords.items()))
        if sig in seen or sig in measured_coords:
            return
        seen.add(sig)
        predicted = _predict(parent, {k: v for k, (v, _) in changes.items()}, by_key)
        uncertainty, nearest = _uncertainty(coords, points, axes)
        out.append({
            "changes": [
                {
                    "key": k,
                    "pipe": axes[k]["pipe"],
                    "field": axes[k]["field"],
                    "field_label": axes[k]["field_label"],
                    "unit": axes[k]["unit"],
                    "from": parent["coords"][k],
                    "to": v,
                    "why": why,
                }
                for k, (v, why) in changes.items()
            ],
            "parent": {
                "fingerprint": parent["fingerprint"],
                "name": parent["name"],
                "label": parent["label"],
                "overall": round(parent["overall"], 2),
            },
            "coords": coords,
            "predicted": round(predicted, 2),
            "uncertainty": round(uncertainty, 2),
            "upside": round(predicted + EXPLORATION_WEIGHT * uncertainty, 2),
            "nearest_measured": round(nearest, 3),
            "settings": _settings_for(parent, {k: v for k, (v, _) in changes.items()}, axes),
        })

    for parent in parents:
        one_lever = [
            (key, value, why)
            for key, value, why in moves
            if key in parent["coords"] and value != parent["coords"][key]
        ]
        for key, value, why in one_lever:
            _emit(parent, {key: (value, why)})
        # Pairs of levers, from the strongest parent only. Levers interact (see
        # `_interactions`), so the best profile may need two moved together — but the full
        # product explodes, and a candidate nobody can explain is worth nothing, so this
        # stays a bounded, stated handful rather than a search.
        if parent is parents[0]:
            for i, (ka, va, wa) in enumerate(one_lever):
                for kb, vb, wb in one_lever[i + 1:]:
                    if ka != kb:
                        _emit(parent, {ka: (va, wa), kb: (vb, wb)})
    # Best upside first: the point most likely to beat everything measured, not the point
    # most likely to score well (which is almost always something we've already got).
    out.sort(key=lambda c: -c["upside"])
    for c in out:
        c["beats_best_by"] = round(c["upside"] - best_measured, 2)
        c["summary"] = _candidate_summary(c, best_measured)
    return out[:limit]


def _candidate_summary(candidate: dict, best_measured: float) -> str:
    moves = ", ".join(
        f"{c['pipe']} {c['field_label']} {c['from']:g}{c['unit'] or ''} → {c['to']:g}{c['unit'] or ''}"
        for c in candidate["changes"]
    )
    why = "; ".join(dict.fromkeys(c["why"] for c in candidate["changes"]))
    return (
        f"{candidate['parent']['name'] or candidate['parent']['label']} "
        f"(Overall {candidate['parent']['overall']:g}) with {moves} — {why}. "
        f"Predicted {candidate['predicted']:g} ± {candidate['uncertainty']:g}; "
        f"upside {candidate['upside']:g} against the best measured {best_measured:.2f}."
    )


def landscape(session, *, suggestions: int = DEFAULT_SUGGESTIONS, confident_only: bool = True) -> dict:
    """The whole exploration picture: axes, response curves, interactions, gaps, candidates.

    ``confident_only`` keeps thin profiles out of the model by default — a lucky Overall on
    two iterations is noise, and noise in the training set becomes a confident-sounding
    prediction. It falls back to every scored profile when that leaves too little to model.
    """
    from .api.routes_settings import compute_profiles

    field = compute_profiles(session)
    scored = [
        p for p in field.get("profiles", [])
        if isinstance(p.get("overall"), (int, float))
    ]
    pool = [p for p in scored if p.get("confident")] if confident_only else list(scored)
    fell_back = False
    if len(pool) < MIN_POINTS:
        pool, fell_back = scored, bool(scored) and confident_only

    axes = _numeric_axes(pool)
    points = []
    for p in pool:
        coords = _coords(p, axes)
        if not coords:
            continue
        points.append({
            "fingerprint": p["fingerprint"],
            "name": p.get("name"),
            "label": p.get("label"),
            "overall": float(p["overall"]),
            "iterations": int(p.get("iterations") or 0),
            "confident": bool(p.get("confident")),
            "coords": coords,
        })

    if len(points) < MIN_POINTS or not axes:
        return {
            "axes": [],
            "points": [],
            "curves": [],
            "interactions": [],
            "gaps": [],
            "candidates": [],
            "best_overall": None,
            "profiles_modelled": len(points),
            "confident_only": confident_only and not fell_back,
            "reason": (
                f"Not enough comparable profiles to map the space yet — {len(points)} of the "
                f"{MIN_POINTS} needed. Collect runs (or re-grade history) and come back."
            ),
        }

    curves = _response_curves(points, axes)
    gaps = _gaps(points, axes, curves)
    return {
        "axes": sorted(axes.values(), key=lambda a: (a["pipe"], a["field"])),
        "points": points,
        "curves": curves,
        "interactions": _interactions(points, axes),
        "gaps": gaps,
        "candidates": _candidates(points, axes, curves, gaps, max(1, int(suggestions))),
        "best_overall": round(max(p["overall"] for p in points), 2),
        "profiles_modelled": len(points),
        "confident_only": confident_only and not fell_back,
        "exploration_weight": EXPLORATION_WEIGHT,
        "reason": None,
    }


__all__ = ["landscape"]
