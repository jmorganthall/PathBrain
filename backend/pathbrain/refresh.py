"""Profile refresh: re-run every stored profile for a chosen number of iterations.

The batch sibling of ``profile_test`` ("Test to minimum"). Publishing a methodology
that adds a crown metric quarantines historical runs whose raw can't supply it
(``methodology.comparability`` → ``incomparable``); those profiles then have no
comparable data under the current methodology. This session gives them fresh data:

1. Snapshot the live firewall settings (the baseline to restore).
2. For each stored profile: apply it for real, read it back to confirm it was reached,
   and run one benchmark with the **caller-chosen** number of iterations (the caller
   decides how much fresh data to collect per profile — not auto-forced to the minimum).
3. **Always** restore the pre-refresh baseline at the end (and on crash-restart, via
   ``reconcile_interrupted_refreshes``).

Like ``profile_test``/``challenger`` it runs in its own thread and holds the
coordination lock for the whole session, so it never overlaps a sweep, an experiment,
or a monitoring/manual run. Each benchmark adds the read-before/after integrity
guarantee (see ``runner``). One profile failing to apply doesn't abort the batch — it's
logged and the refresh moves on.
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from statistics import median

from sqlalchemy import func, select

from . import coordinator
from .database import session_scope
from .logging_config import get_logger
from .session_runtime import describe_failure
from .models import Methodology, ProfileRefresh, ProfileRefreshStatus, Run, RunStatus, Score
from .profile_names import names_for
from .profile_test import _apply_all
from .providers import get_provider
from .runner import MAX_ITERATIONS, create_run, execute_run
from .settings_profile import fingerprint, normalize, plan_apply, summarize

log = get_logger("refresh")

# One refresh at a time. Module state coordinates with the driver thread, carries the
# work-list (settings aren't all stored on the row), and the cooperative cancel flag.
_state: dict = {"active": False, "id": None, "thread": None, "cancel": False, "plan": None}


def active() -> bool:
    return bool(_state.get("active"))


def cancel() -> bool:
    """Request the running refresh stop after the current profile. False if none."""
    if not active():
        return False
    _state["cancel"] = True
    log.info("Profile refresh %s: cancel requested", _state.get("id"))
    return True


# ── The stored-profile list, cached incrementally ────────────────────────────
# `list_profiles` used to decode every completed run's `settings` blob on every call —
# 120k JSON documents to keep ~150 of them (measured ~3s, pure Python, GIL held). That was
# tolerable while it ran only from a refresh dialog and the brief no-crown window after a
# publish; once the seed applied on every Settings-Impact load and every ladder session it
# was the difference between a page and a timeout ("couldn't reach the server"). The
# newest settings per fingerprint only ever change when a NEW run lands (a fingerprint IS
# its normalized settings), so the cache is keyed on the newest run id and topped up with
# just the rows that arrived since — one indexed range query, not a table scan. The one
# event that rewrites fingerprints in place (refingerprint) goes through
# `invalidate_seed_cache`, the same hook the field memo uses. Deletions are caught by the
# row count: if the count is not what the top-up implies, the list is rebuilt whole.
_PROFILES_LOCK = threading.Lock()
_PROFILES_CACHE: dict = {"max_id": 0, "max_fp": None, "count": 0, "latest": {}}
_PRIOR_LOCK = threading.Lock()
_PRIOR_CACHE: dict = {"key": None, "value": None}


def invalidate_seed_cache() -> None:
    """Drop the cached profile list and the cached prior-version field. Called from
    `routes_settings.invalidate_profiles_cache` (refingerprint / wholesale re-grade)."""
    with _PROFILES_LOCK:
        _PROFILES_CACHE.update({"max_id": 0, "max_fp": None, "count": 0, "latest": {}})
    with _PRIOR_LOCK:
        _PRIOR_CACHE.update({"key": None, "value": None})


def _latest_settings_by_fingerprint(session) -> dict[str, list]:
    """``{fingerprint: newest settings}`` over every completed run, from the incremental
    cache. Ascending id order within a top-up, so the last seen per fingerprint is the newest
    — the same answer the full descending scan gave, without the scan."""
    base = (
        select(Run.id, Run.settings_fingerprint, Run.settings)
        .where(
            Run.status == RunStatus.COMPLETE,
            Run.settings_fingerprint.is_not(None),
            Run.settings.is_not(None),
        )
    )
    max_id, count = session.execute(
        select(func.max(Run.id), func.count(Run.id)).where(
            Run.status == RunStatus.COMPLETE,
            Run.settings_fingerprint.is_not(None),
            Run.settings.is_not(None),
        )
    ).one()
    max_id, count = int(max_id or 0), int(count or 0)
    with _PROFILES_LOCK:
        cached_max, cached_count = _PROFILES_CACHE["max_id"], _PROFILES_CACHE["count"]
        # The top-up is only sound if what the cache saw is still what the table holds. Two
        # things break that: rows vanished (a cleanup), or the cache was filled from a
        # session that later ROLLED BACK — SQLite then re-issues the same ids to the next
        # rows, so max id and count can both match while the rows differ. The count catches
        # the first; the fingerprint sitting at the cached max id catches the second (a
        # reissued id lands there first) — one primary-key lookup, checked on the fast path
        # too. Either → rebuild from scratch, the rare path priced only when it happens.
        probe = (
            session.execute(select(Run.settings_fingerprint).where(Run.id == cached_max)).scalar()
            if cached_max
            else None
        )
        intact = bool(cached_max) and probe == _PROFILES_CACHE["max_fp"]
        if intact and max_id == cached_max and count == cached_count:
            return dict(_PROFILES_CACHE["latest"])
        latest = dict(_PROFILES_CACHE["latest"])
        rows = session.execute(base.where(Run.id > cached_max).order_by(Run.id.asc())).all()
        if cached_max and (not intact or count != cached_count + len(rows)):
            latest = {}
            rows = session.execute(base.order_by(Run.id.asc())).all()
        max_fp = _PROFILES_CACHE["max_fp"]
        for rid, fp, settings in rows:
            latest[fp] = settings
            if rid == max_id:
                max_fp = fp
        _PROFILES_CACHE.update({"max_id": max_id, "max_fp": max_fp, "count": count, "latest": latest})
        return dict(latest)


def list_profiles(session) -> list[dict]:
    """Every distinct stored profile (newest settings per fingerprint), as
    ``[{fingerprint, settings, label, name}]`` — the candidates a refresh re-runs.

    ``name`` is the profile's call sign, resolved in one query for the whole list: a run
    labelled *"refresh · Speedy Sloth"* says which profile it measured, while one labelled
    with the settings summary makes the reader decode the settings to find out.
    """
    latest = _latest_settings_by_fingerprint(session)
    names = names_for(session, list(latest))
    return [
        {
            "fingerprint": fp,
            "settings": settings,
            "label": summarize(settings),
            "name": names.get(fp),
        }
        for fp, settings in latest.items()
    ]


# ── Winner-first prioritization ──────────────────────────────────────────────
# A refresh can re-run a *chosen top N* profiles, ordered by how well they scored under a
# prior methodology — so after publishing a new methodology (which quarantines history that
# can't supply a new crown metric), the profiles that were *winning* get fresh, comparable
# data first, instead of blindly re-running everything in arbitrary order.


def _prior_methodology_version(session) -> str | None:
    """The methodology version a winner-first refresh ranks by: the most recently recorded
    methodology that isn't the one now current — i.e. the rubric profiles were last judged
    under before the current publish. ``None`` on a fresh instance with only one methodology."""
    versions = session.scalars(
        select(Methodology.version)
        .where(Methodology.is_current.is_(False))
        .order_by(Methodology.created_at.desc())
    ).all()
    return versions[0] if versions else None


def _overall_by_profile(session, version: str) -> dict[str, float]:
    """Median persisted Overall (``Score.axis_scores['overall']``) per profile fingerprint under
    a methodology version — the winner-first ranking signal. Reads the same first-class Overall
    the crown ranks on, so 'top profiles' here means the same thing Settings-Impact showed."""
    rows = session.execute(
        select(Run.settings_fingerprint, Score.axis_scores)
        .join(Score, Score.run_id == Run.id)
        .where(
            Run.status == RunStatus.COMPLETE,
            Run.settings_fingerprint.is_not(None),
            Score.methodology_version == version,
        )
    ).all()
    buckets: dict[str, list[float]] = {}
    for fp, axis_scores in rows:
        ov = (axis_scores or {}).get("overall")
        if ov is not None:
            buckets.setdefault(fp, []).append(float(ov))
    return {fp: median(vals) for fp, vals in buckets.items() if vals}


def prior_field(session, min_iterations: int | None = None) -> dict | None:
    """The previous methodology's verdict, as a seed for the current one.

    Right after a publish (a new crown metric, a site change) nothing has a comparable run
    under the current version: the pooled crown is empty, every profile is "untested", and
    the ring, the race and the heirs card would order the field by nothing. The prior
    version's standings are the best available guess at where to look first — a prior for
    **ordering who gets measured**, never a score under the current version.

    Returns ``{version, overall: {fp: median}, best_fingerprint}`` or ``None`` when there is
    no prior version or it scored nothing. ``best_fingerprint`` is the top profile with at
    least ``min_iterations`` under that version (falling back to the top by median)."""
    # Walk back through the non-current versions, newest first, to the first that scored
    # anything: a re-anchor fork published and abandoned between two real rubrics holds no
    # scores, and stopping at it would seed nothing when the version before it could.
    versions = _prior_methodology_versions(session)
    if not versions:
        return None
    # Memoized on a cheap stamp: a prior version's scores are frozen (nothing scores under a
    # non-current version except an explicit re-grade, which moves `computed_at`), so the
    # ~120k-row decode this used to do on every read happens once per change instead —
    # it is read on every Settings-Impact load and every ladder session since the seed
    # stopped being gated on the crown.
    stamp = session.execute(
        select(func.count(Score.id), func.max(Score.id), func.max(Score.computed_at)).where(
            Score.methodology_version.in_(versions[:3])
        )
    ).one()
    key = (tuple(versions[:3]), tuple(stamp), int(min_iterations or 0))
    with _PRIOR_LOCK:
        if _PRIOR_CACHE["key"] == key:
            return copy.deepcopy(_PRIOR_CACHE["value"])
    seeded = None
    for version in versions:
        seeded = _prior_field_under(session, version, min_iterations)
        if seeded:
            break
    with _PRIOR_LOCK:
        _PRIOR_CACHE.update({"key": key, "value": copy.deepcopy(seeded)})
    return seeded


def _prior_methodology_versions(session) -> list[str]:
    return list(session.scalars(
        select(Methodology.version)
        .where(Methodology.is_current.is_(False))
        .order_by(Methodology.created_at.desc())
    ).all())


def _prior_field_under(session, version: str, min_iterations: int | None) -> dict | None:
    rows = session.execute(
        select(Run.settings_fingerprint, Run.iterations, Score.axis_scores)
        .join(Score, Score.run_id == Run.id)
        .where(
            Run.status == RunStatus.COMPLETE,
            Run.settings_fingerprint.is_not(None),
            Score.methodology_version == version,
            Score.comparability != "incomparable",
        )
    ).all()
    buckets: dict[str, list[float]] = {}
    iterations: dict[str, int] = {}
    for fp, iters, axis_scores in rows:
        ov = (axis_scores or {}).get("overall")
        if ov is None:
            continue
        buckets.setdefault(fp, []).append(float(ov))
        iterations[fp] = iterations.get(fp, 0) + int(iters or 1)
    overall = {fp: round(median(vals), 2) for fp, vals in buckets.items() if vals}
    if not overall:
        return None
    need = int(min_iterations or 0)
    confident = [fp for fp in overall if iterations.get(fp, 0) >= need] or list(overall)
    best = max(confident, key=lambda fp: overall[fp])
    return {"version": version, "overall": overall, "best_fingerprint": best}


def seed_field_from_prior(session, field: dict, min_iterations: int | None = None) -> dict:
    """The field with the prior version's standings folded in, for every stored profile the
    current version has no data on yet.

    Returns a **copy** (``compute_profiles`` memoizes its result; the seed must never leak
    into the cache). Every profile carries ``prior_overall``; stored profiles the current
    version has not scored are added as ``no_data`` entries (with their settings, so the
    ladder and the race can apply them); ``best_fingerprint`` becomes the prior crown **only
    when the current field has none** — a current crown is a current measurement and is
    never displaced by a seed; ``seeded_from`` names the version.

    The seed applies whether or not the current version has a crown. It used to switch off
    the moment one appeared — and right after a publish the one profile the firewall sits
    on reaches confidence within hours from monitoring alone, at which point the field
    read as one crowned profile and nothing else: the heirs card emptied, the race had
    nobody to run, and the ladder had nobody to challenge with, while the prior version's
    two hundred ranked profiles sat unmeasured (the *"no evidence dueling pulls profiles
    from the prior methodology"* report). With a crown present the no-data entries are the
    profiles the prior version actually scored — its standings are the seed, and a profile
    it never ranked has no standing to seed with (the race's own no-data pass still covers
    those). With no crown at all every stored profile is seeded, as before, so a field with
    nothing measured still has a full queue."""
    out = {**field, "profiles": [dict(p) for p in field.get("profiles", [])]}
    prior = prior_field(session, min_iterations)
    if not prior:
        return out
    has_crown = bool(out.get("best_fingerprint"))
    known = {p["fingerprint"] for p in out["profiles"]}
    for p in out["profiles"]:
        p["prior_overall"] = prior["overall"].get(p["fingerprint"])
    added = 0
    for p in list_profiles(session):
        if p["fingerprint"] in known:
            continue
        prior_overall = prior["overall"].get(p["fingerprint"])
        if has_crown and prior_overall is None:
            continue
        out["profiles"].append({
            "fingerprint": p["fingerprint"], "settings": p["settings"], "label": p["label"],
            "name": p.get("name"), "confident": False, "overall": None, "optimistic": None,
            "crown_spreads": {}, "last_seen": None, "no_data": True, "iterations": 0,
            "prior_overall": prior_overall,
        })
        added += 1
    if not has_crown:
        fps = {p["fingerprint"] for p in out["profiles"]}
        if prior["best_fingerprint"] in fps:
            out["best_fingerprint"] = prior["best_fingerprint"]
    elif not added:
        return out  # every profile the prior ranked already has current data: nothing to seed
    out["seeded_from"] = prior["version"]
    out["seeded_profiles"] = sum(1 for p in out["profiles"] if p.get("prior_overall") is not None)
    # The prior's own crown and whether the current field has one — so the page can say
    # "the prior crown stands in" vs "the current crown defends, the prior ranks the rest".
    out["seeded_best_fingerprint"] = prior["best_fingerprint"]
    out["seeded_has_current_crown"] = has_crown
    return out


def ranked_profiles(session, rank_version: str | None) -> list[dict]:
    """Stored profiles ordered best-first by their median persisted Overall under
    ``rank_version`` (winner-first). Profiles with no comparable score under that version sort
    last — they still get re-run if within the chosen top-N, just after the known performers.
    Falls back to the raw list order when there's no ranking version or no scored data for it."""
    profiles = list_profiles(session)
    if not rank_version:
        return profiles
    overall = _overall_by_profile(session, rank_version)
    if not overall:
        return profiles
    return sorted(
        profiles,
        key=lambda p: overall.get(p["fingerprint"], float("-inf")),
        reverse=True,
    )


def _select(
    session, top: int | None, rank_by: str | None, fingerprints: list[str] | None = None,
) -> tuple[list[dict], str | None]:
    """Resolve the profile work-list + the version it was ranked by. Plain (unranked) list when
    neither ``top`` nor ``rank_by`` is given; otherwise ranked winner-first (``rank_by`` or the
    auto-detected prior methodology) and capped to ``top`` when a positive cap is given.

    An explicit ``fingerprints`` list is a third scope — *these profiles, in this order* —
    for a caller that has already decided what needs re-measuring (the Settings-Impact
    "Re-run outliers" action). It is exact: unknown fingerprints are dropped rather than
    guessed at, and ``top``/``rank_by`` are ignored, since the caller's list IS the ranking."""
    if fingerprints:
        wanted = [fp for fp in fingerprints if fp]
        by_fp = {p["fingerprint"]: p for p in list_profiles(session)}
        return [by_fp[fp] for fp in wanted if fp in by_fp], None
    rank_version = rank_by or (_prior_methodology_version(session) if top else None)
    profiles = ranked_profiles(session, rank_version) if (top or rank_by) else list_profiles(session)
    if top is not None and top > 0:
        profiles = profiles[:top]
    return profiles, rank_version


# Rough fixed overhead per profile (apply + read-back verify + final restore), added to
# the benchmark time so the estimate isn't optimistic. Seconds.
_PER_PROFILE_OVERHEAD_S = 3.0


def _median_iteration_ms(session) -> float | None:
    """The cost of one benchmark iteration (ms) — the same recent-first, iteration-weighted
    median every other ETA uses (``iteration_cost``), so a preview and the countdown that
    follows it can't disagree. ``None`` when no run has recorded a timing yet."""
    from .iteration_cost import estimate_ms

    return estimate_ms(session)


def preview(
    session, iterations: int, top: int | None = None, rank_by: str | None = None,
    fingerprints: list[str] | None = None,
) -> dict:
    """What a refresh would do + how long it'd take: profile count, total iterations, and
    an estimated duration (median per-iteration time × total iterations + per-profile
    apply/restore overhead). ``estimated_seconds`` is None when there's no timing history
    to base it on. With ``top`` set, previews a winner-first subset (ranked by ``rank_by`` or
    the auto-detected prior methodology), so the estimate reflects the capped batch. With
    ``fingerprints`` set, previews exactly those profiles (see ``_select``)."""
    iters = max(1, min(MAX_ITERATIONS, int(iterations)))
    profiles, rank_version = _select(session, top, rank_by, fingerprints)
    n_profiles = len(profiles)
    per_ms = _median_iteration_ms(session)
    total_iterations = n_profiles * iters
    estimated = None
    if per_ms is not None:
        estimated = round(total_iterations * (per_ms / 1000.0) + n_profiles * _PER_PROFILE_OVERHEAD_S)
    return {
        "profiles": n_profiles,
        "iterations": iters,
        "total_iterations": total_iterations,
        "per_iteration_ms": round(per_ms, 1) if per_ms is not None else None,
        "estimated_seconds": estimated,
        # Winner-first context (null when running the full, unranked batch).
        "top": top if (top and top > 0 and not fingerprints) else None,
        "ranked_by": rank_version,
        # How many of an explicit list were actually found (null for the other scopes).
        "fingerprints": n_profiles if fingerprints else None,
    }


def start(
    iterations: int, top: int | None = None, rank_by: str | None = None,
    fingerprints: list[str] | None = None,
) -> int:
    """Launch a profile refresh that runs ``iterations`` benchmarks on stored profiles.
    Returns the ``ProfileRefresh`` id.

    With ``top`` set, only the top-N profiles are re-run, ordered **winner-first** by their
    median persisted Overall under ``rank_by`` (or, when omitted, the prior methodology) — so
    after a methodology publish, the profiles that were performing best get fresh, comparable
    data first instead of an arbitrary sweep of everything. Without ``top``/``rank_by`` it
    re-runs every stored profile (the original behavior).

    Raises ``RuntimeError`` if one is already running, or if there are no stored
    profiles. ``iterations`` is clamped to ``1..MAX_ITERATIONS``. The baseline is
    snapshotted inside the driver (under the lock) so it reflects the true pre-refresh
    state."""
    if active():
        raise RuntimeError("A profile refresh is already running.")
    iters = max(1, min(MAX_ITERATIONS, int(iterations)))
    with session_scope() as session:
        profiles, rank_version = _select(session, top, rank_by, fingerprints)
        if not profiles:
            raise RuntimeError(
                "None of the requested profiles is stored." if fingerprints
                else "No stored profiles to refresh."
            )
        plan = [{**p, "needed": iters} for p in profiles]
        row = ProfileRefresh(status=ProfileRefreshStatus.PENDING, profiles_total=len(plan))
        session.add(row)
        session.flush()
        rid = row.id

    _state.update({"active": True, "id": rid, "cancel": False, "plan": plan})
    thread = threading.Thread(target=_drive, args=(rid,), name="pathbrain-refresh", daemon=True)
    _state["thread"] = thread
    thread.start()
    log.info(
        "Profile refresh %s started: %s profile(s) × %s iteration(s)%s",
        rid, len(plan), iters,
        " (explicit profile list)" if fingerprints
        else f" (winner-first top {top} by {rank_version})" if (top and top > 0) else "",
    )
    return rid


def _apply_profile(provider, target_settings: list[dict], target_fp: str) -> None:
    """Apply a stored profile and read it back to confirm we reached it."""
    changes, _warnings = plan_apply(target_settings, provider.discover())
    _apply_all(provider, changes)
    reached = fingerprint(normalize(provider.discover()))
    if reached != target_fp:
        raise RuntimeError(f"Could not reach profile (got {reached}, wanted {target_fp}).")


def _drive(refresh_id: int) -> None:
    provider = get_provider()
    plan = _state.get("plan") or []
    final_status = ProfileRefreshStatus.COMPLETE
    err: str | None = None
    baseline: list[dict] = []
    failures: list[str] = []
    try:
        # Hold the coordination lock for the whole session (apply → benchmark → … →
        # restore). Queues behind any in-progress firewall/benchmark session.
        with coordinator.hold(f"refresh#{refresh_id}"):
            baseline = normalize(provider.discover())
            with session_scope() as session:
                row = session.get(ProfileRefresh, refresh_id)
                row.status = ProfileRefreshStatus.RUNNING
                row.started_at = datetime.now(timezone.utc)
                row.baseline = baseline
            iterations_run = 0
            done = 0
            try:
                for item in plan:
                    if _state.get("cancel"):
                        final_status = ProfileRefreshStatus.CANCELLED
                        break
                    fp, settings = item["fingerprint"], item["settings"]
                    label, needed = item["label"], item["needed"]
                    with session_scope() as session:
                        row = session.get(ProfileRefresh, refresh_id)
                        row.current_fingerprint = fp
                        row.current_label = label
                    try:
                        _apply_profile(provider, settings, fp)
                        run_id = create_run(
                            label=f"refresh · {item.get('name') or label}",
                            notes=f"Profile refresh #{refresh_id}: {needed} fresh iteration(s) of {fp}",
                            iterations=needed,
                        )
                        execute_run(run_id)  # blocking; its own read-before/after integrity applies
                        iterations_run += needed
                    except Exception as exc:  # noqa: BLE001 — one bad profile shouldn't abort the batch
                        log.exception("Profile refresh %s: profile %s failed", refresh_id, fp)
                        failures.append(f"{label}: {type(exc).__name__}: {exc}")
                    done += 1
                    with session_scope() as session:
                        row = session.get(ProfileRefresh, refresh_id)
                        row.profiles_done = done
                        row.iterations_run = iterations_run
            finally:
                # Always restore the pre-refresh baseline.
                try:
                    restore, _ = plan_apply(baseline, provider.discover())
                    _apply_all(provider, restore)
                    log.info("Profile refresh %s: restored baseline", refresh_id)
                except Exception:  # noqa: BLE001 — never raise out of cleanup
                    log.exception("Profile refresh %s: baseline restore failed", refresh_id)
    except Exception as exc:  # noqa: BLE001 — record + (best-effort) restore, never crash the thread
        log.exception("Profile refresh %s failed", refresh_id)
        final_status = ProfileRefreshStatus.FAILED
        err = describe_failure(exc)
        try:
            if baseline:
                restore, _ = plan_apply(baseline, get_provider().discover())
                _apply_all(get_provider(), restore)
        except Exception:  # noqa: BLE001
            log.exception("Profile refresh %s: restore after failure failed", refresh_id)
    finally:
        if failures and err is None:
            err = f"{len(failures)} profile(s) could not be refreshed: " + "; ".join(failures)
        with session_scope() as session:
            row = session.get(ProfileRefresh, refresh_id)
            if row is not None:
                row.status = final_status
                row.error = err
                row.current_fingerprint = None
                row.current_label = None
                row.finished_at = datetime.now(timezone.utc)
        _state.update({"active": False, "id": None, "cancel": False, "plan": None})
        log.info("Profile refresh %s finished: %s", refresh_id, final_status.value)


def _serialize(row: ProfileRefresh) -> dict:
    return {
        "id": row.id,
        "status": row.status.value if hasattr(row.status, "value") else str(row.status),
        "profiles_total": row.profiles_total or 0,
        "profiles_done": row.profiles_done or 0,
        "iterations_run": row.iterations_run or 0,
        "current_fingerprint": row.current_fingerprint,
        "current_label": row.current_label,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        # Best-effort label of whatever currently holds the lock (for a queued refresh).
        "lock_owner": coordinator.owner(),
    }


def current() -> dict | None:
    """The most recent profile refresh (for status polling), or None."""
    with session_scope() as session:
        row = session.scalars(select(ProfileRefresh).order_by(ProfileRefresh.id.desc())).first()
        return _serialize(row) if row else None


def reconcile_interrupted_refreshes() -> int:
    """Restore the baseline for any refresh left RUNNING by a previous process.

    Called once at startup, like ``challenger.reconcile_interrupted_challenges``. The
    driving thread is gone, so the firewall may be stranded on a refreshed profile —
    set it back to the snapshotted baseline.
    """
    provider = None
    restored = 0
    with session_scope() as session:
        rows = session.scalars(
            select(ProfileRefresh).where(
                ProfileRefresh.status.in_(
                    [ProfileRefreshStatus.RUNNING, ProfileRefreshStatus.PENDING]
                )
            )
        ).all()
        for row in rows:
            baseline = row.baseline or []
            if baseline:
                try:
                    provider = provider or get_provider()
                    changes, _ = plan_apply(baseline, provider.discover())
                    _apply_all(provider, changes)
                except Exception:  # noqa: BLE001
                    log.exception("Profile refresh %s: restore on reconcile failed", row.id)
            row.status = ProfileRefreshStatus.FAILED
            row.error = "Interrupted — service restarted mid-refresh; baseline restored (best-effort)."
            row.finished_at = datetime.now(timezone.utc)
            restored += 1
    if restored:
        log.warning("Reconciled %s interrupted profile refresh(es); baseline restored", restored)
    return restored


__all__ = [
    "start", "active", "cancel", "current", "preview", "list_profiles",
    "reconcile_interrupted_refreshes",
]
