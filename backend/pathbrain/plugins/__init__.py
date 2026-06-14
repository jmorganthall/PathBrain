"""Benchmark plugins.

Importing this package registers all built-in benchmark plugins. New benchmarks
are added by creating a module here that defines a ``BenchmarkPlugin`` subclass
and decorates it with ``@register``.
"""
from __future__ import annotations

from .base import BenchmarkPlugin, PluginResult, get_plugin, iter_plugins, register

# Import side-effect: register built-in plugins.
from . import (  # noqa: E402,F401
    benchmark_icmp,
    benchmark_dns,
    benchmark_tcp,
    benchmark_tls,
    benchmark_http,
)

__all__ = [
    "BenchmarkPlugin",
    "PluginResult",
    "register",
    "get_plugin",
    "iter_plugins",
]
