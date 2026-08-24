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
from . import profile_names
from .config_store import get_config
from .database import session_scope
from .logging_config import get_logger
from .models import Duel, DuelStatus, Score
from .rating import PROVISIONAL_PAIRS, RANK_SIGMA, fit_bradley_terry
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


# ── One dial instead of six ───────────────────────────────────────────────────────────
#
# "When is someone the winner?" is one question, and it was spread across six numeric
# fields (min/max pairs, error rate, practical margin, edge-to-detect, method) that only
# make sense together. These presets collapse the statistical ones into a single choice
# with a stated consequence; the raw fields remain for anyone who wants them, and any
# hand-edit simply reads back as "custom".
#
# The numbers on each preset are MEASURED, not asserted: simulated against a true edge
# with ~1.5-point run-to-run noise (test_duel re-checks them). A dial you can't predict
# the behavior of is no simpler than six you can't.
PRESETS: dict[str, dict] = {
    "snap": {
        "label": "Snap call",
        "alpha": 0.10,
        "min_pairs": 3,
        "max_pairs": 12,
        "streak_wins": 3,
        "summary": "3 rounds in a row ends it. Fast, and self-correcting on a nightly ladder.",
        "detail": "Names the better profile ~91% of the time at a 1-point edge. Between equal profiles it's a coin toss — read the standings, not one match.",
    },
    "quick": {
        "label": "Quick call",
        "alpha": 0.10,
        "streak_wins": 0,
        "min_pairs": 5,
        "max_pairs": 20,
        "summary": "6 rounds in a row ends it. About 1 verdict in 9 will be wrong.",
        "detail": "Also calls a profile that wins most (not all) rounds — ~80% of true 1-point edges, usually within 10.",
    },
    "balanced": {
        "label": "Balanced",
        "alpha": 0.05,
        "streak_wins": 0,
        "min_pairs": 8,
        "max_pairs": 30,
        "summary": "8 rounds in a row ends it. About 1 verdict in 16 will be wrong.",
        "detail": "Also calls a profile that wins most (not all) rounds — ~85% of true 1-point edges, usually within 14.",
    },
    "strict": {
        "label": "Only when certain",
        "alpha": 0.01,
        "streak_wins": 0,
        "min_pairs": 12,
        "max_pairs": 60,
        "summary": "12 rounds in a row ends it. Rarely wrong (~1 verdict in 60).",
        "detail": "Also calls a profile that wins most (not all) rounds — ~97% of true 1-point edges, usually within 24.",
    },
}

# The keys a preset owns. Anything else (the practical margin, the schedule) is a
# separate question and is never touched by choosing one.
PRESET_KEYS = ("alpha", "min_pairs", "max_pairs", "streak_wins")


def preset_for(cfg: dict) -> str:
    """Which preset the current numbers correspond to, or ``"custom"``."""
    for name, preset in PRESETS.items():
        if all(
            abs(float(cfg.get(k, preset[k]) or 0) - float(preset[k])) < 1e-9 for k in PRESET_KEYS
        ):
            return name
    return "custom"


def preset_config(name: str) -> dict:
    """The config updates a preset writes. Raises ``ValueError`` for an unknown name."""
    preset = PRESETS.get(str(name or "").lower())
    if preset is None:
        raise ValueError(f"unknown preset {name!r} (choose one of {', '.join(PRESETS)})")
    return {k: preset[k] for k in PRESET_KEYS}


def streak_to_decide(
    alpha: float, min_pairs: int, max_pairs: int, streak_wins: int = 0
) -> int | None:
    """How many pairs in a row end a bout on the spot.

    This is the "if it wins back to back, it wins" rule, and it falls straight out of the
    test rather than being bolted on: an unbroken run of n pairs has p = 1/2ⁿ, so a bout
    is decided the moment the run is long enough to clear the threshold.

    The length matters more than intuition suggests. Between two IDENTICAL profiles, a
    30-pair bout throws up a 3-in-a-row streak 99.7% of the time, 5-in-a-row 62%, and
    8-in-a-row 9% — a short streak is what a coin flip looks like, not evidence. Going the
    other way, a pure streak rule is glacial: a profile winning 70% of its pairs needs ~54
    pairs on average to string 8 together. Hence the paired test, which honours a clean
    run *and* can still call a profile that goes 12-3 without ever stringing 8 together.
    """
    if streak_wins:
        return int(streak_wins)  # the user set the rule outright
    ev = PairedEvidence(alpha, 0.0, min_pairs, max_pairs)
    nominal = ev.nominal_alpha
    for n in range(max(int(min_pairs or 1), 4), max(int(max_pairs or 1), 4) + 1):
        if 0.5**n <= nominal:
            return n
    return None


def paired_requirements(
    alpha: float, min_pairs: int, max_pairs: int, streak_wins: int = 0
) -> dict:
    """What the magnitude-aware rule demands — the sensitivity readout for the UI.

    ``sweep_pairs`` is the fewest consistently one-sided pairs that clear the peek-
    corrected threshold (the fastest possible verdict). Unlike the sign test, there is no
    cap at which a verdict becomes unreachable: more evidence always helps, so the
    "restrictive" trap the pair-win rule falls into cannot happen here.
    """
    ev = PairedEvidence(alpha, 0.0, min_pairs, max_pairs)
    nominal = ev.nominal_alpha
    sweep = None
    for n in range(max(int(min_pairs or 1), 4), max(int(max_pairs or 1), 4) + 1):
        # Distinct magnitudes, all one-sided: the best case a bout can present.
        if wilcoxon_p([float(i + 1) for i in range(n)]) <= nominal:
            sweep = n
            break
    return {
        "sweep_pairs": sweep,
        # The plain-language version of the same rule: N wins in a row ends it now.
        "streak_pairs": streak_to_decide(alpha, min_pairs, max_pairs, streak_wins),
        "wins_needed": None,  # not a win-count rule — the margins decide
        "win_rate_needed": None,
        "nominal_alpha": round(nominal, 5),
        "peek_penalty": round(peek_penalty(max(int(max_pairs or 1) - int(min_pairs or 1) + 1, 2)), 2),
        "restrictive": sweep is None,
    }


def sprt_requirements(p1: float, alpha: float, min_pairs: int, max_pairs: int) -> dict:
    """What it actually takes to win a bout under the current stopping rule.

    The SPRT's evidence bar and the pair cap interact in a way that is invisible from the
    numbers alone: each pair won moves the walk by ``ln(p1/0.5)`` and each pair lost by
    ``ln((1-p1)/0.5)`` — a *bigger* step — so a cap set too low can make a verdict
    arithmetically unreachable for any realistic edge. At p1=0.70/alpha=0.05 with a
    15-pair cap, a winner needs 13 of 15 (87%); a genuinely better profile winning 80% of
    pairs is recorded as a draw, forever, no matter how many nights it runs.

    Returns ``{sweep_pairs, wins_needed, win_rate_needed, restrictive}``:

    * ``sweep_pairs`` — fastest possible verdict (an unbroken run of wins).
    * ``wins_needed`` — pairs a winner must take *at the cap*, or None if the cap makes a
      verdict impossible.
    * ``restrictive`` — True when the cap demands a near-sweep (>80% of pairs), i.e. the
      rule will mostly return draws.
    """
    p1 = min(max(float(p1 or 0.70), 0.501), 0.999)
    alpha = min(max(float(alpha or 0.05), 0.001), 0.2)
    win_step = math.log(p1 / 0.5)
    loss_step = math.log((1 - p1) / 0.5)
    upper = math.log((1 - alpha) / alpha)
    sweep = max(int(math.ceil(upper / win_step)), int(min_pairs or 0))
    cap = max(int(max_pairs or 0), 0)
    wins_needed = next(
        (w for w in range(cap + 1) if w * win_step + (cap - w) * loss_step >= upper), None
    )
    return {
        "sweep_pairs": sweep,
        "wins_needed": wins_needed,
        "win_rate_needed": round(wins_needed / cap, 3) if wins_needed and cap else None,
        "restrictive": wins_needed is None or (cap > 0 and wins_needed / cap > 0.8),
    }


# ── Magnitude-aware adjudication (Wilcoxon signed-rank on the paired margins) ─────────
#
# The SPRT above is a SIGN test: it records which side won each pair and discards by how
# much. That costs enormous power on exactly the data duels produce. Measured against a
# true 1.0-point edge with ~1.5-point run noise, a 15-pair bout called a winner just 28%
# of the time — profiles genuinely winning, never presented as winners.
#
# The paired differences carry the magnitudes, so testing THEM instead recovers the
# signal: the same bout is decided 60-70% of the time, and a 2-point edge is called
# essentially always (measured; see test_duel). Wilcoxon signed-rank is the right test
# here — it uses magnitude via ranks without assuming normal margins, which matters
# because a duel's deltas are a handful of medians with occasional wild ones.
#
# Peeking after every pair inflates false positives (a 40-pair bout peeks 36 times, and
# an uncorrected 5% test fires on ~26% of true ties). The nominal threshold is therefore
# divided by a Pocock-style penalty fitted by simulation over the practical range of peek
# counts, which holds the realized false-positive rate at ~5%.


def _rank_sum_counts(n: int) -> list[int]:
    """Counts of rank subsets by sum — the exact Wilcoxon null distribution for n pairs."""
    total = n * (n + 1) // 2
    counts = [0] * (total + 1)
    counts[0] = 1
    for rank in range(1, n + 1):
        for w in range(total, rank - 1, -1):
            counts[w] += counts[w - rank]
    return counts


def wilcoxon_p(deltas: list[float], direction: int = 1) -> float:
    """One-sided p-value that the paired differences favour ``direction`` (+1 = positive).

    Exact for small samples (the regime a duel actually runs in — a normal approximation
    is noticeably conservative under ~25 pairs, which would throw away the sensitivity
    this whole change is about) and the tie-corrected normal approximation beyond.
    """
    d = [x * direction for x in deltas if x != 0]
    n = len(d)
    if n < 4:  # too little to distinguish from chance at any sane alpha
        return 1.0
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:  # average ranks within ties
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    w_pos = sum(r for r, x in zip(ranks, d) if x > 0)

    if n <= 25 and all(float(r).is_integer() for r in ranks):  # exact, no ties
        counts = _rank_sum_counts(n)
        total = sum(counts)
        at_least = sum(c for w, c in enumerate(counts) if w >= w_pos)
        return at_least / total

    mean = n * (n + 1) / 4
    var = n * (n + 1) * (2 * n + 1) / 24
    if var <= 0:
        return 1.0
    z = (w_pos - mean - 0.5) / math.sqrt(var)
    return 0.5 * math.erfc(z / math.sqrt(2))


def peek_penalty(peeks: int) -> float:
    """How much to tighten the nominal threshold for testing after every pair.

    Fitted by simulation (null margins, sequential peeking) so the realized false-positive
    rate lands at the configured alpha across 6-36 peeks: alpha/3 at 6 peeks, alpha/5 at
    11, alpha/9 at 36. Without it a 40-pair bout would call a coin-flip a winner a quarter
    of the time.
    """
    return max(2.0, 3.32 * math.log(max(peeks, 2)) - 2.97)


class PairedEvidence:
    """Accumulates each pair's Overall margin and decides when one side has really won.

    ``add(delta)`` takes the challenger-minus-incumbent margin of one interleaved pair.
    ``decision()`` returns ``"challenger"``/``"incumbent"`` once the signed-rank test
    clears the peek-corrected threshold **and** the median margin clears the practical
    floor (``duel.min_margin``) — statistical reality and practical significance are
    separate questions, and a verdict needs both.
    """

    def __init__(
        self,
        alpha: float,
        min_margin: float,
        min_pairs: int,
        max_pairs: int,
        streak_wins: int = 0,
    ) -> None:
        self.alpha = min(max(float(alpha or 0.05), 0.001), 0.2)
        self.min_margin = max(float(min_margin or 0.0), 0.0)
        self.min_pairs = max(int(min_pairs or 1), 1)
        self.max_pairs = max(int(max_pairs or 1), self.min_pairs)
        # An explicit "N wins in a row ends it" rule, if the user set one. It deliberately
        # overrides `min_pairs`: "3 in a row wins" has to mean exactly that, or the field
        # is lying. 0 = derive the streak from the statistical threshold instead.
        self.streak_wins = max(int(streak_wins or 0), 0)
        self.deltas: list[float] = []

    @property
    def nominal_alpha(self) -> float:
        peeks = self.max_pairs - self.min_pairs + 1
        return self.alpha / peek_penalty(peeks)

    def add(self, delta: float) -> None:
        self.deltas.append(float(delta))

    @property
    def pairs(self) -> int:
        return len(self.deltas)

    def p_value(self, direction: int = 1) -> float:
        return wilcoxon_p(self.deltas, direction)

    @property
    def current_streak(self) -> tuple[int, int]:
        """(length, direction) of the current unbroken run of pair wins."""
        if not self.deltas:
            return 0, 0
        direction = 1 if self.deltas[-1] > 0 else -1
        length = 0
        for delta in reversed(self.deltas):
            if (1 if delta > 0 else -1) != direction:
                break
            length += 1
        return length, direction

    def decision(self) -> str | None:
        # An explicit streak rule fires first and on its own terms. On a nightly ladder a
        # verdict is cheap and self-correcting, so a short streak is a defensible trade:
        # measured against a true 1-point edge, 3-in-a-row names the better profile ~91%
        # of the time and the worse one ~7%. Between genuinely equal profiles it is a coin
        # toss — which costs nothing, because either answer is right.
        if self.streak_wins:
            length, direction = self.current_streak
            if length >= self.streak_wins and (
                not self.min_margin or abs(_median(self.deltas)) >= self.min_margin
            ):
                return "challenger" if direction > 0 else "incumbent"

        if self.pairs < self.min_pairs:
            return None
        median = _median(self.deltas)
        if abs(median) < self.min_margin:  # real but not worth acting on
            return None
        direction = 1 if median > 0 else -1
        if self.p_value(direction) <= self.nominal_alpha:
            return "challenger" if direction > 0 else "incumbent"
        return None


def _median(values: list[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


# ── Engine ────────────────────────────────────────────────────────────────────────────


def _duel_config(session) -> dict:
    return get_config(session).get("duel", {}) or {}


def _live_scoreboard(
    *,
    bout: int,
    inc: dict,
    cha: dict,
    sprt,
    paired,
    why_challenger: str,
    min_pairs: int,
    max_pairs: int,
    min_margin: float,
    streak_needed: int,
) -> dict:
    """The bout in progress as structured state — who is ahead, by how much, how close.

    A scoreline in a sentence ("pair 4 (2-1)") cannot answer the only question a live duel
    raises: *who is winning?* It doesn't say which side those wins belong to, it discards
    the margins the verdict is actually decided on, and it gives no sense of how near the
    bout is to ending. All three are known at this point in the loop; this hands them over
    instead of formatting them away.
    """
    deltas = list(paired.deltas)
    median = _median(deltas) if deltas else None
    streak_len, streak_dir = paired.current_streak
    inc_wins, cha_wins = sprt.wins_incumbent, sprt.wins_challenger
    if cha_wins > inc_wins:
        leader = "challenger"
    elif inc_wins > cha_wins:
        leader = "incumbent"
    else:
        leader = "level"
    # The p-value is only meaningful in the direction the margins actually point.
    p_value = None
    if deltas and median is not None and median != 0:
        p_value = paired.p_value(1 if median > 0 else -1)
    return {
        "bout": bout,
        "pairs": sprt.pairs,
        "incumbent": {
            "fingerprint": inc.get("fingerprint"),
            "name": inc.get("name"),
            "label": inc.get("label"),
            "wins": inc_wins,
        },
        "challenger": {
            "fingerprint": cha.get("fingerprint"),
            "name": cha.get("name"),
            "label": cha.get("label"),
            "wins": cha_wins,
            "why": why_challenger,
        },
        "leader": leader,
        # Margins are challenger − incumbent, in Overall points: positive means the
        # challenger is ahead. The median is what the verdict is decided on; the series
        # is what shows whether the lead is steady or a single lucky pair.
        "median_margin": round(median, 2) if median is not None else None,
        "last_margin": round(deltas[-1], 2) if deltas else None,
        "margins": [round(d, 2) for d in deltas[-24:]],
        "min_pairs": min_pairs,
        "max_pairs": max_pairs,
        "min_margin": min_margin,
        "p_value": round(p_value, 4) if p_value is not None else None,
        "alpha": round(paired.nominal_alpha, 4),
        "streak": {
            "length": streak_len,
            "side": ("challenger" if streak_dir > 0 else "incumbent") if streak_len else None,
            "needed": streak_needed,
        },
    }


def _set_live(duel_id: int, payload: dict | None) -> None:
    """Persist the live scoreboard (or clear it when no bout is running)."""
    try:
        with session_scope() as session:
            d = session.get(Duel, duel_id)
            if d is not None:
                d.live = payload
    except Exception:  # noqa: BLE001 — a status write must never break the duel
        log.debug("Duel %s: could not persist live state", duel_id, exc_info=True)


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
    # Query by TIME, not "the last 20 sessions": a continuous ladder finishes several
    # sessions a day, so a fixed row cap covered barely three days of a seven-day
    # cooldown and matchups quietly became re-fightable early.
    rows = session.scalars(
        select(Duel).where(Duel.finished_at.is_not(None), Duel.finished_at >= cutoff)
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


def ledger_leader(
    ratings: dict[str, dict], eligible: set[str] | None = None
) -> str | None:
    """The highest ``rating_floor`` on a fitted ledger — the ring's #1, full stop.

    The ONE place that answers "who is the champion?", so the belt on the page, the
    defender in the ring, and the profile the crowning policy would apply are the same
    profile by construction. They used to be three different answers: the badge read a
    stored ``Duel.champion_fingerprint`` written at session end, while the standings were
    fitted live over the whole ledger — so any bout in a running session moved the table
    without moving the badge, and every row written before the belt became the ring's #1
    recorded whoever happened to survive that session instead.

    ``eligible`` narrows the pool (the *defender* must be reachable from the live
    environment; the *champion* is a statement about the ledger and is never filtered).
    """
    best_fp: str | None = None
    best: tuple[float, int] | None = None
    for fp, r in ratings.items():
        if eligible is not None and fp not in eligible:
            continue
        if r.get("rating_floor") is None:
            continue
        # Ties break toward the deeper record, exactly as the standings do.
        key = (float(r["rating_floor"]), int(r.get("pairs") or 0))
        if best is None or key > best:
            best_fp, best = fp, key
    return best_fp


def ring_leader(
    field: dict, ratings: dict[str, dict], baseline: list[dict] | None = None
) -> tuple[str | None, str]:
    """The ring's own #1 — the top of the standings, by the conservative rating floor.

    This is the one profile the ladder exists to attack, so it is the one that defends. It
    is deliberately the SAME number the standings rank on (`rating - RANK_SIGMA*se`), which
    is what makes the belt and the league table impossible to disagree: the badge naming a
    profile that isn't #1 on the table underneath it is a bug, not a nuance.

    Returns ``(fingerprint, why)``, or ``(None, "")`` when nothing rated and reachable
    exists — a fresh ledger, or a live environment no stored profile matches.
    """
    profiles = {p["fingerprint"]: p for p in field.get("profiles", [])}
    eligible = {
        fp for fp, p in profiles.items() if _reachable(p.get("settings"), baseline)
    }
    best_fp = ledger_leader(ratings, eligible)
    if best_fp is None:
        return None, ""
    r = ratings[best_fp]
    opps = int(r.get("opponents") or 0)
    return best_fp, (
        f"the ring's #1 defends (proven {r.get('rating_floor'):.0f} over "
        f"{r.get('pairs') or 0} rounds against {opps} opponent{'' if opps == 1 else 's'})"
    )


def select_incumbent(
    session,
    field: dict,
    baseline: list[dict] | None,
    cfg: dict,
    ratings: dict[str, dict] | None = None,
) -> tuple[str | None, str]:
    """Who holds the belt, and why. **The ring's #1 defends — always.**

    This replaced "the reigning champion defends", which meant *whoever survived the last
    session*. That is not the same profile as the best one, and on a continuous ladder the
    two drift apart within hours: a mid-table profile wins one bout, inherits the belt, and
    the ladder then spends the night defending IT — bouts between profiles ranked #83 and
    #128 that tell us nothing about whether anything beats the leader. The whole purpose of
    the ring is to keep attacking the best profile we have, so the best profile is what
    stands in it, re-read from the ledger **before every bout** rather than once a session.

    A consequence worth stating: the winner does not automatically stay on. Beating the
    leader promotes you only when it moves your floor above its — which is the same bar the
    standings apply, so "who is champion" and "who is #1" can never disagree. A challenger
    that wins one bout but is still thin keeps a wide error bar, so the leader defends
    again, against the next-best threat; the winner's rating rose, so it comes back around
    quickly, and a second win usually settles it.

    Falls back to the pooled crown only when the ring has nothing to say yet — an empty
    ledger, or no rated profile the live environment can be set to. Returns
    ``(fingerprint, reason)``.
    """
    pooled_fp = field.get("best_fingerprint")
    if ratings is None:
        ratings = ledger_ratings(session)
    fp, why = ring_leader(field, ratings, baseline)
    if fp is not None:
        return fp, why
    return pooled_fp, "no profile has a ring record yet — the pooled crown defends"


def _reachable(settings: list[dict] | None, baseline: list[dict] | None) -> bool:
    """Can the live environment actually be set to this profile? (Same check as the heirs.)"""
    from .settings_profile import environment_signature

    if not settings or not baseline:
        return True
    try:
        return environment_signature(settings) == environment_signature(baseline)
    except Exception:  # noqa: BLE001 — a reachability probe must never break matchmaking
        return True


def _no_contenders_reason(
    field: dict, heirs: dict, incumbent_fp: str | None, baseline: list[dict] | None
) -> str:
    """Why the queue came out empty, in terms the user can act on.

    "No eligible challengers" is useless on its own: the interesting cases are *the live
    environment no longer matches any profile you've measured* (change the scheduler, the
    queue count or the upload bandwidth and every stored profile becomes unreachable) and
    *nothing else has a comparable score yet*. Both are recoverable, and neither is
    obvious from a bare failure.
    """
    others = [
        p for p in field.get("profiles", []) if p.get("fingerprint") != incumbent_fp
    ]
    if not others:
        return "No other profiles to duel — only one profile has been measured so far."
    scored = [p for p in others if p.get("overall") is not None]
    if not scored:
        return (
            f"None of the {len(others)} other profiles has a comparable score under the "
            "current methodology — re-grade history, or collect fresh runs, before duelling."
        )
    unreachable = [p for p in scored if not _reachable(p.get("settings"), baseline)]
    if len(unreachable) == len(scored):
        return (
            f"All {len(scored)} scored profiles are unreachable from the live environment: "
            "the firewall's scheduler / queue count / upload bandwidth differs from every "
            "profile on record, so none of them can be applied. Measure a profile under the "
            "current environment (a manual run is enough) and the ladder has something to race."
        )
    return "No eligible challengers (nothing left after the rematch cooldown)."


def build_queue(
    field: dict,
    heirs: dict,
    incumbent_fp: str,
    *,
    contenders: str = "ring",
    top_n: int = 8,
    baseline: list[dict] | None = None,
    ratings: dict[str, dict] | None = None,
) -> list[str]:
    """Who the champion actually fights, in order.

    ``"ring"`` (default) orders challengers by **the ring's own findings** — each one's
    optimistic ceiling on the fitted head-to-head rating, so the bout most likely to unseat
    the belt-holder runs first. See ``contender_order``. This is the fix for the ladder's
    circularity: the duel exists to check the pooled verdict, so the pooled verdict must not
    be what decides who gets checked. It keeps one job — seeding profiles that have never
    been in the ring, which have no other signal.

    ``"leaders"`` is the previous behaviour, kept for comparison: it races **contenders**: the reachable profiles closest to the
    crown by Overall, best-established first. That is what makes a perpetual ladder worth
    running — a night spent adjudicating the top of the table keeps finding better
    profiles, while a night spent sampling arbitrary unmeasured ones mostly re-confirms
    that they are worse.

    The pooled crown goes first whenever it isn't the one defending: the two verdicts
    disagreeing is the single most informative matchup in the system, and the crown is
    never in ``heirs`` (heirs are contenders *to* it), so it has to be added by hand.

    Then the field itself, ranked by Overall — **confident profiles first**, since a
    profile with two runs and a lucky Overall is not a contender, it's noise. The heirs
    (limited-data / stale / untested) follow, so unknowns still get measured, just after
    the matchups that can actually change the answer.

    ``"heirs"`` keeps the original exploring behaviour (the heirs priority order).

    Reachability is tested against ``baseline`` — the live environment — profile by
    profile. It used to be inherited from the heirs pass, which quietly restricted the
    "leaders" to the heirs pool: since heirs are *by definition* under-sampled or stale,
    the ladder ended up racing exactly the thin profiles this mode exists to avoid.
    """
    profiles = {p["fingerprint"]: p for p in field.get("profiles", [])}
    heir_items = [h for h in (heirs.get("items") or []) if h.get("fingerprint") in profiles]
    heir_order = [h["fingerprint"] for h in heir_items]
    pooled_fp = field.get("best_fingerprint")
    if pooled_fp and pooled_fp != incumbent_fp and pooled_fp in profiles:
        heir_order = [pooled_fp] + [fp for fp in heir_order if fp != pooled_fp]
    if contenders == "ring":
        order = contender_order(
            field, ratings or {}, incumbent_fp, baseline=baseline, heirs=heirs
        )
        return [c["fingerprint"] for c in order]
    if contenders != "leaders":
        return heir_order

    def _eligible(fp: str, p: dict) -> bool:
        if fp == incumbent_fp or p.get("overall") is None:
            return False
        # The heirs pass already applied the environment check, so anything it surfaced is
        # known-reachable; everything else is tested directly against the live settings.
        return fp in heir_order or fp == pooled_fp or _reachable(p.get("settings"), baseline)

    ranked = [
        fp
        for fp, p in sorted(
            profiles.items(),
            key=lambda kv: (bool(kv[1].get("confident")), kv[1].get("overall") or -1),
            reverse=True,
        )
        if _eligible(fp, p)
    ]
    leaders = ranked[: max(int(top_n or 0), 1)]
    if pooled_fp and pooled_fp != incumbent_fp and pooled_fp in profiles:
        leaders = [pooled_fp] + [fp for fp in leaders if fp != pooled_fp]
    # Contenders first, strongest first; everything else the heirs pass surfaced follows,
    # so nothing is lost — it just waits its turn. The final sort is by priority TIER, so a
    # well-measured contender that only showed up in the heirs tail (a stale one, say) still
    # outranks an unmeasured profile that happened to make the top-N.
    order = leaders + [fp for fp in heir_order if fp not in leaders]
    tiers = contender_tiers(field, order)
    return sorted(order, key=lambda fp: tiers[fp])  # stable: intra-tier order is preserved


# Priority tiers. The ladder must never spend the ring on a lower tier while a higher one
# still has someone waiting — that single rule is what keeps a perpetual ladder pointed at
# the matchups that can change the answer.
CROWN_TIER = 0  # the pooled crown: the two verdicts disagreeing is the most informative bout
# A profile the ring has never rated whose **pooled ceiling clears the crown** — on the
# record it could already be the best thing measured, and nobody has checked. It runs before
# the rated contenders, deliberately: those have been examined and their ceiling is a
# statement about beating the *belt-holder*, while this is an unexamined claim on the crown
# itself. Racing it answers that claim head-to-head AND matures it — a bout's paired runs go
# into the pooled record like any others, so the same hour buys the verdict and the evidence.
# Waiting is what costs: the claim is only interesting while it is unresolved.
LIVE_THREAT_TIER = 1
CONTENDER_TIER = 2  # rated; on the ring's own record, could plausibly take the belt
UNTESTED_TIER = 3  # no ring record, and its own runs don't reach the crown even optimistically
OUTCLASSED_TIER = 4  # the ring already says they can't reach the belt: raced last, not never
UNPROMISING_TIER = UNTESTED_TIER  # the same thing named for what it is
FILLER_TIER = UNTESTED_TIER  # legacy alias for the pre-ring-ranking modes


def contender_tiers(
    field: dict,
    queue: list[str],
    ratings: dict[str, dict] | None = None,
    incumbent_fp: str | None = None,
) -> dict[str, int]:
    """Each queued profile's priority tier, derived from the same field the queue was built
    from (so there is no second ranking to drift out of step with the first).

    When ring ratings are supplied the tiers come from ``contender_order`` — the one place
    that decides them — so the loop can never disagree with the order it was handed.
    """
    if ratings is not None and incumbent_fp is not None:
        by_fp = {c["fingerprint"]: c["tier"] for c in contender_order(field, ratings, incumbent_fp)}
        return {fp: by_fp.get(fp, UNTESTED_TIER) for fp in queue}
    profiles = {p["fingerprint"]: p for p in field.get("profiles", [])}
    pooled_fp = field.get("best_fingerprint")
    out: dict[str, int] = {}
    for fp in queue:
        p = profiles.get(fp) or {}
        crown_overall = (profiles.get(pooled_fp or "") or {}).get("overall")
        opt = p.get("optimistic")
        if fp == pooled_fp:
            out[fp] = CROWN_TIER
        elif not p.get("confident") and opt is not None and (
            crown_overall is None or opt >= crown_overall
        ):
            # Thin, unexamined, and its ceiling reaches the crown — the same claim the ring
            # ordering promotes, recognised here too so a mode change can't demote it.
            out[fp] = LIVE_THREAT_TIER
        elif p.get("confident") and p.get("overall") is not None:
            out[fp] = CONTENDER_TIER
        else:
            out[fp] = FILLER_TIER
    return out


TIER_NAMES = {
    CROWN_TIER: "pooled crown",
    LIVE_THREAT_TIER: "live threat",
    CONTENDER_TIER: "contender",
    UNTESTED_TIER: "own runs say no",
    OUTCLASSED_TIER: "outclassed",
}


def _challenger_order(
    field: dict,
    ratings: dict[str, dict],
    defender_fp: str,
    *,
    mode: str,
    heirs: dict,
    baseline: list[dict] | None,
    top_n: int,
) -> list[dict]:
    """The candidates to face ``defender_fp``, best first, as ``[{fingerprint, tier, why}]``.

    Under the default ``"ring"`` mode this is ``contender_order`` — ranked by each
    profile's optimistic ceiling against *this* defender's rating, so it is re-derived
    whenever the defender changes. The legacy ``"leaders"``/``"heirs"`` orders are mapped
    onto the same shape so the engine has one loop rather than one per mode.
    """
    if mode == "ring":
        return contender_order(field, ratings, defender_fp, baseline=baseline, heirs=heirs)
    fps = build_queue(
        field, heirs, defender_fp, contenders=mode, top_n=top_n, baseline=baseline, ratings=ratings
    )
    tiers = contender_tiers(field, fps, None, defender_fp)
    return [
        {
            "fingerprint": fp,
            "tier": tiers.get(fp, FILLER_TIER),
            "why": TIER_NAMES.get(tiers.get(fp, FILLER_TIER), "contender"),
        }
        for fp in fps
    ]


def next_challenger(
    session,
    field: dict,
    ratings: dict[str, dict],
    defender_fp: str,
    *,
    heirs: dict,
    baseline: list[dict] | None = None,
    rematch_days: int = 7,
    mode: str = "ring",
    top_n: int = 8,
    fought: set[frozenset[str]] | None = None,
) -> tuple[str | None, str]:
    """The single best next opponent for whoever currently holds the belt.

    Called **before every bout**, against ratings refit over the ledger including the bouts
    already fought in this session — so the ladder is always running the matchup most
    likely to unseat the current leader, not walking a running order decided hours ago
    against a defender that may since have been replaced.

    Two different exclusions, deliberately not the same strength:

    * ``fought`` — pairs already fought **in this session**. A hard skip: re-running the
      bout you just ran adds nothing, so this may drop to a lower tier.
    * the rematch cooldown — **orders within a tier, never excludes** (the fix from the
      "random duels not involving the crown" report). The leader and its nearest rivals are
      the first matchups to go on cooldown precisely because they are fought first, so
      treating the cooldown as an exclusion hands the ring to filler within a day.

    Returns ``(fingerprint, why)``; ``(None, "")`` once this defender has faced everything
    reachable in the session, which ends it — the next session refits and starts again.
    """
    fought = fought or set()
    order = [
        c
        for c in _challenger_order(
            field, ratings, defender_fp, mode=mode, heirs=heirs, baseline=baseline, top_n=top_n
        )
        if c["fingerprint"] != defender_fp
        and frozenset((defender_fp, c["fingerprint"])) not in fought
    ]
    if not order:
        return None, ""
    best = min(c["tier"] for c in order)
    tier = [c for c in order if c["tier"] == best]
    for c in tier:
        if not _recently_decided(session, defender_fp, c["fingerprint"], rematch_days):
            return c["fingerprint"], c["why"]
    c = tier[0]
    return c["fingerprint"], f"{c['why']} — re-raced (last decided within {rematch_days}d)"



# ── The operating model: always be trying to beat the profile holding the crown ──────
#
# For a long time the queue was ordered by the POOLED Overall, which made the ladder
# circular: the duel exists to be the independent check on the pooled verdict, and the
# pooled verdict decided who got checked. A profile the ring had proven strong stayed
# buried if its pooled score was mid-table; a pooled-flattered profile got first billing
# every session even after losing five bouts running.
#
# So the queue is now ordered by **the ring's own findings**, with one objective: run the
# bout most likely to unseat the profile currently holding the crown. Each contender is
# scored by its **optimistic ceiling** — the fitted rating plus `CEILING_SIGMA` standard
# errors, i.e. how good it could plausibly turn out to be. That single number does the
# right thing three ways at once:
#
# * a strong, well-established contender ranks high (high rating);
# * an unknown ranks high (wide error bar) — it might be anything, so go and look;
# * a well-measured weak profile ranks low and stays out of the way, because the ring has
#   already answered that question and re-answering it finds nothing.
#
# Pooled Overall keeps exactly one job: seeding profiles that have never been in the ring,
# so they can be ordered among themselves. It no longer decides who fights.
CEILING_SIGMA = 1.0
# What an unrated profile's error bar is taken to be. Wide on purpose: we know nothing
# about it head-to-head, so its ceiling should be high enough to earn a look.
UNRATED_SE = 150.0


def _pair_record(sessions_data: list[dict]) -> dict[tuple[str, str], int]:
    """``{(winner, loser): pairs won}`` over the ledger — the evidence the rating fits.

    The unit is the **pair**, not the bout: a hard-fought 12-8 carries more than a 3-0
    snap, and a drawn bout still informs the rating instead of being discarded.
    """
    out: dict[tuple[str, str], int] = {}
    for sess in sessions_data:
        for m in sess.get("matchups") or []:
            if not m or not m.get("incumbent") or not m.get("challenger"):
                continue
            inc, cha = str(m["incumbent"]), str(m["challenger"])
            for key, wins in (((inc, cha), m.get("wins_incumbent")), ((cha, inc), m.get("wins_challenger"))):
                n = int(wins or 0)
                if n > 0:
                    out[key] = out.get(key, 0) + n
    return out


def _ledger_sessions(session, limit_sessions: int = 50) -> list[dict]:
    rows = session.scalars(
        select(Duel).order_by(Duel.id.desc()).limit(max(1, min(int(limit_sessions or 50), 200)))
    ).all()
    return [
        {
            "id": d.id,
            "status": d.status.value if hasattr(d.status, "value") else str(d.status),
            "matchups": list(d.matchups or []),
            "champion_fingerprint": d.champion_fingerprint,
            "champion_label": d.champion_label,
            "finished_at": d.finished_at.isoformat() if d.finished_at else None,
        }
        for d in rows
    ]


def ledger_ratings(session, limit_sessions: int = 50) -> dict[str, dict]:
    """Each profile's fitted strength from the head-to-head ledger alone.

    The same fit the standings table ranks on, without the pooled join — so matchmaking
    can consult the ring's own verdict cheaply, every session.
    """
    return fit_bradley_terry(_pair_record(_ledger_sessions(session, limit_sessions)))


def contender_order(
    field: dict,
    ratings: dict[str, dict],
    incumbent_fp: str,
    *,
    baseline: list[dict] | None = None,
    heirs: dict | None = None,
) -> list[dict]:
    """Who should challenge the belt-holder, best chance of unseating it first.

    Returns ``[{fingerprint, tier, ceiling, rating, why}, …]`` in running order. The tiers
    exist so the ring is never handed to a lower one while a higher still has someone:

    * ``CROWN_TIER`` — the pooled crown. The two verdicts disagreeing is the single most
      informative bout in the system, so it goes first whenever it isn't already defending.
    * ``CONTENDER_TIER`` — rated profiles whose **ceiling clears the champion's rating**:
      on the ring's own evidence they could plausibly win. Ordered by that ceiling.
    * ``UNTESTED_TIER`` — no ring record, and its **pooled optimistic ceiling still reaches
      the pooled crown**: on paper it could displace the best profile found so far, and only
      the ring can say whether it does. This is where a freshly-explored profile lands after
      a five-iteration placement — thin, wide-banded, and precisely the thing worth an hour
      of the ring's time. Ordered by that ceiling, biggest potential threat first.
    * ``UNPROMISING_TIER`` — no ring record, and even optimistically its own runs fall short
      of the crown. Given up on, **not excluded**: five iterations is a weak "no", and the
      ring has not actually asked. Raced after everything with a live claim.

    The division of labour behind this: the pooled Overall is the winner *on paper*, and the
    ring is the real-world back-to-back result. Paper decides who gets to make a claim —
    which is all a thin profile's ceiling is, a claim — and the ring decides whether the
    claim survives contact. That is why an unrated profile is ranked on pooled evidence and
    a rated one never is: the moment the ring has an opinion, paper stops being consulted.
    * ``OUTCLASSED_TIER`` — rated, and even at their optimistic best they don't reach the
      champion. The ring has answered this; re-asking finds nothing. Not excluded — a long
      window still gets to them — just last, which is the same discipline the rematch
      cooldown follows.
    """
    profiles = {p["fingerprint"]: p for p in field.get("profiles", [])}
    pooled_fp = field.get("best_fingerprint")
    heir_set = {h.get("fingerprint") for h in ((heirs or {}).get("items") or [])}

    champ = ratings.get(incumbent_fp) or {}
    bar = champ.get("rating")
    # The pooled crown's Overall — the bar a profile with no ring record has to be able to
    # reach. Deliberately the *pooled* crown rather than the belt-holder: "could this displace
    # the best profile we have found so far?" is the question that decides whether a bout on a
    # thin profile is worth the ring's time.
    crown = profiles.get(pooled_fp or "") or {}
    crown_overall = crown.get("overall")

    def _ceiling(fp: str) -> tuple[float | None, float | None]:
        r = ratings.get(fp)
        if not r or r.get("rating") is None:
            return (None, None)
        return (r["rating"], r["rating"] + CEILING_SIGMA * (r.get("rating_se") or UNRATED_SE))

    out: list[dict] = []
    for fp, p in profiles.items():
        if fp == incumbent_fp:
            continue
        # A profile with no pooled score is still raceable — it's simply unknown twice over
        # (no ring record and no measured standing), which makes it a legitimate unknown to
        # go and measure, just the last of them. Requiring a pooled Overall here would put
        # the pooled verdict back in charge of who gets checked.
        # The heirs pass already applied the environment check; everything else is tested
        # against the live settings directly.
        if fp not in heir_set and fp != pooled_fp and not _reachable(p.get("settings"), baseline):
            continue
        rating, ceiling = _ceiling(fp)
        if fp == pooled_fp:
            tier, why = CROWN_TIER, "the pooled crown — the two verdicts disagreeing is the most informative match there is"
        elif rating is None:
            # No ring record — so the only question that can be asked is the pooled one, and
            # it is asked *optimistically*: not "is this better?" but "could this be better?".
            # A profile with five runs has a wide band, and its ceiling is what says whether
            # a bout could change the answer. Same number the heirs card and the challenger
            # race use, so all three agree on what counts as a threat.
            opt = p.get("optimistic")
            if crown_overall is None or opt is None or opt >= crown_overall:
                tier, why = LIVE_THREAT_TIER, (
                    f"thin but live — its pooled ceiling ({opt:.0f}) still reaches the crown "
                    f"({crown_overall:.0f}), so a match can change the answer"
                    if (opt is not None and crown_overall is not None)
                    else "never been in the ring, and nothing rules it out — worth a look"
                )
            else:
                tier, why = UNTESTED_TIER, (
                    f"its own runs say no — even optimistically it reads {opt:.0f} against a "
                    f"crown of {crown_overall:.0f}. Raced last, never excluded: that reading "
                    "is thin, and the ring has not actually asked it yet"
                )
        elif bar is None or (ceiling is not None and ceiling >= bar):
            tier, why = (
                CONTENDER_TIER,
                f"could plausibly beat the belt on its ring record (ceiling {ceiling:.0f} vs {bar:.0f})"
                if bar is not None
                else "the belt-holder has no ring record yet, so everything is a live contender",
            )
        else:
            tier, why = (
                OUTCLASSED_TIER,
                f"the ring already says it can't reach the belt (ceiling {ceiling:.0f} vs {bar:.0f}) — raced last",
            )
        out.append({
            "fingerprint": fp,
            "tier": tier,
            "rating": round(rating, 1) if rating is not None else None,
            "ceiling": round(ceiling, 1) if ceiling is not None else None,
            "pooled_overall": p.get("overall"),
            # The pooled optimistic ceiling, which is what orders the profiles the ring has
            # no rating for — the biggest *potential* threat first, exactly as the rated tier
            # is ordered by its ring ceiling.
            "pooled_ceiling": p.get("optimistic"),
            "why": why,
        })

    # Within a tier: by ceiling where we have one (best chance of dethroning first), by
    # pooled Overall where we don't (the only thing that distinguishes two unknowns).
    out.sort(
        key=lambda c: (
            c["tier"],
            -(c["ceiling"] if c["ceiling"] is not None else -1e9),
            -(c["pooled_ceiling"] if c["pooled_ceiling"] is not None else -1e9),
            -(c["pooled_overall"] or -1e9),
        )
    )
    return out


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
            method = str(cfg.get("method", "margins") or "margins").lower()
            streak_wins = int(cfg.get("streak_wins", 0) or 0)
            p1 = float(cfg.get("p1", 0.70) or 0.70)
            alpha = float(cfg.get("alpha", 0.05) or 0.05)
            min_pairs = int(cfg.get("min_pairs", 10) or 10)
            max_pairs = int(cfg.get("max_pairs", 40) or 40)
            min_margin = float(cfg.get("min_margin", 1.0) or 0.0)
            rematch_days = int(cfg.get("rematch_days", 7) or 7)
            settle_s = max(0, int(cfg.get("settle_seconds", 3) or 0))

            # Matchmaking, re-decided BEFORE EVERY BOUT rather than once a session: the
            # ring's current #1 defends, against whichever profile the ledger says is most
            # likely to beat *it*. Deciding this once up front is what produced "random
            # duels" — a queue chosen hours ago against a defender that had since been
            # replaced, walked to the end regardless of what the bouts in between found.
            _set_stage(duel_id, "Ranking the field for matchmaking")
            with session_scope() as session:
                field = compute_profiles(session)
                heirs = _compute_heirs(field, session, baseline)
            settings_by_fp = {p["fingerprint"]: p for p in field.get("profiles", [])}
            mode = str(cfg.get("contenders", "ring") or "ring")
            top_n = int(cfg.get("contender_top_n", 8) or 8)

            deadline = time.monotonic() + duration_s
            matchups: list[dict] = []
            # Pairs already fought in this session — a hard skip, so the ladder moves down
            # the threat list instead of re-running the bout it just ran.
            fought: set[frozenset[str]] = set()
            # …with one exception, tracked here so it can only be taken once per pair.
            rematched: set[frozenset[str]] = set()
            rematch_now: frozenset[str] | None = None
            incumbent_fp: str | None = None

            while time.monotonic() < deadline and not _state.get("cancel"):
                # Refit the ring's own verdict over the whole ledger INCLUDING this
                # session's bouts (`_ledger_sessions` reads the running duel), so a
                # challenger that just won is re-rated before the next matchup is chosen.
                with session_scope() as session:
                    ratings = ledger_ratings(session)
                    defender_fp, defender_why = select_incumbent(
                        session, field, baseline, cfg, ratings
                    )
                    if defender_fp is None or defender_fp not in settings_by_fp:
                        if not matchups:
                            raise RuntimeError(
                                "No confident profile to defend — nothing to duel."
                            )
                        break
                    # **Unfinished business gets its rematch.** A challenger that WON its
                    # bout but didn't take the belt — its floor hasn't cleared the
                    # leader's yet — has raised the most informative question on the
                    # ledger: its record says it is the better profile and the standings
                    # still say the other one is. Setting that pair aside for the rest of
                    # the session (as the plain "already fought" skip does) leaves exactly
                    # the profile most likely to dethrone the leader unable to finish the
                    # job, which is the "two profiles beat this one but it keeps the belt"
                    # report. So the pair is re-opened — once, so the ladder can't
                    # ping-pong on it — and its freshly-raised rating puts it near the top
                    # of the order on its own.
                    rematch_now = None
                    if matchups:
                        last = matchups[-1]
                        pair = frozenset((last["incumbent"], last["challenger"]))
                        # Only when the LEDGER is what put this profile in the ring. On the
                        # pooled fallback (nothing rated and reachable) "the belt didn't
                        # move" says nothing about the head-to-head record, so there is no
                        # unfinished business to re-open.
                        if (
                            last["verdict"] == "challenger"
                            and defender_fp == last["incumbent"]
                            and defender_fp == ledger_leader(ratings)
                            and pair not in rematched
                        ):
                            rematched.add(pair)
                            fought.discard(pair)
                            rematch_now = pair
                    challenger_fp, why_challenger = next_challenger(
                        session,
                        field,
                        ratings,
                        defender_fp,
                        heirs=heirs,
                        baseline=baseline,
                        rematch_days=rematch_days,
                        mode=mode,
                        top_n=top_n,
                        fought=fought,
                    )
                if challenger_fp is None or challenger_fp not in settings_by_fp:
                    if not matchups:
                        raise RuntimeError(
                            _no_contenders_reason(field, heirs, defender_fp, baseline)
                        )
                    break
                if rematch_now == frozenset((defender_fp, challenger_fp)):
                    why_challenger = (
                        f"{why_challenger} — rematch: it won the last match without taking "
                        f"the belt"
                    )
                if defender_fp != incumbent_fp:
                    log.info("Duel %s: %s (%s)", duel_id, defender_why, defender_fp)
                incumbent_fp = defender_fp
                # The belt names whoever is actually in the ring, from the first pair on.
                holder = settings_by_fp.get(incumbent_fp) or {}
                with session_scope() as session:
                    d = session.get(Duel, duel_id)
                    if d is not None:
                        d.champion_fingerprint = incumbent_fp
                        d.champion_label = holder.get("name") or holder.get("label")
                log.info(
                    "Duel %s bout %s: challenger %s (%s)",
                    duel_id, len(matchups) + 1, challenger_fp, why_challenger,
                )
                fought.add(frozenset((incumbent_fp, challenger_fp)))
                inc = settings_by_fp[incumbent_fp]
                cha = settings_by_fp[challenger_fp]
                sprt = SprtState(p1, alpha)
                # Magnitude-aware adjudicator. The SPRT walk still runs alongside it: the
                # sign test is a poor winner-detector but a fine FUTILITY detector, and
                # its "pair wins are ~50/50" exit is what stops a settled tie from eating
                # the rest of the window.
                paired = PairedEvidence(
                    alpha, min_margin, min_pairs, max_pairs, streak_wins=streak_wins
                )
                deltas: list[float] = []
                bad_streak = 0
                verdict: str | None = None
                reason = ""

                while verdict is None and time.monotonic() < deadline and not _state.get("cancel"):
                    # Zipper merge. The ladder holds the coordination lock for its whole
                    # window — hours — so anything a user starts meanwhile (an Explore
                    # "Test now", a manual run) would otherwise queue behind the entire
                    # night. Between pairs nothing is in flight, so this is the seam: let
                    # ONE queued session through, then take the ring back for the next
                    # pair, then look again. Within-pair adjacency — the thing that makes
                    # the two legs share their weather — is untouched, because a pair is
                    # never interrupted once it starts.
                    #
                    # The yielded time is deliberately NOT added back to the deadline: a
                    # nightly window is a wall-clock agreement about when the ladder may
                    # run, and quietly running past 05:00 to make up for a detour would
                    # break the thing the window is for.
                    yielded = coordinator.yield_if_waiting(f"duel#{duel_id}")
                    if yielded:
                        log.info(
                            "Duel %s: stepped aside for %.0fs of queued work", duel_id, yielded
                        )
                        if time.monotonic() >= deadline or _state.get("cancel"):
                            break
                    # Name the bout AND why this challenger is in the ring — "who are these
                    # two and how did they get here?" is the question a live readout has to
                    # answer, and a bare pair of names doesn't.
                    _set_stage(
                        duel_id,
                        f"Match {len(matchups) + 1} · "
                        f"{inc.get('name') or inc['label']} (belt) defends vs "
                        f"{cha.get('name') or cha['label']} ({why_challenger}) — round "
                        f"{sprt.pairs + 1} ({sprt.wins_incumbent}-{sprt.wins_challenger})",
                    )
                    # …and the same bout as structured state, so the page can say who is
                    # ahead and by how much rather than printing an unattributed scoreline.
                    _set_live(duel_id, _live_scoreboard(
                        bout=len(matchups) + 1, inc=inc, cha=cha, sprt=sprt, paired=paired,
                        why_challenger=why_challenger, min_pairs=min_pairs,
                        max_pairs=max_pairs, min_margin=min_margin,
                        streak_needed=streak_to_decide(alpha, min_pairs, max_pairs, streak_wins),
                    ))
                    # ABBA, not ABAB. The incumbent used to run first in every single
                    # pair, which makes "goes first" and "is the incumbent" the same
                    # variable: any position-in-pair effect — the state the previous run
                    # left behind, a cache still warm, the shaper freshly reconfigured —
                    # lands on the same side every time and is indistinguishable from a
                    # real difference between the profiles. Alternating which side leads
                    # cancels it instead of hoping it's zero. The margin is always
                    # challenger − incumbent, so the verdict doesn't care who ran first.
                    lead_incumbent = sprt.pairs % 2 == 0
                    order = (
                        ((incumbent_fp, inc), (challenger_fp, cha))
                        if lead_incumbent
                        else ((challenger_fp, cha), (incumbent_fp, inc))
                    )
                    scored: dict[str, float | None] = {}
                    for side_fp, side in order:
                        _apply_profile(provider, side["settings"], side_fp)
                        # Let the link settle after the reconfigure before believing what
                        # we measure (duel.settle_seconds; 0 = measure immediately).
                        if settle_s > 0:
                            time.sleep(settle_s)
                        run_id, ok, completed = run_chunk(
                            label=f"duel · {side.get('name') or side['label']}",
                            notes=f"Duel #{duel_id}: {inc['label']} vs {cha['label']}",
                            iterations=1,
                            teardown=False,  # keep Chromium warm across the whole ladder
                            job_group=f"duel-{duel_id}",
                        )
                        run_ids.append(run_id)
                        iterations_run += completed
                        scored[side_fp] = _run_overall(run_id, meth_version) if ok else None
                    pair_overalls = [scored[incumbent_fp], scored[challenger_fp]]
                    with session_scope() as session:
                        d = session.get(Duel, duel_id)
                        if d is not None:
                            d.run_ids = list(run_ids)
                            d.iterations_run = iterations_run
                    inc_val, cha_val = pair_overalls
                    if inc_val is None or cha_val is None:
                        bad_streak += 1
                        if bad_streak >= MAX_CONSECUTIVE_BAD_PAIRS:
                            verdict, reason = "draw", "aborted: repeated unusable rounds"
                        continue
                    bad_streak = 0
                    delta = cha_val - inc_val
                    deltas.append(delta)
                    sprt.add_pair(delta > 0)
                    paired.add(delta)

                    if method == "pair_wins":
                        # Legacy: adjudicate on which side won each pair, magnitudes ignored.
                        verdict = sprt.decision(min_pairs, max_pairs)
                        if verdict in ("challenger", "incumbent"):
                            if abs(_median(deltas)) < min_margin:
                                verdict, reason = "draw", (
                                    f"boundary crossed but |median Δ| < {min_margin} — practically equal"
                                )
                            else:
                                reason = "SPRT boundary crossed"
                        elif verdict == "draw":
                            reason = (
                                "mutual futility (round wins ~50/50)"
                                if sprt.pairs < max_pairs
                                else f"no decision in {max_pairs} rounds"
                            )
                    else:
                        # Default: decide on the paired MARGINS (signed-rank), which is
                        # where the evidence actually lives; fall back to the sign test's
                        # futility exit and the pair cap for draws.
                        verdict = paired.decision()
                        if verdict is not None:
                            reason = (
                                f"margins consistently one-sided "
                                f"(p={paired.p_value(1 if verdict == 'challenger' else -1):.4f} "
                                f"≤ {paired.nominal_alpha:.4f}, median Δ "
                                f"{_median(deltas):+.2f})"
                            )
                        else:
                            # No winner yet. Three ways a bout still ends — and the pair cap
                            # is checked HERE rather than being inherited from the sign
                            # test, which can sit on a "winner" verdict the margin floor
                            # keeps rejecting and so never terminates the loop.
                            sign_verdict = sprt.decision(min_pairs, max_pairs)
                            below_floor = (
                                sign_verdict in ("challenger", "incumbent")
                                and abs(_median(deltas)) < min_margin
                            )
                            if below_floor:
                                verdict = "draw"
                                reason = (
                                    f"one side wins the rounds, but by < {min_margin} "
                                    f"Overall pts — practically equal"
                                )
                            elif sign_verdict == "draw":
                                verdict = "draw"
                                reason = "mutual futility (no consistent margin either way)"
                            elif paired.pairs >= max_pairs:
                                verdict = "draw"
                                reason = f"no decision in {max_pairs} rounds"

                if verdict is None:
                    verdict, reason = "draw", "window closed mid-matchup (undecided)"
                record = {
                    "incumbent": incumbent_fp,
                    "challenger": challenger_fp,
                    "incumbent_label": inc["label"],
                    "challenger_label": cha["label"],
                    # Call signs are what the tape is read in ("Speedy Sloth vs Quantum
                    # Quasar"); the technical labels stay beside them so a bout still says
                    # which settings actually fought.
                    "incumbent_name": inc.get("name"),
                    "challenger_name": cha.get("name"),
                    # Why this challenger got the ring — the tape should answer "why these
                    # two?" without the reader having to reconstruct the matchmaking.
                    "challenger_why": why_challenger,
                    # Pairs alternate which profile runs first (ABBA), so "went first" is
                    # not confounded with "is the incumbent". Recorded so the counterbalance
                    # is auditable from the ledger rather than taken on trust.
                    "lead_alternated": True,
                    "pairs": sprt.pairs,
                    "wins_incumbent": sprt.wins_incumbent,
                    "wins_challenger": sprt.wins_challenger,
                    "median_delta": round(_median(deltas), 2) if deltas else None,
                    "llr_incumbent": round(sprt.llr_incumbent, 2),
                    "llr_challenger": round(sprt.llr_challenger, 2),
                    # How the bout was judged, and the evidence that judged it.
                    "method": method,
                    "p_value": (
                        round(min(paired.p_value(1), paired.p_value(-1)), 5) if deltas else None
                    ),
                    "alpha_used": round(paired.nominal_alpha, 5),
                    "verdict": verdict,
                    "reason": reason,
                }
                matchups.append(record)
                # Persist the bout, which is also what the next loop's rating refit reads:
                # who defends next is re-derived from the ledger, not carried over. So the
                # winner does not simply "stay on" — beating the leader promotes you when
                # it lifts your floor above its, the same bar the standings apply, which is
                # what keeps the belt and the #1 row from ever naming different profiles.
                with session_scope() as session:
                    d = session.get(Duel, duel_id)
                    if d is not None:
                        d.matchups = list(matchups)
                log.info(
                    "Duel %s verdict: %s vs %s → %s (%s; pairs=%s Δmed=%s)",
                    duel_id, inc["label"], cha["label"], verdict, reason,
                    sprt.pairs, record["median_delta"],
                )

            # The session's champion is the ring's #1 over the ledger this session just
            # extended — the same profile the standings put at the top, so the belt on the
            # page and the row underneath it can't disagree.
            with session_scope() as session:
                # Deliberately NOT reachability-filtered, unlike the choice of who defends:
                # the champion is a statement about the ledger, and the standings it has to
                # agree with aren't filtered either. A champion the live environment can't
                # be set to simply never defends, and the crown follower already refuses to
                # apply an unreachable profile.
                final_fp, _ = ring_leader(field, ledger_ratings(session))
                final_fp = final_fp or incumbent_fp
                champion = settings_by_fp.get(final_fp) or {}
                d = session.get(Duel, duel_id)
                if d is not None and final_fp is not None:
                    d.champion_fingerprint = final_fp
                    d.champion_label = champion.get("name") or champion.get("label")
            if _state.get("cancel"):
                final_status = DuelStatus.CANCELLED

            # Always restore the pre-duel baseline: the duel adjudicates, it never
            # promotes. Applying the champion is the crown follower's job under the
            # crowning policy (crown_follow.policy = "duel").
            _set_live(duel_id, None)  # no bout in progress any more
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
        try:  # let continuous mode leave a gap before the next session
            from . import scheduler

            scheduler._state["duel_last_finished"] = time.monotonic()
        except Exception:  # noqa: BLE001 — bookkeeping must never break the duel
            log.debug("Duel: could not stamp finish time for continuous mode", exc_info=True)
        # A fresh verdict may change what the crowning policy resolves to.
        try:
            from . import crown_follower

            crown_follower.poke("duel verdict")
        except Exception:  # noqa: BLE001
            log.debug("Duel: crown follower poke failed", exc_info=True)
        log.info("Duel %s finished: %s", duel_id, final_status.value)


# ── The fight card (who fights whom, before a duel starts) ───────────────────────────


def fight_card(session, limit: int = 12) -> dict:
    """The matchups a duel started right now would run, in order, if nothing upsets them.

    "Are we just racing randoms?" is a fair question to ask of any ladder, and the honest
    answer is a list, not a paragraph — so this builds the queue with exactly the code the
    engine uses (``build_queue`` over ``compute_profiles`` + ``_compute_heirs``) and hands
    it back with each contender's standing and why it's there. Anything on rematch cooldown
    is marked rather than silently skipped.

    The **first** entry is exactly what the engine would fight, because both call the same
    ``select_incumbent`` and the same ordering. The rest is a projection rather than a
    schedule: the engine re-decides the defender and the challenger from the ledger before
    every bout, so an upset in bout 1 re-orders everything after it. That is the point of
    the ladder, not a caveat about the preview.

    Costs a ``compute_profiles`` pass, so it's fetched on demand rather than on page load.
    """
    from .api.routes_settings import _compute_heirs, compute_profiles
    from .providers import get_provider

    cfg = _duel_config(session)
    live = None
    try:
        live = normalize(get_provider().discover())
    except Exception:  # noqa: BLE001 — reachability is a filter, not a hard requirement
        log.debug("Fight card: could not read live settings", exc_info=True)

    field = compute_profiles(session)
    heirs = _compute_heirs(field, session, live)
    profiles = {p["fingerprint"]: p for p in field.get("profiles", [])}
    # Exactly the engine's choice of who defends, so the preview can't promise a different
    # champion than the one that actually walks out.
    incumbent_fp, incumbent_why = select_incumbent(session, field, live, cfg)
    ratings = ledger_ratings(session)
    if incumbent_fp is None or incumbent_fp not in profiles:
        return {
            "incumbent": None,
            "queue": [],
            "contenders": str(cfg.get("contenders", "ring") or "ring"),
            "top_n": int(cfg.get("contender_top_n", 8) or 8),
            "reason": "No confident profile to defend yet — collect more iterations.",
        }

    heir_reason = {
        h["fingerprint"]: h.get("reason") for h in (heirs.get("items") or []) if h.get("fingerprint")
    }
    mode = str(cfg.get("contenders", "ring") or "ring")
    order = build_queue(
        field,
        heirs,
        incumbent_fp,
        contenders=mode,
        top_n=int(cfg.get("contender_top_n", 8) or 8),
        baseline=live,
        ratings=ratings,
    )
    # The ring's reason for each entry — so the preview explains the running order in the
    # ladder's own terms rather than restating the pooled standings.
    ring_why = {
        c["fingerprint"]: c
        for c in contender_order(field, ratings, incumbent_fp, baseline=live, heirs=heirs)
    }
    if not order:
        return {
            "incumbent": None,
            "queue": [],
            "contenders": str(cfg.get("contenders", "ring") or "ring"),
            "top_n": int(cfg.get("contender_top_n", 8) or 8),
            "reason": _no_contenders_reason(field, heirs, incumbent_fp, live),
        }
    rematch_days = int(cfg.get("rematch_days", 7) or 7)

    tiers = contender_tiers(field, order, ratings if mode == "ring" else None, incumbent_fp)

    def _entry(fp: str, position: int) -> dict:
        p = profiles.get(fp, {})
        return {
            "position": position,
            "fingerprint": fp,
            "name": p.get("name"),
            "label": p.get("label"),
            "overall": p.get("overall"),
            "iterations": p.get("iterations"),
            "confident": p.get("confident"),
            # Why this profile is in the queue at all.
            "reason": (
                "pooled-crown"
                if fp == pooled_fp
                else heir_reason.get(fp)
                or ("contender" if p.get("overall") is not None else "untested")
            ),
            # What the RING says about it: its fitted rating, the optimistic ceiling the
            # queue is ordered by, and why it sits where it does.
            "rating": (ring_why.get(fp) or {}).get("rating"),
            "ceiling": (ring_why.get(fp) or {}).get("ceiling"),
            "ring_why": (ring_why.get(fp) or {}).get("why"),
            # Its priority tier — the ring is never given to a lower tier while a higher
            # one still has someone waiting, so this is the real running order.
            "tier": tiers.get(fp, FILLER_TIER),
            "tier_name": TIER_NAMES.get(tiers.get(fp, FILLER_TIER), "untested"),
            # Fought inside the rematch window. It is NOT skipped for that any more — the
            # cooldown only decides the order among equals, so this bout still runs (last
            # within its tier) rather than handing the ring to an unmeasured profile.
            "on_cooldown": _recently_decided(session, incumbent_fp, fp, rematch_days),
        }

    inc = profiles[incumbent_fp]
    pooled_fp = field.get("best_fingerprint")
    return {
        "incumbent": {
            "fingerprint": incumbent_fp,
            "name": inc.get("name"),
            "label": inc.get("label"),
            "overall": inc.get("overall"),
            "iterations": inc.get("iterations"),
            # Why this profile is the one defending — champion carrying its belt in, or
            # the pooled crown standing in because there's no fresh champion.
            "why": incumbent_why,
            "is_duel_champion": incumbent_fp != pooled_fp,
        },
        "queue": [_entry(fp, i + 1) for i, fp in enumerate(order[: max(int(limit), 1)])],
        "total": len(order),
        "contenders": mode,
        "top_n": int(cfg.get("contender_top_n", 8) or 8),
        "rematch_days": rematch_days,
        # The belt-holder's own ring rating — the bar every ceiling above is measured against.
        "incumbent_rating": (ratings.get(incumbent_fp) or {}).get("rating"),
        "reason": None,
    }


# ── Ledger accessors ─────────────────────────────────────────────────────────────────


def latest_champion(session, max_age_days: int) -> dict | None:
    """The ring's #1 over the whole ledger — the reigning duel champion — if fresh enough.

    This used to read the newest completed session's stored ``champion_fingerprint``, which
    is the profile that *survived that session*. Two things then guaranteed it would
    disagree with the standings, which are fitted live over the entire ledger: rows written
    before the belt became the ring's #1 recorded a survivor, and a bout in a running
    session moves the table without touching any stored row. The champion is now derived
    from the same fit the table ranks on (``ledger_leader`` over ``ledger_ratings``), so
    "reigning champion" and "row 1" cannot name different profiles.

    Deliberately NOT reachability-filtered — the champion is a claim about the ledger, like
    the standings. The choice of who *defends* is filtered (``ring_leader``), and the crown
    follower independently refuses to apply an unreachable profile.

    Returns ``{fingerprint, label, duel_id, finished_at, decisive, consecutive_sessions,
    provisional}`` or None. The gates that automation depends on are kept, translated onto
    the ledger: ``decisive`` is True when this profile has at least one non-draw verdict (a
    record made entirely of draws demonstrates nothing over the pooled verdict), and a
    champion whose most recent bout is older than ``max_age_days`` is reported as None —
    stale evidence must not drive a firewall write.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max_age_days)
    sessions_data = _ledger_sessions(session)
    ratings = fit_bradley_terry(_pair_record(sessions_data))
    fp = ledger_leader(ratings)
    if fp is None:
        return None

    label: str | None = None
    duel_id: int | None = None
    finished_at: str | None = None
    last_bout: datetime | None = None
    decisive = False
    for sess in sessions_data:  # newest first
        involved = False
        for m in sess.get("matchups") or []:
            if fp not in (m.get("incumbent"), m.get("challenger")):
                continue
            involved = True
            if (m or {}).get("verdict") != "draw":
                decisive = True
            if label is None:
                label = (
                    m.get("incumbent_name") or m.get("incumbent_label")
                    if m.get("incumbent") == fp
                    else m.get("challenger_name") or m.get("challenger_label")
                )
        if not involved or sess["status"] != "complete":
            continue
        when = _parse_finished(sess.get("finished_at"))
        if last_bout is None and when is not None:
            last_bout, duel_id, finished_at = when, sess["id"], sess["finished_at"]

    # Freshness is a property of the EVIDENCE, not of who filed it: a champion nobody has
    # raced in a week is a stale verdict however recently some other session finished.
    if last_bout is None or last_bout < cutoff:
        return None

    # How long it has held the top of the table, in completed sessions.
    reign = 0
    for sess in sessions_data:
        if sess["status"] != "complete" or not (sess.get("matchups") or []):
            continue
        if sess.get("champion_fingerprint") != fp:
            break
        reign += 1
    return {
        "fingerprint": fp,
        "label": label,
        "duel_id": duel_id,
        "finished_at": finished_at,
        "decisive": decisive,
        "consecutive_sessions": reign,
        "provisional": bool(ratings[fp].get("provisional", True)),
    }


def _parse_finished(value: str | None) -> datetime | None:
    """A ledger session's ``finished_at`` as a naive-UTC datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _matchup_sides(m: dict) -> list[tuple[str, str, str, float | None, int, int]]:
    """Split one matchup record into (fingerprint, label, result, margin, pair_wins, pair_losses)
    for **both** sides.

    ``median_delta`` is stored challenger-minus-incumbent, so each side's margin is signed from
    its own point of view (positive = it was the better profile in that ring).
    """
    verdict = m.get("verdict")
    delta = m.get("median_delta")
    delta = float(delta) if isinstance(delta, (int, float)) else None
    inc_wins = int(m.get("wins_incumbent") or 0)
    cha_wins = int(m.get("wins_challenger") or 0)
    inc_result = "draw" if verdict == "draw" else ("win" if verdict == "incumbent" else "loss")
    cha_result = "draw" if verdict == "draw" else ("win" if verdict == "challenger" else "loss")
    return [
        (
            str(m.get("incumbent")),
            str(m.get("incumbent_label") or m.get("incumbent")),
            inc_result,
            (-delta if delta is not None else None),
            inc_wins,
            cha_wins,
        ),
        (
            str(m.get("challenger")),
            str(m.get("challenger_label") or m.get("challenger")),
            cha_result,
            delta,
            cha_wins,
            inc_wins,
        ),
    ]


def _pooled_overalls(session, fingerprints) -> dict[str, tuple[float | None, int]]:
    """``{fingerprint: (pooled Overall, iterations)}`` for the profiles in the standings.

    The pooled Overall is the *observational* score — everything that profile has ever
    measured — while the standings around it are its *head-to-head* record. Showing both
    on one row is the point: they answer different questions and their disagreement is
    information, not an error.
    """
    from .config_store import get_config
    from .crown_follower import profile_overalls
    from .methodology import ensure_current_methodology, overall_metrics, overall_weights

    methodology = ensure_current_methodology(session, get_config(session))
    definition = methodology.definition or {}
    crown_metrics, crown_required = overall_metrics(definition)
    weights = overall_weights(definition)
    try:
        return profile_overalls(
            session, fingerprints, methodology.version, crown_metrics, crown_required, weights
        )
    except Exception:  # noqa: BLE001 — a scoring hiccup must not break the table
        log.debug("Standings: could not compute pooled Overalls", exc_info=True)
        return {}


def standings(limit_sessions: int = 50) -> dict:
    """The **head-to-head league table** — every profile's record earned in the ring.

    This is the duel ladder's own verdict surface and deliberately shares nothing with the
    pooled crown: no averages over history, no weather adjustment, no percentile field —
    only decided matchups, each of which was interleaved A/B/A/B so both sides met the same
    weather. A profile ranks by what it *beat*, not by what it averaged.

    Returns ``{champion, standings, head_to_head, sessions_analyzed, matchups_analyzed,
    decisive_matchups, generated_from}``. Ranking: match points (win 3 / draw 1), then
    decisive-win rate, then pair-win rate, then matchups played.
    """
    limit_sessions = max(1, min(int(limit_sessions or 50), 200))
    with session_scope() as session:
        rows = session.scalars(
            select(Duel).order_by(Duel.id.desc()).limit(limit_sessions)
        ).all()
        sessions_data = [
            {
                "id": d.id,
                "status": d.status.value if hasattr(d.status, "value") else str(d.status),
                "matchups": list(d.matchups or []),
                "champion_fingerprint": d.champion_fingerprint,
                "champion_label": d.champion_label,
                "finished_at": d.finished_at.isoformat() if d.finished_at else None,
            }
            for d in rows
        ]

    records: dict[str, dict] = {}
    h2h: dict[str, dict[str, dict]] = {}
    # The pairwise record the Bradley-Terry rating is fitted to: every interleaved pair
    # ever run, keyed (winner, loser). The unit of evidence is the PAIR rather than the
    # bout, so a hard-fought 12-8 counts for more than a 3-0 snap and a drawn bout still
    # informs the rating instead of being thrown away.
    pair_wins: dict[tuple[str, str], int] = {}
    matchups_analyzed = 0
    decisive = 0

    # Oldest → newest so "last seen" / label freshness resolve to the most recent sighting.
    for sess in reversed(sessions_data):
        for m in sess["matchups"]:
            if not m or not m.get("incumbent") or not m.get("challenger"):
                continue
            matchups_analyzed += 1
            if m.get("verdict") != "draw":
                decisive += 1
            sides = _matchup_sides(m)
            inc_fp, cha_fp = str(m.get("incumbent")), str(m.get("challenger"))
            if int(m.get("wins_incumbent") or 0) > 0:
                key = (inc_fp, cha_fp)
                pair_wins[key] = pair_wins.get(key, 0) + int(m.get("wins_incumbent") or 0)
            if int(m.get("wins_challenger") or 0) > 0:
                key = (cha_fp, inc_fp)
                pair_wins[key] = pair_wins.get(key, 0) + int(m.get("wins_challenger") or 0)
            for idx, (fp, label, result, margin, side_pair_wins, pair_losses) in enumerate(sides):
                opp_fp, opp_label = sides[1 - idx][0], sides[1 - idx][1]
                rec = records.setdefault(
                    fp,
                    {
                        "fingerprint": fp,
                        "label": label,
                        "matchups": 0,
                        "wins": 0,
                        "losses": 0,
                        "draws": 0,
                        "pair_wins": 0,
                        "pair_losses": 0,
                        "margins": [],
                        "beaten": [],
                        "lost_to": [],
                        "opponents": set(),
                        "championships": 0,
                        "last_dueled_at": None,
                        "last_duel_id": None,
                    },
                )
                rec["label"] = label
                rec["matchups"] += 1
                rec[{"win": "wins", "loss": "losses", "draw": "draws"}[result]] += 1
                rec["pair_wins"] += side_pair_wins
                rec["pair_losses"] += pair_losses
                rec["opponents"].add(opp_fp)
                if margin is not None:
                    rec["margins"].append(margin)
                if result == "win" and opp_fp not in {fp for fp, _ in rec["beaten"]}:
                    rec["beaten"].append((opp_fp, opp_label))
                if result == "loss" and opp_fp not in {fp for fp, _ in rec["lost_to"]}:
                    rec["lost_to"].append((opp_fp, opp_label))
                rec["last_dueled_at"] = sess["finished_at"] or rec["last_dueled_at"]
                rec["last_duel_id"] = sess["id"]

                cell = h2h.setdefault(fp, {}).setdefault(
                    opp_fp,
                    {"wins": 0, "losses": 0, "draws": 0, "pairs": 0, "margins": []},
                )
                cell[{"win": "wins", "loss": "losses", "draw": "draws"}[result]] += 1
                cell["pairs"] += side_pair_wins + pair_losses
                if margin is not None:
                    cell["margins"].append(margin)

        fp = sess.get("champion_fingerprint")
        if fp and sess["status"] == "complete":
            rec = records.get(fp)
            if rec is not None:
                rec["championships"] += 1

    table: list[dict] = []
    for rec in records.values():
        decided = rec["wins"] + rec["losses"]
        pairs = rec["pair_wins"] + rec["pair_losses"]
        table.append(
            {
                "fingerprint": rec["fingerprint"],
                "label": rec["label"],
                "matchups": rec["matchups"],
                "wins": rec["wins"],
                "losses": rec["losses"],
                "draws": rec["draws"],
                "points": rec["wins"] * 3 + rec["draws"],
                "win_rate": round(rec["wins"] / decided, 3) if decided else None,
                "pair_wins": rec["pair_wins"],
                "pair_losses": rec["pair_losses"],
                "pair_win_rate": round(rec["pair_wins"] / pairs, 3) if pairs else None,
                "median_margin": round(_median(rec["margins"]), 2) if rec["margins"] else None,
                "opponents": len(rec["opponents"]),
                "beaten_pairs": rec["beaten"],
                "lost_to_pairs": rec["lost_to"],
                "championships": rec["championships"],
                # Settled after the sort — the champion is row 1, so it can't be known yet.
                "is_champion": False,
                "last_dueled_at": rec["last_dueled_at"],
                "last_duel_id": rec["last_duel_id"],
            }
        )
    # Resolve call signs by FINGERPRINT rather than trusting the label frozen into each
    # matchup: rows recorded before naming (or before a rename) then read under the same
    # name as everywhere else, so the league table and the standings can't disagree.
    #
    # Each row also carries its POOLED Overall — the all-history measured score — so the
    # ring record and the raw record can be read against each other in one table. That's
    # the interesting comparison: a profile winning its bouts while sitting mid-table on
    # Overall (or the reverse) is exactly what the two-verdict design exists to surface.
    # Computed per profile via the crown follower's single-profile accessor (one indexed
    # query each) rather than a full `compute_profiles` pass, keeping the standings cheap.
    with session_scope() as session:
        call_signs = profile_names.names_for(session, [r["fingerprint"] for r in table])
        overalls = _pooled_overalls(session, [r["fingerprint"] for r in table])
    for row in table:
        row["name"] = call_signs.get(row["fingerprint"]) or row["label"]
        pooled = overalls.get(row["fingerprint"]) or (None, 0)
        row["overall"], row["pooled_iterations"] = pooled
        row["beaten"] = [call_signs.get(fp, lbl) for fp, lbl in row["beaten_pairs"]]
        row["lost_to"] = [call_signs.get(fp, lbl) for fp, lbl in row["lost_to_pairs"]]
        del row["beaten_pairs"], row["lost_to_pairs"]
    # ── The rating ───────────────────────────────────────────────────────────────
    # Ranking used to be match points (3/1/0), which records how many you beat but not
    # WHO: beating the champion and beating a profile nobody has measured were both worth
    # three, the belt-holder farmed points simply by defending (the winner stays on, so it
    # fights more than anyone), and two profiles that never met could not be compared at
    # all. The Bradley-Terry fit answers all three from the same ledger — see rating.py.
    ratings = fit_bradley_terry(pair_wins)
    for row in table:
        r = ratings.get(row["fingerprint"]) or {}
        row["rating"] = r.get("rating")
        row["rating_se"] = r.get("rating_se")
        # The conservative floor is what the table ranks on — see rating.RANK_SIGMA.
        row["rating_floor"] = r.get("rating_floor")
        row["rating_pairs"] = r.get("pairs")
        row["rating_provisional"] = bool(r.get("provisional", True))
        # How many pairs the fit expected this profile to win against the exact opponents
        # it actually faced — "beating your schedule" in one number.
        row["expected_pair_wins"] = r.get("expected_wins")

    # Ranked on the CONSERVATIVE floor, not the fitted rating. The four best profiles in a
    # real field sat within 55 points of each other with error bars of 50-120: ordering them
    # by the point estimate put a five-pair record on top of a forty-four-pair one on a
    # difference smaller than either bar, which is presenting noise as a standing. The floor
    # asks what a record has *demonstrated*, so evidence has to be earned rather than
    # borrowed from one lucky bout. The fitted rating stays the headline number and stays
    # sortable — this changes which claim the default order is making.
    table.sort(
        key=lambda r: (
            r["rating_floor"] if r["rating_floor"] is not None else -1e9,
            r["rating_pairs"] or 0,  # more evidence first among equal floors
            r["points"],
        ),
        reverse=True,
    )
    for i, row in enumerate(table, start=1):
        row["rank"] = i

    # The reigning champion IS row 1 — not a stored `champion_fingerprint` from whichever
    # session finished last. A badge that names a different profile than the table beneath
    # it is a bug, and it was one: the stored value recorded the session's survivor, and
    # was written at session end, so a running ladder moved the table and not the badge.
    # Taking it from the sorted table makes disagreement structurally impossible.
    champion = None
    if table:
        top = table[0]
        reign = 0
        for sess in sessions_data:  # newest first
            if sess["status"] != "complete" or not (sess.get("matchups") or []):
                continue
            if sess.get("champion_fingerprint") != top["fingerprint"]:
                break
            reign += 1
        champion = {
            "fingerprint": top["fingerprint"],
            "name": top.get("name"),
            "label": top.get("label"),
            "duel_id": top.get("last_duel_id"),
            "finished_at": top.get("last_dueled_at"),
            "consecutive_sessions": reign,
            # Has it actually beaten anyone, or is its record all draws?
            "decisive": bool(top.get("wins") or top.get("losses")),
            "provisional": bool(top.get("rating_provisional", True)),
        }
        top["is_champion"] = True

    matrix = {
        fp: {
            opp: {
                "wins": cell["wins"],
                "losses": cell["losses"],
                "draws": cell["draws"],
                "pairs": cell["pairs"],
                "median_margin": round(_median(cell["margins"]), 2) if cell["margins"] else None,
            }
            for opp, cell in opponents.items()
        }
        for fp, opponents in h2h.items()
    }

    return {
        "champion": champion,
        "standings": table,
        "head_to_head": matrix,
        "sessions_analyzed": len(sessions_data),
        "matchups_analyzed": matchups_analyzed,
        "decisive_matchups": decisive,
        "generated_from": limit_sessions,
        # What the ranking means, so the page can explain it rather than assert it.
        "ranked_by": "rating_floor",
        "provisional_pairs": PROVISIONAL_PAIRS,
        "rank_sigma": RANK_SIGMA,
        "rating_pairs_total": sum(pair_wins.values()),
    }


def profile_ledger(fingerprint: str, limit_sessions: int = 50) -> dict:
    """**One profile's record in the ring** — its standings row, its opponents, its bouts.

    The Profile Detail page shows what a profile has *measured* (pooled runs, its Overall,
    its rank in the field). That's the observational verdict. This is the other one: what it
    has actually *beaten*, head to head, under shared weather — and the two disagreeing is
    the entire reason the ladder exists, so a profile page that shows only the first is
    telling half the story.

    Everything is signed from **this profile's point of view**: a bout it lost reads as a
    loss with a negative margin, whichever corner it happened to occupy. The ranking, rating
    and champion come from :func:`standings` rather than being recomputed, so the profile
    page and the league table can never name different numbers for the same record.

    Returns ``{fingerprint, name, label, in_ring, record, rank_of, champion, is_champion,
    opponents, bouts, sessions_analyzed, ranked_by, rank_sigma, provisional_pairs}``.
    ``record`` is None for a profile that has never been in the ring — an ordinary state
    (most profiles haven't fought), not an error.
    """
    fp = str(fingerprint)
    table = standings(limit_sessions)
    rows = table.get("standings") or []
    record = next((r for r in rows if r.get("fingerprint") == fp), None)

    # Per-opponent aggregate, read off the same head-to-head matrix the grid renders.
    cells = (table.get("head_to_head") or {}).get(fp) or {}
    opponents = [
        {
            "fingerprint": opp,
            "wins": cell.get("wins", 0),
            "losses": cell.get("losses", 0),
            "draws": cell.get("draws", 0),
            "pairs": cell.get("pairs", 0),
            "median_margin": cell.get("median_margin"),
        }
        for opp, cell in cells.items()
    ]
    opponents.sort(key=lambda o: (o["wins"] + o["losses"] + o["draws"], o["pairs"]), reverse=True)

    # The bout tape, matchup by matchup. `standings` aggregates the ledger and can't answer
    # "which bouts, against whom, when" — that detail only exists on the matchup records.
    bouts: list[dict] = []
    with session_scope() as session:
        for sess in _ledger_sessions(session, limit_sessions):  # newest session first
            for m in sess.get("matchups") or []:
                if not m or fp not in (m.get("incumbent"), m.get("challenger")):
                    continue
                sides = _matchup_sides(m)
                mine = 0 if sides[0][0] == fp else 1
                _, label, result, margin, pair_wins, pair_losses = sides[mine]
                opp_fp, opp_label = sides[1 - mine][0], sides[1 - mine][1]
                bouts.append(
                    {
                        "duel_id": sess["id"],
                        "finished_at": sess["finished_at"],
                        "session_status": sess["status"],
                        # Which corner it fought from. The verdict doesn't depend on it (the
                        # lead alternates within a bout), but "defended the belt" and
                        # "challenged for it" are different stories about the same record.
                        "role": "defended" if mine == 0 else "challenged",
                        "opponent": opp_fp,
                        "opponent_label": opp_label,
                        "opponent_name": m.get(
                            "challenger_name" if mine == 0 else "incumbent_name"
                        ),
                        "label": label,
                        "result": result,
                        "pairs": int(m.get("pairs") or (pair_wins + pair_losses)),
                        "pair_wins": pair_wins,
                        "pair_losses": pair_losses,
                        # Median Overall-point margin from this profile's side (+ = better).
                        "margin": margin,
                        "reason": m.get("reason"),
                        "method": m.get("method"),
                        "p_value": m.get("p_value"),
                        "challenger_why": m.get("challenger_why"),
                        "lead_alternated": m.get("lead_alternated"),
                    }
                )
        # Resolve call signs by fingerprint, exactly as the tape and the standings do, so a
        # bout fought before naming (or before a rename) reads under today's names. Only for
        # fingerprints that actually appear in the ledger: naming persists what it derives,
        # and reading a profile's (empty) ring record shouldn't mint a name row for it.
        wanted = [b["opponent"] for b in bouts] + [o["fingerprint"] for o in opponents]
        if record or bouts:
            wanted.append(fp)
        call_signs = profile_names.names_for(session, wanted) if wanted else {}
    for b in bouts:
        b["opponent_name"] = call_signs.get(b["opponent"]) or b["opponent_name"] or b["opponent_label"]
    for o in opponents:
        o["name"] = call_signs.get(o["fingerprint"])

    champion = table.get("champion")
    return {
        "fingerprint": fp,
        "name": (record or {}).get("name") or call_signs.get(fp),
        "label": (record or {}).get("label"),
        # Has this profile ever fought? Distinguishes "no ring record" from "no ledger yet".
        "in_ring": bool(bouts),
        "record": record,
        "rank_of": len(rows),
        "champion": champion,
        "is_champion": bool(champion and champion.get("fingerprint") == fp),
        "opponents": opponents,
        "bouts": bouts,
        "sessions_analyzed": table.get("sessions_analyzed", 0),
        "matchups_analyzed": table.get("matchups_analyzed", 0),
        "ranked_by": table.get("ranked_by"),
        "rank_sigma": table.get("rank_sigma"),
        "provisional_pairs": table.get("provisional_pairs"),
    }


def _name_matchups(session, matchups: list[dict]) -> list[dict]:
    """Fill in call signs on a tape, resolving by fingerprint.

    Bouts fought before naming existed (or before a rename) carry only the technical
    label, so the tape would read in two different vocabularies at once. Resolving here
    means the ledger always speaks the same names as the standings.
    """
    rows = [dict(m or {}) for m in matchups]
    fps = [m.get(side) for m in rows for side in ("incumbent", "challenger")]
    call_signs = profile_names.names_for(session, [fp for fp in fps if fp])
    for m in rows:
        for side in ("incumbent", "challenger"):
            m[f"{side}_name"] = m.get(f"{side}_name") or call_signs.get(m.get(side) or "")
    return rows


def _serialize(d: Duel, session=None) -> dict:
    matchups = d.matchups or []
    if session is not None:
        matchups = _name_matchups(session, matchups)
    return {
        "id": d.id,
        "status": d.status.value if hasattr(d.status, "value") else str(d.status),
        "stage": d.stage,
        "trigger": d.trigger,
        "duration_s": d.duration_s,
        "matchups": matchups,
        "live": d.live,
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
        return _serialize(d, session) if d else None


def history(limit: int = 10) -> list[dict]:
    """Recent duel sessions, newest first (the head-to-head ledger)."""
    with session_scope() as session:
        rows = session.scalars(select(Duel).order_by(Duel.id.desc()).limit(limit)).all()
        return [_serialize(d, session) for d in rows]


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
    "PRESETS",
    "PairedEvidence",
    "build_queue",
    "next_challenger",
    "ring_leader",
    "select_incumbent",
    "fight_card",
    "SprtState",
    "preset_config",
    "preset_for",
    "streak_to_decide",
    "paired_requirements",
    "peek_penalty",
    "sprt_requirements",
    "wilcoxon_p",
    "active",
    "cancel",
    "current",
    "history",
    "latest_champion",
    "reconcile_interrupted_duels",
    "standings",
    "start",
]
