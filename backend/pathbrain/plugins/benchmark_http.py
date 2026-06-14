"""HTTP benchmark: TTFB, download duration, transfer speed.

For each configured URL, measures time-to-first-byte, total download duration,
and the resulting transfer speed, averaged across URLs.
"""
from __future__ import annotations

import time
from statistics import mean

import httpx

from .base import BenchmarkPlugin, PluginResult, register


def _measure(url: str, timeout: float) -> dict:
    start = time.perf_counter()
    ttfb_ms: float | None = None
    total_bytes = 0

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                if ttfb_ms is None:
                    ttfb_ms = (time.perf_counter() - start) * 1000.0
                total_bytes += len(chunk)

    total_ms = (time.perf_counter() - start) * 1000.0
    download_ms = max(total_ms - (ttfb_ms or 0.0), 0.0)
    # Transfer speed over the download phase, in megabits per second.
    mbps = None
    if download_ms > 0 and total_bytes > 0:
        mbps = (total_bytes * 8) / (download_ms / 1000.0) / 1_000_000.0

    return {
        "ttfb_ms": round(ttfb_ms, 3) if ttfb_ms is not None else None,
        "download_ms": round(download_ms, 3),
        "total_ms": round(total_ms, 3),
        "bytes": total_bytes,
        "transfer_mbps": round(mbps, 3) if mbps is not None else None,
    }


@register
class HttpBenchmark(BenchmarkPlugin):
    name = "http"
    description = "HTTP TTFB, download duration and transfer speed across URLs"

    def run(self, config: dict) -> PluginResult:
        urls: list[str] = config.get("urls", [])
        timeout = float(config.get("timeout_s", 15.0))

        if not urls:
            return PluginResult(self.name, success=False, error="No HTTP URLs configured")

        def work() -> dict:
            per_url: dict[str, dict] = {}
            ttfbs: list[float] = []
            downloads: list[float] = []
            speeds: list[float] = []

            for url in urls:
                try:
                    m = _measure(url, timeout)
                    per_url[url] = m
                    if m["ttfb_ms"] is not None:
                        ttfbs.append(m["ttfb_ms"])
                    downloads.append(m["download_ms"])
                    if m["transfer_mbps"] is not None:
                        speeds.append(m["transfer_mbps"])
                except Exception as exc:  # noqa: BLE001
                    per_url[url] = {"error": f"{type(exc).__name__}: {exc}"}

            metrics = {
                "ttfb_ms": round(mean(ttfbs), 3) if ttfbs else None,
                "download_ms": round(mean(downloads), 3) if downloads else None,
                "transfer_mbps": round(mean(speeds), 3) if speeds else None,
            }
            return {"metrics": metrics, "details": {"per_url": per_url}}

        return self.timed(work)
