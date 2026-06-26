"""ICMP probe: raw round-trip times per target.

Uses ``icmplib`` in unprivileged mode (SOCK_DGRAM) where the OS allows it,
falling back to privileged raw sockets. This is a pure sensor: it emits the raw
per-target RTT series + sent/received counts and does **not** compute latency,
jitter or loss — those are derived later (``pathbrain.interpret.derive``), so the
definition of e.g. jitter (stddev vs p99) can change and be re-derived over history.
"""
from __future__ import annotations

from icmplib import ping

from .base import BenchmarkPlugin, PluginResult, register


@register
class IcmpBenchmark(BenchmarkPlugin):
    name = "icmp"
    description = "ICMP latency, jitter and packet loss across configured targets"

    def run(self, config: dict) -> PluginResult:
        targets: list[str] = config.get("targets", [])
        count = int(config.get("count", 10))
        interval = float(config.get("interval_s", 0.25))
        timeout = float(config.get("timeout_s", 2.0))

        if not targets:
            return PluginResult(self.name, success=False, error="No ICMP targets configured")

        def work() -> dict:
            # Raw observations only: per-target RTT series + sent/received counts.
            per_target: dict[str, dict] = {}

            for target in targets:
                try:
                    host = ping(
                        target,
                        count=count,
                        interval=interval,
                        timeout=timeout,
                        privileged=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    per_target[target] = {
                        "rtts_ms": [],
                        "sent": count,
                        "received": 0,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    continue

                per_target[target] = {
                    "rtts_ms": [round(r, 3) for r in host.rtts],
                    "sent": host.packets_sent,
                    "received": host.packets_received,
                }

            return {"raw": {"targets": per_target}, "details": {"per_target": per_target}}

        return self.timed(work)
