"""Publish follow-through: what runs by itself once history has been re-graded.

A methodology publish has two costs, and only one of them was ever paid automatically. The
re-grade re-scores every run from stored raw, so a re-weighted crown costs nothing but a
job. But a publish that changes the *instrument* — the sites, the browser client, a metric
older raw cannot supply — quarantines the old runs, and then the new version has a field
with nothing in it: no crown, every profile "untested", and the prior version's two hundred
ranked profiles sitting there with their standings known and their current data at zero.
The seed (`refresh.seed_field_from_prior`) orders the empty field by the old verdict so the
ring, the race and the heirs card have somebody to measure; the winner-first re-run
(`refresh.start(top=…)`) collects the fresh data. Both existed, and the second was a button
— so after every real publish somebody had to remember to press it, and the ladder spent
its first nights seeding a field by hand.

So the re-grade job now finishes the publish on its own, through one body every re-grade
route shares (:func:`regrade_then_follow_through`): grade history, re-read the duel ledger
under the new version (`duel.regrade_ledger`), and **queue the prior version's winners for
one chunk each** (:func:`queue_prior_winners`) — the top ``publish.reseed_top`` profiles by
the prior verdict, *confident under it* (a thin lucky reading was never a winner by this
project's own rule), that lack ``publish.reseed_iterations`` comparable iterations under the
current version. Queued as an ordinary winner-first refresh through `job_queue`, so it waits
its turn behind whatever holds the pipeline, survives a restart, and restores the baseline
like every refresh. Idempotent by construction: a winner that already has its chunk is
skipped, so a second re-grade queues nothing; a refresh already queued or running is left
alone rather than doubled. A publish that re-grades history whole (a re-weighting) queues
nothing, because every winner already has data — which is exactly right. Setting
``publish.reseed_top`` or ``reseed_iterations`` to 0 switches it off; `POST
/api/methodologies/reseed` runs it on demand (``dry_run`` previews the plan).

What this deliberately does not do: pick the winners by the *current* version (there is no
current data — that is the whole situation), re-run everything (that is the un-ranked
refresh, still a button), or write the firewall itself — it submits a job that does so under
every existing rule.
"""
from __future__ import annotations

from sqlalchemy import func, select

from . import job_queue
from .config_store import get_config
from .database import session_scope
from .logging_config import get_logger
from .methodology import ensure_current_methodology
from .models import Run, RunStatus, Score

log = get_logger("publish_followthrough")

#: How many of the prior version's winners are queued after a publish (0 = off).
DEFAULT_TOP = 10
#: Iterations each winner is queued for — one `runner.CHUNK_ITERATIONS` block, enough for a
#: first placement under the new rubric; the ring matures whatever that placement makes
#: interesting (0 = off).
DEFAULT_ITERATIONS = 5


def settings(cfg: dict | None) -> tuple[int, int]:
    """``(top, iterations)`` from config, both floored at 0 (0 switches the step off)."""
    block = (cfg or {}).get("publish") or {}
    try:
        top = int(block.get("reseed_top", DEFAULT_TOP) or 0)
    except (TypeError, ValueError):
        top = DEFAULT_TOP
    try:
        iterations = int(block.get("reseed_iterations", DEFAULT_ITERATIONS) or 0)
    except (TypeError, ValueError):
        iterations = DEFAULT_ITERATIONS
    return max(0, top), max(0, iterations)


def iterations_under(session, version: str) -> dict[str, int]:
    """``{fingerprint: comparable iterations}`` under one methodology version — the same
    counting rule as a profile's confidence (`correlation.min_iterations`): completed runs,
    scored under the version, not quarantined. A run with no iteration count counts as one."""
    rows = session.execute(
        select(Run.settings_fingerprint, func.sum(func.coalesce(Run.iterations, 1)))
        .join(Score, Score.run_id == Run.id)
        .where(
            Run.status == RunStatus.COMPLETE,
            Run.settings_fingerprint.is_not(None),
            Score.methodology_version == version,
            Score.comparability != "incomparable",
        )
        .group_by(Run.settings_fingerprint)
    ).all()
    return {str(fp): int(total or 0) for fp, total in rows}


def plan_prior_winners(session, *, top: int | None = None, iterations: int | None = None) -> dict:
    """What a reseed would queue, and why each prior winner is or is not in it. Read-only.

    Returns ``{version, prior_version, top, iterations, winners, needed, skipped, reason?}``:
    ``winners`` are the prior version's top profiles (confident under it, best first),
    ``needed`` the ones short of ``iterations`` comparable iterations under the current
    version and still stored, ``skipped`` the rest with the reason each.
    """
    from . import refresh

    cfg = get_config(session)
    d_top, d_iters = settings(cfg)
    top = d_top if top is None else max(0, int(top))
    iterations = d_iters if iterations is None else max(0, int(iterations))
    current = ensure_current_methodology(session, cfg).version
    min_iters = int((cfg.get("correlation") or {}).get("min_iterations") or 15)
    plan: dict = {
        "version": current, "prior_version": None, "top": top, "iterations": iterations,
        "winners": [], "needed": [], "skipped": [],
    }
    if top <= 0 or iterations <= 0:
        plan["reason"] = "switched off (publish.reseed_top or publish.reseed_iterations is 0)"
        return plan
    prior = refresh.prior_field(session, min_iters)
    if not prior:
        plan["reason"] = "no prior methodology has scored anything to seed from"
        return plan
    plan["prior_version"] = prior["version"]
    prior_iters = iterations_under(session, prior["version"])
    current_iters = iterations_under(session, current)
    stored = {p["fingerprint"]: p for p in refresh.list_profiles(session)}
    ranked = sorted(prior["overall"].items(), key=lambda kv: kv[1], reverse=True)
    # Confident under the prior: a two-iteration 95 was never a winner by the project's own
    # rule, and queueing it would spend the first night after a publish on noise.
    winners = [(fp, ov) for fp, ov in ranked if prior_iters.get(fp, 0) >= min_iters][:top]
    for fp, ov in winners:
        have = current_iters.get(fp, 0)
        row = {
            "fingerprint": fp,
            "name": (stored.get(fp) or {}).get("name"),
            "label": (stored.get(fp) or {}).get("label"),
            "prior_overall": ov,
            "prior_iterations": prior_iters.get(fp, 0),
            "current_iterations": have,
        }
        plan["winners"].append(row)
        if fp not in stored:
            plan["skipped"].append({**row, "why": "no stored settings to apply"})
        elif have >= iterations:
            plan["skipped"].append({**row, "why": f"already has {have} comparable iteration(s) under {current}"})
        else:
            plan["needed"].append(row)
    if not plan["winners"]:
        plan["reason"] = f"no profile is confident under {prior['version']}"
    elif not plan["needed"]:
        plan["reason"] = f"every prior winner already has comparable data under {current}"
    return plan


def _refresh_in_flight() -> bool:
    try:
        if job_queue.kind_active("refresh"):
            return True
        return any((t or {}).get("kind") == "refresh" for t in job_queue.pending())
    except Exception:  # noqa: BLE001 — an unreadable queue must not stop the reseed
        return False


def queue_prior_winners(
    session, *, top: int | None = None, iterations: int | None = None, dry_run: bool = False,
) -> dict:
    """Queue the prior version's winners that lack data under the current one, as a
    winner-first refresh of exactly those profiles (`refresh.start(fingerprints=…)`) through
    `job_queue` — it starts now if the pipeline is free, otherwise waits its turn and survives a
    restart. Returns the plan plus ``submitted`` and, when submitted, the queue's placement
    block (whose own ``queued`` says whether it waits or started at once)."""
    plan = plan_prior_winners(session, top=top, iterations=iterations)
    fps = [n["fingerprint"] for n in plan["needed"]]
    if not fps:
        return {**plan, "submitted": False}
    if dry_run:
        return {**plan, "submitted": False, "dry_run": True}
    if _refresh_in_flight():
        return {**plan, "submitted": False, "reason": "a profile refresh is already queued or running"}
    label = (
        f"Prior winners · {len(fps)} profile(s) × {plan['iterations']} iteration(s) — "
        f"first data under {plan['version']}"
    )
    try:
        submission = job_queue.submit(
            "refresh", label, spec={"iterations": plan["iterations"], "fingerprints": fps},
        )
    except RuntimeError as exc:  # nothing stored to re-run — the plan said why, this confirms it
        return {**plan, "submitted": False, "reason": str(exc)}
    log.info(
        "Publish follow-through: %s under %s (%s)", label, plan["version"],
        "started" if submission.started else f"queued behind {submission.blocked_by or 'the queue'}",
    )
    return {
        **plan,
        **submission.placement(),
        "submitted": True,
        "label": label,
        "refresh_id": submission.result if submission.started else None,
    }


def regrade_then_follow_through(job) -> dict:
    """The one body every re-grade job runs: grade history under the current methodology,
    then finish the publish — re-read the duel ledger under the new version and queue the
    prior winners for their first chunk. Each follow-through step is best-effort and reports
    into the summary; the history re-grade itself has already landed when they run."""
    from .runner import score_history_under_current

    with session_scope() as session:
        summary = dict(score_history_under_current(session, progress=job.set_progress) or {})
    try:
        from . import duel

        with session_scope() as session:
            summary["ledger"] = duel.regrade_ledger(session)
    except Exception as exc:  # noqa: BLE001 — the history re-grade already landed
        log.warning("Re-grade: the duel ledger could not be re-graded", exc_info=True)
        summary["ledger"] = {"error": str(exc)}
    try:
        with session_scope() as session:
            summary["reseed"] = queue_prior_winners(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("Re-grade: the prior winners could not be queued", exc_info=True)
        summary["reseed"] = {"submitted": False, "error": str(exc)}
    return summary
