"""ICMP benchmark: latency, jitter, packet loss.

Uses ``icmplib`` in unprivileged mode (SOCK_DGRAM) where the OS allows it,
falling back to privileged raw sockets. Results are aggregated across all
configured targets into mean latency / jitter / packet-loss metrics, with a
per-target breakdown in ``details``.
"""
from __future__ import annotations

from statistics import mean, pstdev

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
            per_target: dict[str, dict] = {}
            latencies: list[float] = []
            jitters: list[float] = []
            losses: list[float] = []

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
                    per_target[target] = {"error": f"{type(exc).__name__}: {exc}"}
                    losses.append(100.0)
                    continue

                rtts = list(host.rtts)
                jitter = round(pstdev(rtts), 3) if len(rtts) > 1 else 0.0
                loss_pct = round(host.packet_loss * 100.0, 3)
                per_target[target] = {
                    "latency_ms": round(host.avg_rtt, 3) if host.is_alive else None,
                    "min_ms": round(host.min_rtt, 3) if host.is_alive else None,
                    "max_ms": round(host.max_rtt, 3) if host.is_alive else None,
                    "jitter_ms": jitter,
                    "packet_loss_pct": loss_pct,
                    "alive": host.is_alive,
                }
                if host.is_alive:
                    latencies.append(host.avg_rtt)
                    jitters.append(jitter)
                losses.append(loss_pct)

            metrics = {
                "latency_ms": round(mean(latencies), 3) if latencies else None,
                "jitter_ms": round(mean(jitters), 3) if jitters else None,
                "packet_loss_pct": round(mean(losses), 3) if losses else None,
            }
            return {"metrics": metrics, "details": {"per_target": per_target}}

        return self.timed(work)
