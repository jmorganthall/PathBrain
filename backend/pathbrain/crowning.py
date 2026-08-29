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


__all__ = ["POLICIES", "active_policy", "resolve"]
