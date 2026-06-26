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
        "targets": ["1.1.1.1", "8.8.8.8", "9.9.9.9"],
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
        "hostnames": ["google.com", "github.com", "cloudflare.com"],
        "timeout_s": 3.0,
    },
    "tcp": {
        # host:port pairs to measure connection establishment against.
        "targets": [
            {"host": "1.1.1.1", "port": 443},
            {"host": "google.com", "port": 443},
            {"host": "github.com", "port": 443},
        ],
        "timeout_s": 5.0,
    },
    "tls": {
        "targets": [
            {"host": "google.com", "port": 443},
            {"host": "github.com", "port": 443},
            {"host": "cloudflare.com", "port": 443},
        ],
        "timeout_s": 5.0,
    },
    "http": {
        "urls": [
            "https://www.google.com/",
            "https://github.com/",
            "https://www.cloudflare.com/",
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
        "wait_until": "load",
        "headless": True,
        "screenshot": True,
        "har": True,
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
        # confident (eligible for a "best" badge / significance calls).
        "min_runs": 5,
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
