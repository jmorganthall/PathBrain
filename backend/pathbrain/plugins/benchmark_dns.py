"""DNS probe: raw A-record lookup times across resolvers.

Measures how long each configured resolver takes to answer A-record queries for
a set of hostnames. A pure sensor: it emits the raw per-lookup time samples; the
aggregate ``lookup_ms`` metric is derived later (``pathbrain.interpret.derive``).
"""
from __future__ import annotations

import time

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
            # Raw observations only: per-provider list of per-hostname lookup times.
            providers_raw: list[dict] = []
            per_provider: dict[str, dict] = {}

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
                        times.append(round(elapsed, 3))
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{hostname}: {type(exc).__name__}")

                providers_raw.append(
                    {"label": label, "server": server, "lookups_ms": times, "errors": errors or None}
                )
                per_provider[label] = {
                    "server": server,
                    "samples": len(times),
                    "errors": errors or None,
                }

            return {
                "raw": {"providers": providers_raw},
                "details": {"per_provider": per_provider},
            }

        return self.timed(work)
