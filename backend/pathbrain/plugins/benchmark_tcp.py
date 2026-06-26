"""TCP probe: raw connection-establishment times.

Measures the time to complete a TCP handshake (``connect()``) to each configured
host:port. A pure sensor: it emits the raw per-target connect samples; the
aggregate ``connect_ms`` metric is derived later (``pathbrain.interpret.derive``).
"""
from __future__ import annotations

import socket
import time

from .base import BenchmarkPlugin, PluginResult, register


def _connect_time(host: str, port: int, timeout: float) -> float:
    start = time.perf_counter()
    sock = socket.create_connection((host, port), timeout=timeout)
    elapsed = (time.perf_counter() - start) * 1000.0
    sock.close()
    return elapsed


@register
class TcpBenchmark(BenchmarkPlugin):
    name = "tcp"
    description = "TCP connection establishment time to configured host:port targets"

    def run(self, config: dict) -> PluginResult:
        targets: list[dict] = config.get("targets", [])
        timeout = float(config.get("timeout_s", 5.0))

        if not targets:
            return PluginResult(self.name, success=False, error="No TCP targets configured")

        def work() -> dict:
            # Raw observations only: per-target connect time (or error).
            per_target: dict[str, dict] = {}
            for target in targets:
                host = target.get("host")
                port = int(target.get("port", 443))
                key = f"{host}:{port}"
                try:
                    elapsed = _connect_time(host, port, timeout)
                    per_target[key] = {"connect_ms": round(elapsed, 3)}
                except Exception as exc:  # noqa: BLE001
                    per_target[key] = {"error": f"{type(exc).__name__}: {exc}"}

            return {"raw": {"targets": per_target}, "details": {"per_target": per_target}}

        return self.timed(work)
