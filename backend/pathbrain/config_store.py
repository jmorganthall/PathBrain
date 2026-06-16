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

from .models import AppConfig

CONFIG_KEY = "benchmark"

# Default SOPS weights, straight from the PRD. They need not sum to 100; the
# scoring engine normalizes whatever weights are present for available metrics.
DEFAULT_WEIGHTS: dict[str, float] = {
    "dns": 10,
    "tcp": 15,
    "tls": 20,
    "ttfb": 20,
    "render": 25,  # browser engine total render (benchmark_browser)
    "jitter": 5,
    "packet_loss": 5,
}

# Perceptual axis weights/thresholds — the Responsiveness Score. Scored entirely
# separately from SOPS (never folded in) so completion vs. responsiveness can pull
# apart. FCP/LCP dominate (when content starts/finishes appearing); INP carries
# less and is best-effort.
DEFAULT_PERCEPTUAL_WEIGHTS: dict[str, float] = {
    "fcp": 40,  # First Contentful Paint
    "lcp": 40,  # Largest Contentful Paint
    "inp": 20,  # Interaction to Next Paint
}

# Anchored to Google Web Vitals "good/poor" bands (FCP good ≤1.8s/poor >3s;
# LCP good ≤2.5s/poor >4s; INP good ≤200ms/poor >500ms), spread a little so the
# log curve has headroom.
DEFAULT_PERCEPTUAL_THRESHOLDS: dict[str, dict[str, float]] = {
    "fcp": {"best": 1000.0, "worst": 4000.0},  # ms first contentful paint
    "lcp": {"best": 1500.0, "worst": 6000.0},  # ms largest contentful paint
    "inp": {"best": 100.0, "worst": 500.0},    # ms interaction-to-next-paint
}

# Identifier for the active scoring rubric (curve + thresholds). Bump when the
# calibration changes so historical scores can be tracked/re-graded.
DEFAULT_RUBRIC_VERSION = "perceptual-v1"

# Normalization thresholds: the value at which a metric scores 100 (best) and the
# value at which it scores 0 (worst). Lower-is-better; interpolated on a log
# (Weber–Fechner) curve. These are calibrated to human-perception research rather
# than guessed — anchored to Nielsen's response-time limits (0.1s feels instant,
# 1s keeps flow, 10s loses attention) and Google's RAIL (~100ms = instant).
DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    # DNS is invisible under a few ms; painful past ~150ms.
    "dns": {"best": 10.0, "worst": 150.0},         # ms lookup
    # Connection setup is ~1 RTT; LAN-fast vs clearly laggy.
    "tcp": {"best": 10.0, "worst": 250.0},         # ms connect
    # TLS adds 1–2 RTT on top of TCP.
    "tls": {"best": 30.0, "worst": 500.0},         # ms handshake
    # Nielsen: 100ms feels instant, ~1s is the edge of "flowing".
    "ttfb": {"best": 100.0, "worst": 1000.0},      # ms time-to-first-byte
    # RAIL/Nielsen: ~1s page feels good, several seconds feels slow.
    "render": {"best": 1000.0, "worst": 6000.0},   # ms total render
    # Interactive media: a few ms imperceptible, tens of ms disruptive.
    "jitter": {"best": 1.0, "worst": 30.0},        # ms
    # Loss hurts interactivity quickly (retransmits/stalls).
    "packet_loss": {"best": 0.0, "worst": 2.5},    # percent
}

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
    # Perceptual (Responsiveness Score) rubric — a separate axis from SOPS.
    "perceptual_weights": DEFAULT_PERCEPTUAL_WEIGHTS,
    "perceptual_thresholds": DEFAULT_PERCEPTUAL_THRESHOLDS,
}


def default_rubric() -> dict:
    """The scoring rubric portion of the defaults (weights + thresholds + version)."""
    return {
        "rubric_version": DEFAULT_RUBRIC_VERSION,
        "weights": copy.deepcopy(DEFAULT_WEIGHTS),
        "thresholds": copy.deepcopy(DEFAULT_THRESHOLDS),
        "perceptual_weights": copy.deepcopy(DEFAULT_PERCEPTUAL_WEIGHTS),
        "perceptual_thresholds": copy.deepcopy(DEFAULT_PERCEPTUAL_THRESHOLDS),
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
