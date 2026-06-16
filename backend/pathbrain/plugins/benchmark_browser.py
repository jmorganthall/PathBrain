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

# Installed (via add_init_script) before any page script runs, so the observers
# are buffering from the very start. We read `window.__paint` after load. FCP/LCP/
# INP are the core of the perception-led SOPS (Seat of Pants) score.
_PAINT_INIT_JS = """
(() => {
  window.__paint = { fcp: null, lcp: null, inp: null };
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries())
        if (e.name === 'first-contentful-paint') window.__paint.fcp = e.startTime;
    }).observe({ type: 'paint', buffered: true });
  } catch (e) {}
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries())
        window.__paint.lcp = e.startTime || e.renderTime || e.loadTime;
    }).observe({ type: 'largest-contentful-paint', buffered: true });
  } catch (e) {}
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) {
        const d = e.duration || 0;
        if (window.__paint.inp == null || d > window.__paint.inp) window.__paint.inp = d;
      }
    }).observe({ type: 'event', durationThreshold: 16, buffered: true });
  } catch (e) {}
})()
"""

_PAINT_READ_JS = "() => window.__paint || null"


def extract_paint_metrics(paint: dict | None) -> dict:
    """Normalize the captured ``window.__paint`` into perceptual metric values.

    Returns ``fcp_ms`` (First Contentful Paint), ``lcp_ms`` (Largest Contentful
    Paint) and ``inp_ms`` (Interaction to Next Paint — best-effort, ``None`` when
    no interaction was observed). All in milliseconds; ``None`` for any missing.
    """
    paint = paint or {}

    def ms(v) -> float | None:
        return round(float(v), 3) if isinstance(v, (int, float)) and v >= 0 else None

    return {
        "fcp_ms": ms(paint.get("fcp")),
        "lcp_ms": ms(paint.get("lcp")),
        "inp_ms": ms(paint.get("inp")),
    }


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
            fcps: list[float] = []
            lcps: list[float] = []
            inps: list[float] = []

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=headless)
                try:
                    for idx, url in enumerate(urls):
                        slug = _slug(url, idx)
                        har_path = os.path.join(run_dir, f"{slug}.har") if want_har else None
                        shot_path = os.path.join(run_dir, f"{slug}.png") if want_screenshot else None
                        context = browser.new_context(record_har_path=har_path)
                        # Buffer paint/LCP/interaction timing from the very start.
                        context.add_init_script(_PAINT_INIT_JS)
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

                            # Best-effort INP: drive a few synthetic interactions and
                            # let event-timing settle before reading the observers.
                            try:
                                page.mouse.click(5, 5)
                                page.keyboard.press("Tab")
                                page.mouse.wheel(0, 400)
                                page.wait_for_timeout(200)
                            except Exception:  # noqa: BLE001 — interaction is optional
                                pass

                            paint = None
                            try:
                                paint = page.evaluate(_PAINT_READ_JS)
                            except Exception:  # noqa: BLE001 — paint capture is optional
                                pass

                            if want_screenshot and shot_path:
                                page.screenshot(path=shot_path)

                            metrics = compute_navigation_metrics(nav)
                            metrics["total_render_ms"] = total_render_ms
                            metrics.update(extract_paint_metrics(paint))
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
                            if metrics.get("fcp_ms") is not None:
                                fcps.append(metrics["fcp_ms"])
                            if metrics.get("lcp_ms") is not None:
                                lcps.append(metrics["lcp_ms"])
                            if metrics.get("inp_ms") is not None:
                                inps.append(metrics["inp_ms"])
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
                "fcp_ms": round(mean(fcps), 3) if fcps else None,
                "lcp_ms": round(mean(lcps), 3) if lcps else None,
                "inp_ms": round(mean(inps), 3) if inps else None,
            }
            return {
                "metrics": metrics,
                "details": {"per_url": per_url, "artifact_dir": run_dir},
            }

        return self.timed(work)
