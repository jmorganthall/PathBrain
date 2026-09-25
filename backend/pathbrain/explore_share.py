"""The duel's exploration share: a slice of every window spent measuring Explore's best bet.

The ladder only ever adjudicates profiles that already exist. Explore is the one engine that
proposes profiles nobody has tried — and until this, its proposals reached the ring only when
a person pressed "Test now". So the field the ladder fought over grew only by hand, and a
night of duels could spend eight hours refining a ranking of yesterday's ideas.

This closes the loop inside the window. A fixed **share** of every session's wall clock
(``duel.explore_share``, default 5%) goes to Explore's top **bet** — the candidate ranked at
the pessimistic end of its measured band (``explore.rank_bets``), i.e. the one we would
*back*, not the one we would merely go and look at. The bet runs as an ordinary Explore
test (``routes_explore._start_candidate``: the claim is written to the recommendation
ledger first, the candidate is materialized on its parent, applied, benchmarked and
restored), queued behind the ring and let through at the ring's own seam by the zipper
yield. When it lands, the ring re-reads the field, so the new profile — thin, wide-banded,
exactly what ``contender_order``'s untested tier exists for — can be seated against the
belt the same night if its ceiling reaches the crown.

**It scales on its own**, because the budget is a share of the window rather than a count:
a two-hour window buys one or two bets, an eight-hour night five or six, and a continuous
ladder spends the share of every session it runs. It is **paced**, not front-loaded: a bet
is due only while the time spent so far is at or under ``share × elapsed``, so the bets are
spread across the night rather than all taken at the first seam — a bet is a claim about the
field *as it stands*, and one proposed at 04:00 is priced on everything the ring learnt
before it. The first bet goes at the first seam (nothing spent yet), so even a short
session contributes one; the overshoot is bounded by that single bet. No bet is queued that
cannot finish inside the window, since a queued test outlives the ladder that asked for it.

The accounting is the bets' own clocks — each ``ProfileTest`` row's start and finish — so
the share is what exploration actually cost, not the duel's guess at it. Nothing here writes
the firewall or scores anything; it only decides when to press the button a person would.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from sqlalchemy import select

from .database import session_scope
from .logging_config import get_logger
from .models import ProfileTest, ProfileTestStatus

log = get_logger("explore_share")

#: The share of a session's window spent on Explore's bets when the config says nothing.
DEFAULT_SHARE = 0.05
#: A ceiling on the share: past half, the session is an exploration batch, not a ladder.
MAX_SHARE = 0.5
#: How long a bet is assumed to take before one has been measured this session.
DEFAULT_BET_S = 300.0
#: After Explore has nothing runnable (or the landscape failed), how long before asking again —
#: the landscape costs a field pass, and the field does not change fast enough to re-ask
#: every cycle.
RETRY_AFTER_S = 1800.0
#: How long to wait for a just-queued bet to reach the coordinator's queue, so the ring's
#: yield at the same seam sees it rather than holding the pipeline for one more cycle.
QUEUE_SETTLE_S = 5.0

_OPEN = (ProfileTestStatus.PENDING, ProfileTestStatus.RUNNING)


def share(cfg: dict | None) -> float:
    """The configured share of the window, clamped to ``[0, MAX_SHARE]`` (0 = off)."""
    raw = (cfg or {}).get("explore_share", DEFAULT_SHARE)
    try:
        value = float(DEFAULT_SHARE if raw is None else raw)
    except (TypeError, ValueError):
        value = DEFAULT_SHARE
    return max(0.0, min(MAX_SHARE, value))


def iterations(cfg: dict | None) -> int:
    """Iterations per bet — Explore's own "Test now" length unless configured."""
    from .explore_tracker import QUICK_ITERATIONS

    try:
        n = int((cfg or {}).get("explore_iterations") or QUICK_ITERATIONS)
    except (TypeError, ValueError):
        n = QUICK_ITERATIONS
    return max(1, min(50, n))


def due(*, share_: float, elapsed_s: float, window_s: float, spent_s: float,
        remaining_s: float, bet_cost_s: float) -> bool:
    """Is a bet owed now? Pure, so the pacing rule is testable on its own.

    Owed while exploration is at or under its share of the time elapsed *and* under its
    share of the whole window, and only when the window has room for one more bet.
    """
    if share_ <= 0 or window_s <= 0:
        return False
    if spent_s >= share_ * window_s:
        return False
    if spent_s > share_ * max(0.0, elapsed_s):
        return False
    return remaining_s >= bet_cost_s


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class ExploreShare:
    """One session's exploration allowance: when a bet is owed, queueing it, and the books."""

    def __init__(self, duel_id: int, cfg: dict | None, window_s: float,
                 *, clock=time.monotonic) -> None:
        self.duel_id = duel_id
        self.share = share(cfg)
        self.iterations = iterations(cfg)
        self.window_s = float(window_s)
        self._clock = clock
        self._t0 = clock()
        self.bets: list[dict] = []
        self._tried: set[str] = set()
        self._folded: set[int] = set()
        self._retry_at = 0.0
        self.note: str | None = None

    @property
    def enabled(self) -> bool:
        return self.share > 0

    # ── the books ────────────────────────────────────────────────────────────────────
    def _rows(self) -> list[ProfileTest]:
        ids = [b["test_id"] for b in self.bets if b.get("test_id") is not None]
        if not ids:
            return []
        with session_scope() as session:
            rows = list(session.scalars(select(ProfileTest).where(ProfileTest.id.in_(ids))))
            session.expunge_all()
            return rows

    def spent_s(self, rows: list[ProfileTest] | None = None) -> float:
        """Wall clock the bets have actually held the pipeline — their own start/finish."""
        now = datetime.now(timezone.utc)
        total = 0.0
        for pt in rows if rows is not None else self._rows():
            start = _utc(pt.started_at)
            if start is None:
                continue
            end = _utc(pt.finished_at) or now
            total += max(0.0, (end - start).total_seconds())
        return total

    def _bet_cost_s(self, rows: list[ProfileTest]) -> float:
        done = [
            (_utc(pt.finished_at) - _utc(pt.started_at)).total_seconds()
            for pt in rows
            if pt.status == ProfileTestStatus.COMPLETE and pt.started_at and pt.finished_at
        ]
        return max(done) if done else DEFAULT_BET_S

    # ── the seam ─────────────────────────────────────────────────────────────────────
    def at_seam(self, remaining_s: float) -> dict | None:
        """Queue Explore's top bet if one is owed. Returns the bet queued, or None.

        Never raises: exploration is a share of the night, never a reason to lose it.
        """
        if not self.enabled or self._clock() < self._retry_at:
            return None
        try:
            rows = self._rows()
            if any(pt.status in _OPEN for pt in rows):
                return None  # one bet at a time; its clock is still running
            if not due(
                share_=self.share, elapsed_s=self._clock() - self._t0, window_s=self.window_s,
                spent_s=self.spent_s(rows), remaining_s=remaining_s,
                bet_cost_s=self._bet_cost_s(rows),
            ):
                return None
            bet = self._queue_top_bet()
        except Exception:  # noqa: BLE001
            log.exception("Duel %s: exploration share could not queue a bet", self.duel_id)
            self._retry_at = self._clock() + RETRY_AFTER_S
            return None
        if bet is None:
            self._retry_at = self._clock() + RETRY_AFTER_S
            return None
        self.bets.append(bet)
        # Let the test reach the coordinator's queue, so the yield at this very seam sees it.
        from . import coordinator

        deadline = time.monotonic() + QUEUE_SETTLE_S
        while coordinator.waiting() <= 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        return bet

    def _queue_top_bet(self) -> dict | None:
        """Walk Explore's bets, best first, and queue the first one that starts."""
        from fastapi import HTTPException

        from . import explore as explore_mod
        from .api import routes_explore

        with session_scope() as session:
            landscape = explore_mod.landscape(
                session,
                suggestions=routes_explore.BATCH_CANDIDATE_POOL,
                confident_only=True,
                allowed_values=routes_explore._allowed_values(),
            )
            pool = landscape.get("bets") or []
            if not pool:
                self.note = landscape.get("reason") or "Explore had nothing runnable to propose."
                log.info("Duel %s: no Explore bet to run — %s", self.duel_id, self.note)
                return None
            best_overall = landscape.get("best_overall")
            for candidate in pool:
                key = "%s|%s" % (
                    (candidate.get("parent") or {}).get("fingerprint"),
                    routes_explore.candidate_label(candidate),
                )
                if key in self._tried:
                    continue
                self._tried.add(key)
                label, payload = routes_explore.candidate_test(
                    candidate, self.iterations, best_overall
                )
                payload.label = f"Explore (duel #{self.duel_id}): {label}"
                try:
                    started = routes_explore._start_candidate(session, payload)
                except HTTPException as exc:
                    log.info("Duel %s: skipped Explore bet %s — %s", self.duel_id, label, exc.detail)
                    continue
                self.note = None
                bet = {
                    "test_id": started.get("id"),
                    "fingerprint": started.get("fingerprint"),
                    "label": label,
                    "summary": candidate.get("summary"),
                    "predicted": candidate.get("predicted"),
                    "confidence_score": candidate.get("confidence_score"),
                    "clears_bar": candidate.get("clears_bar"),
                    "iterations": self.iterations,
                    "recommendation_id": started.get("recommendation_id"),
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                }
                log.info("Duel %s: queued Explore bet #%s %s (%s iterations)",
                         self.duel_id, bet["test_id"], label, self.iterations)
                return bet
            self.note = "Every Explore bet on offer was refused (no-op or unreachable)."
            return None

    def newly_landed(self) -> list[dict]:
        """Bets that finished since the last ask — the cue to re-read the field."""
        landed: list[dict] = []
        try:
            rows = {pt.id: pt for pt in self._rows()}
        except Exception:  # noqa: BLE001
            log.debug("Duel %s: could not read the exploration bets", self.duel_id, exc_info=True)
            return landed
        for bet in self.bets:
            tid = bet.get("test_id")
            pt = rows.get(tid)
            if pt is None or tid in self._folded or pt.status in _OPEN:
                continue
            self._folded.add(tid)
            bet["status"] = pt.status.value
            if pt.status == ProfileTestStatus.COMPLETE:
                landed.append(bet)
        return landed

    def summary(self) -> dict:
        """The session's exploration, for the live board and the stored row."""
        try:
            rows = self._rows()
        except Exception:  # noqa: BLE001
            rows = []
        by_id = {pt.id: pt for pt in rows}
        bets = []
        for b in self.bets:
            pt = by_id.get(b.get("test_id"))
            bets.append({**b, "status": pt.status.value if pt is not None else b.get("status")})
        return {
            "share": self.share,
            "iterations": self.iterations,
            "budget_s": round(self.share * self.window_s),
            "spent_s": round(self.spent_s(rows)),
            "bets": bets,
            "note": self.note,
        }
