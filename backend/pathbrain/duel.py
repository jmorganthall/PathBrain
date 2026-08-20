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
        "summary": "3 wins in a row ends it. Fast, and self-correcting on a nightly ladder.",
        "detail": "Names the better profile ~91% of the time at a 1-point edge. Between equal profiles it's a coin toss — read the standings, not one bout.",
    },
    "quick": {
        "label": "Quick call",
        "alpha": 0.10,
        "streak_wins": 0,
        "min_pairs": 5,
        "max_pairs": 20,
        "summary": "6 wins in a row ends it. About 1 verdict in 9 will be wrong.",
        "detail": "Also calls a profile that wins most (not all) pairs — ~80% of true 1-point edges, usually within 10.",
    },
    "balanced": {
        "label": "Balanced",
        "alpha": 0.05,
        "streak_wins": 0,
        "min_pairs": 8,
        "max_pairs": 30,
        "summary": "8 wins in a row ends it. About 1 verdict in 16 will be wrong.",
        "detail": "Also calls a profile that wins most (not all) pairs — ~85% of true 1-point edges, usually within 14.",
    },
    "strict": {
        "label": "Only when certain",
        "alpha": 0.01,
        "streak_wins": 0,
        "min_pairs": 12,
        "max_pairs": 60,
        "summary": "12 wins in a row ends it. Rarely wrong (~1 verdict in 60).",
        "detail": "Also calls a profile that wins most (not all) pairs — ~97% of true 1-point edges, usually within 24.",
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


def select_incumbent(session, field: dict, baseline: list[dict] | None, cfg: dict) -> tuple[str | None, str]:
    """Who starts the session holding the belt, and why.

    **The reigning champion defends.** A ladder whose champion never has to defend isn't a
    ladder: before this, every session restarted from the pooled crown, so the belt meant
    "won some session once" and the badge could name a profile that wasn't even in the
    ring. Now a fresh, decisive, reachable champion carries its belt into the next session
    and the pooled crown has to come and take it — which is exactly the matchup worth
    running, and the one the crowning policy is deciding on.

    Falls back to the pooled crown when there's no such champion (first ever session, an
    expired verdict, a champion that only won by draws, or one the live environment can no
    longer be set to). Returns ``(fingerprint, reason)``.
    """
    profiles = {p["fingerprint"]: p for p in field.get("profiles", [])}
    pooled_fp = field.get("best_fingerprint")
    rematch_days = int(cfg.get("rematch_days", 7) or 7)

    champion = latest_champion(session, max_age_days=rematch_days)
    if champion and champion.get("decisive"):
        fp = champion["fingerprint"]
        if fp in profiles and _reachable(profiles[fp].get("settings"), baseline):
            return fp, f"reigning champion from duel #{champion['duel_id']} defends the belt"
        if fp in profiles:
            return pooled_fp, "the champion can't be applied in this environment — the pooled crown defends"
        return pooled_fp, "the champion has no comparable data any more — the pooled crown defends"
    return pooled_fp, "no fresh decisive champion — the pooled crown defends"


def _reachable(settings: list[dict] | None, baseline: list[dict] | None) -> bool:
    """Can the live environment actually be set to this profile? (Same check as the heirs.)"""
    from .settings_profile import environment_signature

    if not settings or not baseline:
        return True
    try:
        return environment_signature(settings) == environment_signature(baseline)
    except Exception:  # noqa: BLE001 — a reachability probe must never break matchmaking
        return True


def build_queue(
    field: dict,
    heirs: dict,
    incumbent_fp: str,
    *,
    contenders: str = "leaders",
    top_n: int = 8,
) -> list[str]:
    """Who the champion actually fights, in order.

    ``"leaders"`` (default) races **contenders**: the reachable profiles closest to the
    crown by Overall, best first. That is what makes a perpetual ladder worth running — a
    night spent adjudicating the top of the table keeps finding better profiles, while the
    same night spent sampling arbitrary unmeasured profiles mostly re-confirms that they
    are worse. Heirs with a real optimistic ceiling are folded in ahead of the rest, since
    a limited-data profile that *could* beat the crown is exactly a contender.

    ``"heirs"`` keeps the original behavior (the heirs priority order, which deliberately
    samples untested profiles too — better for exploring a fresh field).

    Reachability is inherited from ``_compute_heirs``: anything the live environment can't
    actually be set to never enters the queue.
    """
    profiles = {p["fingerprint"]: p for p in field.get("profiles", [])}
    heir_items = [h for h in (heirs.get("items") or []) if h.get("fingerprint") in profiles]
    heir_order = [h["fingerprint"] for h in heir_items]
    # When the champion is defending, the pooled crown is a challenger — and the single
    # most interesting one in the system, since the two verdicts disagree by definition.
    # It is never in `heirs` (heirs are contenders *to* it), so it has to be added by hand
    # or the belt holder would never face the profile the all-history record favours.
    pooled_fp = field.get("best_fingerprint")
    if pooled_fp and pooled_fp != incumbent_fp and pooled_fp in profiles:
        heir_order = [pooled_fp] + [fp for fp in heir_order if fp != pooled_fp]
    if contenders != "leaders":
        return heir_order

    # Reachable = it showed up in the heirs pass, or it is the crown itself. The heirs pass
    # is the one place that filters on environment signature, so we lean on it rather than
    # re-deriving reachability here.
    reachable = set(heir_order) | {incumbent_fp}
    ranked = [
        fp
        for fp, p in sorted(
            profiles.items(),
            key=lambda kv: (kv[1].get("overall") is not None, kv[1].get("overall") or -1),
            reverse=True,
        )
        if fp != incumbent_fp and fp in reachable and p.get("overall") is not None
    ]
    leaders = ranked[: max(int(top_n or 0), 1)]
    if pooled_fp and pooled_fp != incumbent_fp and pooled_fp in profiles:
        leaders = [pooled_fp] + [fp for fp in leaders if fp != pooled_fp]
    # Contenders first, strongest first — the matchup most likely to change the answer is
    # the one just below the crown. Everything else the heirs pass surfaced (untested
    # profiles, anything outside the top N) follows, so nothing is lost, it just waits.
    return leaders + [fp for fp in heir_order if fp not in leaders]


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

            # Matchmaking: the reigning champion defends (pooled crown if there isn't one);
            # challengers are the contenders nearest the crown, skipping rematch cooldowns.
            _set_stage(duel_id, "Ranking the field for matchmaking")
            with session_scope() as session:
                field = compute_profiles(session)
                heirs = _compute_heirs(field, session, baseline)
                incumbent_fp, incumbent_why = select_incumbent(session, field, baseline, cfg)
            settings_by_fp = {p["fingerprint"]: p for p in field.get("profiles", [])}
            if incumbent_fp is None or incumbent_fp not in settings_by_fp:
                raise RuntimeError("No confident profile to defend — nothing to duel.")
            log.info("Duel %s: %s (%s)", duel_id, incumbent_why, incumbent_fp)
            # Record the holder up front so the belt names whoever is actually in the ring,
            # rather than last session's winner, from the first pair onward.
            holder = settings_by_fp.get(incumbent_fp) or {}
            with session_scope() as session:
                d = session.get(Duel, duel_id)
                if d is not None:
                    d.champion_fingerprint = incumbent_fp
                    d.champion_label = holder.get("name") or holder.get("label")
            queue = build_queue(
                field,
                heirs,
                incumbent_fp,
                contenders=str(cfg.get("contenders", "leaders") or "leaders"),
                top_n=int(cfg.get("contender_top_n", 8) or 8),
            )
            if not queue:
                raise RuntimeError("No eligible challengers (no reachable contenders to duel).")

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
                    _set_stage(
                        duel_id,
                        f"Duel: {inc.get('name') or inc['label']} (holder) defends vs "
                        f"{cha.get('name') or cha['label']} — pair {sprt.pairs + 1} "
                        f"({sprt.wins_incumbent}-{sprt.wins_challenger})",
                    )
                    pair_overalls: list[float | None] = []
                    for side_fp, side in ((incumbent_fp, inc), (challenger_fp, cha)):
                        _apply_profile(provider, side["settings"], side_fp)
                        run_id, ok, completed = run_chunk(
                            label=f"duel · {side.get('name') or side['label']}",
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
                                "mutual futility (pair wins ~50/50)"
                                if sprt.pairs < max_pairs
                                else f"no decision in {max_pairs} pairs"
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
                                    f"one side wins the pairs, but by < {min_margin} "
                                    f"Overall pts — practically equal"
                                )
                            elif sign_verdict == "draw":
                                verdict = "draw"
                                reason = "mutual futility (no consistent margin either way)"
                            elif paired.pairs >= max_pairs:
                                verdict = "draw"
                                reason = f"no decision in {max_pairs} pairs"

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
                # The winner stays on, so the holder changes DURING the session — record it
                # per bout rather than only at the end. On a continuous ladder the end may
                # be hours away, and a belt that reads hours stale is worse than no belt.
                # The crowning policy still reads completed sessions only (latest_champion),
                # so this changes what's shown, never what automation acts on.
                next_incumbent = challenger_fp if verdict == "challenger" else incumbent_fp
                holder = settings_by_fp.get(next_incumbent) or {}
                with session_scope() as session:
                    d = session.get(Duel, duel_id)
                    if d is not None:
                        d.matchups = list(matchups)
                        d.champion_fingerprint = next_incumbent
                        d.champion_label = holder.get("name") or holder.get("label")
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
                    d.champion_label = champion.get("name") or champion.get("label")
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
    """The matchups a duel started right now would actually run, in order.

    "Are we just racing randoms?" is a fair question to ask of any ladder, and the honest
    answer is a list, not a paragraph — so this builds the queue with exactly the code the
    engine uses (``build_queue`` over ``compute_profiles`` + ``_compute_heirs``) and hands
    it back with each contender's standing and why it's there. Anything on rematch cooldown
    is marked rather than silently skipped.

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
    if incumbent_fp is None or incumbent_fp not in profiles:
        return {
            "incumbent": None,
            "queue": [],
            "contenders": str(cfg.get("contenders", "leaders") or "leaders"),
            "top_n": int(cfg.get("contender_top_n", 8) or 8),
            "reason": "No confident profile to defend yet — collect more iterations.",
        }

    heir_reason = {
        h["fingerprint"]: h.get("reason") for h in (heirs.get("items") or []) if h.get("fingerprint")
    }
    order = build_queue(
        field,
        heirs,
        incumbent_fp,
        contenders=str(cfg.get("contenders", "leaders") or "leaders"),
        top_n=int(cfg.get("contender_top_n", 8) or 8),
    )
    rematch_days = int(cfg.get("rematch_days", 7) or 7)

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
        "contenders": str(cfg.get("contenders", "leaders") or "leaders"),
        "top_n": int(cfg.get("contender_top_n", 8) or 8),
        "rematch_days": rematch_days,
        "reason": None,
    }


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
            for idx, (fp, label, result, margin, pair_wins, pair_losses) in enumerate(sides):
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
                rec["pair_wins"] += pair_wins
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
                cell["pairs"] += pair_wins + pair_losses
                if margin is not None:
                    cell["margins"].append(margin)

        fp = sess.get("champion_fingerprint")
        if fp and sess["status"] == "complete":
            rec = records.get(fp)
            if rec is not None:
                rec["championships"] += 1

    # The reigning duel champion: the newest completed session that crowned one.
    champion = None
    for sess in sessions_data:  # newest first
        if sess["status"] == "complete" and sess.get("champion_fingerprint"):
            fp = sess["champion_fingerprint"]
            reign = 0
            for s2 in sessions_data:
                if s2["status"] != "complete" or not s2.get("champion_fingerprint"):
                    continue
                if s2["champion_fingerprint"] != fp:
                    break
                reign += 1
            champion = {
                "fingerprint": fp,
                "label": sess.get("champion_label"),
                "duel_id": sess["id"],
                "finished_at": sess["finished_at"],
                "consecutive_sessions": reign,
                "decisive": any(
                    (m or {}).get("verdict") != "draw" for m in sess["matchups"]
                ),
            }
            break

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
                "is_champion": bool(champion and champion["fingerprint"] == rec["fingerprint"]),
                "last_dueled_at": rec["last_dueled_at"],
                "last_duel_id": rec["last_duel_id"],
            }
        )
    # Resolve call signs by FINGERPRINT rather than trusting the label frozen into each
    # matchup: rows recorded before naming (or before a rename) then read under the same
    # name as everywhere else, so the league table and the standings can't disagree.
    with session_scope() as session:
        call_signs = profile_names.names_for(session, [r["fingerprint"] for r in table])
    for row in table:
        row["name"] = call_signs.get(row["fingerprint"]) or row["label"]
        row["beaten"] = [call_signs.get(fp, lbl) for fp, lbl in row["beaten_pairs"]]
        row["lost_to"] = [call_signs.get(fp, lbl) for fp, lbl in row["lost_to_pairs"]]
        del row["beaten_pairs"], row["lost_to_pairs"]
    if champion is not None:
        champion["name"] = call_signs.get(champion["fingerprint"]) or champion.get("label")

    table.sort(
        key=lambda r: (
            r["points"],
            r["win_rate"] if r["win_rate"] is not None else -1.0,
            r["pair_win_rate"] if r["pair_win_rate"] is not None else -1.0,
            r["matchups"],
        ),
        reverse=True,
    )
    for i, row in enumerate(table, start=1):
        row["rank"] = i

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
