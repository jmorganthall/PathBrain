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

# Normalization thresholds: the metric value at which a metric scores 100 (best)
# and the value at which it scores 0 (worst). Linear interpolation, clamped.
# Lower-is-better for all of these.
DEFAULT_THRESHOLDS: dict[str, dict[str, float]] = {
    "dns": {"best": 5.0, "worst": 200.0},          # ms lookup
    "tcp": {"best": 5.0, "worst": 300.0},          # ms connect
    "tls": {"best": 20.0, "worst": 500.0},         # ms handshake
    "ttfb": {"best": 50.0, "worst": 1000.0},       # ms time-to-first-byte
    "render": {"best": 300.0, "worst": 5000.0},    # ms total render
    "jitter": {"best": 0.5, "worst": 50.0},        # ms
    "packet_loss": {"best": 0.0, "worst": 5.0},    # percent
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
    "weights": DEFAULT_WEIGHTS,
    "thresholds": DEFAULT_THRESHOLDS,
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
