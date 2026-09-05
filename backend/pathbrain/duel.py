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
`duel.rematch_hours`. The winner stays on as the new incumbent; the next challenger
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
from time import perf_counter as _perf
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import coordinator
from . import profile_names
from .config_store import get_config
from .database import session_scope
from .logging_config import get_logger
from .models import Duel, DuelStatus, Score
from .rating import ELO_SCALE, PROVISIONAL_PAIRS, RANK_SIGMA, fit_bradley_terry
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
    weather_shifts: list[float | None] | None = None,
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
        # The shared-weather audit so far: how many of this bout's rounds were fought
        # across a measurable weather shift between their two legs. Purely informational —
        # the verdict math never reads it.
        "weather": {
            "shifted_rounds": sum(
                1 for w in (weather_shifts or []) if w is not None and w >= ROUND_WEATHER_SHIFT
            ),
            "last_shift": (weather_shifts[-1] if weather_shifts else None),
            "max_shift": max((w for w in (weather_shifts or []) if w is not None), default=None),
            "threshold": ROUND_WEATHER_SHIFT,
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


# ── Per-round weather stamps ──────────────────────────────────────────────────────────
#
# A round's two legs are adjacent BY DESIGN so they share their weather — that adjacency
# is the whole instrument. But an assumption the system relies on this heavily should be
# verified, not trusted: every leg already measures its own conditions through the clean
# covariates (probe DNS/TCP/TLS/latency + nav setup — the signals the shaper can't move),
# so each leg is stamped with its weather severity and the round records the SHIFT between
# its two legs. Strictly flag-and-steer, like every weather reading here: a shifted round
# still counts toward the verdict exactly as before (down-weighting or discarding would
# change adjudication, which is a separate decision for a separate day) — the flag says
# which margins to trust less when reading a surprising bout, on the tape and live.
ROUND_WEATHER_SHIFT = 25.0   #: severity points between a round's legs that read as "the weather moved"
WEATHER_SCALE_SAMPLE = 5000  #: most recent completed runs the severity scale is built from


def _covariate_sources() -> dict:
    """clean covariate key → (plugin, source_key), from the one shared covariate list."""
    # Lazy import: the api package imports this module, so the reverse edge stays runtime.
    from .api.routes_settings import _weather_covariates
    from .metrics import all_metric_sources

    srcs = all_metric_sources()
    return {k: srcs.get(k) for k, is_clean in _weather_covariates() if is_clean}


def _covariate_readings(
    plugin_rows: list[tuple[str, dict | None]],
    metric_values: dict | None,
    sources: dict,
) -> dict[str, float]:
    """One run's clean-covariate values, in the canonical order of trust: the plugin
    metric cache first, then the re-graded ``Score.metric_values`` — the same fallback
    ``_weather_sensitivity`` uses, because a re-grade re-derives from raw into the Score
    without rewriting the plugin cache, so a covariate added after a run was captured
    (the nav_* phases, say) lives only in the Score. One extraction for both the scale
    build and the per-leg stamp, so the two can never read a run differently.
    """
    out: dict[str, float] = {}
    for key, src in sources.items():
        v = None
        if src:
            for plugin, metrics in plugin_rows:
                if plugin == src[0] and metrics:
                    v = metrics.get(src[1])
                    break
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            v = (metric_values or {}).get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[key] = float(v)
    return out


class _WeatherStamper:
    """The session's weather yardstick: a frozen severity scale plus the covariate
    sources and methodology it was built with, so a leg is stamped by exactly the
    extraction the scale's own history went through."""

    def __init__(self, scale, sources: dict, meth_version: str):
        self._scale = scale
        self._sources = sources
        self._meth_version = meth_version

    def leg_severity(self, run_id: int | None) -> float | None:
        """One leg's weather severity, or None on any shortfall — quiet by rule: a
        weather stamp can never be why a duel session fails."""
        if run_id is None:
            return None
        try:
            from .models import BenchmarkResult

            with session_scope() as session:
                rows = session.execute(
                    select(BenchmarkResult.plugin, BenchmarkResult.metrics).where(
                        BenchmarkResult.run_id == run_id
                    )
                ).all()
                mv = session.execute(
                    select(Score.metric_values).where(
                        Score.run_id == run_id,
                        Score.methodology_version == self._meth_version,
                    )
                ).scalar()
            covs = _covariate_readings(list(rows), mv, self._sources)
            return self._scale.severity(covs)
        except Exception:  # noqa: BLE001
            log.debug("Could not stamp weather on run %s", run_id, exc_info=True)
            return None


def _weather_stamper(meth_version: str) -> _WeatherStamper | None:
    """A severity yardstick over recent history, or None. Best-effort by rule — no
    yardstick simply means unstamped rounds, never a failed session."""
    from .models import BenchmarkResult, Run, RunStatus
    from .weather import severity_scale

    try:
        sources = _covariate_sources()
        with session_scope() as session:
            recent = (
                select(Run.id)
                .where(Run.status == RunStatus.COMPLETE)
                .order_by(Run.id.desc())
                .limit(WEATHER_SCALE_SAMPLE)
                .subquery()
            )
            rows = session.execute(
                select(BenchmarkResult.run_id, BenchmarkResult.plugin, BenchmarkResult.metrics)
                .where(BenchmarkResult.run_id.in_(select(recent.c.id)))
            ).all()
            mv_rows = session.execute(
                select(Score.run_id, Score.metric_values).where(
                    Score.run_id.in_(select(recent.c.id)),
                    Score.methodology_version == meth_version,
                )
            ).all()
        by_run: dict[int, list[tuple[str, dict | None]]] = {}
        for run_id, plugin, metrics in rows:
            by_run.setdefault(run_id, []).append((plugin, metrics))
        mv_by_run = dict(mv_rows)
        readings = [
            _covariate_readings(plugin_rows, mv_by_run.get(run_id), sources)
            for run_id, plugin_rows in by_run.items()
        ]
        scale = severity_scale(readings, list(sources.keys()))
        return _WeatherStamper(scale, sources, meth_version) if len(scale) else None
    except Exception:  # noqa: BLE001
        log.debug("Could not build the weather severity scale", exc_info=True)
        return None


def _round_reading(
    run_id: int | None, methodology_version: str, ok: bool = True
) -> tuple[float | None, str | None]:
    """``(Overall, why it is missing)`` for one leg of a round.

    A round is unusable when either leg has no Overall, and three unusable rounds abort the
    match. That was recorded as a bare *"aborted: repeated unusable rounds"* — which says
    the ladder gave up but not what went wrong, so the single most common outcome on a busy
    ledger was also the least diagnosable. There are four distinct causes and they call for
    completely different fixes:

    * the benchmark **failed** — a plugin error, a probe timeout, mid-run settings drift;
    * the run was never **scored** under the methodology the duel is adjudicating on;
    * it scored **incomparable** — its raw could not supply a required metric, which since
      derive-v14 is what happens to a browser run with no LoAF provenance (it cannot
      compute ``network_stall_all``, a crown metric, and must not fabricate one);
    * it scored but carries no Overall at all.

    The third is the one worth naming precisely, so the reason carries the missing metrics:
    "these rounds are being thrown away because the browser isn't emitting LoAF" is a
    fixable statement, and "unusable" is not.
    """
    if not ok:
        return None, "the benchmark run failed"
    if run_id is None:
        return None, "no run was recorded"
    # Read the Overall through the ordinary accessor, then diagnose only when it is
    # missing: the reading is the hot path, the diagnosis is the rare one.
    value = _run_overall(run_id, methodology_version)
    if value is not None:
        return value, None
    with session_scope() as session:
        score = session.scalars(
            select(Score).where(
                Score.run_id == run_id, Score.methodology_version == methodology_version
            )
        ).first()
        if score is None:
            return None, f"not scored under {methodology_version}"
        if score.comparability == "incomparable":
            missing = ", ".join(score.missing_metrics or []) or "a required metric"
            return None, f"incomparable: missing {missing}"
        return None, "scored, but carries no Overall"


def _recently_decided(session, a_fp: str, b_fp: str, cooldown_hours: float) -> bool:
    """Was this matchup actually **adjudicated** within the rematch cooldown?

    The cooldown exists so the ladder moves on from a settled question. An ABORTED match
    settles nothing — the window closed, or the rounds came back with no Overall to compare
    — so counting it here punished a pair for the ladder's own failure to measure it: fail
    to produce a result, and you are locked out of the ring for the whole cooldown.

    That bias was self-reinforcing in the worst possible direction. Matchmaking runs the
    crown and the leaders first, so those are the first pairs to hit a bad patch, and
    therefore the first to be set aside for a week — the ladder quietly stops racing exactly
    the profiles it exists to separate, and the ones it does race are the ones nothing has
    gone wrong with yet. A genuine draw still counts: "these two are equal" IS an
    adjudication, and re-asking it immediately is what the cooldown is for.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=cooldown_hours)
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
            if {m.get("incumbent"), m.get("challenger")} != pair:
                continue
            if outcome(m) == ABORTED:
                continue  # nothing was decided — this pair is still an open question
            return True
    return False


# ── The belt: a lineal title ──────────────────────────────────────────────────────────
#
# Two questions the ladder answers, deliberately kept apart:
#
#   "Who has demonstrated the most strength across the whole network?"  → `rating_floor`,
#       the fitted Bradley-Terry floor the STANDINGS rank on. It is a statement about
#       evidence, and it is conservative on purpose: a 3-0 record against one opponent
#       carries a ±146 error bar, so it ranks below a 37-23 record against forty.
#
#   "Who currently holds the title?"  → the LINEAL belt, replayed here. You take the belt
#       by beating the profile that has it. That is a statement about results, and it is
#       not the same question — a challenger can hold the belt while ranking below the
#       profile it beat, exactly as a boxing champion can sit below the #1 contender on a
#       pound-for-pound list.
#
# The floor answered both until now, which meant a challenger could beat the holder and
# not take the belt, because its record was too thin to have *demonstrated* superiority.
# That is a defensible reading of "best" and a poor reading of "champion", and it left the
# ladder unable to hand the title over at all: the belt-holder defends every bout, so no
# challenger ever accumulates a second opponent, so no challenger's floor ever clears the
# incumbent's, so the belt never moves. The lineal rule cuts that knot.
#
# The guard against crowning a coin flip is the AGGREGATE head-to-head record, not a
# confidence bar: a challenger takes the belt only when its whole shared history with the
# holder favours it on BOTH counts — more matches won and more rounds won. One lucky snap
# against a profile that has beaten you four times before does not make you champion.
# On a FIRST meeting there is no history to appeal to, so a clean win transfers the belt;
# that is the rule working as intended rather than a hole in it — the new holder then
# defends immediately, and a fluke is taken straight back off it. What actually decides
# whether the belt churns is `duel.min_margin`: at 0 a win by 0.01 Overall points is a win.
#
# Replayed, never stored. The belt is path-dependent — it is the *order* of results that
# moves it — but the ledger has a canonical order (session id, then bout index), so
# replaying it is deterministic and re-derives from the record exactly like every other
# verdict in PathBrain. Nothing here reads or writes mutable champion state.

LINEAL_RULE = "lineal"
FLOOR_RULE = "rating_floor"
CROWN_RULES = (LINEAL_RULE, FLOOR_RULE)
DEFAULT_CROWN_RULE = LINEAL_RULE


def iterations_per_round(cfg: dict | None) -> int:
    """Benchmark iterations per LEG of a round — the ring's resolving power.

    A round compares two single measurements, so its margin carries the noise of *both*:
    measured on a real link, ~2.3 points per run becomes ~3.3 points per round, against
    true edges between top profiles of 0.17-0.30 points. No stopping rule fixes a ruler
    coarser than the thing it measures — the recorded margin of a 3-round match is ~1.3
    whether the true edge is 0.3 or exactly zero, which is why the practical-margin floor
    cannot separate real wins from lucky ones and why raising it only deletes matches.

    Taking the median of k iterations divides the noise by sqrt(k). It is the only lever
    here that changes what the ring is *able* to see.
    """
    try:
        value = int((cfg or {}).get("iterations_per_round", 3) or 3)
    except (TypeError, ValueError):
        return 3
    return max(1, min(value, 25))


#: How many pairs a head-to-head could plausibly add before "how many more rounds?" stops
#: being a useful answer. Past this the honest reply is "not soon", not a bigger number.
MAX_SEPARATING_PAIRS = 400


def tie_sigma(cfg: dict | None) -> float:
    """Standard errors of the *difference* a lead must clear to be called real.

    The same test, and the same default, as the pooled crown's ``crown_tie_sigma`` — the
    two verdicts should not disagree about what the word "tied" means. It is deliberately
    **separate** from ``rank_sigma``: that one decides the ORDER (0 — whoever wins the duel
    wins the duel), this one decides whether the order is *meaningful*, which is a
    different question and answered as a flag rather than by moving anyone.
    """
    try:
        return max(0.0, float((cfg or {}).get("tie_sigma", 2.0) or 0.0))
    except (TypeError, ValueError):
        return 2.0


def _pooled_se(a: dict, b: dict) -> float:
    """SE of the difference between two fitted ratings, ``√(SE_a² + SE_b²)``."""
    return math.hypot(float(a.get("rating_se") or 0.0), float(b.get("rating_se") or 0.0))


def _indistinguishable(leader: dict, other: dict, sigma: float) -> bool:
    """Is ``other``'s rating within the ring's own noise of ``leader``'s?

    A **star test against the leader**, never a clustering — which is the whole reason it
    is safe. Statistical ties are *not transitive*: A within noise of B and B of C says
    nothing about A and C, so grouping the table into bands would put two profiles in
    different bands while they are indistinguishable from each other, and which band each
    landed in would depend on the order the grouping walked. Anchoring every comparison on
    one profile has no such freedom. It is exactly what ``routes_settings._clearly_better``
    does for the pooled crown, for exactly this reason.
    """
    lead, val = leader.get("rating"), other.get("rating")
    if lead is None or val is None:
        return False
    return (float(lead) - float(val)) <= sigma * _pooled_se(leader, other)


def _pairs_to_separate(leader: dict, other: dict, sigma: float) -> int | None:
    """Extra head-to-head pairs before the gap between these two would clear the bar.

    More useful than the tie flag on its own: "these two are tied" invites shrugging, while
    "race them for four more rounds and the question resolves" is something the ladder can
    act on — and it is the same arithmetic, run forwards.

    Both sides gain pairs, because the way this gets resolved is the two of them in the
    ring: a pair against an opponent whose win probability is ``p`` adds ``p(1-p)`` to each
    side's Fisher information (``rating.fit_bradley_terry``), and ``SE = ELO_SCALE/√info``.
    So the search walks ``k`` upward until the shrinking pooled SE lets the *current* gap
    through.

    It answers "if the ratings hold, how much more evidence would it take" — not "who will
    win". The ratings will move as those pairs are fought, which is the point of fighting
    them. ``None`` when the answer is not within ``MAX_SEPARATING_PAIRS``.
    """
    lead, val = leader.get("rating"), other.get("rating")
    if lead is None or val is None:
        return None
    gap = float(lead) - float(val)
    if gap <= 0:
        return None
    se_a, se_b = float(leader.get("rating_se") or 0.0), float(other.get("rating_se") or 0.0)
    if se_a <= 0 or se_b <= 0:
        return None
    # Their expected split at the current ratings, and the information one pair buys each.
    prob = 1.0 / (1.0 + 10.0 ** (gap / 400.0))
    per_pair = prob * (1.0 - prob)
    if per_pair <= 0:
        return None
    info_a, info_b = (ELO_SCALE / se_a) ** 2, (ELO_SCALE / se_b) ** 2
    for k in range(1, MAX_SEPARATING_PAIRS + 1):
        pooled = math.hypot(
            ELO_SCALE / math.sqrt(info_a + k * per_pair),
            ELO_SCALE / math.sqrt(info_b + k * per_pair),
        )
        if gap > sigma * pooled:
            return k
    return None


def rank_sigma(cfg: dict | None) -> float:
    """Standard errors subtracted from the rating when ORDERING the standings.

    0 (the default) ranks on the ring's own finding: who beat whom. It was 1.0 — a
    conservative floor — and on a real ledger that overturned head-to-head results, putting
    a challenger that won its match (1687 ±146 → floor 1541) below the leader it beat
    (1563 ±17 → floor 1546). Five points of floor decided it, on error bars eight times
    wider than the gap. The floor remains as the sortable "Proven" column, because "what
    has this record demonstrated?" is still a real question — it is just not the one the
    default order should answer.
    """
    try:
        return max(0.0, float((cfg or {}).get("rank_sigma", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def rating_prior(cfg: dict | None) -> float:
    """Virtual pairs added to every record before fitting — the lever for thin records.

    This, not the rank floor, is the principled guard against a single 3-0 topping the
    table: it shrinks a thin record toward the field mean rather than letting an error bar
    overturn a result that actually happened.
    """
    from .rating import DEFAULT_PRIOR_PAIRS

    try:
        value = float((cfg or {}).get("rating_prior_pairs", DEFAULT_PRIOR_PAIRS))
    except (TypeError, ValueError):
        return DEFAULT_PRIOR_PAIRS
    return value if value > 0 else DEFAULT_PRIOR_PAIRS


def rematch_hours(cfg: dict | None) -> float:
    """How long a DECIDED matchup rests before it can be fought again.

    Hours, and short by default. The cooldown's job is to stop one settled question eating
    a window, not to retire a pairing: on a continuous ladder the leaders are fought first
    and therefore cooled first, so a long cooldown empties the ring of exactly the profiles
    it exists to separate. Legacy configs carry `rematch_days`, which governed this AND the
    champion freshness window; it is read here only when no `rematch_hours` is set.
    """
    cfg = cfg or {}
    hours = cfg.get("rematch_hours")
    if hours is None and cfg.get("rematch_days") is not None:
        return max(0.0, float(cfg["rematch_days"]) * 24)
    return max(0.0, float(hours if hours is not None else 6))


def champion_freshness_days(cfg: dict | None) -> float:
    """How stale the duel champion may be before the crowning policy falls back to pooled.

    Separate from the cooldown, which it used to share a field with. They answer different
    questions — "has this pairing been settled recently?" versus "is this verdict still
    current enough to write to a firewall?" — and they want very different lengths.
    """
    cfg = cfg or {}
    days = cfg.get("champion_freshness_days")
    if days is None:
        days = cfg.get("rematch_days", 7)
    return max(0.0, float(days if days is not None else 7))


def crown_rule(cfg: dict | None) -> str:
    """Which rule names the champion. Unknown values fall back to the default rather than
    raising: a bad stored config must not be able to stop the ladder naming a champion."""
    rule = str((cfg or {}).get("crown_rule") or DEFAULT_CROWN_RULE).strip().lower()
    return rule if rule in CROWN_RULES else DEFAULT_CROWN_RULE


# ── Aborted is not a draw ─────────────────────────────────────────────────────────────
#
# A **draw** is a verdict: the ring fought the match and says these two profiles are
# equal. An **abort** is the ladder failing to measure anything — the window closed
# mid-match, or three rounds in a row came back with no Overall to compare. Both used to
# be filed as `verdict: "draw"`, so they were counted as draws, displayed as draws, and
# fed the "decisive record" gate the crowning policy depends on. On a continuous ladder
# that is most of the ledger: a 29-18-379 record read as a field of near-identical
# profiles when the truth was that 379 matches never produced a result.
#
# The outcome is DERIVED rather than migrated, like every other verdict here: rows written
# before this distinction existed carry a reason that already says which they were, so
# `outcome()` re-reads history correctly with no migration and no lost rows.
ABORTED = "aborted"
_ABORTED_REASON_PREFIXES = ("aborted:", "window closed")


def outcome(m: dict) -> str:
    """``"incumbent"`` | ``"challenger"`` | ``"draw"`` | ``"aborted"`` for one matchup.

    The single accessor every consumer reads, so the standings, the ratings, the champion's
    decisiveness gate and the tape can never disagree about what a match was.
    """
    verdict = str((m or {}).get("verdict") or "")
    if verdict == ABORTED:
        return ABORTED
    if verdict == "draw":
        reason = str((m or {}).get("reason") or "").strip().lower()
        if reason.startswith(_ABORTED_REASON_PREFIXES):
            return ABORTED
    return verdict


def _dominant_reason(why: dict[str, int]) -> str | None:
    """The most common reason rounds were unusable, for the match's recorded reason."""
    if not why:
        return None
    reason, count = max(why.items(), key=lambda kv: kv[1])
    total = sum(why.values())
    return f"{reason} ({count} of {total} unusable legs)"


def _decided(m: dict) -> tuple[str, str] | None:
    """``(winner, loser)`` for a decided matchup, or None for a draw/undecided one."""
    result = outcome(m)
    inc, cha = str(m.get("incumbent") or ""), str(m.get("challenger") or "")
    if not inc or not cha:
        return None
    if result == "challenger":
        return cha, inc
    if result == "incumbent":
        return inc, cha
    return None


def chronological_matchups(sessions_data: list[dict]) -> list[dict]:
    """Every matchup oldest-first. ``_ledger_sessions`` yields newest-first, and within a
    session ``matchups`` is appended in bout order, so reversing the sessions is the whole
    of it — the ledger's canonical order, which is what makes the replay deterministic."""
    out: list[dict] = []
    for sess in reversed(sessions_data):
        for m in sess.get("matchups") or []:
            if m and m.get("incumbent") and m.get("challenger"):
                out.append({**m, "_session_id": sess.get("id"), "_finished_at": sess.get("finished_at")})
    return out


def lineal_belt(sessions_data: list[dict]) -> dict | None:
    """Replay the ledger and return who holds the belt, or None if nobody does yet.

    The belt is seeded by the first decided matchup — before that there is no title to
    take. It then moves only when the holder is beaten AND the winner leads their whole
    shared record on both matches and rounds; otherwise the holder retains, which is
    recorded as a defence. Bouts the holder is not in cannot move it.

    Returns ``{fingerprint, defences, title_bouts, changes, since_session, last_bout_at,
    took_it_from, record_vs_last_challenger}``.
    """
    matches: dict[tuple[str, str], int] = {}
    rounds: dict[tuple[str, str], int] = {}
    holder: str | None = None
    defences = 0
    title_bouts = 0
    changes = 0
    since_session: int | None = None
    took_from: str | None = None
    last_bout_at: str | None = None

    for m in chronological_matchups(sessions_data):
        inc, cha = str(m["incumbent"]), str(m["challenger"])
        # Rounds accumulate from every bout, decided or drawn: a drawn bout still says
        # something about who wins rounds against whom, which is the finer of the two
        # gates the belt has to clear.
        rounds[(inc, cha)] = rounds.get((inc, cha), 0) + int(m.get("wins_incumbent") or 0)
        rounds[(cha, inc)] = rounds.get((cha, inc), 0) + int(m.get("wins_challenger") or 0)
        decided = _decided(m)
        if decided is not None:
            matches[decided] = matches.get(decided, 0) + 1

        if holder is None:
            if decided is not None:
                holder, since_session = decided[0], m.get("_session_id")
                took_from, last_bout_at = decided[1], m.get("_finished_at")
                changes += 1
                title_bouts += 1
            continue

        if holder not in (inc, cha):
            continue  # the belt was not on the line
        title_bouts += 1
        last_bout_at = m.get("_finished_at") or last_bout_at
        if decided is None:
            defences += 1  # a drawn title bout: the challenger did not take it
            continue
        winner, loser = decided
        if loser != holder:
            defences += 1  # the holder won
            continue
        # The holder was beaten. The belt moves only if the winner's WHOLE record against
        # it now favours the winner on both counts — so a single snap against a profile
        # that has beaten you repeatedly is a win, not a title.
        if (
            matches.get((winner, holder), 0) > matches.get((holder, winner), 0)
            and rounds.get((winner, holder), 0) > rounds.get((holder, winner), 0)
        ):
            holder, since_session = winner, m.get("_session_id")
            took_from = loser
            changes += 1
            defences = 0
        else:
            defences += 1

    if holder is None:
        return None
    return {
        "fingerprint": holder,
        "defences": defences,
        "title_bouts": title_bouts,
        "changes": changes,
        "since_session": since_session,
        "last_bout_at": last_bout_at,
        "took_it_from": took_from,
    }


def belt_holder(
    sessions_data: list[dict], ratings: dict[str, dict], rule: str = DEFAULT_CROWN_RULE
) -> tuple[str | None, dict | None, str]:
    """``(fingerprint, lineal detail or None, why)`` — the one answer to "who is champion?".

    Under ``"lineal"`` the belt is the replayed title, falling back to the rating floor
    only when no bout has ever been decided (there is no title yet to hold). Under
    ``"rating_floor"`` it is the ring's #1, the previous behaviour, kept so the two rules
    can be compared on the same ledger.
    """
    if rule == LINEAL_RULE:
        belt = lineal_belt(sessions_data)
        if belt is not None:
            return belt["fingerprint"], belt, (
                f"holds the lineal title ({belt['defences']} defence"
                f"{'' if belt['defences'] == 1 else 's'} since taking it)"
            )
        return ledger_leader(ratings), None, (
            "no match has been decided yet — the ring's #1 holds the belt by default"
        )
    fp = ledger_leader(ratings)
    return fp, None, "the ring's #1 by proven rating"


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
    field: dict,
    ratings: dict[str, dict],
    baseline: list[dict] | None = None,
    belt_fp: str | None = None,
) -> tuple[str | None, str]:
    """Who stands in the ring — **the champion defends**.

    A lineal title only changes hands if its holder is in the ring to lose it, so the
    belt-holder is the defender whenever the live environment can actually be set to it.
    ``belt_fp`` is that holder (from `belt_holder`); when it is None, or unreachable, this
    falls back to the ring's #1 by the conservative rating floor — the same number the
    standings rank on — so there is always someone to defend.

    Returns ``(fingerprint, why)``, or ``(None, "")`` when nothing rated and reachable
    exists — a fresh ledger, or a live environment no stored profile matches.
    """
    profiles = {p["fingerprint"]: p for p in field.get("profiles", [])}
    eligible = {
        fp for fp, p in profiles.items() if _reachable(p.get("settings"), baseline)
    }
    if belt_fp is not None and belt_fp in eligible:
        r = ratings.get(belt_fp) or {}
        floor = r.get("rating_floor")
        proven = f", proven {floor:.0f}" if isinstance(floor, (int, float)) else ""
        return belt_fp, f"the champion defends its title{proven}"
    best_fp = ledger_leader(ratings, eligible)
    if best_fp is None:
        return None, ""
    r = ratings[best_fp]
    opps = int(r.get("opponents") or 0)
    unreachable = (
        "the champion cannot be applied to the live environment, so "
        if belt_fp is not None
        else ""
    )
    return best_fp, (
        f"{unreachable}the ring's #1 defends (proven {r.get('rating_floor'):.0f} over "
        f"{r.get('pairs') or 0} rounds against {opps} opponent{'' if opps == 1 else 's'})"
    )


def _seeded_field(session, field: dict) -> dict:
    """The field the ladder matchmakes over, seeded from the prior methodology when the
    current one has no crown yet.

    Right after a publish — a new crown metric, a changed site list — every run on record
    is incomparable under the current version, so ``compute_profiles`` returns a field
    with no crown and no profiles: nothing to defend, nothing to race, a ladder that
    stands idle exactly when fresh comparable data is worth the most. The prior version's
    standings are the best guess anyone has about where to look first, so they seed the
    order: its crown stands in as the pooled fallback defender and its Overall breaks
    ties among the profiles the current version knows nothing about. A field that already
    has a crown comes back unchanged — the seed never outranks a current measurement, and
    it never enters a verdict: the ring still decides on its own paired runs."""
    from .refresh import seed_field_from_prior

    if field.get("best_fingerprint"):
        return field
    try:
        return seed_field_from_prior(session, field, field.get("min_iterations"))
    except Exception:  # noqa: BLE001 — seeding orders the queue; it must never stop the ladder
        log.warning("Duel: could not seed the field from the prior methodology", exc_info=True)
        return field


def select_incumbent(
    session,
    field: dict,
    baseline: list[dict] | None,
    cfg: dict,
    ratings: dict[str, dict] | None = None,
) -> tuple[str | None, str]:
    """Who stands in the ring, and why. **The champion defends — always.**

    Under the lineal crown rule (`duel.crown_rule`, the default) that is the holder of the
    title, replayed from the ledger by `lineal_belt`; a title that its holder never had to
    defend could never change hands. Under `"rating_floor"` it is the ring's #1 by proven
    rating, the previous behaviour, described below and kept for comparison.

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
    belt_fp, _detail, _why = belt_holder(
        _ledger_sessions(session), ratings, crown_rule(cfg)
    )
    fp, why = ring_leader(field, ratings, baseline, belt_fp)
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
    if not scored and field.get("seeded_from"):
        return (
            f"None of the {len(others)} other profiles has a comparable score under the "
            f"current methodology, and the seed from {field['seeded_from']} found nothing "
            "the live environment can be set to — collect fresh runs before duelling."
        )
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
# **The ring's own #1, when it is not the one holding the belt.** Two ring-derived verdicts
# disagreeing is the most informative match on the card — more so than the pooled crown
# below it, because BOTH sides are controlled head-to-head evidence, so the disagreement is
# purely about scope and path: the rating is global strength across everyone (including
# opponents the champion never faced), the belt is a chain of custody (who beat whom, in
# order). One match collapses it.
#
# It is gated on the rematch cooldown, and that gate is load-bearing rather than tidy. This
# tier contains exactly ONE profile by construction, so an ungated promotion would open
# every session with the same match forever — the ladder would spend the night racing two
# profiles, which is the failure the tiering exists to prevent. Gated, the disagreement is
# resolved promptly and then normal matchmaking resumes until the cooldown lapses.
RING_LEADER_TIER = 0
CROWN_TIER = 1  # the pooled crown: the two verdicts disagreeing is the most informative bout
# A profile the ring has never rated whose **pooled ceiling clears the crown** — on the
# record it could already be the best thing measured, and nobody has checked. It runs before
# the rated contenders, deliberately: those have been examined and their ceiling is a
# statement about beating the *belt-holder*, while this is an unexamined claim on the crown
# itself. Racing it answers that claim head-to-head AND matures it — a bout's paired runs go
# into the pooled record like any others, so the same hour buys the verdict and the evidence.
# Waiting is what costs: the claim is only interesting while it is unresolved.
LIVE_THREAT_TIER = 2
CONTENDER_TIER = 3  # rated; on the ring's own record, could plausibly take the belt
UNTESTED_TIER = 4  # no ring record, and its own runs don't reach the crown even optimistically
OUTCLASSED_TIER = 5  # the ring already says they can't reach the belt: raced last, not never
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
    RING_LEADER_TIER: "ring's #1",
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
    ring_leader_fp: str | None = None,
) -> list[dict]:
    """The candidates to face ``defender_fp``, best first, as ``[{fingerprint, tier, why}]``.

    Under the default ``"ring"`` mode this is ``contender_order`` — ranked by each
    profile's optimistic ceiling against *this* defender's rating, so it is re-derived
    whenever the defender changes. The legacy ``"leaders"``/``"heirs"`` orders are mapped
    onto the same shape so the engine has one loop rather than one per mode.
    """
    if mode == "ring":
        return contender_order(
            field, ratings, defender_fp, baseline=baseline, heirs=heirs,
            ring_leader_fp=ring_leader_fp,
        )
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
    cooldown_hours: float = 6.0,
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
    # **The ring's #1 gets first billing when it isn't the one defending — but only once per
    # cooldown.** The promotion resolves a real disagreement (global strength vs chain of
    # custody), and it is gated precisely because that tier holds exactly one profile: an
    # ungated promotion would open every session with the same match, which is the ladder
    # spending the night on two profiles — the failure the tiering exists to prevent. One
    # extra indexed query, and only for the leader, since nothing else can be promoted.
    ring_leader_fp = ledger_leader(ratings)
    if ring_leader_fp is not None and (
        ring_leader_fp == defender_fp
        or frozenset((defender_fp, ring_leader_fp)) in fought
        or _recently_decided(session, defender_fp, ring_leader_fp, cooldown_hours)
    ):
        ring_leader_fp = None
    order = [
        c
        for c in _challenger_order(
            field, ratings, defender_fp, mode=mode, heirs=heirs, baseline=baseline,
            top_n=top_n, ring_leader_fp=ring_leader_fp,
        )
        if c["fingerprint"] != defender_fp
        and frozenset((defender_fp, c["fingerprint"])) not in fought
    ]
    if not order:
        return None, ""
    best = min(c["tier"] for c in order)
    tier = [c for c in order if c["tier"] == best]
    for c in tier:
        if not _recently_decided(session, defender_fp, c["fingerprint"], cooldown_hours):
            return c["fingerprint"], c["why"]
    c = tier[0]
    return c["fingerprint"], f"{c['why']} — re-raced (decided within the last {cooldown_hours:g}h)"



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
    ring_leader_fp: str | None = None,
) -> list[dict]:
    """Who should challenge the belt-holder, best chance of unseating it first.

    Returns ``[{fingerprint, tier, ceiling, rating, why}, …]`` in running order. The tiers
    exist so the ring is never handed to a lower one while a higher still has someone:

    * ``RING_LEADER_TIER`` — the ring's own #1, when it isn't the one holding the belt.
      Two ring-derived verdicts disagreeing beats the pooled one below it, because both
      sides are controlled evidence. Supplied by the caller (``ring_leader_fp``) rather
      than derived here, because the promotion is gated on the rematch cooldown and only
      the caller has the session to check it — without that gate this tier holds exactly
      one profile forever and the ladder races the same pair every session.
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
        if fp == ring_leader_fp:
            # The ring rates this profile above the one wearing the belt. Resolve it.
            out.append({
                "fingerprint": fp,
                "tier": RING_LEADER_TIER,
                "rating": round(ratings[fp]["rating"], 1) if ratings.get(fp) else None,
                "ceiling": None,
                "pooled_overall": p.get("overall"),
                "pooled_ceiling": p.get("optimistic"),
                "prior_overall": p.get("prior_overall"),
                "why": (
                    "the ring's #1 isn't the champion — two head-to-head verdicts "
                    "disagreeing, which one match can settle"
                ),
            })
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
                    else (
                        f"nothing measured under the current methodology yet — seeded from "
                        f"its {p['prior_overall']:.0f} under the previous one"
                        if p.get("prior_overall") is not None
                        else "never been in the ring, and nothing rules it out — worth a look"
                    )
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
            # The prior methodology's Overall, present only on a field seeded after a
            # publish (`_seeded_field`): the last thing consulted, so it only ever orders
            # profiles the current version has no number for at all.
            "prior_overall": p.get("prior_overall"),
            "why": why,
        })

    # Within a tier: by ceiling where we have one (best chance of dethroning first), by
    # pooled Overall where we don't (the only thing that distinguishes two unknowns), and
    # by the prior methodology's Overall when even that is missing (a seeded field).
    out.sort(
        key=lambda c: (
            c["tier"],
            -(c["ceiling"] if c["ceiling"] is not None else -1e9),
            -(c["pooled_ceiling"] if c["pooled_ceiling"] is not None else -1e9),
            -(c["pooled_overall"] or -1e9),
            -(c["prior_overall"] if c["prior_overall"] is not None else -1e9),
        )
    )
    return out


# ── The ring: belt cadence + concurrent seats ─────────────────────────────────────────
#
# A session used to be a sequence of two-profile matches, each round a pair of legs
# (belt, challenger) in ABBA order. That spends half of every session re-measuring the
# belt — a profile that already has thousands of iterations — and it is the only shape
# the pair-based accounting could express.
#
# The ring generalises it with two settings and no new statistics:
#
# * ``duel.belt_every`` — the belt (the *reference* leg) recurs every N legs. N=2 is strict
#   alternation, ``B C B D B C B D…``: every challenger leg has a belt leg on both sides,
#   which is the strongest shared-weather guarantee the ladder can offer and the default.
#   N=3 is ``B C D B C D``: a third more challenger legs per hour, and a challenger leg can
#   be two legs from its nearest belt leg. Raising it is a measured trade, which is what
#   ``weather_by_distance`` exists to price.
# * ``duel.seats`` — how many challengers are in the ring at once. Their matches run
#   concurrently, sharing the same belt legs and the same weather window, and a seat refills
#   the moment its match decides, so time flows to the undecided.
#
# Each challenger leg yields ONE margin: its Overall minus the mean of the usable belt legs
# flanking it (the one opening its cycle and the one closing it). ``pairs`` on the record
# still counts margins, so the stopping rule, the ratings fit, the standings and the tape
# consume the record unchanged. Two honest notes on the arithmetic, so nobody reads more
# into this than it gives: consecutive margins share a belt leg, so they are mildly
# correlated (about 1/6 at equal leg noise — the signed-rank test is slightly optimistic);
# and the information about an edge is still set by how many legs each profile ran, so the
# throughput gain comes from ``belt_every``, not from alternation itself.

#: The reference leg opening/closing a cycle, or a challenger's leg within it.
class _Leg:
    __slots__ = ("fp", "role", "run_id", "value", "severity", "why_missing", "index", "position")

    def __init__(self, fp: str, role: str, run_id: int | None, value: float | None,
                 severity: float | None, why_missing: str | None, index: int, position: int):
        self.fp = fp
        self.role = role            # "belt" | "challenger"
        self.run_id = run_id
        self.value = value          # Overall, or None when the leg is unusable
        self.severity = severity    # weather stamp, or None
        self.why_missing = why_missing
        self.index = index          # leg number within the session (1-based)
        self.position = position    # slot within its cycle (belt = 0)


class _Seat:
    """One challenger's match in progress against the current reference."""

    def __init__(self, fp: str, profile: dict, why: str, reference_fp: str, *,
                 p1: float, alpha: float, min_margin: float, min_pairs: int,
                 max_pairs: int, streak_wins: int):
        self.fp = fp
        self.profile = profile
        self.why = why
        self.reference_fp = reference_fp
        self.sprt = SprtState(p1, alpha)
        self.paired = PairedEvidence(alpha, min_margin, min_pairs, max_pairs, streak_wins=streak_wins)
        self.deltas: list[float] = []
        self.weather_shifts: list[float | None] = []
        self.leg_distances: list[int] = []
        self.bad_streak = 0
        self.unusable = 0
        self.unusable_why: dict[str, int] = {}
        self.legs = 0
        self.verdict: str | None = None
        self.reason = ""
        # Every session this match has been seated in. More than one means it was carried
        # across a window close (or a restart) and resumed with its margins intact.
        self.sessions: list[int] = []


# ── Open matches survive the window ──────────────────────────────────────────────
#
# A match's adjudication state — its margins, the SPRT walk, the signed-rank evidence, the
# streak — used to live only in the running thread. Every run was on disk the moment it
# landed and every DECIDED match was written at its verdict, but a match still open when
# the window closed was recorded as "window closed mid-matchup (undecided)" and the next
# session started that pair again from round zero. On a nightly ladder that is the top
# pairs, every night: the two best profiles are the first seated and the last to resolve,
# so the ladder spent its evidence on them and then threw it away. A process restart lost
# even the record.
#
# The fix is that the state is a pure function of the ordered margins: the SPRT and the
# paired test are both fed one delta at a time, so replaying the deltas rebuilds them
# exactly. A snapshot per seated match is written to the duel row after every round; the
# next session moves the snapshots onto its own row and replays each into a seat, provided
# the match's reference is still the profile defending — a match is never switched to a
# different opponent, in-session or across sessions.


def _seat_snapshot(seat: _Seat, duel_id: int, methodology: str | None = None) -> dict:
    """Everything needed to resume this match in a later session.

    Stamped with the methodology its margins were read under: a margin is a difference of
    two Overalls on ONE rubric's scale, so a match can only resume under the version it
    started under — the carry-over check closes it otherwise, with the reason."""
    return {
        "challenger": seat.fp,
        "incumbent": seat.reference_fp,
        "methodology": methodology,
        "challenger_label": seat.profile.get("label"),
        "challenger_name": seat.profile.get("name"),
        "why": seat.why,
        "deltas": [round(float(d), 4) for d in seat.deltas],
        "weather_shifts": list(seat.weather_shifts),
        "leg_distances": list(seat.leg_distances),
        "legs": int(seat.legs),
        "unusable": int(seat.unusable),
        "unusable_why": dict(seat.unusable_why),
        "bad_streak": int(seat.bad_streak),
        "sessions": sorted(set(int(x) for x in seat.sessions) | {int(duel_id)}),
    }


def _seat_from_snapshot(snap: dict, profile: dict, *, p1: float, alpha: float,
                        min_margin: float, min_pairs: int, max_pairs: int,
                        streak_wins: int) -> _Seat:
    """Rebuild a seated match from its snapshot by replaying its margins — the stopping
    rules are fed one delta at a time, so the replay reproduces their state exactly. The
    rule parameters are the CURRENT config's: a match resumed under a changed preset is
    adjudicated by the rules in force now, like every other match in the session."""
    why = str(snap.get("why") or "resumed")
    seat = _Seat(
        str(snap["challenger"]), profile, why, str(snap["incumbent"]),
        p1=p1, alpha=alpha, min_margin=min_margin, min_pairs=min_pairs,
        max_pairs=max_pairs, streak_wins=streak_wins,
    )
    for raw in snap.get("deltas") or []:
        delta = float(raw)
        seat.deltas.append(delta)
        seat.sprt.add_pair(delta > 0)
        seat.paired.add(delta)
    seat.weather_shifts = [None if w is None else float(w) for w in (snap.get("weather_shifts") or [])]
    seat.leg_distances = [int(x) for x in (snap.get("leg_distances") or [])]
    seat.legs = int(snap.get("legs") or 0)
    seat.unusable = int(snap.get("unusable") or 0)
    seat.unusable_why = {str(k): int(v) for k, v in (snap.get("unusable_why") or {}).items()}
    seat.bad_streak = int(snap.get("bad_streak") or 0)
    seat.sessions = [int(x) for x in (snap.get("sessions") or [])]
    return seat


def _current_methodology_version(session) -> str | None:
    """The version pinned current right now — one indexed read, cheap enough to ask at
    every cycle of a running ladder (a cycle is minutes; this is microseconds)."""
    from .models import Methodology

    return session.scalars(
        select(Methodology.version).where(Methodology.is_current.is_(True)).limit(1)
    ).first()


def _carried_open_matches(duel_id: int) -> list[dict]:
    """The open matches the previous session left behind, MOVED onto this session's row
    so each is carried exactly once (a crashed session's row still holds them, since
    they are written after every round — so a restart carries them too)."""
    with session_scope() as session:
        rows = session.scalars(
            select(Duel).where(Duel.id != duel_id).order_by(Duel.id.desc()).limit(25)
        ).all()
        for prev in rows:
            snaps = [dict(x) for x in (prev.open_matches or []) if isinstance(x, dict)]
            if not snaps:
                continue
            prev.open_matches = None
            d = session.get(Duel, duel_id)
            if d is not None:
                d.open_matches = snaps
            log.info("Duel %s: carrying %d open match(es) from duel %s", duel_id, len(snaps), prev.id)
            return snaps
    return []


def belt_every(cfg: dict | None) -> int:
    """How often the belt's reference leg recurs (2 = strict alternation). Bounded so a
    stored nonsense value can never stop the ladder measuring."""
    try:
        value = int((cfg or {}).get("belt_every", 2) or 2)
    except (TypeError, ValueError):
        return 2
    return max(2, min(value, 6))


def seats(cfg: dict | None) -> int:
    """Challengers in the ring at once."""
    try:
        value = int((cfg or {}).get("seats", 2) or 2)
    except (TypeError, ValueError):
        return 2
    return max(1, min(value, 6))


def browser_only(cfg: dict | None) -> bool:
    """Run duel legs with the browser plugin alone. Every crown metric is browser-derived and
    the weather stamp's clean covariates include the browser's own nav_dns/nav_tcp/nav_tls/
    nav_request phases, so a browser-only leg still scores and still gets stamped; what it
    drops is the probe plugins' share of every leg."""
    return bool((cfg or {}).get("browser_only", False))


def leg_overrides(leg_iterations: int, only_browser: bool) -> dict:
    """The per-run config for one duel leg: lift the browser's iteration cap to the leg's
    (every crown metric is browser-derived — see `iterations_per_round`), and under
    ``browser_only`` skip every other plugin."""
    from .plugins import iter_plugins

    overrides: dict = {"browser": {"iterations": leg_iterations}}
    if only_browser:
        for plugin in iter_plugins():
            if plugin.name != "browser":
                overrides[plugin.name] = {"skip": True}
    return overrides


def _adjudicate(seat: _Seat, *, method: str, min_pairs: int, max_pairs: int,
                min_margin: float) -> tuple[str | None, str]:
    """The stopping rule over one seat's margins so far — the same rule as before, on
    per-leg margins instead of per-pair ones."""
    sprt, paired, deltas = seat.sprt, seat.paired, seat.deltas
    if method == "pair_wins":
        # Legacy: adjudicate on which side won each margin, magnitudes ignored.
        verdict = sprt.decision(min_pairs, max_pairs)
        if verdict in ("challenger", "incumbent"):
            if abs(_median(deltas)) < min_margin:
                return "draw", f"boundary crossed but |median Δ| < {min_margin} — practically equal"
            return verdict, "SPRT boundary crossed"
        if verdict == "draw":
            return "draw", (
                "mutual futility (round wins ~50/50)"
                if sprt.pairs < max_pairs
                else f"no decision in {max_pairs} rounds"
            )
        return None, ""
    verdict = paired.decision()
    if verdict is not None:
        return verdict, (
            f"margins consistently one-sided "
            f"(p={paired.p_value(1 if verdict == 'challenger' else -1):.4f} "
            f"≤ {paired.nominal_alpha:.4f}, median Δ {_median(deltas):+.2f})"
        )
    # No winner yet. Three ways a match still ends — and the cap is checked HERE rather
    # than inherited from the sign test, which can sit on a "winner" the margin floor
    # keeps rejecting and so never terminates.
    sign_verdict = sprt.decision(min_pairs, max_pairs)
    if sign_verdict in ("challenger", "incumbent") and abs(_median(deltas)) < min_margin:
        return "draw", f"one side wins the rounds, but by < {min_margin} Overall pts — practically equal"
    if sign_verdict == "draw":
        return "draw", "mutual futility (no consistent margin either way)"
    if paired.pairs >= max_pairs:
        return "draw", f"no decision in {max_pairs} rounds"
    return None, ""


def _match_record(seat: _Seat, inc: dict, *, method: str, verdict: str, reason: str,
                  cadence: int, only_browser: bool, methodology: str | None = None) -> dict:
    """One decided (or closed) match as the ledger stores it. Same shape the pair engine
    wrote, plus the ring's design fields."""
    sprt, paired, deltas = seat.sprt, seat.paired, seat.deltas
    cha = seat.profile
    return {
        "incumbent": seat.reference_fp,
        "challenger": seat.fp,
        # The rubric the margins were read under — so a tape spanning a publish says which
        # matches were fought on which scale.
        "methodology": methodology,
        "incumbent_label": inc["label"],
        "challenger_label": cha["label"],
        "incumbent_name": inc.get("name"),
        "challenger_name": cha.get("name"),
        "challenger_why": seat.why,
        # Every challenger leg is flanked by belt legs on both sides (or one at a cycle the
        # window cut short), so "went first" is balanced by construction rather than by
        # alternating it.
        "lead_alternated": True,
        "design": "ring",
        "belt_every": cadence,
        "browser_only": only_browser,
        "pairs": sprt.pairs,
        "wins_incumbent": sprt.wins_incumbent,
        "wins_challenger": sprt.wins_challenger,
        "median_delta": round(_median(deltas), 2) if deltas else None,
        "llr_incumbent": round(sprt.llr_incumbent, 2),
        "llr_challenger": round(sprt.llr_challenger, 2),
        "method": method,
        "p_value": round(min(paired.p_value(1), paired.p_value(-1)), 5) if deltas else None,
        "alpha_used": round(paired.nominal_alpha, 5),
        "verdict": verdict,
        "reason": reason,
        "unusable_rounds": seat.unusable,
        "unusable_why": dict(seat.unusable_why) or None,
        # The shared-weather audit, aligned with the margins: per usable margin, the largest
        # severity shift between the challenger leg and a belt leg it was compared with.
        "weather_shifts": list(seat.weather_shifts),
        "weather_shifted_rounds": sum(
            1 for w in seat.weather_shifts if w is not None and w >= ROUND_WEATHER_SHIFT
        ),
        "weather_max_shift": max((w for w in seat.weather_shifts if w is not None), default=None),
        "weather_shift_threshold": ROUND_WEATHER_SHIFT,
        # How far (in legs) each margin's challenger leg sat from the belt legs it was
        # compared against — 1 under strict alternation, up to belt_every-1 otherwise.
        "leg_distances": list(seat.leg_distances),
        "max_leg_distance": max(seat.leg_distances, default=None),
        # Every session the match was seated in; more than one = resumed across a window
        # close (or a restart) with its margins intact rather than restarted from zero.
        "sessions": list(seat.sessions),
        "carried": len(seat.sessions) > 1,
    }


def _rematch_candidate(
    matchups: list[dict], incumbent_fp: str | None, belt_now: str | None,
    rematched: set[frozenset[str]],
) -> frozenset[str] | None:
    """**Unfinished business gets its rematch — once.** The most recent match this session
    in which a challenger BEAT the current defender without taking the belt (their shared
    record doesn't favour it yet), so the pair can be re-opened.

    Scans every match rather than only the last one: with several seats deciding in the
    same pass, the qualifying win is routinely not the newest record, and a rule that only
    ever looked at ``matchups[-1]`` would silently drop it under the default ring shape.
    Only when the LEDGER put this profile in the ring (``belt_now == incumbent_fp``): on the
    pooled fallback "the belt didn't move" says nothing about the head-to-head record.
    """
    if incumbent_fp is None or belt_now != incumbent_fp:
        return None
    for last in reversed(matchups):
        pair = frozenset((last["incumbent"], last["challenger"]))
        if (
            last.get("verdict") == "challenger"
            and last.get("incumbent") == incumbent_fp
            and pair not in rematched
        ):
            return pair
    return None


def _leg_in_flight(fp: str, profile: dict, role: str, seat_index: int | None) -> dict:
    """The descriptor of the leg the ring is on: WHICH profile the firewall is being set to
    and measured, in which role, and — once the run exists — which run. ``phase`` walks
    ``applying`` (profile written, link settling) → ``measuring`` (a run is in flight);
    the status route attaches that run's live progress so each profile's own bar can move.
    """
    return {
        "fingerprint": fp,
        "name": profile.get("name"),
        "label": profile.get("label"),
        "role": role,
        "seat": seat_index,
        "run_id": None,
        "phase": "applying",
    }


def _ring_live(*, matchups_done: int, inc: dict, seats_list: list[_Seat], current: _Seat | None,
               legs_tape: list[dict], cadence: int, n_seats: int, only_browser: bool,
               min_pairs: int, max_pairs: int, min_margin: float, streak_needed: int,
               stage: str, leg: dict | None = None) -> dict:
    """The ring as structured state: every seated match's scoreboard, the recent legs in run
    order, the design it is running under, and the **leg in flight** (``leg``: which
    profile is on the firewall right now, and the run measuring it). The top level is the
    seat being measured (or the first), so a reader built for the one-match scoreboard
    still works."""
    boards = []
    for i, seat in enumerate(seats_list):
        board = _live_scoreboard(
            bout=matchups_done + 1 + i, inc=inc, cha=seat.profile, sprt=seat.sprt,
            paired=seat.paired, why_challenger=seat.why, min_pairs=min_pairs,
            max_pairs=max_pairs, min_margin=min_margin, streak_needed=streak_needed,
            weather_shifts=seat.weather_shifts,
        )
        board["leg_distances"] = list(seat.leg_distances[-24:])
        board["measuring"] = seat is current
        board["sessions"] = list(seat.sessions)
        boards.append(board)
    # Callers only publish the ring while at least one seat is filled (`_run_ring` breaks
    # before its first `_live` otherwise), so there is always a board to lead with.
    head = boards[seats_list.index(current)] if current in seats_list else boards[0]
    return {
        **head,
        "seats": boards,
        "legs": legs_tape[-24:],
        "reference": {
            "fingerprint": inc.get("fingerprint"),
            "name": inc.get("name"),
            "label": inc.get("label"),
        },
        "design": {
            "belt_every": cadence,
            "seats": n_seats,
            "browser_only": only_browser,
        },
        "stage": stage,
        # Which profile the ring is measuring at this moment — the belt or a seat — so the
        # page can light up THAT profile's bar rather than only marking a seat.
        "leg": dict(leg) if leg else None,
    }


def _run_ring(
    *,
    duel_id: int,
    lease,
    provider,
    cfg: dict,
    field: dict,
    heirs: dict,
    baseline: list[dict],
    settings_by_fp: dict[str, dict],
    weather,
    meth_version: str,
    deadline: float,
    run_ids: list[int],
) -> tuple[list[dict], str | None, int]:
    """Run the ring until the window closes. Returns ``(matchups, last reference, iterations)``.

    The loop is a cycle scheduler: a belt leg opens the cycle, ``belt_every - 1`` challenger
    legs follow (seats rotating through the slots so every seat sees every position), and a
    belt leg closes it — which is also the leg that opens the next one. Margins resolve when
    the closing belt leg lands. Between cycles the seated matches are adjudicated, decided
    seats refill from the same matchmaking the pair engine used, and the coordinator seam
    (lease check, beat, zipper yield) is honoured exactly as before.
    """
    from .challenger import _apply_profile

    method = str(cfg.get("method", "margins") or "margins").lower()
    streak_wins = int(cfg.get("streak_wins", 0) or 0)
    p1 = float(cfg.get("p1", 0.70) or 0.70)
    alpha = float(cfg.get("alpha", 0.05) or 0.05)
    min_pairs = int(cfg.get("min_pairs", 10) or 10)
    max_pairs = int(cfg.get("max_pairs", 40) or 40)
    min_margin = float(cfg.get("min_margin", 1.0) or 0.0)
    cooldown_hours = rematch_hours(cfg)
    leg_iterations = iterations_per_round(cfg)
    settle_s = max(0, int(cfg.get("settle_seconds", 3) or 0))
    cadence = belt_every(cfg)
    n_seats = seats(cfg)
    only_browser = browser_only(cfg)
    overrides = leg_overrides(leg_iterations, only_browser)
    streak_needed = streak_to_decide(alpha, min_pairs, max_pairs, streak_wins)
    mode = str(cfg.get("contenders", "ring") or "ring")
    top_n = int(cfg.get("contender_top_n", 8) or 8)

    matchups: list[dict] = []
    fought: set[frozenset[str]] = set()
    rematched: set[frozenset[str]] = set()
    seated: list[_Seat] = []
    incumbent_fp: str | None = None
    inc: dict = {}
    draining = False
    lead: _Leg | None = None
    legs_tape: list[dict] = []
    counters = {"legs": 0, "iterations": 0, "cycles": 0, "reseeds": 0}
    seat_kw = dict(p1=p1, alpha=alpha, min_margin=min_margin, min_pairs=min_pairs,
                   max_pairs=max_pairs, streak_wins=streak_wins)
    # Open matches the previous session (or a crashed one) left behind, moved onto this
    # row. Each is seated the moment its reference is the profile defending, or closed
    # with a reason when it can't be — never silently dropped.
    carried: list[dict] = _carried_open_matches(duel_id)

    def _stopped() -> bool:
        return time.monotonic() >= deadline or bool(_state.get("cancel"))

    def _persist_matchups() -> None:
        with session_scope() as session:
            d = session.get(Duel, duel_id)
            if d is not None:
                d.matchups = list(matchups)

    def _persist_open() -> None:
        # The open matches' full adjudication state, on the row, after every change — so
        # nothing about a match in progress exists only in this thread.
        snaps = [_seat_snapshot(x, duel_id, meth_version) for x in seated if x.verdict is None]
        snaps += [dict(c) for c in carried]
        with session_scope() as session:
            d = session.get(Duel, duel_id)
            if d is not None:
                d.open_matches = snaps or None

    # The live payload as last published, and the leg it is on: `_run_leg` republishes the
    # same board with the leg's run id the moment the run exists, so the page can show
    # which profile is being measured — and how far its run has got — while it runs.
    last_live: dict = {}
    leg_state: dict | None = None

    def _live(current: _Seat | None, stage: str, leg: dict | None = None) -> None:
        nonlocal leg_state
        leg_state = leg
        _set_stage(duel_id, stage)
        last_live.clear()
        last_live.update(_ring_live(
            matchups_done=len(matchups), inc=inc, seats_list=seated, current=current,
            legs_tape=legs_tape, cadence=cadence, n_seats=n_seats, only_browser=only_browser,
            min_pairs=min_pairs, max_pairs=max_pairs, min_margin=min_margin,
            streak_needed=streak_needed, stage=stage, leg=leg,
        ))
        _set_live(duel_id, dict(last_live))

    def _leg_run_created(run_id: int) -> None:
        # Same board, one fact added: the leg is now measuring, and this is its run.
        if leg_state is None:
            return
        leg_state["run_id"] = run_id
        leg_state["phase"] = "measuring"
        last_live["leg"] = dict(leg_state)
        _set_live(duel_id, dict(last_live))

    def _run_leg(fp: str, profile: dict, role: str, position: int, seat: _Seat | None) -> _Leg:
        lease.check()  # never write the firewall on an evicted lease
        _apply_profile(provider, profile["settings"], fp)
        # Let the link settle after the reconfigure before believing what we measure.
        if settle_s > 0:
            time.sleep(settle_s)
        opponents = ", ".join(s.profile["label"] for s in seated) or "—"
        run_id, ok, completed = run_chunk(
            label=f"duel · {profile.get('name') or profile['label']}",
            notes=f"Duel #{duel_id}: {inc['label']} (belt) vs {opponents}",
            iterations=leg_iterations,
            teardown=False,  # keep Chromium warm across the whole ladder
            job_group=f"duel-{duel_id}",
            config_overrides=overrides,
            on_created=_leg_run_created,
        )
        run_ids.append(run_id)
        counters["iterations"] += completed
        counters["legs"] += 1
        value, why_missing = _round_reading(run_id, meth_version, ok)
        severity = weather.leg_severity(run_id) if weather else None
        with session_scope() as session:
            d = session.get(Duel, duel_id)
            if d is not None:
                d.run_ids = list(run_ids)
                d.iterations_run = counters["iterations"]
        leg = _Leg(fp, role, run_id, value, severity, why_missing, counters["legs"], position)
        legs_tape.append({
            "index": leg.index,
            "fingerprint": fp,
            "name": profile.get("name"),
            "label": profile.get("label"),
            "role": role,
            "position": position,
            "overall": round(value, 2) if value is not None else None,
            "severity": round(severity, 1) if severity is not None else None,
            "run_id": run_id,
        })
        if seat is not None:
            seat.legs += 1
        return leg

    def _resolve(seat: _Seat, leg: _Leg, flanks: list[_Leg]) -> None:
        usable = [f for f in flanks if f.value is not None]
        if leg.value is None or not usable:
            seat.bad_streak += 1
            seat.unusable += 1
            why = leg.why_missing or next(
                (f.why_missing for f in flanks if f.why_missing), "the belt leg was unusable"
            )
            seat.unusable_why[why] = seat.unusable_why.get(why, 0) + 1
            if seat.bad_streak >= MAX_CONSECUTIVE_BAD_PAIRS:
                # NOT a draw: the ladder failing to measure, not a verdict of equality.
                seat.verdict = ABORTED
                seat.reason = "aborted: " + (
                    _dominant_reason(seat.unusable_why) or "repeated unusable rounds"
                )
            return
        seat.bad_streak = 0
        reference = sum(f.value for f in usable) / len(usable)
        delta = leg.value - reference
        seat.deltas.append(delta)
        seat.leg_distances.append(max(abs(leg.index - f.index) for f in usable))
        shifts = [
            abs(leg.severity - f.severity)
            for f in usable
            if leg.severity is not None and f.severity is not None
        ]
        seat.weather_shifts.append(round(max(shifts), 1) if shifts else None)
        seat.sprt.add_pair(delta > 0)
        seat.paired.add(delta)

    def _close(seat: _Seat, verdict: str, reason: str, ref: dict | None = None) -> None:
        if not seat.sessions or seat.sessions[-1] != duel_id:
            seat.sessions.append(duel_id)
        seat.verdict = verdict
        record = _match_record(
            seat, ref or inc, method=method, verdict=verdict, reason=reason,
            cadence=cadence, only_browser=only_browser, methodology=meth_version,
        )
        matchups.append(record)
        _persist_matchups()
        _persist_open()
        log.info(
            "Duel %s verdict: %s vs %s → %s (%s; pairs=%s Δmed=%s)",
            duel_id, inc["label"], seat.profile["label"], verdict, reason,
            seat.sprt.pairs, record["median_delta"],
        )

    def _reseed(session, new_version: str) -> None:
        """The methodology changed under a running ladder — re-seed instead of stranding.

        A leg is scored under whatever version is current when it lands (``execute_run``),
        while the ring reads every leg under the version it opened with: after a publish
        every later round would come back "not scored under <old>", every seated match
        would abort on unusable rounds, and the ladder would spend the rest of its window
        measuring nothing (a site-list publish at 23:00 wasting the night). So the seated
        matches are closed with the reason — their margins are on the old scale and can't
        take a round on the new one — and the field is rebuilt exactly as a fresh session
        builds it: seeded from the prior version's standings (``_seeded_field``), so the
        old rubric's best profiles are the first ones the new rubric measures.
        """
        nonlocal meth_version, field, heirs, lead, incumbent_fp, inc, draining
        from .api.routes_settings import _compute_heirs, compute_profiles

        old = meth_version
        log.info("Duel %s: methodology changed %s → %s mid-session — re-seeding the ring",
                 duel_id, old, new_version)
        _live(None, f"Methodology changed ({old} → {new_version}) — re-seeding the ring", leg=None)
        for seat in list(seated):
            _close(seat, "draw", (
                f"aborted: the methodology changed mid-session ({old} → {new_version}) — "
                "rounds under the old rubric can't be compared with the new"
            ))
            seated.remove(seat)
        meth_version = new_version
        field = _seeded_field(session, compute_profiles(session, include_weather=False))
        heirs = _compute_heirs(field, session, baseline)
        # In place: `_drive` names the champion off the same dict after the ring closes.
        settings_by_fp.clear()
        settings_by_fp.update({p["fingerprint"]: p for p in field.get("profiles", [])})
        # Pairs fought under the old rubric don't block a rematch under the new one.
        fought.clear()
        rematched.clear()
        incumbent_fp, inc, draining, lead = None, {}, False, None
        counters["reseeds"] += 1
        _persist_open()

    while not _stopped():
        # 0. Is the ring still adjudicating on the current methodology? A publish (a new
        #    crown metric, a changed site list) mid-session re-seeds rather than strands.
        with session_scope() as session:
            now_version = _current_methodology_version(session)
            reseeded = bool(now_version and now_version != meth_version)
            if reseeded:
                _reseed(session, now_version)
        if reseeded:
            # The yardstick reads covariates through the version's re-graded scores, so
            # it is rebuilt for the new one (outside the session above: it opens its own).
            weather = _weather_stamper(meth_version)
        # 1. Who defends, and who is seated against it. Refit the ledger INCLUDING this
        #    session's matches, so a challenger that just won is re-rated before the next
        #    seat is filled.
        with session_scope() as session:
            ratings = ledger_ratings(session)
            defender_fp, defender_why = select_incumbent(session, field, baseline, cfg, ratings)
            if defender_fp is None or defender_fp not in settings_by_fp:
                if not matchups and not seated:
                    raise RuntimeError("No confident profile to defend — nothing to duel.")
                break
            if incumbent_fp is not None and defender_fp != incumbent_fp and seated:
                # The belt moved while matches are still seated against the old holder.
                # Finish those against the reference they started with — a match is never
                # switched mid-way — and only then hand the ring to the new holder.
                if not draining:
                    log.info("Duel %s: belt moved to %s — draining %d seated match(es)",
                             duel_id, defender_fp, len(seated))
                draining = True
            else:
                draining = False
                if defender_fp != incumbent_fp:
                    log.info("Duel %s: %s (%s)", duel_id, defender_why, defender_fp)
                    incumbent_fp = defender_fp
                    inc = settings_by_fp[incumbent_fp]
                    lead = None  # a new reference opens with a fresh belt leg
            # The title holder over the ledger this session is extending — one replay per
            # cycle, reused below for the stored belt (the badge changes hands mid-session,
            # so it is written every cycle rather than at session end).
            belt_now, _, _ = belt_holder(_ledger_sessions(session), ratings, crown_rule(cfg))
            # Unfinished business gets its rematch — once. A challenger that WON its match
            # without taking the belt (their shared record doesn't favour it yet) has
            # raised the most informative question on the ledger, so the pair re-opens.
            rematch_now: frozenset[str] | None = None
            if matchups and not draining:
                rematch_now = _rematch_candidate(matchups, incumbent_fp, belt_now, rematched)
                if rematch_now is not None:
                    rematched.add(rematch_now)
                    fought.discard(rematch_now)
            # Carried matches that cannot resume are closed with the reason, outside this
            # read session; the ones that can are seated first, ahead of any new challenger,
            # because their evidence is already paid for.
            unresumable: list[tuple[dict, str]] = []
            if not draining:
                for snap in list(carried):
                    cfp = str(snap.get("challenger") or "")
                    prof = settings_by_fp.get(cfp)
                    fought_under = snap.get("methodology")
                    if fought_under and str(fought_under) != meth_version:
                        # Its margins are Overalls on another rubric's scale; a round read
                        # under this one cannot be added to them.
                        problem = (
                            f"it was fought under methodology {fought_under}, and the "
                            f"current one is {meth_version}"
                        )
                    elif str(snap.get("incumbent")) != incumbent_fp:
                        problem = "the belt changed hands before it could resume"
                    elif prof is None:
                        problem = "its challenger is no longer in the field"
                    elif not _reachable(prof.get("settings"), baseline):
                        problem = "its challenger is unreachable from the live environment"
                    else:
                        continue
                    carried.remove(snap)
                    unresumable.append((snap, problem))
            while not draining and len(seated) < n_seats:
                snap = next(
                    (c for c in carried if str(c.get("incumbent")) == incumbent_fp), None
                )
                if snap is not None:
                    carried.remove(snap)
                    seat = _seat_from_snapshot(snap, settings_by_fp[str(snap["challenger"])], **seat_kw)
                    if "resumed" not in seat.why:
                        seat.why = f"{seat.why} — resumed with {len(seat.deltas)} round(s) carried over"
                    seat.sessions.append(duel_id)
                    fought.add(frozenset((incumbent_fp, seat.fp)))
                    seated.append(seat)
                    log.info("Duel %s seat %d: resumed %s vs %s with %d round(s) from session(s) %s",
                             duel_id, len(seated), seat.fp, incumbent_fp, len(seat.deltas),
                             seat.sessions[:-1])
                    continue
                challenger_fp, why_challenger = next_challenger(
                    session, field, ratings, incumbent_fp, heirs=heirs, baseline=baseline,
                    cooldown_hours=cooldown_hours, mode=mode, top_n=top_n, fought=fought,
                )
                if challenger_fp is None or challenger_fp not in settings_by_fp:
                    break
                if rematch_now == frozenset((incumbent_fp, challenger_fp)):
                    why_challenger = (
                        f"{why_challenger} — rematch: it won the last match without taking the belt"
                    )
                fought.add(frozenset((incumbent_fp, challenger_fp)))
                fresh = _Seat(
                    challenger_fp, settings_by_fp[challenger_fp], why_challenger, incumbent_fp,
                    **seat_kw,
                )
                fresh.sessions.append(duel_id)
                seated.append(fresh)
                log.info("Duel %s seat %d: challenger %s (%s)", duel_id, len(seated),
                         challenger_fp, why_challenger)
        for snap, problem in unresumable:
            cfp = str(snap.get("challenger") or "")
            prof = settings_by_fp.get(cfp) or {
                "fingerprint": cfp, "label": snap.get("challenger_label") or cfp,
                "name": snap.get("challenger_name"), "settings": [],
            }
            ref_fp = str(snap.get("incumbent") or "")
            ref = settings_by_fp.get(ref_fp) or {"fingerprint": ref_fp, "label": ref_fp, "name": None}
            stale = _seat_from_snapshot(snap, prof, **seat_kw)
            _close(stale, "draw", f"aborted: carried over, but could not resume — {problem}", ref=ref)
            log.info("Duel %s: carried match %s vs %s closed — %s", duel_id, cfp, ref_fp, problem)
        _persist_open()
        if not seated:
            if not matchups:
                raise RuntimeError(_no_contenders_reason(field, heirs, incumbent_fp, baseline))
            break

        # The stored belt names the TITLE HOLDER (the replay above), written every cycle
        # because the title changes hands mid-session and a badge that waits for session
        # end reads stale.
        with session_scope() as session:
            belt_fp = belt_now or incumbent_fp
            holder = settings_by_fp.get(belt_fp) or {}
            d = session.get(Duel, duel_id)
            if d is not None:
                d.champion_fingerprint = belt_fp
                d.champion_label = holder.get("name") or holder.get("label")

        # 2. One cycle: belt (if the previous one's closing leg can't serve), then the
        #    challenger slots, then the closing belt leg that resolves them. A cycle never
        #    seats the same challenger twice: two legs of one profile against the same two
        #    belt legs are one comparison counted as two, and a back-to-back re-apply that
        #    measures nothing new. With fewer seats than slots the cycle is simply shorter.
        names = ", ".join(s.profile.get("name") or s.profile["label"] for s in seated)
        if lead is None:
            _live(None, f"{inc.get('name') or inc['label']} (belt) — reference leg, {names} seated",
                  leg=_leg_in_flight(incumbent_fp, inc, "belt", None))
            lead = _run_leg(incumbent_fp, inc, "belt", 0, None)
            if _stopped():
                break
        pending: list[tuple[_Seat, _Leg]] = []
        cycle = counters["cycles"]
        slots = min(cadence - 1, len(seated))
        for slot in range(slots):
            if _stopped():
                break
            # Rotate which seat takes which slot from cycle to cycle, so no seat is always
            # the one nearest (or furthest from) the opening belt leg.
            seat = seated[(cycle + slot) % len(seated)]
            _live(seat, (
                f"Match {len(matchups) + 1 + seated.index(seat)} · "
                f"{inc.get('name') or inc['label']} (belt) defends vs "
                f"{seat.profile.get('name') or seat.profile['label']} ({seat.why}) — round "
                f"{seat.sprt.pairs + 1} ({seat.sprt.wins_incumbent}-{seat.sprt.wins_challenger})"
            ), leg=_leg_in_flight(seat.fp, seat.profile, "challenger", seated.index(seat)))
            pending.append((seat, _run_leg(seat.fp, seat.profile, "challenger", slot + 1, seat)))
        counters["cycles"] += 1
        trail: _Leg | None = None
        if pending and not _stopped():
            _live(None, f"{inc.get('name') or inc['label']} (belt) — closing reference leg",
                  leg=_leg_in_flight(incumbent_fp, inc, "belt", None))
            trail = _run_leg(incumbent_fp, inc, "belt", 0, None)
        for seat, leg in pending:
            _resolve(seat, leg, [f for f in (lead, trail) if f is not None])
        lead = trail  # the closing leg opens the next cycle (None if the window cut it)

        # 3. Adjudicate every seated match and free the decided seats.
        for seat in list(seated):
            if seat.verdict is None:
                seat.verdict, seat.reason = _adjudicate(
                    seat, method=method, min_pairs=min_pairs, max_pairs=max_pairs,
                    min_margin=min_margin,
                )
            if seat.verdict is not None:
                _close(seat, seat.verdict, seat.reason)
                seated.remove(seat)
        _persist_open()
        if _stopped():
            break

        # 4. The seam. Check the lease (an evicted ladder must not write over whoever holds
        #    the pipeline now), beat (reaching here is the proof of progress), then let ONE
        #    queued session through. Yielded time is not added back to the deadline. A yield
        #    breaks adjacency, so the next cycle opens with a fresh belt leg.
        lease.check()
        lease.beat()
        if coordinator.waiting():
            # Nothing of the ring's is on the firewall while another session runs, so no
            # profile's bar may claim otherwise: clear the leg before stepping aside.
            _live(None, "Stepping aside for queued work — the ring resumes after it", leg=None)
        yielded = coordinator.yield_if_waiting(f"duel#{duel_id}")
        if yielded:
            log.info("Duel %s: stepped aside for %.0fs of queued work", duel_id, yielded)
            lead = None

    # Whatever is still seated when the window closes is CARRIED, not closed: its snapshot
    # is on the row (rewritten after every round), so the next session resumes it with
    # every margin intact instead of recording "window closed (undecided)" and restarting
    # the pair from round zero — which on a nightly ladder was the top pairs, every night.
    _persist_open()
    if seated or carried:
        log.info("Duel %s: %d open match(es) carried to the next session",
                 duel_id, len(seated) + len(carried))
    return matchups, incumbent_fp, counters["iterations"]


def weather_by_distance(limit_sessions: int = 10, max_legs: int = 400) -> dict:
    """**How fast does the weather move between legs?** — measured from the ledger's own runs.

    Raising ``belt_every`` puts a challenger leg further from the belt leg it is compared
    with, and the only honest way to price that is to look at how much the measured
    severity shifts between legs one, two, three and four apart on THIS link. Every duel
    session's legs are already on record in run order (``Duel.run_ids``), so the answer
    comes from history that exists, not from a session that has to be run first.

    Per distance: the number of leg pairs, the median and p75 absolute severity shift, and
    the share over ``ROUND_WEATHER_SHIFT``. Read the share: if two-apart legs shift no more
    often than adjacent ones, ``belt_every=3`` costs nothing the adjacent design wasn't
    already paying.
    """
    from .methodology import ensure_current_methodology

    from .models import Run

    with session_scope() as session:
        meth_version = ensure_current_methodology(session, get_config(session)).version
        rows = session.scalars(
            select(Duel).where(Duel.status == DuelStatus.COMPLETE)
            .order_by(Duel.id.desc()).limit(limit_sessions)
        ).all()
        sequences = [list(d.run_ids or []) for d in rows]
        all_ids = [rid for seq in sequences for rid in seq]
        started: dict[int, datetime] = {}
        for chunk_start in range(0, len(all_ids), 500):
            chunk = all_ids[chunk_start:chunk_start + 500]
            if chunk:
                started.update(dict(session.execute(
                    select(Run.id, Run.created_at).where(Run.id.in_(chunk))
                ).all()))
    stamper = _weather_stamper(meth_version)
    if stamper is None:
        return {"available": False, "reason": "no weather yardstick could be built from recent runs",
                "threshold": ROUND_WEATHER_SHIFT, "by_distance": [], "sessions_analyzed": len(sequences)}
    # "N legs apart" only means anything within a run of back-to-back legs. A session's
    # run_ids also span the seams where the ladder stepped aside for queued work (the zipper
    # yield) or stalled, and two legs either side of a forty-minute detour are not adjacent
    # — scoring them as such would inflate the adjacent row with real weather changes, in
    # exactly the direction that flatters raising the cadence. So each session's sequence
    # is split wherever the gap between consecutive legs is far longer than that session's
    # typical leg.
    def _split(seq: list[int]) -> list[list[int]]:
        times = [started.get(rid) for rid in seq]
        gaps = [
            (b - a).total_seconds()
            for a, b in zip(times, times[1:])
            if a is not None and b is not None
        ]
        if len(gaps) < 2:
            return [seq]
        typical = _median(sorted(gaps))
        limit = max(typical * 2.5, typical + 120.0)
        runs: list[list[int]] = [[seq[0]]]
        for prev, cur in zip(seq, seq[1:]):
            a, b = started.get(prev), started.get(cur)
            if a is not None and b is not None and (b - a).total_seconds() > limit:
                runs.append([cur])
            else:
                runs[-1].append(cur)
        return runs

    budget = max_legs
    stamped: list[list[float | None]] = []
    sessions_used = 0
    for seq in sequences:
        if budget <= 0:
            break
        seq = seq[-budget:]
        budget -= len(seq)
        sessions_used += 1
        for stretch in _split(seq):
            stamped.append([stamper.leg_severity(run_id) for run_id in stretch])
    out = []
    for distance in (1, 2, 3, 4):
        shifts = [
            abs(s[i + distance] - s[i])
            for s in stamped
            for i in range(len(s) - distance)
            if s[i] is not None and s[i + distance] is not None
        ]
        if not shifts:
            out.append({"distance": distance, "pairs": 0, "median_shift": None,
                        "p75_shift": None, "shifted_share": None})
            continue
        ordered = sorted(shifts)
        out.append({
            "distance": distance,
            "pairs": len(shifts),
            "median_shift": round(_median(ordered), 1),
            "p75_shift": round(ordered[min(len(ordered) - 1, int(0.75 * len(ordered)))], 1),
            "shifted_share": round(sum(1 for x in shifts if x >= ROUND_WEATHER_SHIFT) / len(shifts), 3),
        })
    return {
        "available": True,
        "threshold": ROUND_WEATHER_SHIFT,
        "by_distance": out,
        "sessions_analyzed": sessions_used,
        "stretches": len(stamped),
        "legs_stamped": sum(1 for s in stamped for x in s if x is not None),
    }


def _drive(duel_id: int) -> None:
    from .api.routes_settings import _compute_heirs, compute_profiles
    from .challenger import _apply_all
    from .methodology import ensure_current_methodology

    provider = get_provider()
    final_status = DuelStatus.COMPLETE
    err: str | None = None
    run_ids: list[int] = []
    iterations_run = 0
    try:
        with coordinator.hold(f"duel#{duel_id}") as lease:
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

            # Matchmaking, re-decided BEFORE EVERY BOUT rather than once a session: the
            # ring's current #1 defends, against whichever profile the ledger says is most
            # likely to beat *it*. Deciding this once up front is what produced "random
            # duels" — a queue chosen hours ago against a defender that had since been
            # replaced, walked to the end regardless of what the bouts in between found.
            _set_stage(duel_id, "Ranking the field for matchmaking")
            with session_scope() as session:
                field = _seeded_field(session, compute_profiles(session, include_weather=False))
                heirs = _compute_heirs(field, session, baseline)
            # The session's weather yardstick, built once: each leg is stamped with its
            # severity against recent history, so a round can say whether its two legs
            # actually shared their weather instead of assuming adjacency proved it.
            weather = _weather_stamper(meth_version)
            settings_by_fp = {p["fingerprint"]: p for p in field.get("profiles", [])}
            deadline = time.monotonic() + duration_s
            matchups, incumbent_fp, iterations_run = _run_ring(
                duel_id=duel_id, lease=lease, provider=provider, cfg=cfg, field=field,
                heirs=heirs, baseline=baseline, settings_by_fp=settings_by_fp, weather=weather,
                meth_version=meth_version, deadline=deadline, run_ids=run_ids,
            )
            log.info("Duel %s: ring closed — %d match(es), %d iteration(s)",
                     duel_id, len(matchups), iterations_run)

            # The session's champion is the title holder over the ledger this session just
            # extended — the same replay the standings badge and `latest_champion` run, so
            # the belt on the page and the stored row can't disagree.
            with session_scope() as session:
                # Deliberately NOT reachability-filtered, unlike the choice of who defends:
                # the champion is a statement about the ledger, and the standings it has to
                # agree with aren't filtered either. A champion the live environment can't
                # be set to simply never defends, and the crown follower already refuses to
                # apply an unreachable profile.
                final_fp, _, _ = belt_holder(
                    _ledger_sessions(session), ledger_ratings(session), crown_rule(cfg)
                )
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
    except coordinator.LeaseRevoked as exc:
        # The ladder went quiet long enough for the pipeline to be handed on (a wedged
        # probe, most likely). It is not a duel failure so much as a duel that was
        # overtaken — recorded plainly, and deliberately WITHOUT restoring the baseline,
        # because another session owns the firewall now and writing to it is the one
        # thing an evicted session must not do.
        log.error("Duel %s stopped: %s", duel_id, exc)
        final_status = DuelStatus.FAILED
        # The stored error is what the page shows a person, so it leads with what
        # happened and what it means, and keeps the coordinator's line as the
        # diagnostic tail. "Stopped: Coordination lease for duel#49 was revoked…"
        # under a "Last duel failed:" heading read as a crash with jargon; this is
        # the designed self-heal, and the one real consequence — the firewall was
        # deliberately not restored — went unsaid.
        err = (
            "Stood down mid-session: the ladder went quiet (usually one wedged "
            "measurement) and the pipeline was handed on. Matches already decided are "
            "kept on the ledger. The firewall was deliberately left as-is — another "
            "session owned it by then — so it may still be on the last profile the "
            f"duel applied. Detail: {exc}"
        )
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

    field = _seeded_field(session, compute_profiles(session, include_weather=False))
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
    cooldown_hours = rematch_hours(cfg)

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
            "on_cooldown": _recently_decided(session, incumbent_fp, fp, cooldown_hours),
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
        "rematch_hours": cooldown_hours,
        # The belt-holder's own ring rating — the bar every ceiling above is measured against.
        "incumbent_rating": (ratings.get(incumbent_fp) or {}).get("rating"),
        "reason": None,
    }


# ── Ledger accessors ─────────────────────────────────────────────────────────────────


def latest_champion(session, max_age_days: int) -> dict | None:
    """The ring's #1 over the whole ledger — the reigning duel champion — if fresh enough.

    Derived, never read from a stored row. It used to read the newest completed session's
    ``champion_fingerprint`` — the profile that *survived that session*, which a running
    ladder leaves hours stale. It is now the same `belt_holder` replay that the standings
    badge, the stored row and the choice of defender all run, so those four can never name
    different profiles.

    Note it is NOT the top of the standings table, and under the lineal rule it is not
    meant to be: the table ranks on demonstrated strength (`rating_floor`) while the belt
    records who beat whom. A champion sitting below row 1 is the two questions disagreeing,
    which is information rather than a bug.

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
    from .config_store import get_config

    rule = crown_rule((get_config(session).get("duel", {}) or {}))
    fp, belt, _why = belt_holder(sessions_data, ratings, rule)
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
            if outcome(m) in ("incumbent", "challenger"):
                # A record of nothing but draws demonstrates nothing — and neither does one
                # of nothing but aborts, which is the case this gate used to wave through.
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
        "provisional": bool((ratings.get(fp) or {}).get("provisional", True)),
        # How the title was won, for the badge — a champion that has defended eleven times
        # and one that took the belt in its first bout are both "champion", and the crowning
        # policy's user deserves to see which.
        "rule": rule,
        "defences": (belt or {}).get("defences"),
        "title_changes": (belt or {}).get("changes"),
        "took_it_from": (belt or {}).get("took_it_from"),
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
    verdict = outcome(m)
    delta = m.get("median_delta")
    delta = float(delta) if isinstance(delta, (int, float)) else None
    inc_wins = int(m.get("wins_incumbent") or 0)
    cha_wins = int(m.get("wins_challenger") or 0)

    def _side(winner: str) -> str:
        # "aborted" is its own result for both sides: neither profile drew, because no
        # match was completed to draw.
        if verdict == ABORTED:
            return ABORTED
        if verdict == "draw":
            return "draw"
        return "win" if verdict == winner else "loss"

    inc_result, cha_result = _side("incumbent"), _side("challenger")
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
    decisive_matchups, generated_from}``. Rows rank on ``rating_floor`` (the "Proven"
    column) — what a record has *demonstrated*.

    The ``champion`` is computed separately and is deliberately allowed to differ from row
    1: under the lineal rule the belt goes to whoever beat the holder, which is a claim
    about results, while the ranking is a claim about evidence. Each row carries ``rank``
    and the champion carries its own, so the page can show both without joining them.
    """
    limit_sessions = max(1, min(int(limit_sessions or 50), 200))
    # Where the time went, reported with the answer. Every performance report on this page
    # has been diagnosed by guessing at the production scale and rebuilding it locally to
    # test the guess; the two halves have very different cost shapes (the ring is bounded by
    # the ledger, the pooled join by all of history), so saying which one was slow turns the
    # next report into a reading instead of another round of guesswork.
    timings: dict[str, int] = {}
    _t0 = _perf()
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
        # Read inside the same session as the ledger: the belt rule decides which of the
        # two verdicts the badge states, and reading it later would need a second session.
        from .config_store import get_config

        cfg = get_config(session).get("duel", {}) or {}

    records: dict[str, dict] = {}
    h2h: dict[str, dict[str, dict]] = {}
    # The pairwise record the Bradley-Terry rating is fitted to: every interleaved pair
    # ever run, keyed (winner, loser). The unit of evidence is the PAIR rather than the
    # bout, so a hard-fought 12-8 counts for more than a 3-0 snap and a drawn bout still
    # informs the rating instead of being thrown away.
    pair_wins: dict[tuple[str, str], int] = {}
    matchups_analyzed = 0
    decisive = 0
    aborted = 0

    # Oldest → newest so "last seen" / label freshness resolve to the most recent sighting.
    for sess in reversed(sessions_data):
        for m in sess["matchups"]:
            if not m or not m.get("incumbent") or not m.get("challenger"):
                continue
            matchups_analyzed += 1
            result = outcome(m)
            if result == ABORTED:
                aborted += 1
            elif result != "draw":
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
                        # Matches that produced no result at all — kept apart from draws,
                        # which are a verdict. See `outcome()`.
                        "aborted": 0,
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
                rec[
                    {"win": "wins", "loss": "losses", "draw": "draws", ABORTED: "aborted"}[result]
                ] += 1
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
                    {"wins": 0, "losses": 0, "draws": 0, "aborted": 0, "pairs": 0, "margins": []},
                )
                cell[
                    {"win": "wins", "loss": "losses", "draw": "draws", ABORTED: "aborted"}[result]
                ] += 1
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
                # Matches that never produced a result. Reported so a thin-looking record
                # can be read as "barely raced" vs "raced a lot, measured nothing" — and
                # excluded from `matchups`-derived rates, which describe adjudications.
                "aborted": rec["aborted"],
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
    # Read off the per-profile rollup (`profile_aggregates`) rather than a `compute_profiles`
    # pass or a rescan of every run — this column is the only part of the standings whose
    # cost is bounded by history rather than by the ledger, so it is the only part that ever
    # made this page slow, and it is timed separately for exactly that reason.
    _t = _perf()
    with session_scope() as session:
        call_signs = profile_names.names_for(session, [r["fingerprint"] for r in table])
        overalls = _pooled_overalls(session, [r["fingerprint"] for r in table])
    timings["pooled_ms"] = int((_perf() - _t) * 1000)
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
    ratings = fit_bradley_terry(pair_wins, prior_pairs=rating_prior(cfg))
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

    # **Whoever wins the duel wins the duel.** Ranked on the fitted rating — the ring's own
    # finding about who beat whom — with `duel.rank_sigma` standard errors subtracted, 0 by
    # default.
    #
    # It used to subtract one, ranking on a conservative floor. That reads well as a
    # statement about evidence and badly as a standing, because it overturns results that
    # actually happened: on a real ledger a challenger that beat the leader (1687 ±146 →
    # floor 1541) ranked BELOW it (1563 ±17 → floor 1546) on five points of floor, across
    # error bars eight times wider than the gap. A ladder whose entire purpose is
    # head-to-head adjudication must not then rank the loser above the winner.
    #
    # The trade is real and is named rather than hidden: at sigma 0 a single lucky 3-0 can
    # outrate a deep winning record (measured: 1696 vs 1581). The lever for that is
    # `duel.rating_prior_pairs`, which shrinks a thin record toward the field — at 16 the
    # same snap rates 1569 against the leader's 1584 — rather than letting an error bar
    # overturn a match. The floor stays as the sortable "Proven" column for anyone asking
    # what a record has demonstrated.
    sigma = rank_sigma(cfg)

    def _rank_score(r: dict) -> float:
        rating = r.get("rating")
        if rating is None:
            return -1e9
        return float(rating) - sigma * float(r.get("rating_se") or 0.0)

    for row in table:
        row["rank_score"] = round(_rank_score(row), 1) if row.get("rating") is not None else None
    table.sort(
        key=lambda r: (
            _rank_score(r),
            r["rating_pairs"] or 0,  # more evidence first among equal scores
            r["points"],
        ),
        reverse=True,
    )
    for i, row in enumerate(table, start=1):
        row["rank"] = i

    # ── Is the order meaningful? ─────────────────────────────────────────────────
    # Ranks stay strict (1, 2, 3 …) and nobody shares one. Sharing rank numbers would say
    # two profiles are equal when one of them BEAT the other in the ring, which is the
    # rule this ladder exists to enforce, and it would need the table cut into bands —
    # which statistical ties, being non-transitive, cannot honestly be. So the tie is a
    # **flag on a strict order**, exactly as the pooled crown reports `co_leaders` without
    # moving anyone: whoever wins the duel still wins the duel, and the reader is told when
    # the margin is inside the ring's own noise.
    #
    # Worth knowing why so much of a real table is flagged: `SE = ELO_SCALE/√info` and a
    # profile's information is dominated by how many pairs it has fought, so 9 pairs carries
    # ~±100 Elo against a field whose whole spread is 100-200. That is not a defect in the
    # test, it is the state of the evidence — which is why `pairs_to_separate` sits beside
    # the flag: the actionable half is how much more racing would settle it.
    tie_bar = tie_sigma(cfg)
    leader = table[0] if table else None
    co_leaders: list[str] = []
    for row in table:
        # `row is not leader` also covers the leader itself, which is never "tied with"
        # itself — it is what every other row is measured against.
        tied = (
            leader is not None
            and row is not leader
            and _indistinguishable(leader, row, tie_bar)
        )
        row["tied_with_leader"] = bool(tied)
        row["pairs_to_separate"] = (
            _pairs_to_separate(leader, row, tie_bar) if tied else None
        )
        if tied:
            co_leaders.append(row["fingerprint"])

    # The champion is NOT row 1, and under the lineal rule it is not supposed to be. The
    # table ranks on `rating_floor` — what a record has *demonstrated* across the whole
    # network — while the belt records who beat whom. Those answer different questions, so
    # they are computed separately and shown separately; a champion sitting at row 4 is the
    # two verdicts disagreeing, which is the reason for running both.
    #
    # (They were previously forced to agree by defining the champion AS row 1. That made
    # the badge honest and the title unwinnable: the holder defends every bout, so no
    # challenger accumulates the second opponent its floor would need to overtake one.)
    rule = crown_rule(cfg)
    belt = lineal_belt(sessions_data) if rule == LINEAL_RULE else None
    champion_fp = (belt or {}).get("fingerprint") or ledger_leader(ratings)
    champion = None
    by_fp = {r["fingerprint"]: r for r in table}
    row = by_fp.get(champion_fp)
    if row is not None:
        reign = 0
        for sess in sessions_data:  # newest first
            if sess["status"] != "complete" or not (sess.get("matchups") or []):
                continue
            if sess.get("champion_fingerprint") != champion_fp:
                break
            reign += 1
        champion = {
            "fingerprint": row["fingerprint"],
            "name": row.get("name"),
            "label": row.get("label"),
            "duel_id": row.get("last_duel_id"),
            "finished_at": row.get("last_dueled_at"),
            "consecutive_sessions": reign,
            # Has it actually beaten anyone, or is its record all draws?
            "decisive": bool(row.get("wins") or row.get("losses")),
            "provisional": bool(row.get("rating_provisional", True)),
            # Where it stands on the *other* verdict. "Champion, ranked #4" is the whole
            # point of keeping the two apart, so the page should not have to join them.
            "rank": row.get("rank"),
            "rule": rule,
            "defences": (belt or {}).get("defences"),
            "title_changes": (belt or {}).get("changes"),
            "title_bouts": (belt or {}).get("title_bouts"),
            "took_it_from": (belt or {}).get("took_it_from"),
        }
        row["is_champion"] = True

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

    timings["total_ms"] = int((_perf() - _t0) * 1000)
    # Whatever the total isn't the pooled join is the ring itself: the ledger read, the
    # Bradley-Terry fit and the belt replay, all bounded by `limit_sessions`.
    timings["ring_ms"] = max(0, timings["total_ms"] - timings.get("pooled_ms", 0))
    return {
        "champion": champion,
        "standings": table,
        "head_to_head": matrix,
        "timings_ms": timings,
        "sessions_analyzed": len(sessions_data),
        "matchups_analyzed": matchups_analyzed,
        "decisive_matchups": decisive,
        "generated_from": limit_sessions,
        # What the ranking means, so the page can explain it rather than assert it.
        "ranked_by": "rating" if sigma == 0 else "rating_floor",
        "provisional_pairs": PROVISIONAL_PAIRS,
        "rank_sigma": sigma,
        # Every profile the leader's rating does not clearly stand above — information
        # about the *order*, never a change to it (the pooled crown reports its own
        # `co_leaders` the same way).
        "co_leaders": co_leaders,
        "tie_sigma": tie_bar,
        "rating_pairs_total": sum(pair_wins.values()),
    }


def round_health(limit_sessions: int = 50) -> dict:
    """**Is the ladder actually measuring anything?** — the ledger's data-collection health.

    A duel spends two benchmark runs per round, and a round with no Overall on either side
    is thrown away: three in a row abort the match. That failure was silent — recorded as a
    draw, counted as a draw, indistinguishable from a verdict — so a ladder burning most of
    its night on unusable rounds looked exactly like a field of evenly matched profiles.

    This is the readout that tells the two apart: how many matches were aborted, how many
    rounds were discarded, and **why** — the causes come straight off each match's recorded
    `unusable_why`, so "the browser isn't emitting LoAF, so every run scores incomparable
    and nothing can be compared" is a sentence the page can say instead of "unusable".

    Matches recorded before the diagnosis existed contribute to the counts but not to the
    reasons; `diagnosed_matches` says how many could be explained.
    """
    with session_scope() as session:
        sessions_data = _ledger_sessions(session, limit_sessions)

    matches = aborted = unusable = diagnosed = 0
    decided = drawn = 0
    why: dict[str, int] = {}
    abort_reasons: dict[str, int] = {}
    for m in chronological_matchups(sessions_data):
        matches += 1
        result = outcome(m)
        if result == ABORTED:
            aborted += 1
            bucket = _abort_bucket(m.get("reason"))
            abort_reasons[bucket] = abort_reasons.get(bucket, 0) + 1
        elif result == "draw":
            drawn += 1
        else:
            decided += 1
        unusable += int(m.get("unusable_rounds") or 0)
        reasons = m.get("unusable_why") or {}
        if reasons:
            diagnosed += 1
            for reason, count in reasons.items():
                why[str(reason)] = why.get(str(reason), 0) + int(count or 0)

    return {
        "matches": matches,
        "decided": decided,
        "drawn": drawn,
        "aborted": aborted,
        "aborted_share": round(aborted / matches, 3) if matches else None,
        "unusable_rounds": unusable,
        "diagnosed_matches": diagnosed,
        # Biggest cause first — the one worth fixing.
        "reasons": [
            {"reason": r, "legs": n}
            for r, n in sorted(why.items(), key=lambda kv: kv[1], reverse=True)
        ],
        # WHAT ended each aborted match — the split that says whether the ladder failed to
        # measure (unusable rounds) or simply ran out of window (matches closed undecided,
        # which, before open matches were carried across sessions, restarted from zero).
        "abort_reasons": [
            {"reason": r, "matches": n}
            for r, n in sorted(abort_reasons.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "sessions_analyzed": len(sessions_data),
    }


def _abort_bucket(reason) -> str:
    """One readable bucket per abort cause, so the health card can say which it was."""
    text = str(reason or "").strip()
    low = text.lower()
    if low.startswith("window closed"):
        return "window closed with the match undecided (restarted from zero next session)"
    if low.startswith("aborted: carried over"):
        return "carried over but could not resume"
    if "unusable" in low or "no overall" in low:
        return "rounds unusable — no Overall to compare"
    return text[:80] if text else "no reason recorded"


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

    # Per-opponent aggregate, read off the same head-to-head matrix the grid renders —
    # and joined to each opponent's POOLED Overall, which `standings` has already computed
    # for every row. That join is what makes the card answer the question it exists to
    # raise: this profile is #1 in the ring and #113 on Overall, so *who* did it beat, and
    # does the pooled verdict rate them above or below it? A per-opponent W-L-D on its own
    # cannot say — the reader would have to open 184 other profiles to find out.
    by_fp = {r.get("fingerprint"): r for r in rows}
    my_overall = (record or {}).get("overall")
    cells = (table.get("head_to_head") or {}).get(fp) or {}
    opponents = []
    for opp, cell in cells.items():
        opp_row = by_fp.get(opp) or {}
        opp_overall = opp_row.get("overall")
        wins, losses = cell.get("wins", 0), cell.get("losses", 0)
        opponents.append(
            {
                "fingerprint": opp,
                "name": opp_row.get("name"),
                "label": opp_row.get("label"),
                "wins": wins,
                "losses": losses,
                "draws": cell.get("draws", 0),
                "pairs": cell.get("pairs", 0),
                "median_margin": cell.get("median_margin"),
                # The other verdict on this opponent.
                "overall": opp_overall,
                "duel_rank": opp_row.get("rank"),
                # Signed from THIS profile's side, like every other number here: positive
                # means this profile scores higher on the pooled Overall.
                "overall_delta": (
                    round(my_overall - opp_overall, 2)
                    if my_overall is not None and opp_overall is not None
                    else None
                ),
                # Did the ring actually decide anything here? Most pairings are draws — on
                # a continuous ladder many are sessions that closed mid-match — and a list
                # sorted without regard to that buries every real result among them.
                "decisive": bool(wins or losses),
            }
        )
    # Decided pairings first, then the ones where the pooled verdict most disagrees with
    # the ring result: beating a profile Overall rates ABOVE you is the most informative
    # row on the card, so it sorts to the top rather than being hunted for.
    def _disagreement(o: dict) -> float:
        delta = o.get("overall_delta")
        if delta is None or not o["decisive"]:
            return -1e9
        # A win against a higher-Overall profile (delta < 0) scores high; so does a loss
        # against a lower-Overall one. Agreement scores low.
        direction = 1 if o["wins"] > o["losses"] else -1 if o["losses"] > o["wins"] else 0
        return -direction * delta
    opponents.sort(
        key=lambda o: (o["decisive"], _disagreement(o), o["pairs"]), reverse=True
    )

    # The headline the card leads with: does this profile's ring record disagree with the
    # pooled ranking, and in which direction? Counted only over DECIDED pairings where both
    # sides have a pooled Overall — a draw and an unscored opponent say nothing either way.
    beat_better = sum(
        1 for o in opponents if o["wins"] > o["losses"] and (o["overall_delta"] or 0) < 0
    )
    lost_to_worse = sum(
        1 for o in opponents if o["losses"] > o["wins"] and (o["overall_delta"] or 0) > 0
    )
    decided = [o for o in opponents if o["decisive"]]
    versus_overall = {
        "beat_higher_overall": beat_better,
        "lost_to_lower_overall": lost_to_worse,
        "decided_opponents": len(decided),
        "undecided_opponents": len(opponents) - len(decided),
        "overall": my_overall,
    }

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
        o["name"] = call_signs.get(o["fingerprint"]) or o.get("name") or o.get("label")

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
        # How the ring record stands against the pooled ranking — the card's headline.
        "versus_overall": versus_overall,
        "bouts": bouts,
        "sessions_analyzed": table.get("sessions_analyzed", 0),
        "matchups_analyzed": table.get("matchups_analyzed", 0),
        "ranked_by": table.get("ranked_by"),
        "rank_sigma": table.get("rank_sigma"),
        "tie_sigma": table.get("tie_sigma"),
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


def _serialize(d: Duel, session=None, matchup_limit: int | None = None) -> dict:
    """One duel session as JSON.

    ``matchup_limit`` keeps the MOST RECENT n matches and reports the true count in
    ``matchups_total``. A continuous ladder runs dozens of matches a session, so a list
    view of twenty sessions was serializing thousands of them — megabytes of JSON for a
    card that shows the last handful, which on a phone is a dropped connection rather than
    a slow one. The newest are kept because they are the ones that decided where the belt
    ended up; the complete record lives in the standings and the per-profile ledger.
    """
    matchups = list(d.matchups or [])
    total = len(matchups)
    if matchup_limit is not None and total > matchup_limit:
        matchups = matchups[-matchup_limit:]
    if session is not None:
        matchups = _name_matchups(session, matchups)
    return {
        "matchups_total": total,
        "id": d.id,
        "status": d.status.value if hasattr(d.status, "value") else str(d.status),
        "stage": d.stage,
        "trigger": d.trigger,
        "duration_s": d.duration_s,
        "matchups": matchups,
        "live": d.live,
        "iterations_run": d.iterations_run,
        "run_ids": d.run_ids or [],
        # Matches still open on this row: carried to the next session with their rounds.
        "open_matches": [
            {
                "challenger": o.get("challenger"),
                "incumbent": o.get("incumbent"),
                "challenger_label": o.get("challenger_label"),
                "challenger_name": o.get("challenger_name"),
                "rounds": len(o.get("deltas") or []),
                "sessions": list(o.get("sessions") or []),
            }
            for o in (d.open_matches or [])
            if isinstance(o, dict)
        ],
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


def history(limit: int = 10, matchup_limit: int | None = 25) -> list[dict]:
    """Recent duel sessions, newest first (the head-to-head ledger).

    Each session's match list is capped (see `_serialize`) so the payload stays bounded as
    the ladder runs; `matchups_total` says how many there really were.
    """
    with session_scope() as session:
        rows = session.scalars(select(Duel).order_by(Duel.id.desc()).limit(limit)).all()
        return [_serialize(d, session, matchup_limit) for d in rows]


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
            d.error = (
                "Interrupted — service restarted mid-duel; baseline restored (best-effort). "
                "Any match still open carries over to the next session with its rounds."
            )
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
