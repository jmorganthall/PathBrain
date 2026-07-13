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
