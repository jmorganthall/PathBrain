"""Challenger race: adaptively test promising profiles one iteration at a time.

The adaptive, multi-profile sibling of ``profile_test`` ("Test to minimum"). Instead
of topping a single profile all the way up to ``correlation.min_iterations`` (often
wasted on a fluke), this races every *limited-data* profile against the confident
"best":

1. Snapshot the live firewall settings (the baseline to restore).
2. Loop, time-boxed, while there's a challenger that could still win:
   - rank under-minimum profiles by an **optimistic Overall** (feel-trinity corner over
     each crown metric's upper estimate; see ``optimistic_overall``),
   - **eliminate** any whose optimistic best-case can't beat the best's Overall,
   - if the crowned incumbent's newest run is **stale** (older than
     ``challenger.incumbent_refresh_minutes``), re-measure IT first so challengers race
     a *contemporaneous* bar (removes time-of-day drift; keeps the crown's band tight),
   - else apply the top challenger (only when it isn't already live), run **one**
     benchmark iteration, and re-rank. A challenger that reaches the minimum and beats
     the best becomes the new bar the rest race against.
3. At the end, **restore the baseline** — unless ``auto_promote`` is set and a
   challenger confirmed it beats the best, in which case the winner is left applied.

It runs in its own thread and holds the coordination lock for the whole session, so it
never overlaps a sweep, an experiment, or a monitoring/manual run (the scheduler yields
while ``coordinator.busy()``). Each iteration's benchmark adds the read-before/after
fingerprint integrity guarantee (see ``runner``). A crash mid-race is recovered by
``reconcile_interrupted_challenges`` on startup (baseline restored, best-effort).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import coordinator
from .config_store import get_config
from .database import session_scope
from .logging_config import get_logger
from .models import ChallengerRace, ChallengerRaceStatus
from .profile_test import _apply_all
from .providers import get_provider
from .runner import create_run, execute_run
from .settings_profile import fingerprint, normalize, plan_apply

log = get_logger("challenger")

# One race at a time. Module state coordinates with the driver thread and carries the
# cooperative cancel flag (checked at the top of each step).
_state: dict = {"active": False, "id": None, "thread": None, "cancel": False}


def active() -> bool:
    return bool(_state.get("active"))


def cancel() -> bool:
    """Request the running race stop after its current iteration. Returns False if none."""
    if not active():
        return False
    _state["cancel"] = True
    log.info("Challenger race %s: cancel requested", _state.get("id"))
    return True


def _min_iterations(session) -> int:
    return int((get_config(session).get("correlation", {}) or {}).get("min_iterations", 15) or 15)


def _incumbent_refresh_minutes(session) -> int:
    """How stale (minutes) the crowned incumbent's newest run may be before the race
    re-measures it. 0 disables the refresh."""
    cfg = get_config(session).get("challenger") or {}
    val = cfg.get("incumbent_refresh_minutes", 60)
    return int(val) if val is not None else 60


def _incumbent_stale(last_seen_iso: str | None, refresh_minutes: int, now: datetime) -> bool:
    """True when the crowned incumbent should be re-run before sampling a challenger.

    The bar challengers race against is the incumbent's Overall — but it's measured from
    its *historical* runs, which may be hours old and from a different network window.
    Re-running the incumbent when its newest run ages past ``refresh_minutes`` keeps the
    comparison contemporaneous (and tightens/validates the crown). Disabled when
    ``refresh_minutes`` ≤ 0; never fires when we can't tell the age (missing or
    unparseable ``last_seen``) so we don't churn the firewall on a degenerate field."""
    if refresh_minutes <= 0 or not last_seen_iso:
        return False
    try:
        ts = datetime.fromisoformat(last_seen_iso)
    except ValueError:
        return False
    if ts.tzinfo is not None:  # compare in naive UTC (last_seen is stored naive UTC)
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return (now - ts) > timedelta(minutes=refresh_minutes)


def _contender_stale_minutes(session) -> int:
    """How stale (minutes) a *confident* profile's newest run may be before the race
    re-measures it (ordered closest-to-winner first). 0 disables stale re-racing."""
    cfg = get_config(session).get("challenger") or {}
    val = cfg.get("contender_stale_minutes", 180)
    return int(val) if val is not None else 180


def _field(session) -> dict:
    """The current profile field (ranked, with the crowned best), via the shared
    ``compute_profiles`` so the race ranks exactly like the Settings-Impact UI — then
    **augmented** with *no-data* profiles: every stored profile with zero comparable
    current-methodology runs (invisible to ``compute_profiles``, which drops them). These
    are the "no data on the latest methodology" contenders the race exists to measure.

    Imported lazily to avoid a core↔api import cycle."""
    from .api.routes_settings import compute_profiles
    from .refresh import list_profiles

    field = compute_profiles(session, complete_only=True)
    comparable_fps = {p["fingerprint"] for p in field["profiles"]}
    no_data = [
        {
            "fingerprint": p["fingerprint"], "settings": p["settings"], "label": p["label"],
            "confident": False, "overall": None, "crown_spreads": {}, "last_seen": None,
            "no_data": True,
        }
        for p in list_profiles(session)
        if p["fingerprint"] not in comparable_fps
    ]
    field["profiles"] = list(field["profiles"]) + no_data
    return field


def rank_challengers(
    field: dict,
    already_eliminated: dict | set | None = None,
    now: datetime | None = None,
    stale_minutes: int = 0,
) -> tuple:
    """Pure ranking step over an (augmented) ``compute_profiles`` field. Returns
    ``(best_fingerprint, bar, leader, contenders, newly_eliminated)`` — the next profile
    to sample plus the eliminations. Contenders span, in priority order:

    1. **no-data** profiles (``no_data``) — zero current-methodology data; always raced
       (can't be eliminated until they have data), sampled first;
    2. **under-minimum** comparable profiles whose *optimistic* Overall ≥ ``bar`` (the
       confident best's Overall, or None in bootstrap) — highest optimistic first;
       eliminated when optimistic < bar or a required crown metric is missing;
    3. **stale confident** profiles (not the crowned best) whose newest run is older than
       ``stale_minutes`` — re-measured to verify they still hold, **ordered by closeness
       to the winner** (smallest |Overall − bar| first). Never eliminated.

    Bootstrap: with no confident best, ``bar`` is None → no-data + under-min all race.
    Factored out of the driver so the selection logic is unit-testable."""
    from .api.routes_settings import CROWN_METRICS, CROWN_REQUIRED, optimistic_overall

    already = already_eliminated or {}
    profiles = {p["fingerprint"]: p for p in field["profiles"]}
    best_fp = field.get("best_fingerprint")
    bar = profiles[best_fp]["overall"] if best_fp else None
    # The crown metric set comes from compute_profiles (the methodology's overall spec), so
    # the race's optimistic estimate corners over exactly the metrics the persisted Overall
    # does — it can't drift from methodology Overall logic.
    crown_metrics = field.get("overall_metrics") or CROWN_METRICS
    crown_required = field.get("overall_required") or CROWN_REQUIRED
    newly: dict[str, dict] = {}
    scored: list[tuple[tuple, dict, float | None]] = []  # (priority_key, profile, value)
    for fp, p in profiles.items():
        if fp in already:
            continue
        if p.get("no_data"):
            scored.append(((0, 0.0), p, None))  # highest priority — measure the unknowns
        elif not p["confident"]:
            opt = optimistic_overall(p.get("crown_spreads") or {}, crown_metrics, crown_required)
            if opt is None:
                newly[fp] = {"label": p["label"], "reason": "incomplete corner coverage"}
            elif bar is not None and opt < bar:
                newly[fp] = {"label": p["label"], "reason": f"best-case Overall {opt} < best {bar}"}
            else:
                scored.append(((1, -opt), p, opt))  # higher optimistic first
        elif fp != best_fp and stale_minutes and now is not None and _incumbent_stale(
            p.get("last_seen"), stale_minutes, now
        ):
            closeness = abs((p.get("overall") or 0.0) - (bar if bar is not None else 0.0))
            scored.append(((2, closeness), p, p.get("overall")))  # closest-to-winner first
    scored.sort(key=lambda t: t[0])
    contenders = [(p, v) for _, p, v in scored]
    leader = contenders[0][0] if contenders else None
    return best_fp, bar, leader, contenders, newly


def start(time_budget_s: int, auto_promote: bool = False) -> int:
    """Launch a challenger race. Returns the ``ChallengerRace`` id.

    Raises ``RuntimeError`` if a race is already running. The baseline is snapshotted
    inside the driver (under the lock) so it reflects the true pre-race state.
    """
    if active():
        raise RuntimeError("A challenger race is already running.")
    time_budget_s = max(int(time_budget_s), 30)
    with session_scope() as session:
        race = ChallengerRace(
            status=ChallengerRaceStatus.PENDING,
            time_budget_s=time_budget_s,
            auto_promote=bool(auto_promote),
            eliminated=[],
        )
        session.add(race)
        session.flush()
        race_id = race.id

    _state.update({"active": True, "id": race_id, "cancel": False})
    thread = threading.Thread(target=_drive, args=(race_id,), name="pathbrain-challenger", daemon=True)
    _state["thread"] = thread
    thread.start()
    log.info(
        "Challenger race %s started: %ss budget, auto_promote=%s", race_id, time_budget_s, auto_promote
    )
    return race_id


def _apply_profile(provider, target_settings: list[dict], target_fp: str) -> None:
    """Apply a stored profile and read it back to confirm we reached it."""
    changes, _warnings = plan_apply(target_settings, provider.discover())
    _apply_all(provider, changes)
    reached = fingerprint(normalize(provider.discover()))
    if reached != target_fp:
        raise RuntimeError(f"Could not reach challenger profile (got {reached}, wanted {target_fp}).")


def _drive(race_id: int) -> None:  # noqa: C901 — linear session lifecycle, kept in one place
    provider = get_provider()
    final_status = ChallengerRaceStatus.COMPLETE
    err: str | None = None
    winner_fp: str | None = None
    promoted = False
    baseline: list[dict] = []
    winner_settings: list[dict] | None = None
    try:
        # Hold the coordination lock for the whole session (apply → benchmark → …).
        with coordinator.hold(f"challenger#{race_id}"):
            baseline = normalize(provider.discover())
            with session_scope() as session:
                race = session.get(ChallengerRace, race_id)
                race.status = ChallengerRaceStatus.RUNNING
                race.started_at = datetime.now(timezone.utc)
                race.baseline = baseline
                budget_s = race.time_budget_s
                auto_promote = race.auto_promote
                min_iters = _min_iterations(session)
                refresh_min = _incumbent_refresh_minutes(session)
                stale_min = _contender_stale_minutes(session)

            deadline = time.monotonic() + budget_s
            applied_fp: str | None = None  # baseline is live to start
            eliminated: dict[str, dict] = {}  # fingerprint -> {label, reason}
            iterations_run = 0
            incumbent_refreshes = 0
            initial_confident: set[str] = set()
            seeded_initial = False

            while time.monotonic() < deadline and not _state.get("cancel"):
                with session_scope() as session:
                    field = _field(session)
                profiles = {p["fingerprint"]: p for p in field["profiles"]}
                if not seeded_initial:
                    initial_confident = {fp for fp, p in profiles.items() if p["confident"]}
                    seeded_initial = True

                now = datetime.now(timezone.utc).replace(tzinfo=None)
                best_fp, _bar, leader, _contenders, newly_elim = rank_challengers(
                    field, eliminated, now=now, stale_minutes=stale_min
                )

                # A challenger that became the crowned best (wasn't confident at start)
                # is a confirmed winner; the bar simply rises and the field races on.
                if best_fp and best_fp not in initial_confident and profiles[best_fp]["confident"]:
                    winner_fp = best_fp
                    winner_settings = profiles[best_fp].get("settings")

                eliminated.update(newly_elim)
                if leader is None:
                    log.info("Challenger race %s: no challenger can still beat the best", race_id)
                    break

                # Keep the bar honest: if the crowned incumbent's newest run is stale,
                # re-measure IT first so challengers race a *contemporaneous* bar (removes
                # time-of-day drift) and the crown's own band stays tight + validated.
                # The fresh run updates its last_seen, so next loop it won't be stale.
                incumbent = profiles.get(best_fp) if best_fp else None
                if incumbent is not None and _incumbent_stale(
                    incumbent.get("last_seen"), refresh_min, now
                ):
                    if best_fp != applied_fp:
                        _apply_profile(provider, incumbent["settings"], best_fp)
                        applied_fp = best_fp
                    run_id = create_run(
                        label=f"race · incumbent {incumbent['label']}",
                        notes=f"Challenger race #{race_id}: refresh stale incumbent {best_fp}",
                        iterations=1,
                    )
                    execute_run(run_id)
                    iterations_run += 1
                    incumbent_refreshes += 1
                    log.info(
                        "Challenger race %s: refreshed stale incumbent %s (bar now contemporaneous)",
                        race_id, best_fp,
                    )
                    with session_scope() as session:
                        race = session.get(ChallengerRace, race_id)
                        race.iterations_run = iterations_run
                        race.incumbent_refreshes = incumbent_refreshes
                    continue  # re-rank against the fresh bar before touching a challenger

                leader_fp = leader["fingerprint"]

                # Apply the leader only when it isn't already the live profile.
                if leader_fp != applied_fp:
                    _apply_profile(provider, leader["settings"], leader_fp)
                    applied_fp = leader_fp

                run_id = create_run(
                    label=f"race · {leader['label']}",
                    notes=f"Challenger race #{race_id}: one iteration of {leader_fp}",
                    iterations=1,
                )
                execute_run(run_id)  # blocking; its own read-before/after integrity applies
                iterations_run += 1

                with session_scope() as session:
                    race = session.get(ChallengerRace, race_id)
                    race.iterations_run = iterations_run
                    race.incumbent_refreshes = incumbent_refreshes
                    race.leader_fingerprint = leader_fp
                    race.leader_label = leader["label"]
                    race.eliminated = list(
                        {"fingerprint": fp, **info} for fp, info in eliminated.items()
                    )

            if _state.get("cancel"):
                final_status = ChallengerRaceStatus.CANCELLED

            # Finalize the firewall: promote the winner, or restore the baseline.
            try:
                if auto_promote and winner_fp and winner_settings is not None:
                    _apply_profile(provider, winner_settings, winner_fp)
                    promoted = True
                    log.info("Challenger race %s: auto-promoted winner %s", race_id, winner_fp)
                else:
                    restore, _ = plan_apply(baseline, provider.discover())
                    _apply_all(provider, restore)
                    log.info("Challenger race %s: restored baseline", race_id)
            except Exception:  # noqa: BLE001 — never raise out of cleanup
                log.exception("Challenger race %s: final firewall step failed", race_id)
    except Exception as exc:  # noqa: BLE001 — record + (best-effort) restore, never crash the thread
        log.exception("Challenger race %s failed", race_id)
        final_status = ChallengerRaceStatus.FAILED
        err = f"{type(exc).__name__}: {exc}"
        try:
            if baseline:
                restore, _ = plan_apply(baseline, get_provider().discover())
                _apply_all(get_provider(), restore)
        except Exception:  # noqa: BLE001
            log.exception("Challenger race %s: restore after failure failed", race_id)
    finally:
        with session_scope() as session:
            race = session.get(ChallengerRace, race_id)
            if race is not None:
                race.status = final_status
                race.error = err
                race.winner_fingerprint = winner_fp
                race.promoted = promoted
                race.finished_at = datetime.now(timezone.utc)
        _state.update({"active": False, "id": None, "cancel": False})
        log.info("Challenger race %s finished: %s", race_id, final_status.value)


def _serialize(race: ChallengerRace) -> dict:
    return {
        "id": race.id,
        "status": race.status.value if hasattr(race.status, "value") else str(race.status),
        "time_budget_s": race.time_budget_s,
        "auto_promote": race.auto_promote,
        "iterations_run": race.iterations_run or 0,
        "incumbent_refreshes": race.incumbent_refreshes or 0,
        "leader_fingerprint": race.leader_fingerprint,
        "leader_label": race.leader_label,
        "winner_fingerprint": race.winner_fingerprint,
        "promoted": race.promoted,
        "eliminated": race.eliminated or [],
        "error": race.error,
        "created_at": race.created_at.isoformat() if race.created_at else None,
        "started_at": race.started_at.isoformat() if race.started_at else None,
        "finished_at": race.finished_at.isoformat() if race.finished_at else None,
        # Best-effort label of whatever currently holds the lock (for a queued race).
        "lock_owner": coordinator.owner(),
    }


def current() -> dict | None:
    """The most recent challenger race (for status polling), or None."""
    with session_scope() as session:
        race = session.scalars(select(ChallengerRace).order_by(ChallengerRace.id.desc())).first()
        return _serialize(race) if race else None


def reconcile_interrupted_challenges() -> int:
    """Restore the baseline for any race left RUNNING by a previous process.

    Called once at startup, like ``profile_test.reconcile_interrupted_profile_tests``.
    The driving thread is gone, so the firewall may be stranded on a challenger profile
    — set it back to the snapshotted baseline.
    """
    provider = None
    restored = 0
    with session_scope() as session:
        races = session.scalars(
            select(ChallengerRace).where(
                ChallengerRace.status.in_(
                    [ChallengerRaceStatus.RUNNING, ChallengerRaceStatus.PENDING]
                )
            )
        ).all()
        for race in races:
            baseline = race.baseline or []
            if baseline:
                try:
                    provider = provider or get_provider()
                    changes, _ = plan_apply(baseline, provider.discover())
                    _apply_all(provider, changes)
                except Exception:  # noqa: BLE001
                    log.exception("Challenger race %s: restore on reconcile failed", race.id)
            race.status = ChallengerRaceStatus.FAILED
            race.error = "Interrupted — service restarted mid-race; baseline restored (best-effort)."
            race.finished_at = datetime.now(timezone.utc)
            restored += 1
    if restored:
        log.warning("Reconciled %s interrupted challenger race(s); baseline restored", restored)
    return restored


__all__ = ["start", "active", "cancel", "current", "reconcile_interrupted_challenges"]
