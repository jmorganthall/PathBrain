"""Browser benchmark: real headless-Chromium page-load timing via Playwright.

This is the metric that most directly models *human-perceived* responsiveness:
how long a real browser takes to fetch, parse and render a page. It emits
``total_render_ms`` — the SOPS ``render`` metric (25% default weight) — which the
scoring engine picks up automatically (see ``scoring.METRIC_SOURCES``).

For each configured URL it captures the W3C Navigation Timing breakdown
(DNS / TCP / TLS / TTFB / DOMContentLoaded / load), measures wall-clock time to
network idle (``total_render_ms``), and optionally stores a screenshot and HAR
file under the artifact directory.

Playwright is imported lazily inside ``run`` so the plugin registry still loads
on hosts where Playwright / Chromium isn't installed; in that case the plugin
returns ``success=False`` with guidance and the ``render`` weight is redistributed.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from statistics import mean

from ..config import get_settings
from ..logging_config import get_logger
from .base import BenchmarkPlugin, PluginResult, register

log = get_logger("plugins.browser")

_INSTALL_HINT = (
    "Playwright/Chromium unavailable. Install with "
    "`pip install playwright && playwright install --with-deps chromium`."
)


def compute_navigation_metrics(nav: dict | None) -> dict:
    """Derive page-load sub-metrics from a PerformanceNavigationTiming entry.

    ``nav`` is the JSON form of ``performance.getEntriesByType('navigation')[0]``.
    All values are milliseconds relative to the entry's own timeline. Returns
    ``None`` for any metric that can't be derived.
    """
    nav = nav or {}

    def span(end: str, start: str) -> float | None:
        a, b = nav.get(end), nav.get(start)
        if a is None or b is None:
            return None
        delta = a - b
        return round(delta, 3) if delta >= 0 else None

    origin = nav.get("startTime", 0) or 0
    secure = nav.get("secureConnectionStart") or 0
    connect_end = nav.get("connectEnd")

    # TLS time is connectEnd - secureConnectionStart, but only when TLS occurred.
    tls_ms: float | None
    if secure and connect_end is not None and connect_end >= secure:
        tls_ms = round(connect_end - secure, 3)
    else:
        tls_ms = 0.0

    def since_origin(field: str) -> float | None:
        v = nav.get(field)
        if not v:
            return None
        delta = v - origin
        return round(delta, 3) if delta >= 0 else None

    return {
        "dns_ms": span("domainLookupEnd", "domainLookupStart"),
        "tcp_ms": span("connectEnd", "connectStart"),
        "tls_ms": tls_ms,
        "ttfb_ms": span("responseStart", "requestStart"),
        "dom_content_loaded_ms": since_origin("domContentLoadedEventEnd"),
        "load_event_ms": since_origin("loadEventEnd"),
    }


_NAV_JS = (
    "() => { const n = performance.getEntriesByType('navigation')[0];"
    " return n ? n.toJSON() : null; }"
)


@register
class BrowserBenchmark(BenchmarkPlugin):
    name = "browser"
    description = "Headless-Chromium page-load timing and total render (Playwright)"

    def run(self, config: dict) -> PluginResult:
        urls: list[str] = config.get("urls", [])
        if not urls:
            return PluginResult(self.name, success=False, error="No browser URLs configured")

        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001 — ImportError or env issue
            return PluginResult(self.name, success=False, error=f"{_INSTALL_HINT} ({exc})")

        timeout_ms = float(config.get("timeout_s", 30.0)) * 1000.0
        wait_until = config.get("wait_until", "load")
        headless = bool(config.get("headless", True))
        want_screenshot = bool(config.get("screenshot", True))
        want_har = bool(config.get("har", True))

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        base_dir = os.path.abspath(get_settings().artifact_dir)
        run_dir = os.path.join(base_dir, stamp)
        os.makedirs(run_dir, exist_ok=True)

        def _slug(url: str, idx: int) -> str:
            safe = "".join(c if c.isalnum() else "-" for c in url)[:48].strip("-")
            return f"{idx:02d}-{safe or 'page'}"

        def work() -> dict:
            per_url: dict[str, dict] = {}
            renders: list[float] = []
            ttfbs: list[float] = []
            dcls: list[float] = []
            loads: list[float] = []

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=headless)
                try:
                    for idx, url in enumerate(urls):
                        slug = _slug(url, idx)
                        har_path = os.path.join(run_dir, f"{slug}.har") if want_har else None
                        shot_path = os.path.join(run_dir, f"{slug}.png") if want_screenshot else None
                        context = browser.new_context(record_har_path=har_path)
                        page = context.new_page()
                        try:
                            from time import perf_counter

                            t0 = perf_counter()
                            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                            try:
                                page.wait_for_load_state("networkidle", timeout=timeout_ms)
                            except Exception:  # noqa: BLE001 — idle may never settle
                                pass
                            total_render_ms = round((perf_counter() - t0) * 1000.0, 3)

                            nav = page.evaluate(_NAV_JS)
                            if want_screenshot and shot_path:
                                page.screenshot(path=shot_path)

                            metrics = compute_navigation_metrics(nav)
                            metrics["total_render_ms"] = total_render_ms
                            metrics["screenshot_url"] = (
                                f"/artifacts/{stamp}/{os.path.basename(shot_path)}"
                                if shot_path
                                else None
                            )
                            metrics["har_url"] = (
                                f"/artifacts/{stamp}/{os.path.basename(har_path)}"
                                if har_path
                                else None
                            )
                            per_url[url] = metrics

                            renders.append(total_render_ms)
                            if metrics.get("ttfb_ms") is not None:
                                ttfbs.append(metrics["ttfb_ms"])
                            if metrics.get("dom_content_loaded_ms") is not None:
                                dcls.append(metrics["dom_content_loaded_ms"])
                            if metrics.get("load_event_ms") is not None:
                                loads.append(metrics["load_event_ms"])
                        except Exception as exc:  # noqa: BLE001 — per-URL boundary
                            per_url[url] = {"error": f"{type(exc).__name__}: {exc}"}
                        finally:
                            context.close()  # flushes the HAR file
                finally:
                    browser.close()

            metrics = {
                "total_render_ms": round(mean(renders), 3) if renders else None,
                "ttfb_ms": round(mean(ttfbs), 3) if ttfbs else None,
                "dom_content_loaded_ms": round(mean(dcls), 3) if dcls else None,
                "load_event_ms": round(mean(loads), 3) if loads else None,
            }
            return {
                "metrics": metrics,
                "details": {"per_url": per_url, "artifact_dir": run_dir},
            }

        return self.timed(work)
