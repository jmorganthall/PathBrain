"""DNS benchmark: lookup time across resolvers.

Measures how long each configured resolver takes to answer A-record queries for
a set of hostnames, averaged into a single ``lookup_ms`` metric with a
per-provider breakdown.
"""
from __future__ import annotations

import time
from statistics import mean

import dns.message
import dns.query
import dns.rdatatype
import dns.resolver

from .base import BenchmarkPlugin, PluginResult, register


def _query_server(server: str, hostname: str, timeout: float) -> float:
    """Return lookup time in ms for one hostname against one resolver."""
    query = dns.message.make_query(hostname, dns.rdatatype.A)
    start = time.perf_counter()
    dns.query.udp(query, server, timeout=timeout)
    return (time.perf_counter() - start) * 1000.0


def _query_local(hostname: str, timeout: float) -> float:
    resolver = dns.resolver.Resolver()
    resolver.lifetime = timeout
    resolver.timeout = timeout
    start = time.perf_counter()
    resolver.resolve(hostname, "A")
    return (time.perf_counter() - start) * 1000.0


@register
class DnsBenchmark(BenchmarkPlugin):
    name = "dns"
    description = "DNS lookup time across local resolver, Cloudflare, Google, Quad9"

    def run(self, config: dict) -> PluginResult:
        providers: list[dict] = config.get("providers", [])
        hostnames: list[str] = config.get("hostnames", [])
        timeout = float(config.get("timeout_s", 3.0))

        if not providers or not hostnames:
            return PluginResult(
                self.name, success=False, error="DNS providers/hostnames not configured"
            )

        def work() -> dict:
            per_provider: dict[str, dict] = {}
            all_times: list[float] = []

            for provider in providers:
                label = provider.get("name", provider.get("server", "?"))
                server = provider.get("server")
                times: list[float] = []
                errors: list[str] = []
                for hostname in hostnames:
                    try:
                        if server in (None, "", "local", "system"):
                            elapsed = _query_local(hostname, timeout)
                        else:
                            elapsed = _query_server(server, hostname, timeout)
                        times.append(elapsed)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{hostname}: {type(exc).__name__}")

                per_provider[label] = {
                    "server": server,
                    "lookup_ms": round(mean(times), 3) if times else None,
                    "samples": len(times),
                    "errors": errors or None,
                }
                all_times.extend(times)

            metrics = {"lookup_ms": round(mean(all_times), 3) if all_times else None}
            return {"metrics": metrics, "details": {"per_provider": per_provider}}

        return self.timed(work)
