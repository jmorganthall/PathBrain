"""HTTP probe: raw TTFB, download duration and bytes transferred.

For each configured URL, measures time-to-first-byte, download duration and the
number of bytes received. A pure sensor: transfer speed (Mbps) is *derived* from
bytes + duration later (``pathbrain.interpret.derive``), not computed here.
"""
from __future__ import annotations

import time

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

    # Raw observations only: bytes + timing. Throughput (Mbps) is derived later.
    return {
        "ttfb_ms": round(ttfb_ms, 3) if ttfb_ms is not None else None,
        "download_ms": round(download_ms, 3),
        "total_ms": round(total_ms, 3),
        "bytes": total_bytes,
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
            # Raw observations only: per-URL timing + bytes.
            per_url: dict[str, dict] = {}
            for url in urls:
                try:
                    per_url[url] = _measure(url, timeout)
                except Exception as exc:  # noqa: BLE001
                    per_url[url] = {"error": f"{type(exc).__name__}: {exc}"}

            return {"raw": {"urls": per_url}, "details": {"per_url": per_url}}

        return self.timed(work)
