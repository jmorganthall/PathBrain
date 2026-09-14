"""**Which profile should I run?** — the one answer, in one place.

PathBrain measures a great deal and, until this, said so in pieces. The Dashboard's
`TwoCrowns` shows the pooled crown beside the duel champion with *following* / *for
reference* chips; the Dueling Champions page adds the ring's #1 as a third claim; the
standings carry a rating, a proven floor, match points, a win rate, a pair rate and a
median margin; `crown_confidence` — the one reading that says how *sure* any of it is —
was fetched only on Profile Detail, a page you can only open once you already know which
profile you were going to ask about.

So the platform could tell you a dozen true things and not the one thing a person opens it
for. Reported, exactly: *"we've got ALL of these metrics and numbers and crowns and win
rate and pts and margin — where is the THIS IS THE BEST PROFILE rating?"*

Showing the verdicts apart was the right call while they disagreed *meaningfully*: two
controlled and observational readings naming different profiles is a real finding, and
averaging it away would have hidden it. What changed is that the ladder can now measure its
own resolving power (`decidability.resolving_power`) — and on this link that is ~0.85
Overall points against gaps between the top profiles of ~0.1. Three names and no resolution
is not richer than one name with an honest error bar; it is the same information, arranged
so nobody can act on it.

**The answer is the argmax, full stop** — the highest Overall among confident profiles, the
same `_select_crown` rule, no floor and no hysteresis, because a profile better by 0.000001
is better. What this module adds is not a different verdict but the two facts that make the
verdict *usable*:

* **How sure** — the lead over the runner-up against `crown_tie_sigma` × the pooled standard
  error of the two medians, and every profile that lead does not clear. "Palm Oyster, and
  fourteen profiles are tied with it" is a complete answer; "Palm Oyster" alone is a
  precision the data does not have, and three competing names is no answer at all.
* **Whether it matters** — the crown's margin over the unshaped baseline (`% vs SQM off`).
  A reader deciding where to spend a night needs to know that shaping is worth ~2 points and
  the choice between the tied leaders is worth ~0.2; without it, the ranking looks like the
  important question when it is the small one.

**Deliberately cheap: no `compute_profiles` pass.** This renders on the Dashboard, and a
cold field pass on page load is the documented way to take the process down (`_field_stamp`,
the unresponsiveness incident). Everything here comes from `profile_aggregates` — one row per
profile, stamp-verified — so the grade, its quartiles and the tie set are read the same way
`crown_follower` reads them, and the answer is identical to the field pass's for the same
data. The rollup stores **subscores** per crown metric, so the Overall and its own p25/p75
are three calls to the one `_grade_medians`, which keeps this arithmetic and the standings'
arithmetic from drifting apart.

That equivalence holds because a **weighted** crown grades each profile on its own. A
corner/percentile crown does not — it is field-relative, one run re-ranks everybody — so the
same medians would give a different ordering, and the headline card would quietly disagree
with the standings on the one screen that exists to settle the question. So a non-weighted
methodology is **declined in words**, with a pointer to the standings that pay for the field
pass: the same line `crown_follower._needs_full_check` draws, for the same reason. Under
`speed-smoothness-v16` it never fires.

Read-only: rollup in, one sentence out. Nothing here changes a score, and nothing here
writes the firewall — what to *do* with the answer is the crowning policy's decision.
"""
from __future__ import annotations

from . import decidability
from .config_store import get_config
from .logging_config import get_logger
from .methodology import (
    ensure_current_methodology,
    overall_method,
    overall_metrics,
    overall_weights,
)
from .settings_profile import SQM_OFF_FINGERPRINT

log = get_logger(__name__)

#: Profiles listed as tied with the leader. Beyond this the list stops being information and
#: starts being the field; the count is always reported in full.
MAX_TIED_LISTED = 8


def _quartile_grade(metrics: dict, key: str, crown: list[str], required: list[str],
                    weights: dict, iters: int) -> float | None:
    """The Overall formed from one quartile of each crown metric's subscore.

    ``_grade_medians`` is the single place the crown grade is formed, so the point Overall
    and the band around it are the same arithmetic read at three places in the rollup rather
    than two implementations that agree until one is edited.
    """
    from .crown_follower import _grade_medians

    row = {
        m: v.get(key)
        for m, v in (metrics or {}).items()
        if isinstance(v, dict) and v.get(key) is not None
    }
    return _grade_medians(row, iters, crown, required, weights)[0]


def _se(p25: float | None, p75: float | None, iterations: int) -> float | None:
    """Standard error of the median Overall ≈ IQR/√n — the same convention as
    ``routes_settings._overall_se``. ``None`` when the spread or the sample size is unknown."""
    if p25 is None or p75 is None or iterations < 1:
        return None
    return max(0.0, float(p75) - float(p25)) / (iterations ** 0.5)


def _pooled(a: dict, b: dict) -> float:
    """SE of the difference of two medians, ``√(SE_a² + SE_b²)``.

    An unknown SE contributes **0**, not "cannot say" — the same call
    ``routes_settings._finite`` makes, and for the same reason: absent evidence of noise
    must not *inflate* the bar a challenger has to clear. Matching it is what keeps this
    card and the standings' "tied" chip from ever disagreeing about what counts as
    separated.
    """
    se_a, se_b = a.get("se") or 0.0, b.get("se") or 0.0
    return (se_a ** 2 + se_b ** 2) ** 0.5


def _graded_field(session, version: str) -> list[dict]:
    """Every stored profile with its Overall and band, straight off the rollup."""
    from . import profile_aggregates
    from .refresh import list_profiles

    stored = list_profiles(session)
    if not stored:
        return []
    definition = ensure_current_methodology(session, get_config(session)).definition or {}
    crown, required = overall_metrics(definition)
    weights = overall_weights(definition)

    rolled = profile_aggregates.aggregates(session, version, [p["fingerprint"] for p in stored])
    out: list[dict] = []
    for p in stored:
        fp = p["fingerprint"]
        agg = rolled.get(fp) or {}
        metrics, iters = agg.get("metrics") or {}, int(agg.get("iterations") or 0)
        overall = _quartile_grade(metrics, "median", crown, required, weights, iters)
        if overall is None:
            continue
        p25 = _quartile_grade(metrics, "p25", crown, required, weights, iters)
        p75 = _quartile_grade(metrics, "p75", crown, required, weights, iters)
        out.append({
            "fingerprint": fp,
            "name": p.get("name"),
            "label": p.get("label"),
            "overall": round(float(overall), 2),
            "iterations": iters,
            "se": _se(p25, p75, iters),
            "is_sqm_off": fp == SQM_OFF_FINGERPRINT,
        })
    return out


def _sentence(best: dict, lead: float | None, bar: float | None, tied: list[dict],
              vs_off: float | None, resolves: float | None) -> str:
    """The answer, as a person would say it.

    Leads with the name in every branch — the question is "what do I run?", and a sentence
    that opens with a caveat has answered a different one. The hedge follows, because how
    much the choice is worth is the second thing you need and never the first.
    """
    who = best.get("name") or best.get("label") or best["fingerprint"][:8]
    out = f"Run {who} — Overall {best['overall']:.1f}."

    if lead is None:
        out += " Nothing else is measured well enough to compare it against yet."
    elif bar is not None and lead > bar:
        out += (
            f" It is ahead of the next profile by {lead:.2f} points, clear of the "
            f"±{bar:.2f} the run-to-run noise allows — a real lead."
        )
    elif tied:
        n = len(tied)
        out += (
            f" Its {lead:.2f}-point lead is inside the ±{bar:.2f} noise bar, and "
            f"{n} other profile{'s are' if n != 1 else ' is'} statistically tied with it — "
            f"running any of them is defensible on this evidence."
        )
    else:
        out += f" Its lead over the next profile is {lead:.2f} points."

    if vs_off is not None:
        out += (
            f" Shaping is worth {vs_off:+.1f}% against no shaper at all"
            + (
                f", and picking between the tied leaders is worth about {lead:.2f} points."
                if tied and lead is not None else "."
            )
        )
    if resolves is not None and tied:
        out += f" The duel resolves {resolves:.2f} points, so it cannot separate them either."
    return out


def verdict(session) -> dict:
    """Which profile to run, how sure, and whether the choice is worth anything.

    ``{best, runner_up, lead, noise_bar, clear, tied, tied_count, resolves, vs_sqm_off,
    sqm_off_overall, live, on_firewall, confident_profiles, methodology, overall_method,
    verdict}``. Every field is filled on every path, so a caller never has to ask which
    branch answered — `best is None` is the whole test, and `verdict` says why.
    """
    cfg = get_config(session)
    corr = cfg.get("correlation") or {}
    min_iterations = int(corr.get("min_iterations") or 15)
    sigma = float(corr.get("crown_tie_sigma") or 2.0)
    min_margin = float(corr.get("crown_tie_min_margin") or 0.0)

    methodology = ensure_current_methodology(session, cfg)
    definition = methodology.definition or {}
    method = overall_method(definition)

    out: dict = {
        "methodology": methodology.version,
        "overall_method": method,
        "min_iterations": min_iterations,
        "confident_profiles": 0,
        "sqm_off_overall": None,
        "best": None,
        "runner_up": None,
        "lead": None,
        "noise_bar": None,
        "clear": None,
        "tied": [],
        "tied_count": 0,
        "resolves": None,
        "vs_sqm_off": None,
        "live": None,
        "on_firewall": None,
        "verdict": (
            f"No profile has reached {min_iterations} iterations yet, so there is nothing "
            f"to crown. Measure one and this becomes an answer."
        ),
    }

    # A weighted crown grades each profile on its own, so the rollup's per-profile medians
    # ARE the standings' arithmetic. A corner/percentile crown is **field-relative** — one
    # run re-ranks everybody — so the same medians would give a different ordering, and a
    # headline card that quietly disagreed with the standings is worse than one that says it
    # cannot answer. This is the same line `crown_follower._needs_full_check` draws, for the
    # same reason; under `speed-smoothness-v16` it never fires. The honest way out is the
    # standings, which pay for the field pass a page load must not.
    if method != "weighted":
        out["verdict"] = (
            f"This methodology ({methodology.version}) ranks profiles against each other "
            f"rather than on their own, so the crown can't be read cheaply enough for this "
            f"card. The standings on Settings Impact are the answer."
        )
        return out

    field = _graded_field(session, methodology.version)
    # The crown is the argmax among CONFIDENT profiles — the same bar `_select_crown` uses.
    # A lucky five-iteration reading is not a verdict, and letting one hold this card would
    # make the one statement the product exists to make the least reliable thing on screen.
    confident = [p for p in field if p["iterations"] >= min_iterations and not p["is_sqm_off"]]
    confident.sort(key=lambda p: (p["overall"], p["iterations"]), reverse=True)
    out["confident_profiles"] = len(confident)
    out["sqm_off_overall"] = max((p["overall"] for p in field if p["is_sqm_off"]), default=None)
    sqm_off = out["sqm_off_overall"]
    if not confident:
        return out

    best = confident[0]
    runner_up = confident[1] if len(confident) > 1 else None
    lead = round(best["overall"] - runner_up["overall"], 2) if runner_up else None

    # The bar the lead must clear to be a real ordering rather than run-to-run noise — the
    # same test `_clearly_better` applies, so this card and the standings' "tied" chip can
    # never disagree about what counts as separated.
    bar = (
        round(max(min_margin, sigma * _pooled(best, runner_up)), 2)
        if runner_up is not None else None
    )

    tied = [
        p for p in confident[1:]
        if best["overall"] - p["overall"] <= max(min_margin, sigma * _pooled(best, p))
    ]

    # What the ring could settle, so the card can say whether racing would even help.
    # Best-effort: an unreadable ledger leaves the reading out rather than failing the one
    # card the product exists to render.
    resolves = None
    try:
        from .duel import _ledger_sessions

        d = cfg.get("duel") or {}
        resolves = decidability.resolving_power(
            _ledger_sessions(session, 200), int(d.get("max_pairs") or 30)
        ).get("min_margin")
    except Exception:  # noqa: BLE001 — a reading, never a reason this fails
        log.debug("Verdict: the ring's resolving power could not be read", exc_info=True)

    vs_off = (
        round((best["overall"] - sqm_off) / sqm_off * 100, 1)
        if sqm_off else None
    )

    # Is the firewall actually on it? The answer to "what should I run" is much less useful
    # without "and are you running it".
    live_fp = None
    try:
        from .providers import get_provider
        from .settings_profile import fingerprint, normalize

        live_fp = fingerprint(normalize(get_provider().discover()))
    except Exception:  # noqa: BLE001 — a firewall that will not answer is not this card's failure
        log.debug("Verdict: could not read the live profile", exc_info=True)

    by_fp = {p["fingerprint"]: p for p in field}
    out.update({
        "best": best,
        "runner_up": runner_up,
        "lead": lead,
        "noise_bar": bar,
        "clear": None if (lead is None or bar is None) else bool(lead > bar),
        "tied": tied[:MAX_TIED_LISTED],
        "tied_count": len(tied),
        "resolves": resolves,
        "vs_sqm_off": vs_off,
        "live": by_fp.get(live_fp) if live_fp else None,
        "on_firewall": None if live_fp is None else bool(live_fp == best["fingerprint"]),
        "verdict": _sentence(best, lead, bar, tied, vs_off, resolves),
    })
    return out


__all__ = ["MAX_TIED_LISTED", "verdict"]
