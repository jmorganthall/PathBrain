"""Interleaved head-to-head duel ladder — the adjudication engine.

The pooled Overall is an *observational* ranking: profiles measured at different times
under different weather, where a thin newcomer can never outweigh an incumbent's mass.
The duel is the *controlled trial*: strict A/B/A/B alternation through one window, so
each adjacent pair of one-iteration runs shares its weather by construction — the
confound vanishes and paired differences are tight, which is why a brand-new variant
can be adjudicated against a 3000-iteration crown in a single night.

Each matchup runs a **sequential stopping rule** so the window never burns all night on
a settled question (Wald's SPRT on the pair-win rate, H0 p=0.5 vs H1 p=`duel.p1`):

* one side's log-likelihood ratio crosses the upper boundary → that side **wins** —
  provided the median pair delta also clears `duel.min_margin` Overall points (a
  statistically real but practically meaningless edge is recorded as a draw);
* both sides sink below the lower boundary, or `duel.max_pairs` is reached → **draw**
  ("no difference worth chasing");
* verdicts never fire before `duel.min_pairs` pairs.

Ladder: the incumbent starts as the pooled crown; challengers queue in the heirs
priority order (reachability-filtered), skipping matchups decided within
`duel.rematch_days`. The winner stays on as the new incumbent; the next challenger
steps up until the window closes.

Two-ledger discipline: duel *runs* flow into the pooled record like any runs; duel
*verdicts* live beside it as the head-to-head ledger and NEVER enter the pooled score.
The engine also never writes a winner to the firewall — it always restores the
pre-duel baseline; acting on the verdict is the crown follower's job under the
``crown_follow.policy`` crowning policy (see ``crowning.py``).
"""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import coordinator
from .config_store import get_config
from .database import session_scope
from .logging_config import get_logger
from .models import Duel, DuelStatus, Score
from .providers import get_provider
from .runner import run_chunk, teardown_plugins
from .settings_profile import fingerprint, normalize, plan_apply

log = get_logger("duel")

_state: dict = {"active": False, "id": None, "cancel": False, "thread": None}

# Give up on a matchup after this many consecutive unusable pairs (failed runs /
# missing Overalls) — the environment isn't stable enough to adjudicate right now.
MAX_CONSECUTIVE_BAD_PAIRS = 3


def active() -> bool:
    return bool(_state.get("active"))


def cancel() -> bool:
    """Ask the running duel to stop after its current pair (baseline still restored)."""
    if not active():
        return False
    _state["cancel"] = True
    log.info("Duel %s: cancel requested", _state.get("id"))
    return True


# ── Sequential stopping rule (Wald SPRT on the pair-win rate) ─────────────────────────


class SprtState:
    """Two mirrored SPRTs — one per side — over the stream of pair outcomes.

    ``add_pair(challenger_won)`` updates both walks; ``decision(...)`` returns
    ``"challenger"`` / ``"incumbent"`` when a side's LLR crosses the upper boundary,
    ``"draw"`` when both walks have sunk below the lower boundary (mutual futility:
    the pair wins are hovering around 50/50), and ``None`` while undecided.
    """

    def __init__(self, p1: float, alpha: float) -> None:
        p1 = min(max(p1, 0.501), 0.999)
        alpha = min(max(alpha, 0.001), 0.2)
        self.win_step = math.log(p1 / 0.5)
        self.loss_step = math.log((1 - p1) / 0.5)
        # Symmetric error rates: A = ln((1-b)/a), B = ln(b/(1-a)) with a = b = alpha.
        self.upper = math.log((1 - alpha) / alpha)
        self.lower = -self.upper
        self.llr_challenger = 0.0
        self.llr_incumbent = 0.0
        self.pairs = 0
        self.wins_challenger = 0
        self.wins_incumbent = 0

    def add_pair(self, challenger_won: bool) -> None:
        self.pairs += 1
        if challenger_won:
            self.wins_challenger += 1
            self.llr_challenger += self.win_step
            self.llr_incumbent += self.loss_step
        else:
            self.wins_incumbent += 1
            self.llr_incumbent += self.win_step
            self.llr_challenger += self.loss_step

    def decision(self, min_pairs: int, max_pairs: int) -> str | None:
        if self.pairs < min_pairs:
            return None
        if self.llr_challenger >= self.upper:
            return "challenger"
        if self.llr_incumbent >= self.upper:
            return "incumbent"
        if self.llr_challenger <= self.lower and self.llr_incumbent <= self.lower:
            return "draw"
        if self.pairs >= max_pairs:
            return "draw"
        return None


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


# ── Engine ────────────────────────────────────────────────────────────────────────────


def _duel_config(session) -> dict:
    return get_config(session).get("duel", {}) or {}


def _set_stage(duel_id: int, stage: str) -> None:
    log.info("Duel %s: %s", duel_id, stage)
    try:
        with session_scope() as session:
            d = session.get(Duel, duel_id)
            if d is not None:
                d.stage = stage[:255]
    except Exception:  # noqa: BLE001 — a status write must never break the duel
        log.debug("Duel %s: could not persist stage %r", duel_id, stage, exc_info=True)


def start(duration_minutes: int | None = None, *, trigger: str = "manual") -> int:
    """Launch a duel-ladder session. Returns the ``Duel`` id.

    Raises ``RuntimeError`` if one is already running; ``ValueError`` for a bad duration.
    """
    if active():
        raise RuntimeError("A duel is already running.")
    with session_scope() as session:
        cfg = _duel_config(session)
        minutes = duration_minutes if duration_minutes else int(cfg.get("duration_minutes", 120) or 120)
        if minutes <= 0:
            raise ValueError("duration_minutes must be positive")
        d = Duel(
            status=DuelStatus.PENDING,
            duration_s=minutes * 60,
            trigger=trigger,
            matchups=[],
            run_ids=[],
            stage="Queued — waiting for any running benchmark to finish",
        )
        session.add(d)
        session.flush()
        duel_id = d.id

    _state.update({"active": True, "id": duel_id, "cancel": False})
    thread = threading.Thread(target=_drive, args=(duel_id,), name="pathbrain-duel", daemon=True)
    _state["thread"] = thread
    thread.start()
    log.info("Duel %s started (%s min, %s)", duel_id, minutes, trigger)
    return duel_id


def _run_overall(run_id: int, methodology_version: str) -> float | None:
    """The per-run Overall persisted at scoring time (None if unscored/incomparable)."""
    with session_scope() as session:
        score = session.scalars(
            select(Score).where(
                Score.run_id == run_id, Score.methodology_version == methodology_version
            )
        ).first()
        if score is None:
            return None
        val = (score.axis_scores or {}).get("overall")
        return float(val) if isinstance(val, (int, float)) else None


def _recently_decided(session, a_fp: str, b_fp: str, rematch_days: int) -> bool:
    """Was this matchup already adjudicated within the rematch cooldown?"""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=rematch_days)
    rows = session.scalars(
        select(Duel).where(Duel.finished_at.is_not(None)).order_by(Duel.id.desc()).limit(20)
    ).all()
    pair = {a_fp, b_fp}
    for row in rows:
        finished = row.finished_at
        if finished is not None and finished.tzinfo is not None:
            finished = finished.astimezone(timezone.utc).replace(tzinfo=None)
        if finished is not None and finished < cutoff:
            continue
        for m in row.matchups or []:
            if {m.get("incumbent"), m.get("challenger")} == pair:
                return True
    return False


def _drive(duel_id: int) -> None:
    from .api.routes_settings import _compute_heirs, compute_profiles
    from .challenger import _apply_all, _apply_profile
    from .methodology import ensure_current_methodology

    provider = get_provider()
    final_status = DuelStatus.COMPLETE
    err: str | None = None
    run_ids: list[int] = []
    iterations_run = 0
    try:
        with coordinator.hold(f"duel#{duel_id}"):
            _set_stage(duel_id, "Reading current firewall settings")
            baseline = normalize(provider.discover())
            with session_scope() as session:
                d = session.get(Duel, duel_id)
                d.status = DuelStatus.RUNNING
                d.started_at = datetime.now(timezone.utc)
                d.baseline = baseline
                duration_s = d.duration_s
                cfg = _duel_config(session)
                meth_version = ensure_current_methodology(session, get_config(session)).version
            p1 = float(cfg.get("p1", 0.70) or 0.70)
            alpha = float(cfg.get("alpha", 0.05) or 0.05)
            min_pairs = int(cfg.get("min_pairs", 10) or 10)
            max_pairs = int(cfg.get("max_pairs", 40) or 40)
            min_margin = float(cfg.get("min_margin", 1.0) or 0.0)
            rematch_days = int(cfg.get("rematch_days", 7) or 7)

            # Matchmaking: incumbent = pooled crown; challengers = the heirs priority order
            # (reachability-filtered by _compute_heirs), skipping pairs on rematch cooldown.
            _set_stage(duel_id, "Ranking the field for matchmaking")
            with session_scope() as session:
                field = compute_profiles(session)
                heirs = _compute_heirs(field, session, baseline)
            settings_by_fp = {p["fingerprint"]: p for p in field.get("profiles", [])}
            incumbent_fp = field.get("best_fingerprint")
            if incumbent_fp is None or incumbent_fp not in settings_by_fp:
                raise RuntimeError("No confident pooled crown to defend — nothing to duel.")
            queue = [
                h["fingerprint"]
                for h in (heirs.get("items") or [])
                if h.get("fingerprint") in settings_by_fp
            ]
            if not queue:
                raise RuntimeError("No eligible challengers (no reachable heirs to duel).")

            deadline = time.monotonic() + duration_s
            matchups: list[dict] = []

            while queue and time.monotonic() < deadline and not _state.get("cancel"):
                challenger_fp = queue.pop(0)
                with session_scope() as session:
                    if _recently_decided(session, incumbent_fp, challenger_fp, rematch_days):
                        log.info(
                            "Duel %s: %s vs %s decided within %sd — skipping",
                            duel_id, incumbent_fp, challenger_fp, rematch_days,
                        )
                        continue
                inc = settings_by_fp[incumbent_fp]
                cha = settings_by_fp[challenger_fp]
                sprt = SprtState(p1, alpha)
                deltas: list[float] = []
                bad_streak = 0
                verdict: str | None = None
                reason = ""

                while verdict is None and time.monotonic() < deadline and not _state.get("cancel"):
                    _set_stage(
                        duel_id,
                        f"Duel: {inc['label']} vs {cha['label']} — pair {sprt.pairs + 1} "
                        f"({sprt.wins_incumbent}-{sprt.wins_challenger})",
                    )
                    pair_overalls: list[float | None] = []
                    for side_fp, side in ((incumbent_fp, inc), (challenger_fp, cha)):
                        _apply_profile(provider, side["settings"], side_fp)
                        run_id, ok, completed = run_chunk(
                            label=f"duel · {side['label']}",
                            notes=f"Duel #{duel_id}: {inc['label']} vs {cha['label']}",
                            iterations=1,
                            teardown=False,  # keep Chromium warm across the whole ladder
                            job_group=f"duel-{duel_id}",
                        )
                        run_ids.append(run_id)
                        iterations_run += completed
                        pair_overalls.append(
                            _run_overall(run_id, meth_version) if ok else None
                        )
                    with session_scope() as session:
                        d = session.get(Duel, duel_id)
                        if d is not None:
                            d.run_ids = list(run_ids)
                            d.iterations_run = iterations_run
                    inc_val, cha_val = pair_overalls
                    if inc_val is None or cha_val is None:
                        bad_streak += 1
                        if bad_streak >= MAX_CONSECUTIVE_BAD_PAIRS:
                            verdict, reason = "draw", "aborted: repeated unusable pairs"
                        continue
                    bad_streak = 0
                    delta = cha_val - inc_val
                    deltas.append(delta)
                    sprt.add_pair(delta > 0)
                    verdict = sprt.decision(min_pairs, max_pairs)
                    if verdict in ("challenger", "incumbent"):
                        # Practical-significance floor: a real-but-negligible edge is a draw.
                        if abs(_median(deltas)) < min_margin:
                            verdict, reason = "draw", (
                                f"boundary crossed but |median Δ| < {min_margin} — practically equal"
                            )
                        else:
                            reason = "SPRT boundary crossed"
                    elif verdict == "draw":
                        reason = (
                            "mutual futility (pair wins ~50/50)"
                            if sprt.pairs < max_pairs
                            else f"no decision in {max_pairs} pairs"
                        )

                if verdict is None:
                    verdict, reason = "draw", "window closed mid-matchup (undecided)"
                record = {
                    "incumbent": incumbent_fp,
                    "challenger": challenger_fp,
                    "incumbent_label": inc["label"],
                    "challenger_label": cha["label"],
                    "pairs": sprt.pairs,
                    "wins_incumbent": sprt.wins_incumbent,
                    "wins_challenger": sprt.wins_challenger,
                    "median_delta": round(_median(deltas), 2) if deltas else None,
                    "llr_incumbent": round(sprt.llr_incumbent, 2),
                    "llr_challenger": round(sprt.llr_challenger, 2),
                    "verdict": verdict,
                    "reason": reason,
                }
                matchups.append(record)
                with session_scope() as session:
                    d = session.get(Duel, duel_id)
                    if d is not None:
                        d.matchups = list(matchups)
                log.info(
                    "Duel %s verdict: %s vs %s → %s (%s; pairs=%s Δmed=%s)",
                    duel_id, inc["label"], cha["label"], verdict, reason,
                    sprt.pairs, record["median_delta"],
                )
                # Winner stays on as the incumbent (a draw keeps the current incumbent).
                if verdict == "challenger":
                    incumbent_fp = challenger_fp

            # The ladder's final incumbent is the duel champion of this session.
            champion = settings_by_fp.get(incumbent_fp) or {}
            with session_scope() as session:
                d = session.get(Duel, duel_id)
                if d is not None:
                    d.champion_fingerprint = incumbent_fp
                    d.champion_label = champion.get("label")
            if _state.get("cancel"):
                final_status = DuelStatus.CANCELLED

            # Always restore the pre-duel baseline: the duel adjudicates, it never
            # promotes. Applying the champion is the crown follower's job under the
            # crowning policy (crown_follow.policy = "duel").
            _set_stage(duel_id, "Restoring your original settings")
            try:
                restore, _ = plan_apply(baseline, provider.discover())
                _apply_all(provider, restore)
            except Exception:  # noqa: BLE001 — never raise out of cleanup
                log.exception("Duel %s: baseline restore failed", duel_id)
    except Exception as exc:  # noqa: BLE001 — record, never crash the thread
        log.exception("Duel %s failed", duel_id)
        final_status = DuelStatus.FAILED
        err = f"{type(exc).__name__}: {exc}"
    finally:
        teardown_plugins()  # Chromium was kept warm across the whole ladder
        with session_scope() as session:
            d = session.get(Duel, duel_id)
            if d is not None:
                d.status = final_status
                d.error = err
                d.stage = {
                    DuelStatus.COMPLETE: "Done — baseline restored",
                    DuelStatus.CANCELLED: "Cancelled — baseline restored",
                }.get(final_status, err or "Failed")
                d.finished_at = datetime.now(timezone.utc)
        _state.update({"active": False, "id": None, "cancel": False})
        # A fresh verdict may change what the crowning policy resolves to.
        try:
            from . import crown_follower

            crown_follower.poke("duel verdict")
        except Exception:  # noqa: BLE001
            log.debug("Duel: crown follower poke failed", exc_info=True)
        log.info("Duel %s finished: %s", duel_id, final_status.value)


# ── Ledger accessors ─────────────────────────────────────────────────────────────────


def latest_champion(session, max_age_days: int) -> dict | None:
    """The most recent completed duel's champion, if fresh enough.

    Returns ``{fingerprint, label, duel_id, finished_at, decisive}`` or None. ``decisive``
    is True when the session contained at least one non-draw verdict — a champion who only
    inherited the crown by draws adds no head-to-head information over the pooled verdict.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max_age_days)
    row = session.scalars(
        select(Duel)
        .where(Duel.status == DuelStatus.COMPLETE, Duel.champion_fingerprint.is_not(None))
        .order_by(Duel.id.desc())
        .limit(1)
    ).first()
    if row is None or row.finished_at is None:
        return None
    finished = row.finished_at
    if finished.tzinfo is not None:
        finished = finished.astimezone(timezone.utc).replace(tzinfo=None)
    if finished < cutoff:
        return None
    return {
        "fingerprint": row.champion_fingerprint,
        "label": row.champion_label,
        "duel_id": row.id,
        "finished_at": row.finished_at.isoformat(),
        "decisive": any((m or {}).get("verdict") != "draw" for m in row.matchups or []),
    }


def _serialize(d: Duel) -> dict:
    return {
        "id": d.id,
        "status": d.status.value if hasattr(d.status, "value") else str(d.status),
        "stage": d.stage,
        "trigger": d.trigger,
        "duration_s": d.duration_s,
        "matchups": d.matchups or [],
        "iterations_run": d.iterations_run,
        "run_ids": d.run_ids or [],
        "champion_fingerprint": d.champion_fingerprint,
        "champion_label": d.champion_label,
        "error": d.error,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "started_at": d.started_at.isoformat() if d.started_at else None,
        "finished_at": d.finished_at.isoformat() if d.finished_at else None,
        "lock_owner": coordinator.owner(),
    }


def current() -> dict | None:
    """The most recent duel session (for status polling), or None."""
    with session_scope() as session:
        d = session.scalars(select(Duel).order_by(Duel.id.desc())).first()
        return _serialize(d) if d else None


def history(limit: int = 10) -> list[dict]:
    """Recent duel sessions, newest first (the head-to-head ledger)."""
    with session_scope() as session:
        rows = session.scalars(select(Duel).order_by(Duel.id.desc()).limit(limit)).all()
        return [_serialize(d) for d in rows]


def reconcile_interrupted_duels() -> int:
    """Restore the baseline for any duel left RUNNING/PENDING by a dead process."""
    from .challenger import _apply_all

    provider = None
    restored = 0
    with session_scope() as session:
        rows = session.scalars(
            select(Duel).where(Duel.status.in_([DuelStatus.RUNNING, DuelStatus.PENDING]))
        ).all()
        for d in rows:
            baseline = d.baseline or []
            if baseline:
                try:
                    provider = provider or get_provider()
                    changes, _ = plan_apply(baseline, provider.discover())
                    _apply_all(provider, changes)
                except Exception:  # noqa: BLE001
                    log.exception("Duel %s: restore on reconcile failed", d.id)
            d.status = DuelStatus.FAILED
            d.error = "Interrupted — service restarted mid-duel; baseline restored (best-effort)."
            d.finished_at = datetime.now(timezone.utc)
            restored += 1
    if restored:
        log.warning("Reconciled %s interrupted duel(s); baseline restored", restored)
    return restored


__all__ = [
    "SprtState",
    "active",
    "cancel",
    "current",
    "history",
    "latest_champion",
    "reconcile_interrupted_duels",
    "start",
]
