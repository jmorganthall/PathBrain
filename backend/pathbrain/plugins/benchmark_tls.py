"""TLS probe: raw handshake durations.

Measures the time to complete a TLS handshake (after the TCP connection is
established) to each configured host:port. A pure sensor: it emits the raw
per-target handshake samples; ``handshake_ms`` is derived later
(``pathbrain.interpret.derive``).
"""
from __future__ import annotations

import socket
import ssl
import time

from .base import BenchmarkPlugin, PluginResult, register


def _handshake_time(host: str, port: int, timeout: float) -> tuple[float, str | None]:
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        start = time.perf_counter()
        with context.wrap_socket(sock, server_hostname=host) as tls:
            elapsed = (time.perf_counter() - start) * 1000.0
            version = tls.version()
    return elapsed, version


@register
class TlsBenchmark(BenchmarkPlugin):
    name = "tls"
    description = "TLS handshake duration to configured host:port targets"

    def run(self, config: dict) -> PluginResult:
        targets: list[dict] = config.get("targets", [])
        timeout = float(config.get("timeout_s", 5.0))

        if not targets:
            return PluginResult(self.name, success=False, error="No TLS targets configured")

        def work() -> dict:
            # Raw observations only: per-target handshake time (or error).
            per_target: dict[str, dict] = {}
            for target in targets:
                host = target.get("host")
                port = int(target.get("port", 443))
                key = f"{host}:{port}"
                try:
                    elapsed, version = _handshake_time(host, port, timeout)
                    per_target[key] = {
                        "handshake_ms": round(elapsed, 3),
                        "tls_version": version,
                    }
                except Exception as exc:  # noqa: BLE001
                    per_target[key] = {"error": f"{type(exc).__name__}: {exc}"}

            return {"raw": {"targets": per_target}, "details": {"per_target": per_target}}

        return self.timed(work)
