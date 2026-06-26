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

import base64
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit

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


def _origins_from_urls(urls: list[str]) -> list[str]:
    """Derive ``host:port`` origins (deduped, ordered) from configured URLs."""
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            continue
        port = parts.port or (443 if parts.scheme == "https" else 80)
        origin = f"{host}:{port}"
        if origin not in seen:
            seen.add(origin)
            out.append(origin)
    return out


def build_chromium_args(config: dict) -> list[str]:
    """Build Chromium launch flags from browser config.

    By default this is empty (Chromium's normal behavior). When ``http3`` is
    enabled we turn QUIC on and *force* it onto specific origins so the browser
    skips Alt-Svc discovery. This is required for meaningful HTTP/3 measurement:
    PathBrain uses a fresh context per URL and tears it down after one load, so
    the Alt-Svc cache (which is what normally lets Chromium upgrade TCP→QUIC on a
    *subsequent* connection) never survives to be used — every load would
    otherwise stay on HTTP/2. ``force_quic_origins`` (a list of ``host:port``)
    overrides the origins; when empty they're derived from the configured URLs.
    """
    if not config.get("http3"):
        return []
    args = ["--enable-quic"]
    origins = config.get("force_quic_origins") or _origins_from_urls(
        config.get("urls", [])
    )
    if origins:
        args.append("--origin-to-force-quic-on=" + ",".join(origins))
    return args


_NAV_JS = (
    "() => { const n = performance.getEntriesByType('navigation')[0];"
    " return n ? n.toJSON() : null; }"
)

# Installed (via add_init_script) before any page script runs, so the observers
# are buffering from the very start. We read `window.__paint` after load. FCP/LCP/
# INP are the core of the perception-led SOPS (Seat of Pants) score.
_PAINT_INIT_JS = """
(() => {
  window.__paint = { fcp: null, lcp: null, inp: null, cls_entries: [], long_tasks: [] };
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
  // Layout instability (CLS): raw per-shift values, excluding input-driven shifts.
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries())
        if (!e.hadRecentInput) window.__paint.cls_entries.push(e.value);
    }).observe({ type: 'layout-shift', buffered: true });
  } catch (e) {}
  // Main-thread blocking: raw long-task durations (>50ms by spec).
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) window.__paint.long_tasks.push(e.duration);
    }).observe({ type: 'longtask', buffered: true });
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


def _start_screencast(context, page, frames: list, run_dir: str, stamp: str, slug: str, t0) -> object | None:
    """Begin a CDP screencast, appending ``{t_ms, frame}`` per frame to ``frames``.

    Best-effort filmstrip capture: frames are written as JPEGs into the artifact
    dir, and the visual-completeness curve / Speed Index are *derived* from them
    later. Returns the CDP session (so the caller can stop it) or ``None`` if
    screencast isn't available, in which case Speed Index simply won't be derivable
    and its weight redistributes — same graceful-degradation model as the rest of
    the plugin.
    """
    from time import perf_counter

    try:
        cdp = context.new_cdp_session(page)
    except Exception:  # noqa: BLE001 — CDP unavailable
        return None

    counter = {"n": 0}

    def _on_frame(params: dict) -> None:
        try:
            n = counter["n"]
            counter["n"] = n + 1
            fname = f"{slug}-f{n:03d}.jpg"
            with open(os.path.join(run_dir, fname), "wb") as fh:
                fh.write(base64.b64decode(params["data"]))
            frames.append(
                {"t_ms": round((perf_counter() - t0) * 1000.0, 1), "frame": f"{stamp}/{fname}"}
            )
            cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
        except Exception:  # noqa: BLE001 — frame handling must never break the load
            pass

    try:
        cdp.on("Page.screencastFrame", _on_frame)
        cdp.send(
            "Page.startScreencast", {"format": "jpeg", "quality": 60, "everyNthFrame": 1}
        )
    except Exception:  # noqa: BLE001
        return None
    return cdp


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
            # Raw observations only: per-URL nav timing, paint/CLS/long-task entries,
            # total render, and the filmstrip. All metric derivation happens later.
            urls_raw: dict[str, dict] = {}
            per_url_display: dict[str, dict] = {}

            with sync_playwright() as pw:
                browser = pw.chromium.launch(
                    headless=headless, args=build_chromium_args(config)
                )
                try:
                    for idx, url in enumerate(urls):
                        slug = _slug(url, idx)
                        har_path = os.path.join(run_dir, f"{slug}.har") if want_har else None
                        shot_path = os.path.join(run_dir, f"{slug}.png") if want_screenshot else None
                        context = browser.new_context(record_har_path=har_path)
                        # Buffer paint/LCP/CLS/long-task timing from the very start.
                        context.add_init_script(_PAINT_INIT_JS)
                        page = context.new_page()
                        try:
                            from time import perf_counter

                            t0 = perf_counter()
                            frames: list[dict] = []
                            cdp = _start_screencast(context, page, frames, run_dir, stamp, slug, t0)

                            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                            try:
                                page.wait_for_load_state("networkidle", timeout=timeout_ms)
                            except Exception:  # noqa: BLE001 — idle may never settle
                                pass
                            total_render_ms = round((perf_counter() - t0) * 1000.0, 3)
                            if cdp is not None:
                                try:
                                    cdp.send("Page.stopScreencast")
                                except Exception:  # noqa: BLE001
                                    pass

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

                            urls_raw[url] = {
                                "nav": nav,
                                "paint": paint,
                                "total_render_ms": total_render_ms,
                                "filmstrip": frames,
                            }
                            per_url_display[url] = {
                                "screenshot_url": (
                                    f"/artifacts/{stamp}/{os.path.basename(shot_path)}"
                                    if shot_path
                                    else None
                                ),
                                "har_url": (
                                    f"/artifacts/{stamp}/{os.path.basename(har_path)}"
                                    if har_path
                                    else None
                                ),
                                "filmstrip_urls": [
                                    {"t_ms": f["t_ms"], "url": f"/artifacts/{f['frame']}"}
                                    for f in frames
                                ],
                            }
                        except Exception as exc:  # noqa: BLE001 — per-URL boundary
                            urls_raw[url] = {"error": f"{type(exc).__name__}: {exc}"}
                            per_url_display[url] = {"error": f"{type(exc).__name__}: {exc}"}
                        finally:
                            context.close()  # flushes the HAR file
                finally:
                    browser.close()

            return {
                "raw": {"urls": urls_raw},
                "details": {"per_url": per_url_display, "artifact_dir": run_dir},
            }

        return self.timed(work)
