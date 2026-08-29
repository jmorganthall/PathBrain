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

from .config_store import get_config
from .logging_config import get_logger

log = get_logger("crowning")

POLICIES = ("pooled", "duel")


def active_policy(session) -> str:
    cfg = get_config(session).get("crown_follow", {}) or {}
    policy = str(cfg.get("policy", "pooled") or "pooled").lower()
    return policy if policy in POLICIES else "pooled"


def resolve(session, pooled_best_fp: str | None) -> dict:
    """Resolve the governing crown under the active policy.

    Returns ``{policy, fingerprint, source, detail, duel_champion}``:

    * ``fingerprint`` — the profile the automation should treat as "the crown".
    * ``source`` — where it came from: ``"pooled"`` or ``"duel"`` (under the duel policy
      the pooled crown is the fallback when no fresh decisive duel verdict exists, and
      ``source`` says so honestly).
    * ``duel_champion`` — the latest fresh champion info (or None), always included so
      the UI can show both verdicts side by side whatever the policy.
    """
    from . import duel as duel_mod

    policy = active_policy(session)
    # Champion freshness, NOT the rematch cooldown — they used to share a field, and the
    # cooldown is now measured in hours. Automation must not abandon the champion just
    # because the ladder paused for an afternoon.
    freshness = duel_mod.champion_freshness_days(get_config(session).get("duel", {}) or {})
    champion = duel_mod.latest_champion(session, max_age_days=freshness)

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
    "RANKINGS",
    "RING_SOURCE",
    "POOLED_SOURCE",
    "UNMEASURED_SOURCE",
    "active_policy",
    "active_ranking",
    "rank_field",
    "resolve",
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
RANKINGS = ("ring", "pooled")


def active_ranking(session) -> str:
    """Which verdict orders the field: ``"ring"`` (default) or ``"pooled"`` (the old
    behaviour, kept so the two can be compared on the same data)."""
    cfg = get_config(session).get("crown_follow", {}) or {}
    ranking = str(cfg.get("ranking", "ring") or "ring").lower()
    return ranking if ranking in RANKINGS else "ring"


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

    ranking = active_ranking(session)
    profiles = list(field.get("profiles") or [])
    if ratings is None:
        ratings = duel_mod.ledger_ratings(session)
    sigma = duel_mod.rank_sigma(get_config(session).get("duel", {}) or {})

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
        entries.append(
            {
                "fingerprint": fp,
                "source": source,
                "ring_rating": rating,
                "ring_rating_se": r.get("rating_se"),
                "ring_rounds": pairs,
                "ring_provisional": bool(r.get("provisional", True)) if rated else None,
                "pooled_overall": pooled,
                "iterations": p.get("iterations"),
                "confident": bool(p.get("confident")),
            }
        )

    # Ring-rated first, then pooled-seeded, then unmeasured. Within each group the ordering
    # is that group's own verdict — the two scales are never compared to each other, which
    # is what makes this a partition rather than a blend.
    _GROUP = {RING_SOURCE: 0, POOLED_SOURCE: 1, UNMEASURED_SOURCE: 2}

    def _key(e: dict) -> tuple:
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
    }
