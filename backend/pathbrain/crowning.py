"""The CROWNING POLICY — the one first-class authority on "which verdict governs".

PathBrain now has more than one way to name a best profile:

* **pooled** — the all-time pooled Overall argmax (``compute_profiles`` →
  ``best_fingerprint``): the observational map of the whole field.
* **duel**  — the head-to-head duel ladder's latest champion (``duel.latest_champion``):
  the controlled-trial adjudication of the top contenders.

Rather than each automation surface (crown follower, race auto-promote, duel) growing
its own "apply the winner" switch, this module is the single resolver every writer
consults: the policy lives in config (``crown_follow.policy``), the GUI shows and
switches it (the Follow-best popover), and **exactly one component writes the firewall**
(the crown follower). Engines measure and adjudicate; the policy selects; the follower
applies. The pooled crown *statistic* is always computed and displayed regardless of
policy — the policy only decides which verdict automation acts on.
"""
from __future__ import annotations

from collections.abc import Mapping

from .config_store import get_config
from .domain.judgement import Judgement
from .logging_config import get_logger

log = get_logger("crowning")

#: ``"fused"`` (the default) is the one ranking fitted over BOTH records — the pooled medians
#: as anchors, every ring round as a paired difference (`overall_ranking`). ``"pooled"`` and
#: ``"duel"`` are its two corners, kept selectable so the three can be compared on one field.
#: ``"math"`` follows the mathematical crown (`pooled_field.math_crown`: the confident
#: profile with the lowest median FCP × median LCP in raw ms) — opt-in, never the default.
#: ``"lcp_first"`` follows the LCP-first crown (`pooled_field.lcp_first_crown`: the fastest
#: FCP among profiles within `crown_follow.lcp_band_pct` of the best LCP) — opt-in for now,
#: written as the candidate to become the real crown. ``"fcp_first"`` is its mirror (the
#: fastest LCP among profiles within `crown_follow.fcp_band_pct` of the best FCP), opt-in.
#: Both run the one `pooled_field.leg_first_crown`.
POLICIES = ("fused", "pooled", "duel", "math", "lcp_first", "fcp_first")
DEFAULT_POLICY = "fused"


def active_policy(session) -> str:
    cfg = get_config(session).get("crown_follow", {}) or {}
    policy = str(cfg.get("policy", DEFAULT_POLICY) or DEFAULT_POLICY).lower()
    return policy if policy in POLICIES else DEFAULT_POLICY


#: The speed budget (``crown_follow.speed_budget``): how much slower than the field's fastest
#: median LCP and fastest median FCP a crown may be before Auto-apply will not follow it.
#: The owner's terms: "give up a little top-end speed for a smoother, more pleasing load" —
#: never "very pleasing but slow". A gap counts only when it clears the 2σ noise rule
#: (`pooled_field.leg_gaps`), so a crown is never refused over a difference nobody measured.
DEFAULT_SPEED_BUDGET = {"enabled": True, "lcp_pct": 20.0, "fcp_pct": 25.0}
SPEED_BUDGET_RANGE = (0.0, 200.0)
#: Where Auto-apply looks when the chosen crown is over budget, in order: the speed-aware
#: candidates first, the fastest on paper last. The first one within budget is followed.
BUDGET_FALLBACK_ORDER = ("lcp_first", "fused", "fcp_first", "pooled", "duel", "math")


def speed_budget(cfg: dict | None, judged: Judgement | None = None) -> dict:
    """The speed budget: the methodology's (``judged``, `methodology.judgement`) when given,
    else the configured one, filled from the default and clamped. For a version with no
    judgement block the two are the same numbers (the judgement reads this setting)."""
    if judged is not None:
        b = judged.speed_budget
        return {"enabled": b.enabled, "lcp_pct": b.lcp_pct, "fcp_pct": b.fcp_pct}
    raw = ((cfg or {}).get("crown_follow") or {}).get("speed_budget") or {}
    out = dict(DEFAULT_SPEED_BUDGET)
    if isinstance(raw, dict):
        if "enabled" in raw:
            out["enabled"] = bool(raw["enabled"])
        lo, hi = SPEED_BUDGET_RANGE
        for key in ("lcp_pct", "fcp_pct"):
            try:
                out[key] = min(hi, max(lo, float(raw.get(key, out[key]))))
            except (TypeError, ValueError):
                pass
    return out


def budget_check(session, fingerprint: str | None, budget: dict | None = None) -> dict | None:
    """Is this profile within the speed budget? ``{within, over: [{leg, pct, limit_pct,
    delta_ms}], gaps}`` or None when it cannot be told (no fastest legs, no medians) —
    and "cannot tell" never refuses a crown."""
    if not fingerprint:
        return None
    return budget_checks(session, [fingerprint], budget).get(fingerprint)


def budget_checks(session, fingerprints, budget: dict | None = None) -> dict[str, dict | None]:
    """`budget_check` for many profiles at once, reading the field's fastest legs once.
    ``{fingerprint: check or None}``; the crown under a methodology that keeps it within the
    budget (`methodology.crown_within_speed_budget`) and Auto-apply read the same answer."""
    fps = [fp for fp in dict.fromkeys(fingerprints or []) if fp]
    if not fps:
        return {}
    from .methodology import ensure_current_methodology
    from .pooled_field import fastest_legs, leg_gaps

    cfg = get_config(session)
    budget = budget or speed_budget(cfg)
    try:
        version = ensure_current_methodology(session, cfg).version
        min_iter = int((cfg.get("correlation") or {}).get("min_iterations") or 15)
        fastest = fastest_legs(session, version, min_iter)
        all_gaps = leg_gaps(session, version, fastest, fps) if fastest else {}
    except Exception:  # noqa: BLE001 — a reading, never a reason the follower stops
        log.warning("Crowning: the speed budget could not be read", exc_info=True)
        return {fp: None for fp in fps}
    out: dict[str, dict | None] = {}
    for fp in fps:
        gaps = all_gaps.get(fp)
        if not gaps:
            out[fp] = None
            continue
        over = []
        for leg in ("lcp", "fcp"):
            g = gaps.get(leg)
            limit = budget[f"{leg}_pct"]
            if g and g.get("clear") and g["pct"] > limit:
                over.append({"leg": leg, "pct": g["pct"], "limit_pct": limit, "delta_ms": g["delta_ms"]})
        out[fp] = {"within": not over, "over": over, "gaps": gaps}
    return out


def _over_text(check: dict) -> str:
    return ", ".join(
        f"{o['leg'].upper()} +{o['pct']:.0f}% slower than the fastest (budget {o['limit_pct']:g}%)"
        for o in check["over"]
    )


def resolve(session, pooled_best_fp: str | None) -> dict:
    """Resolve the governing crown under the active policy, within the speed budget.

    The policy names a profile (`_resolve_policy`); when that profile is clearly slower
    than the field's fastest LCP or FCP by more than the budget allows, the first crown in
    `BUDGET_FALLBACK_ORDER` that fits is followed instead and ``detail`` says why. When
    none fits, the policy's choice stands and ``detail`` says that too. ``speed_budget``
    on the result records the check either way."""
    from .methodology import current_judgement  # lazy: methodology imports this module

    judged = current_judgement(session)
    if judged.crowned_by == "ring":
        return _resolve_ring(session, pooled_best_fp, judged)
    policy = active_policy(session)
    chosen = _resolve_policy(session, pooled_best_fp, policy)
    budget = speed_budget(get_config(session), judged)
    if not budget["enabled"] or not chosen.get("fingerprint"):
        chosen["speed_budget"] = {**budget, "checked": False}
        return chosen
    check = budget_check(session, chosen["fingerprint"], budget)
    if check is None or check["within"]:
        chosen["speed_budget"] = {**budget, "checked": check is not None, "within": True}
        return chosen

    tried = {chosen["fingerprint"]}
    for alt_policy in BUDGET_FALLBACK_ORDER:
        if alt_policy == policy:
            continue
        try:
            alt = _resolve_policy(session, pooled_best_fp, alt_policy)
        except Exception:  # noqa: BLE001
            log.warning("Crowning: fallback %s could not be resolved", alt_policy, exc_info=True)
            continue
        fp = alt.get("fingerprint")
        if not fp or fp in tried:
            continue
        tried.add(fp)
        alt_check = budget_check(session, fp, budget)
        if alt_check is not None and alt_check["within"]:
            log.info("Crowning: %s crown %s is over the speed budget (%s); following %s %s",
                     chosen["source"], chosen["fingerprint"], _over_text(check), alt["source"], fp)
            return {
                **alt,
                "policy": policy,
                "detail": (
                    f"speed budget — the {chosen['source']} crown is {_over_text(check)}, so "
                    f"Auto-apply follows the {alt['source']} crown instead ({alt['detail']})"
                ),
                "duel_champion": chosen.get("duel_champion"),
                "speed_budget": {**budget, "checked": True, "within": False, "over": check["over"],
                                 "replaced": {"fingerprint": chosen["fingerprint"],
                                              "source": chosen["source"]}},
            }
    chosen["detail"] = (
        f"{chosen['detail']} — over the speed budget ({_over_text(check)}), but no other "
        "crown fits it either, so this one stands"
    )
    chosen["speed_budget"] = {**budget, "checked": True, "within": False, "over": check["over"],
                              "replaced": None}
    return chosen


#: The Verdict's ``policy`` when the methodology crowns by the ring: no crowning policy
#: applies, so the Auto-apply card's picker has nothing to say.
RING_POLICY = "ring"


def _resolve_ring(session, pooled_best_fp: str | None, judged: Judgement) -> Mapping[str, object]:
    """The crown when the methodology crowns by the ring (``judged.crowned_by == "ring"``).

    The ring's champion under the methodology's crown rule (`duel.latest_champion`, which
    reads the rule from the judgement) holds the crown. Two things can pass it on, both the
    methodology's: SQM off may not hold the crown unless ``allow_unshaped_crown``, and a
    profile clearly over the speed budget on paired ring milliseconds
    (`ring_budget.ring_budget_checks`) may not either. Then the next profile in the ring's
    own standings that may hold it does. If none may, the champion stands and the detail
    says so. No other judge is consulted: no fallback chain, no pooled stand-in while the
    ring has an answer.

    With no fresh decided match in the ring there is no answer to give, so the crown stays
    where the Verdict last put it (a draw changes nothing); only a fresh install with no
    Verdict yet falls back to the pooled crown, and says so."""
    from . import duel as duel_mod
    from .ring_budget import ring_budget_checks
    from .settings_profile import is_sqm_off_profile

    cfg = get_config(session)
    freshness = duel_mod.champion_freshness_days(cfg["duel"] if "duel" in cfg else None)
    champion = duel_mod.latest_champion(session, max_age_days=freshness, rule=judged.ring_crown_rule)
    budget = speed_budget(None, judged)
    base = {"policy": RING_POLICY, "duel_champion": champion}

    def stays(reason: str) -> Mapping[str, object]:
        from . import verdict

        sitting = verdict.crown_fingerprint(session)
        if sitting:
            return {**base, "fingerprint": sitting, "source": "ring",
                    "detail": f"{reason}, so the crown stays where it is",
                    "speed_budget": {**budget, "checked": False}}
        return {**base, "fingerprint": pooled_best_fp, "source": "pooled",
                "detail": f"{reason} and no crown has been decided yet, so the pooled crown stands in",
                "speed_budget": {**budget, "checked": False}}

    if champion is None or not champion["decisive"]:
        return stays("the ring has no fresh decided match")
    champ = champion["fingerprint"]
    order = [champ] + [fp for fp in duel_mod.ring_order(session) if fp != champ]
    barred_unshaped = [] if judged.allow_unshaped_crown else [fp for fp in order if is_sqm_off_profile(fp)]
    order = [fp for fp in order if fp not in barred_unshaped]
    if not order:
        return stays("SQM off holds the ring's title and this methodology keeps it off the crown")
    lead = order[0]
    won = str(champion["finished_at"])[:10] if "finished_at" in champion and champion["finished_at"] else "its last match"
    note = (f" (SQM off beat the crown on {won}; this methodology keeps SQM off off the crown)"
            if champ in barred_unshaped else "")
    if not budget["enabled"]:
        return {**base, "fingerprint": lead, "source": "ring",
                "detail": f"the ring's champion under the {judged.ring_crown_rule} rule{note}",
                "speed_budget": {**budget, "checked": False}}
    checks = ring_budget_checks(session, order, judged.speed_budget, judged.version, judged.tie_sigma)
    for fp in order:
        check = checks[fp]
        if check is not None and not check.within:
            continue
        passed = [o for o in order[: order.index(fp)]]
        if not passed:
            detail = f"the ring's champion under the {judged.ring_crown_rule} rule{note}"
        else:
            first = checks[passed[0]]
            detail = (
                f"the ring's champion is {_ring_over_text(first)} back to back, so the crown "
                f"passes to the next profile in the ring's standings within the budget{note}"
            )
        return {**base, "fingerprint": fp, "source": "ring", "detail": detail,
                "speed_budget": {**budget, "checked": check is not None, "within": True,
                                 "judged_on": "ring",
                                 "replaced": ({"fingerprint": passed[0], "source": "ring"} if passed else None),
                                 **({"over": checks[passed[0]].as_dict()["over"]} if passed else {})}}
    over = checks[lead]
    return {**base, "fingerprint": lead, "source": "ring",
            "detail": (f"the ring's champion under the {judged.ring_crown_rule} rule{note}, "
                       f"{_ring_over_text(over)} back to back, but no profile in the ring fits "
                       "the budget either, so it stands"),
            "speed_budget": {**budget, "checked": True, "within": False, "judged_on": "ring",
                             "over": over.as_dict()["over"], "replaced": None}}


def preview_ring(session, judged: Judgement) -> Mapping[str, object]:
    """What the crown would be under ``judged`` if its version were current: the same
    resolution (`_resolve_ring`) on today's ring, with the pooled crown as last observed as
    the fresh-install stand-in, for the Crown agreement card before a publish. Read-only;
    ``judged`` must crown by the ring."""
    if judged.crowned_by != "ring":
        raise ValueError(f"{judged.version} does not crown by the ring")
    from .crown_follower import current_crown

    pooled = current_crown(session)
    pooled_fp = pooled["fingerprint"] if pooled and "fingerprint" in pooled else None
    return _resolve_ring(session, pooled_fp, judged)


def _ring_over_text(check) -> str:
    return ", ".join(
        f"{o.leg.upper()} +{o.pct:.0f}% slower than {o.against[:8]} (budget {o.limit_pct:g}%)"
        for o in check.over
    )


def _resolve_policy(session, pooled_best_fp: str | None, policy: str) -> dict:
    """Resolve the governing crown under one policy, before the speed budget.

    Returns ``{policy, fingerprint, source, detail, duel_champion}``:

    * ``fingerprint`` — the profile the automation should treat as "the crown".
    * ``source`` — where it came from: ``"pooled"`` or ``"duel"`` (under the duel policy
      the pooled crown is the fallback when no fresh decisive duel verdict exists, and
      ``source`` says so honestly).
    * ``duel_champion`` — the latest fresh champion info (or None), always included so
      the UI can show both verdicts side by side whatever the policy.
    """
    from . import duel as duel_mod

    # Champion freshness, NOT the rematch cooldown — they used to share a field, and the
    # cooldown is now measured in hours. Automation must not abandon the champion just
    # because the ladder paused for an afternoon.
    freshness = duel_mod.champion_freshness_days(get_config(session).get("duel", {}) or {})
    champion = duel_mod.latest_champion(session, max_age_days=freshness)

    if policy == "fused":
        from . import overall_ranking

        fused = None
        try:
            fused = overall_ranking.crown(session)
        except Exception:  # noqa: BLE001 — a failed fit falls back, it never stops the follower
            log.warning("Crowning: the fused ranking could not be computed", exc_info=True)
        if fused and fused.get("fingerprint"):
            return {
                "policy": policy,
                "fingerprint": fused["fingerprint"],
                "source": "fused",
                "detail": (
                    f"fused ranking — Overall {fused['fused']:.1f} ± {fused['fused_se']:.2f}"
                    + (f", leads by {fused['lead']:.2f}" if fused.get("lead") is not None else "")
                    + (" (clear)" if fused.get("clear") else
                       f" ({fused.get('tied_count') or 0} tied)" if fused.get("lead") is not None else "")
                ),
                "duel_champion": champion,
                "fused": fused,
            }
        return {
            "policy": policy,
            "fingerprint": pooled_best_fp,
            "source": "pooled",
            "detail": "the fused ranking names nobody yet — falling back to the pooled crown",
            "duel_champion": champion,
            "fused": None,
        }

    if policy == "math":
        from .methodology import ensure_current_methodology
        from .pooled_field import math_crown

        math = None
        try:
            cfg = get_config(session)
            version = ensure_current_methodology(session, cfg).version
            min_iter = int((cfg.get("correlation") or {}).get("min_iterations") or 15)
            math = math_crown(session, version, min_iter)
        except Exception:  # noqa: BLE001 — a failed read falls back, it never stops the follower
            log.warning("Crowning: the mathematical crown could not be computed", exc_info=True)
        if math and math.get("fingerprint"):
            return {
                "policy": policy,
                "fingerprint": math["fingerprint"],
                "source": "math",
                "detail": (
                    f"fastest on paper — FCP {math['fcp_ms']:.0f} ms × LCP {math['lcp_ms']:.0f} ms"
                    f" over {math['iterations']} iterations"
                ),
                "duel_champion": champion,
                "math": math,
            }
        return {
            "policy": policy,
            "fingerprint": pooled_best_fp,
            "source": "pooled",
            "detail": "no confident profile has both FCP and LCP medians yet — falling back to the pooled crown",
            "duel_champion": champion,
            "math": None,
        }

    if policy in ("lcp_first", "fcp_first"):
        from . import pooled_field
        from .methodology import ensure_current_methodology
        from .pooled_field import LEG_FIRST, band_pct

        spec = LEG_FIRST.get(policy)
        if spec is None:
            # The current crown has no leg pair this policy can lead with (`pooled_field`).
            return {
                "policy": policy,
                "fingerprint": pooled_best_fp,
                "source": "pooled",
                "detail": f"the current crown has no legs for {policy} — falling back to the pooled crown",
                "duel_champion": champion,
                policy: None,
            }
        lead, then = spec["lead"], spec["then"]
        lf = None
        try:
            cfg = get_config(session)
            version = ensure_current_methodology(session, cfg).version
            min_iter = int((cfg.get("correlation") or {}).get("min_iterations") or 15)
            # Read by name (`lcp_first_crown` / `fcp_first_crown`), the two public entry
            # points onto the one `leg_first_crown` rule.
            lf = getattr(pooled_field, f"{policy}_crown")(session, version, min_iter, band_pct(cfg, policy))
        except Exception:  # noqa: BLE001 — a failed read falls back, it never stops the follower
            log.warning("Crowning: the %s crown could not be computed", policy, exc_info=True)
        if lf and lf.get("fingerprint"):
            best = lf[f"best_{lead}"]
            return {
                "policy": policy,
                "fingerprint": lf["fingerprint"],
                "source": policy,
                "detail": (
                    f"{lead.upper()} first — {lead.upper()} {lf[f'{lead}_ms']:.0f} ms "
                    f"(best {best[f'{lead}_ms']:.0f}, within {lf['band_pct']:g}%), fastest "
                    f"{then.upper()} {lf[f'{then}_ms']:.0f} ms of {lf['qualifiers']} qualifying"
                ),
                "duel_champion": champion,
                policy: lf,
            }
        return {
            "policy": policy,
            "fingerprint": pooled_best_fp,
            "source": "pooled",
            "detail": "no confident profile has both FCP and LCP medians yet — falling back to the pooled crown",
            "duel_champion": champion,
            policy: None,
        }

    if policy == "duel":
        if champion and champion.get("decisive"):
            return {
                "policy": policy,
                "fingerprint": champion["fingerprint"],
                "source": "duel",
                "detail": f"duel #{champion['duel_id']} champion ({champion['finished_at']})",
                "duel_champion": champion,
            }
        detail = (
            "no decisive duel verdict within the freshness window — falling back to the pooled crown"
            if champion is None or not champion.get("decisive")
            else "duel champion unavailable"
        )
        return {
            "policy": policy,
            "fingerprint": pooled_best_fp,
            "source": "pooled",
            "detail": detail,
            "duel_champion": champion,
        }

    return {
        "policy": policy,
        "fingerprint": pooled_best_fp,
        "source": "pooled",
        "detail": "pooled all-time Overall argmax",
        "duel_champion": champion,
    }


__all__ = [
    "POLICIES",
    "RING_POLICY",
    "preview_ring",
    "DEFAULT_POLICY",
    "RANKINGS",
    "RING_SOURCE",
    "POOLED_SOURCE",
    "UNMEASURED_SOURCE",
    "active_policy",
    "active_ranking",
    "rank_field",
    "resolve",
    "speed_budget",
    "budget_check",
    "budget_checks",
    "DEFAULT_SPEED_BUDGET",
]


# ── The field's primary ordering: the ring first, pooled as the seed ──────────────────
#
# A duel round is a PAIRED, interleaved, counterbalanced comparison under shared weather —
# a controlled experiment. The pooled Overall is an average over runs taken at different
# times under conditions that were never held equal — an observational statistic. On the
# same question the controlled comparison wins, so where the ring has actually measured a
# profile head to head, the ring orders it.
#
# Pooled keeps exactly one job here, and it is a real one: **seeding the unrated**. A
# profile the ring has never produced a round for has no head-to-head evidence at all, and
# something still has to say which of those is worth racing first. That is what the pooled
# score is good at — a macro map of the field — and it is the only thing it is asked for.
#
# Two deliberate exclusions, stated here so they read as decisions rather than oversights:
#
# * **The duel engine keeps reading the POOLED crown** (`duel.contender_order`'s
#   CROWN_TIER). Feeding this ordering back into matchmaking would make the ladder
#   circular — the ring would choose who gets checked against the ring — which is the exact
#   failure `contender_order` was written to escape. The pooled crown earns its first
#   billing there precisely BECAUSE it is the independent opinion.
#
# * **Explore keeps branching from the pooled best.** Its response curves, predictions and
#   uncertainties are all fitted in pooled-Overall space; choosing a parent by a ring rating
#   and then pricing it against a pooled model mixes two scales that were never calibrated
#   against each other. Same reason the crown metrics are never mixed with raw ms.
#
# `RING_SOURCE`/`POOLED_SOURCE`/`UNMEASURED_SOURCE` are the only three states, and every
# profile in the field lands in exactly one — there is no fourth case and no blend.

RING_SOURCE = "ring"
POOLED_SOURCE = "pooled"
UNMEASURED_SOURCE = "unmeasured"
RANKINGS = ("fused", "ring", "pooled")
DEFAULT_RANKING = "fused"


def active_ranking(session) -> str:
    """Which verdict orders the field: ``"fused"`` (default — one Overall scale over both
    records), ``"ring"`` (the ring where it has fought, pooled as the seed — a partition of
    two scales) or ``"pooled"`` (the oldest order), the last two kept so the three can be
    compared on the same data."""
    cfg = get_config(session).get("crown_follow", {}) or {}
    ranking = str(cfg.get("ranking", DEFAULT_RANKING) or DEFAULT_RANKING).lower()
    return ranking if ranking in RANKINGS else DEFAULT_RANKING


def _fused_scores(session) -> dict[str, dict]:
    """``{fingerprint: {fused, fused_se, rounds}}`` off the Overall ranking — the one fit
    over both records. Best-effort: an unreadable fit orders the field as if the ring had
    fought nobody, never fails the page."""
    try:
        from . import overall_ranking

        r = overall_ranking.ranking(session, backtest=False, live=False)
        return {
            row["fingerprint"]: {
                "fused": row.get("fused"), "fused_se": row.get("fused_se"),
                "rounds": int(row.get("rounds") or 0),
            }
            for row in (r.get("profiles") or []) if row.get("fingerprint")
        }
    except Exception:  # noqa: BLE001
        log.warning("rank_field: could not read the fused ranking; ordering on the shrunk pooled Overall", exc_info=True)
        return {}


def rank_field(session, field: dict, ratings: dict | None = None) -> dict:
    """The canonical ordering of the whole field — **one resolver, every surface**.

    Returns ``{ranking, order, entries, by_fingerprint, best_fingerprint, best_source,
    ring_rated, seeded, unmeasured}``. ``entries`` carries, per profile, which verdict
    placed it and both underlying numbers, so a caller never has to blend the two scales
    itself — blending in five places is how the two verdicts drift apart.

    Ring-rated means the ledger holds **at least one round** for that profile: real paired
    evidence, fitted by Bradley-Terry. It deliberately does NOT mean "non-provisional" —
    that bar is so high the ring would almost never govern, which is the opposite of the
    point — and it deliberately excludes profiles whose only appearances were aborted
    matches, which produced no rounds and therefore demonstrated nothing.
    """
    from . import duel as duel_mod
    from .phase_timing import laps

    lap = laps("rank_field")
    ranking = active_ranking(session)
    profiles = list(field.get("profiles") or [])
    if ratings is None:
        ratings = duel_mod.ledger_ratings(session)
    sigma = duel_mod.rank_sigma(get_config(session).get("duel", {}) or {})
    lap("ratings")
    fused_by_fp = _fused_scores(session) if ranking == "fused" else {}
    lap("fused")

    entries: list[dict] = []
    for p in profiles:
        fp = p.get("fingerprint")
        r = ratings.get(fp) or {}
        pairs = int(r.get("pairs") or 0)
        rating = r.get("rating")
        pooled = p.get("overall")
        rated = pairs > 0 and rating is not None
        if ranking == "pooled":
            source = POOLED_SOURCE if pooled is not None else UNMEASURED_SOURCE
        elif rated:
            source = RING_SOURCE
        elif pooled is not None:
            source = POOLED_SOURCE
        else:
            source = UNMEASURED_SOURCE
        # The one scale the fused order sorts on: the fused Overall where the ring has
        # fought this profile (its pooled anchor corrected by its rounds), else its shrunk
        # pooled Overall (the posterior under the field prior — a lucky thin reading is
        # pulled toward the field exactly as its noise warrants), else the raw pooled one.
        # All three are posterior-mean estimates on the Overall scale, which is what lets
        # them be sorted together; an Elo rating and a raw Overall could not be.
        fz = fused_by_fp.get(fp) or {}
        score: float | None
        if ranking == "fused" and fz.get("fused") is not None and fz.get("rounds", 0) > 0:
            score, score_source = float(fz["fused"]), "fused"
        elif p.get("overall_shrunk") is not None:
            score, score_source = float(p["overall_shrunk"]), "shrunk"
        elif pooled is not None:
            score, score_source = float(pooled), "pooled"
        else:
            score, score_source = None, None
        entries.append(
            {
                "fingerprint": fp,
                "source": source,
                "ring_rating": rating,
                "ring_rating_se": r.get("rating_se"),
                "ring_rounds": pairs,
                "ring_provisional": bool(r.get("provisional", True)) if rated else None,
                "pooled_overall": pooled,
                "fused_overall": fz.get("fused"),
                "fused_se": fz.get("fused_se"),
                "score": score,
                "score_source": score_source,
                "iterations": p.get("iterations"),
                "confident": bool(p.get("confident")),
            }
        )

    # ``ring``/``pooled``: ring-rated first, then pooled-seeded, then unmeasured, each group
    # ordered by its own verdict — a partition of two scales, never a blend. ``fused``: one
    # scale (``score``), measured profiles first, unmeasured last.
    _GROUP = {RING_SOURCE: 0, POOLED_SOURCE: 1, UNMEASURED_SOURCE: 2}

    def _key(e: dict) -> tuple:
        if ranking == "fused":
            if e["score"] is None:
                return (1, 0.0, 0.0, 0.0)
            return (0, -float(e["score"]), -(e["iterations"] or 0), -(e["ring_rounds"] or 0))
        group = _GROUP[e["source"]]
        if e["source"] == RING_SOURCE:
            score = float(e["ring_rating"]) - sigma * float(e["ring_rating_se"] or 0.0)
            return (group, -score, -(e["ring_rounds"] or 0))
        if e["source"] == POOLED_SOURCE:
            return (group, -float(e["pooled_overall"] or 0.0), -(e["iterations"] or 0))
        return (group, 0.0, 0.0)

    entries.sort(key=_key)
    for i, e in enumerate(entries, start=1):
        e["position"] = i

    counts = {s: sum(1 for e in entries if e["source"] == s) for s in _GROUP}
    return {
        "ranking": ranking,
        "order": [e["fingerprint"] for e in entries],
        "entries": entries,
        "by_fingerprint": {e["fingerprint"]: e for e in entries},
        "best_fingerprint": entries[0]["fingerprint"] if entries else None,
        "best_source": entries[0]["source"] if entries else None,
        "ring_rated": counts[RING_SOURCE],
        "seeded": counts[POOLED_SOURCE],
        "unmeasured": counts[UNMEASURED_SOURCE],
        "rank_sigma": sigma,
        # Under the fused order: how many profiles each kind of score placed.
        "score_sources": {
            k: sum(1 for e in entries if e["score_source"] == k) for k in ("fused", "shrunk", "pooled")
        },
    }
