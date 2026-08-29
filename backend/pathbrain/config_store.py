"""Persisted runtime benchmark configuration.

This is the user-editable configuration that drives benchmarks and scoring:
ICMP/DNS/TCP/TLS/HTTP targets, the SOPS weights, and the normalization
thresholds. It lives in the database (``AppConfig`` row, key ``"benchmark"``)
so it can be edited at runtime via the API/UI, with sensible defaults seeded on
first use.
"""
from __future__ import annotations

import copy

from sqlalchemy.orm import Session

from .metrics import COMPLETION, SOPS, default_thresholds, default_weights
from .models import AppConfig

CONFIG_KEY = "benchmark"

# Scoring rubric defaults are derived from the single metric registry
# (``pathbrain.metrics``) — weights, thresholds, axis membership and calibration
# all live there, so a new metric is a one-place change. These names are kept for
# back-compat with existing importers.
DEFAULT_WEIGHTS: dict[str, float] = default_weights(SOPS)
DEFAULT_COMPLETION_WEIGHTS: dict[str, float] = default_weights(COMPLETION)
DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = default_thresholds(SOPS)
DEFAULT_COMPLETION_THRESHOLDS: dict[str, dict[str, float]] = default_thresholds(COMPLETION)

# Identifier for the active scoring rubric (curve + thresholds). Bump when the
# calibration changes so historical scores can be tracked/re-graded.
DEFAULT_RUBRIC_VERSION = "perceptual-v5"

DEFAULT_CONFIG: dict = {
    "icmp": {
        # Two representative anycast resolvers (was three); 10 pings × 0.25s interval each
        # is the bulk of ICMP wall-clock, so each dropped target saves ~2.5s/iteration.
        "targets": ["1.1.1.1", "8.8.8.8"],
        "count": 10,
        "interval_s": 0.25,
        "timeout_s": 2.0,
    },
    "dns": {
        # Each provider: a label and the resolver IP. "local" uses system DNS.
        "providers": [
            {"name": "Cloudflare", "server": "1.1.1.1"},
            {"name": "Google", "server": "8.8.8.8"},
            {"name": "Quad9", "server": "9.9.9.9"},
            {"name": "Local", "server": "local"},
        ],
        # Two hostnames (was three) — enough to exercise each resolver without tripling
        # the lookup count.
        "hostnames": ["google.com", "cloudflare.com"],
        "timeout_s": 3.0,
    },
    "tcp": {
        # host:port pairs to measure connection establishment against (was three).
        "targets": [
            {"host": "1.1.1.1", "port": 443},
            {"host": "google.com", "port": 443},
        ],
        "timeout_s": 5.0,
    },
    "tls": {
        "targets": [
            {"host": "google.com", "port": 443},
            {"host": "github.com", "port": 443},
        ],
        "timeout_s": 5.0,
    },
    "http": {
        # Two full-page downloads (was three) — each is a real byte transfer.
        "urls": [
            "https://www.google.com/",
            "https://github.com/",
        ],
        "timeout_s": 15.0,
    },
    "browser": {
        # Headless-Chromium page loads (Playwright). Emits `total_render_ms`,
        # which activates the `render` SOPS weight automatically. Requires
        # Playwright + Chromium (bundled in the Docker image); degrades
        # gracefully where unavailable.
        "urls": [
            "https://www.google.com/",
            "https://github.com/",
        ],
        "timeout_s": 30.0,
        # Cap the `networkidle` settle so a never-idle page (trackers/long-poll) doesn't
        # pay up to the full 30s nav timeout every URL. The wait still lets late resources
        # land for the smoothness metrics, just bounded.
        "networkidle_timeout_s": 5.0,
        # The browser is the heaviest probe. It runs at most this many of the run's
        # iterations (the cheap network probes still run the full `iterations`), since
        # paint/page-load metrics are stable enough that fewer browser samples suffice —
        # a big wall-clock cut. Its Chromium is reused across these iterations.
        "iterations": 2,
        "wait_until": "load",
        "headless": True,
        # Screenshot + HAR feed only the artifacts UI (no scored metric), so they're off by
        # default now — set true to capture them for debugging a specific run.
        "screenshot": False,
        "har": False,
        # CPU-intensive CDP screencast that captures a per-frame JPEG filmstrip,
        # used only to derive the *pixel-based* Speed Index / paint-cadence
        # diagnostics. Off by default: the scored SOPS smoothness now comes from
        # the byte-arrival metrics (byte_earliness/longest_stall/perceived_time),
        # which isolate the network layer without the screencast cost. Enable to
        # also capture the visual filmstrip + Speed Index.
        "filmstrip": False,
        # HTTP/3 (QUIC) testing. Off by default (Chromium negotiates HTTP/2 over
        # TCP). When enabled, QUIC is turned on and *forced* onto the target
        # origins so loads actually use HTTP/3 — without forcing, the per-URL
        # context teardown means Alt-Svc discovery never carries to a second
        # connection and every load stays on HTTP/2. `force_quic_origins` is an
        # optional list of `host:port`; when empty it's derived from `urls`.
        "http3": False,
        "force_quic_origins": [],
    },
    # Default number of full-suite iterations to run and average per benchmark.
    # Averaging across iterations reduces per-run variability. Editable per run.
    "iterations": 3,
    # Continuous monitoring: when enabled, the scheduler runs the suite on an
    # interval so a stable windowed "rolling" score can be computed over time.
    "monitoring": {
        "enabled": False,
        "interval_minutes": 15,
        # Watchdog: fail any run still in progress after this many minutes.
        "run_timeout_minutes": 30,
        # Probe watchdog: abandon a single plugin call that hasn't returned in this many
        # minutes (see ``probes``). Far above any legitimate probe, far below "all
        # night" — it is what stops one unanswered browser call from parking the whole
        # pipeline. Must stay below ``coordinator.STALE_HOLDER_S`` so a stalled probe
        # recovers as a failed measurement before the session itself is evicted.
        "probe_timeout_minutes": 10,
    },
    # Settings-vs-responsiveness correlation: flag a settings change as
    # significant when the median SOPS moves by at least this percent.
    "correlation": {
        "significant_change_pct": 5,
        # A profile needs at least this many runs before it's treated as
        # confident (legacy; superseded by min_iterations below).
        "min_runs": 5,
        # A profile needs at least this many *total iterations* (summed across its
        # runs) before it's treated as confident. Iterations — not run count — are
        # the unit of signal: a 15-iteration run carries far more than a 1-iteration
        # one. Eligible for a "best" badge / significance calls once met.
        "min_iterations": 15,
        # Crown tie-awareness (informational co-leader labelling; the crown itself is
        # the highest-median argmax, no hysteresis). Two profiles are a statistical tie
        # unless the median gap exceeds BOTH an absolute floor (guards against splitting
        # on rounding) AND ``crown_tie_sigma`` standard errors of the median difference.
        # The SE is IQR/√n, so — unlike the old raw-IQR fraction — the bar *tightens as
        # runs accrue*: collecting data can break a tie two heavily-sampled profiles
        # would otherwise be stuck in. σ=2 ≈ a ~2-SE (roughly 95%) separation.
        "crown_tie_sigma": 2.0,
        # Recent-evidence window for the informational "Overall (recent)" column
        # (`overall_recent`): each profile's crown grade recomputed over only its most
        # recent N iterations — the drift lens showing what its Overall would be on current
        # evidence. The VERDICT deliberately pools all time (a ~100-iteration window spans
        # only a few days ≈ one weather regime per profile, so ranking on it compared
        # different profiles' different weather head-on and thrashed). 0 disables the column.
        "crown_window_iterations": 100,
        "crown_tie_min_margin": 0.5,
    },
    # Baseline "SQM off" test: occasionally disable shaping on every pipe and benchmark the
    # unshaped link, to see what SQM is actually buying. When `enabled`, the scheduler kicks
    # one nightly at the configured local (container TZ) `hour`:`minute`; it disables SQM on
    # all pipes, waits `settle_seconds` for the link to stabilize, benchmarks `iterations`
    # iterations, then restores each pipe's prior state. All quantities are also overridable
    # per on-demand ("run now") request.
    "baseline_test": {
        "enabled": False,
        "hour": 1,
        "minute": 0,
        "iterations": 10,
        "settle_seconds": 30,
    },
    # Crown follower: keep the firewall on the crowned "best" profile as the crown moves.
    # Event-driven — each completed run triggers a cheap single-profile filter, and the full
    # standings recompute runs only when that run could actually have moved the crown. Every
    # full check *records* any crown change (the churn ledger behind the "how often does the
    # best change?" stat — tracking is always on and read-only). Only when `enabled` does it
    # also *write*: if the firewall isn't on the crown, it applies the crown's writable
    # fields via provider.apply() (one-way, like "Apply this profile"; never applies the
    # SQM-off profile or one unreachable from the live environment). `interval_minutes` is
    # the slow *backstop* full check (default 6h) that catches what run-completion events
    # can't see — re-grades, external firewall edits between runs, config changes.
    "crown_follow": {
        "enabled": False,
        "interval_minutes": 360,
        # The CROWNING POLICY — the first-class choice of which verdict governs what the
        # follower applies (and what the UI calls "the crown to follow"):
        #   "pooled" — the all-time pooled Overall argmax (compute_profiles best_fingerprint).
        #   "duel"   — the head-to-head duel ladder's latest champion (falls back to pooled
        #              when no decisive duel verdict is fresh within duel.rematch_days).
        # The pooled crown STATISTIC is always computed and displayed either way; the policy
        # only selects which verdict automation acts on. One policy, one follower, one write
        # path — engines (race/duel) only measure and adjudicate.
        "policy": "pooled",
        # Which verdict ORDERS the field: "ring" (default) or "pooled" (the old behaviour).
        #
        # A duel round is a paired, interleaved comparison under shared weather — a
        # controlled experiment. The pooled Overall averages runs taken at different times
        # under conditions never held equal. On the same question the controlled comparison
        # wins, so where the ring has real head-to-head evidence for a profile it orders it,
        # and pooled orders only the profiles the ring has never produced a round for —
        # seeding which of the unraced to race first, which is what a macro map is good at.
        #
        # This is display + standings ordering. It deliberately does NOT feed the duel's own
        # matchmaking (that would make the ladder circular) or Explore (whose model is
        # fitted in pooled-Overall space) — see `crowning.rank_field`.
        "ranking": "ring",
    },
    # Interleaved head-to-head duel ladder (the adjudication engine): strict A/B/A/B
    # alternation, paired verdicts via a sequential test that stops the moment a matchup is
    # decided — then the winner stays on and the next challenger steps up.
    "duel": {
        # Nightly schedule (like baseline_test): armed/off + the time to run, expressed in
        # `timezone` (IANA; "" = container-local), running for `duration_minutes`.
        "enabled": False,
        "hour": 3,
        "minute": 0,
        "timezone": "",
        "duration_minutes": 120,
        # Sequential stopping rule (Wald SPRT on the pair-win rate): H0 p=0.5 vs H1 p=`p1`
        # at ~95% confidence (`alpha` two error rates), with a minimum of `min_pairs` before
        # any verdict, a futility cap of `max_pairs`, and a practical-significance floor —
        # a statistical winner whose median pair delta is under `min_margin` Overall points
        # is recorded as a draw (real but meaningless differences don't reshuffle anything).
        # How a bout is judged. "margins" (default) tests the paired Overall differences
        # with a Wilcoxon signed-rank test — it uses HOW MUCH each pair was won by, which
        # is where the evidence lives; measured against a true 1-point edge it decides
        # ~2.4x as often as the sign test it replaced. "pair_wins" is that legacy sign
        # test (counts winners, ignores margins) and is kept for comparison.
        "method": "margins",
        # An explicit "N wins in a row ends the bout" rule. 0 = derive it from the
        # statistical threshold (8 in a row under the default settings). Set it to 3 for a
        # snap verdict: on a nightly ladder a wrong call is cheap and self-correcting, and
        # at a true 1-point edge 3-in-a-row still names the better profile ~91% of the time.
        "streak_wins": 0,
        # Who the champion fights.
        #
        # "ring" (default) is the operating model: always be running the bout most likely to
        # unseat whoever holds the belt. Challengers are ordered by the RING'S OWN findings —
        # each one's optimistic ceiling on the fitted head-to-head rating (`duel.contender_order`)
        # — so a strong or an unknown profile gets the ring and one the ladder has already
        # beaten waits its turn. This replaced ordering by the pooled Overall, which made the
        # ladder circular: the duel exists to check the pooled verdict, so the pooled verdict
        # must not decide who gets checked. Pooled keeps one job — seeding profiles that have
        # never been in the ring and so have no other signal.
        #
        # "leaders" is that former behaviour (reachable profiles closest to the pooled crown,
        # limited-data heirs first), kept for comparison. "heirs" is the oldest, exploring
        # order, which samples untested profiles harder (better for a fresh field).
        # Keep the ladder running instead of once a night: a perpetual race that keeps
        # accruing head-to-head evidence, so a better profile can surface at any hour.
        # Sessions still hold the coordinator lock and still restore the baseline; the gap
        # leaves the pipeline free for monitoring and manual runs in between.
        "continuous": False,
        "continuous_gap_minutes": 5,
        "contenders": "ring",
        "contender_top_n": 8,
        "p1": 0.70,
        "alpha": 0.05,
        # Defaults are the "balanced" preset (duel.PRESETS) so a fresh install reads back
        # as a named choice rather than as "custom".
        "min_pairs": 8,
        "max_pairs": 30,
        # 0 = a consistent win counts, however small — matching the pooled crown, which
        # likewise has no margin floor ("the profile that wins wins"). Raise it only to
        # ignore differences too small to care about.
        "min_margin": 0.0,
        # A decided matchup isn't re-fought for this many HOURS (the ladder moves on).
        #
        # Hours, not days, and short: the cooldown exists so a settled question doesn't eat
        # the window, not to retire a pairing. A week is most of a continuous ladder's
        # useful life — the leaders are the first pairs fought, so they are the first
        # cooled, and the ring is left to profiles nobody has raced. Six hours lets the
        # top of the table be re-examined the same night while still moving on within a
        # session. (Legacy configs storing `rematch_days` are read for CHAMPION FRESHNESS
        # below, which is what that field also governed — see `rematch_hours`.)
        "rematch_hours": 6,
        # How stale the duel champion may be before the crowning policy stops acting on it
        # and falls back to the pooled crown. A SEPARATE question from the cooldown, and it
        # used to share `rematch_days` with it: dropping the cooldown to hours would
        # otherwise have made automation abandon the champion every time the ladder paused
        # for an afternoon.
        "champion_freshness_days": 7,
        # How many standard errors to subtract from the fitted rating when ORDERING the
        # standings. 0 (default) ranks on the rating itself — the ring's own finding about
        # who beat whom. Raise it to rank on a conservative floor instead, which demands a
        # record be *measured* before it can lead.
        #
        # It was 1.0, and on a real ledger that overturned head-to-head results: a
        # challenger with rating 1687 ±146 (floor 1541) ranked below a leader on 1563 ±17
        # (floor 1546) — five points of floor, on a bar eight times wider than the gap,
        # putting the profile that won the match underneath the one that lost it. Whoever
        # wins the duel wins the duel; the floor stays as the sortable "Proven" column for
        # anyone asking the other question.
        #
        # The trade is real and worth stating: at 0 a single lucky 3-0 outranks a deep
        # winning record (measured: 1696 vs 1581). The lever for THAT is the rating prior
        # below, which shrinks a thin record toward the field instead of letting an error
        # bar overturn a result.
        "rank_sigma": 0.0,
        # Virtual pairs against a phantom average opponent, added to every profile's record
        # before fitting. Keeps unbeaten records finite and shrinks thin ones toward the
        # field. Measured against a 3-0 snap versus a deep winning record: 4 → the snap
        # rates 1696 vs 1581; 8 → 1621 vs 1583; 16 → 1569 vs 1584 (the snap finally ranks
        # below). Raise it if single-match records keep topping the table.
        "rating_prior_pairs": 4.0,
        # Which rule names the champion.
        #
        # "lineal" (default) — a LINEAL TITLE: you take the belt by beating the profile
        #   that holds it, provided your whole shared record with it then favours you on
        #   BOTH counts (more matches won and more rounds won). The champion defends every
        #   bout, so the title can actually change hands. Note the aggregate gate is
        #   vacuous on a first meeting — there is no history to appeal to — so what decides
        #   whether the belt churns is `min_margin`: at 0 a win by 0.01 Overall points is a
        #   win, and on a field separated by less than the run-to-run noise that is a coin
        #   flip wearing a belt. Raise it if the title changes hands on nothing.
        #
        # "rating_floor" — the previous behaviour: the champion is the ring's #1 by the
        #   conservative fitted rating (`rating - RANK_SIGMA*se`), the same number the
        #   standings rank on. Honest about evidence, but it made the title unwinnable in
        #   practice: the holder defends every bout, so no challenger ever accumulates the
        #   second opponent its error bar needs to shrink enough to overtake.
        #
        # Either way the STANDINGS still rank on `rating_floor` — "who has demonstrated
        # the most strength" and "who holds the title" are different questions and are
        # shown as two answers rather than forced into one.
        "crown_rule": "lineal",
        # Seconds to wait after writing a profile to the firewall before measuring it.
        # Each run is preceded by a setPipe + reconfigure, which rebuilds the queues; the
        # baseline test has always waited for the link to settle before believing a
        # measurement and the duel should too. Symmetric across both sides, so it never
        # biased a verdict — it just put reconfiguration noise into every pair, and noise
        # costs pairs. 0 restores the old measure-immediately behaviour.
        "settle_seconds": 3,
    },
    # Historical trends: baseline a metric over this many days of history, judge a
    # run against the median over the last `window_hours`, and require at least
    # `min_samples` runs in a (weekday, hour) bucket before trusting its baseline
    # (otherwise the relative reading widens to a coarser time context).
    "trends": {
        "lookback_days": 90,
        "window_hours": 2,
        "min_samples": 3,
    },
    "rubric_version": DEFAULT_RUBRIC_VERSION,
    # Challenger race tuning.
    "challenger": {
        # During a race, re-run the crowned incumbent whenever its newest run is older
        # than this many minutes, so challengers are judged against a *contemporaneous*
        # bar (removes time-of-day drift) and the crown's own confidence band stays
        # tight + re-validated. 0 disables incumbent refresh.
        "incumbent_refresh_minutes": 60,
        # A confident profile whose newest run is older than this many minutes is re-raced
        # (ordered closest-to-winner first), so the race verifies stale standings — not
        # just under-min profiles. 0 disables stale-confident re-racing.
        "contender_stale_minutes": 180,
    },
    # Autonomous experiment engine. Disarmed by default; it never writes to the
    # firewall unless `enabled` is true, and `dry_run` logs intended changes
    # without applying. Window hours use the container's local time (set TZ).
    "experiment": {
        "enabled": False,       # master arm switch
        "dry_run": True,        # log intended changes, do not apply
        "auto_promote": False,  # keep the winner at window close (else restore baseline)
        "window": {
            "days": [1, 3],     # weekdays allowed: 0=Mon … 6=Sun
            "start_hour": 2,    # local hour (inclusive)
            "end_hour": 5,      # local hour (exclusive); start>end means overnight
        },
        "pipe_uuid": "",        # target shaper pipe (blank = first discovered)
        "param": "quantum",     # which FQ-CoDel param to sweep
        "candidates": [],       # values to try, e.g. [1514, 2000, 3000]
        "dwell_minutes": 10,    # hold each value this long before benchmarking it
        "min_trials_per_value": 3,
        "improve_pct": 5,       # winner must beat baseline by this % to auto-promote
    },
    "weights": DEFAULT_WEIGHTS,
    "thresholds": DEFAULT_THRESHOLDS,
    # Completion rubric — the secondary infra axis, separate from SOPS.
    "completion_weights": DEFAULT_COMPLETION_WEIGHTS,
    "completion_thresholds": DEFAULT_COMPLETION_THRESHOLDS,
}


def default_rubric() -> dict:
    """The scoring rubric portion of the defaults (weights + thresholds + version)."""
    return {
        "rubric_version": DEFAULT_RUBRIC_VERSION,
        "weights": copy.deepcopy(DEFAULT_WEIGHTS),
        "thresholds": copy.deepcopy(DEFAULT_THRESHOLDS),
        "completion_weights": copy.deepcopy(DEFAULT_COMPLETION_WEIGHTS),
        "completion_thresholds": copy.deepcopy(DEFAULT_COMPLETION_THRESHOLDS),
    }


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` onto a copy of ``base``."""
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def get_config(session: Session) -> dict:
    """Return the effective benchmark config (defaults merged with stored)."""
    row = session.get(AppConfig, CONFIG_KEY)
    if row is None:
        return copy.deepcopy(DEFAULT_CONFIG)
    return _deep_merge(DEFAULT_CONFIG, row.value or {})


def save_config(session: Session, new_config: dict) -> dict:
    """Persist a (partial) config, merged over defaults. Returns effective config."""
    row = session.get(AppConfig, CONFIG_KEY)
    merged_stored = _deep_merge(row.value or {}, new_config) if row else new_config
    if row is None:
        row = AppConfig(key=CONFIG_KEY, value=merged_stored)
        session.add(row)
    else:
        row.value = merged_stored
    session.commit()
    return _deep_merge(DEFAULT_CONFIG, merged_stored)


def reset_config(session: Session) -> dict:
    row = session.get(AppConfig, CONFIG_KEY)
    if row is not None:
        session.delete(row)
        session.commit()
    return copy.deepcopy(DEFAULT_CONFIG)
