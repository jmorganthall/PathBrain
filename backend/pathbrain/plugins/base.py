"""Benchmark plugin contract and registry.

Each plugin is an independent module exposing a single benchmark. Plugins are
synchronous (they're run in a worker thread by the runner) and return a
:class:`PluginResult`. This keeps the plugin authoring surface tiny: implement
``run(config)`` and return metrics.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class PluginResult:
    """The outcome of a single plugin execution."""

    plugin: str
    success: bool = True
    error: str | None = None
    duration_ms: float | None = None
    # Flat, scoreable metrics, e.g. {"latency_ms": 12.3}.
    metrics: dict[str, float] = field(default_factory=dict)
    # Arbitrary per-target detail for the UI / debugging.
    details: dict | None = None


class BenchmarkPlugin(ABC):
    """Base class for all benchmark plugins."""

    #: Stable identifier, e.g. "icmp". Also the config sub-key.
    name: str = ""
    #: Human-readable description for the /plugins endpoint.
    description: str = ""

    @abstractmethod
    def run(self, config: dict) -> PluginResult:
        """Execute the benchmark.

        ``config`` is the plugin's own config section (e.g. ``config["icmp"]``).
        Implementations should never raise for *measurement* failures; instead
        return a ``PluginResult`` with ``success=False`` and an ``error``.
        """

    # -- helpers -----------------------------------------------------------
    def timed(self, fn: Callable[[], dict]) -> PluginResult:
        """Run ``fn`` returning (metrics, details) and wrap it with timing."""
        start = time.perf_counter()
        try:
            payload = fn()
            duration = (time.perf_counter() - start) * 1000.0
            return PluginResult(
                plugin=self.name,
                success=True,
                duration_ms=duration,
                metrics=payload.get("metrics", {}),
                details=payload.get("details"),
            )
        except Exception as exc:  # noqa: BLE001 — measurement boundary
            duration = (time.perf_counter() - start) * 1000.0
            return PluginResult(
                plugin=self.name,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=duration,
            )


_REGISTRY: dict[str, BenchmarkPlugin] = {}


def register(cls: type[BenchmarkPlugin]) -> type[BenchmarkPlugin]:
    """Class decorator: instantiate and register a plugin by ``name``."""
    if not cls.name:
        raise ValueError(f"Plugin {cls.__name__} must define a non-empty `name`")
    _REGISTRY[cls.name] = cls()
    return cls


def get_plugin(name: str) -> BenchmarkPlugin | None:
    return _REGISTRY.get(name)


def iter_plugins() -> list[BenchmarkPlugin]:
    return list(_REGISTRY.values())
