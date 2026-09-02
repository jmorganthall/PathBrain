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

from .. import browser_procs
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
  // Generous Resource Timing buffer so heavy pages don't silently drop entries
  // (the default 250 fills up fast); top up again if it ever does fill.
  try {
    performance.setResourceTimingBufferSize(1000);
    performance.addEventListener('resourcetimingbufferfull', () => {
      try { performance.setResourceTimingBufferSize(2000); } catch (e) {}
    });
  } catch (e) {}
  // Long Animation Frames (Chromium-only, newer) for network-vs-render stall
  // attribution; fall back to longtask; if neither, leave source null -> 'unknown'.
  window.__loaf = { entries: [], source: null };
  try {
    const supported = (window.PerformanceObserver
      && PerformanceObserver.supportedEntryTypes) || [];
    const loafType = supported.indexOf('long-animation-frame') >= 0
      ? 'long-animation-frame'
      : (supported.indexOf('longtask') >= 0 ? 'longtask' : null);
    if (loafType) {
      window.__loaf.source = loafType === 'long-animation-frame' ? 'loaf' : 'longtask';
      new PerformanceObserver((l) => {
        for (const e of l.getEntries())
          window.__loaf.entries.push({ startTime: e.startTime, duration: e.duration });
      }).observe({ type: loafType, buffered: true });
    }
  } catch (e) {}
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

# Resource Timing entries for the perceived-load-smoothness instrument. Only the
# fields the smoothness math needs (responseEnd + transferSize + nextHopProtocol),
# kept minimal so raw stays small. Cross-origin entries without Timing-Allow-Origin
# expose these but zero the *phase* timings — still enough for stall/cadence/bytes.
_RESOURCE_JS = """
() => performance.getEntriesByType('resource').map((r) => ({
  name: r.name,
  startTime: r.startTime,
  responseStart: r.responseStart,
  responseEnd: r.responseEnd,
  transferSize: r.transferSize,
  encodedBodySize: r.encodedBodySize,
  nextHopProtocol: r.nextHopProtocol,
}))
"""

_LOAF_READ_JS = "() => window.__loaf || null"


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


def _clear_stale_asyncio_loop() -> None:
    """Clear a stale asyncio running-loop marker left on this thread by a dead Playwright.

    Playwright's sync API parks a running event loop on its creating thread (inside a
    greenlet) for as long as the started playwright lives; ``stop()`` is what clears the
    thread-local marker. A playwright that dies without ``stop()`` leaves the marker set
    on a thread that outlives it — and the probe worker deliberately outlives everything.
    Called ONLY when the caller holds no live Playwright on this thread, so anything the
    marker points at is unreachable garbage; clearing it un-poisons the thread. Uses the
    stable-internal ``asyncio.events`` accessors (the same ones event loops themselves
    use); best-effort, never raises.
    """
    try:
        import asyncio

        if asyncio.events._get_running_loop() is not None:
            log.warning(
                "Probe thread carried a stale asyncio loop marker (a sync Playwright died "
                "without stop()) — cleared so browser measurements can continue"
            )
            asyncio.events._set_running_loop(None)
    except Exception:  # noqa: BLE001 — the guard must never be the reason a probe fails
        pass


@register
class BrowserBenchmark(BenchmarkPlugin):
    name = "browser"
    description = "Headless-Chromium page-load timing and total render (Playwright)"

    def __init__(self) -> None:
        # One Chromium instance is launched lazily and **reused across a run's
        # iterations** (a fresh context per URL still isolates each load), then closed
        # in ``teardown`` — so we pay the ~0.5–1s cold start once per run, not per
        # iteration. The runner serializes runs, so this singleton is only ever live
        # within a single run on a single thread.
        self._pw = None
        self._browser = None
        # The OS pid of the node driver behind ``self._pw``, recorded at launch. Python
        # handles are the only thing ``close()``/``stop()``/``abandon()`` touch, and all
        # three can leave the actual process alive — a cross-thread close raises, a
        # wedged one never returns, an abandoned one is deliberately not closed. The pid
        # is what lets us tell "closed" from "believed closed", and reap the difference.
        self._driver_pid: int | None = None
        #: Closes that did not actually free the process tree (each one is a leak we
        #: caught rather than one we shipped). Surfaced via :func:`cleanup_stats`.
        self._cleanup_failures = 0
        self._reaped = 0

    def _ensure_browser(self, config: dict):
        """Return a live Chromium, reusing the cached one or launching a fresh one.

        Raises if Playwright/Chromium is unavailable (caller turns it into a
        ``success=False`` result, same graceful-degradation as before)."""
        if self._browser is not None:
            try:
                if self._browser.is_connected():
                    return self._browser
            except Exception:  # noqa: BLE001 — stale/cross-thread handle; relaunch
                pass
            self._close_browser()
        from playwright.sync_api import sync_playwright

        # The probe worker thread is LONG-LIVED (probes.py), and a sync Playwright that
        # died without ``stop()`` — a launch that raised, a ``stop()`` that failed, handles
        # dropped by ``abandon()`` on this same thread — leaves asyncio's thread-local
        # running-loop marker set. Every later ``sync_playwright().start()`` on the thread
        # then refuses with "Sync API inside the asyncio loop", so ONE bad browser session
        # used to poison every subsequent run until the process restarted (the "all runs
        # stopped reporting" incident: every run scored incomparable/legacy). At this point
        # we provably hold no live Playwright, so any marker on the thread is that stale
        # garbage — clear it instead of dying on it.
        _clear_stale_asyncio_loop()

        # We provably hold no browser here (``self._browser`` is None or was just
        # discarded), so any driver tree still running under this process is garbage —
        # dropped by ``abandon()`` after a wedged probe, or left behind by a ``stop()``
        # that could not complete. Nothing in Python can reach it, so nothing but this
        # will ever free it. Reaping BEFORE launching keeps the steady state at one tree.
        reaped = browser_procs.reap_orphans()
        self._reaped += int(reaped.get("reaped") or 0)

        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(
                headless=bool(config.get("headless", True)), args=build_chromium_args(config)
            )
        except BaseException:
            # The launch failed but the playwright STARTED — stopping it is what keeps
            # this thread's asyncio marker clean for the next attempt. Without this, the
            # first Chromium hiccup (an OOM, a missing dep, a crash) breaks every
            # browser measurement that follows it.
            try:
                pw.stop()
            except Exception:  # noqa: BLE001 — best-effort; the marker guard above heals the rest
                pass
            raise
        self._pw = pw
        self._browser = browser
        # Exactly one driver is alive at this point (we reaped the rest above), so the
        # single remaining pid is this playwright's.
        pids = browser_procs.driver_pids()
        self._driver_pid = pids[-1] if pids else None
        return self._browser

    def _close_browser(self) -> None:
        """Close Chromium and stop the driver, then **verify the process actually died**.

        Both calls are best-effort by necessity, and both have a failure mode that leaves
        a live process behind while looking exactly like success from Python: called from
        a thread that does not own the objects, Playwright's sync API raises a
        cross-thread greenlet error, which a bare ``except`` swallows — so the handles are
        dropped, nothing is closed, and a node driver plus its Chromium tree is orphaned.
        That is precisely how a host with 32 GiB ended up with 593 MiB free.

        So the outcome is checked against the OS rather than assumed from the absence of
        an exception: if the driver pid is still alive after ``stop()``, the tree is
        killed and the failure counted. Never raises.
        """
        driver_pid = self._driver_pid
        failed: list[str] = []
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception as exc:  # noqa: BLE001 — best-effort; verified below
            failed.append(f"browser.close: {type(exc).__name__}: {exc}")
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception as exc:  # noqa: BLE001 — best-effort; verified below
            failed.append(f"playwright.stop: {type(exc).__name__}: {exc}")
        self._browser = None
        self._pw = None
        self._driver_pid = None

        # Verify. A driver that is still running owns a Chromium tree that no Python
        # reference reaches any more, so killing it is the only remaining option and is
        # safe by construction — it is our own child and we have just let go of it.
        leftover = browser_procs.reap_orphans()
        stranded = int(leftover.get("reaped") or 0)
        if stranded:
            self._reaped += stranded
            self._cleanup_failures += 1
            log.warning(
                "Browser close left %s driver tree(s) running (pid %s); killed. %s",
                stranded, driver_pid, "; ".join(failed) or "close() reported no error",
            )
        elif failed:
            # The process is gone, so nothing leaked — but a close that raised is still
            # worth a line, since it usually means we were called from the wrong thread.
            log.info("Browser close reported errors but the process tree is gone: %s",
                     "; ".join(failed))

    def teardown(self) -> None:
        """Close the reused Chromium at the end of a run (never raises)."""
        self._close_browser()

    def abandon(self) -> None:
        """Drop the Chromium handles without touching them (never raises, never blocks).

        Called when a probe blew its deadline and its thread was abandoned mid-call. The
        browser and the Playwright connection belong to that thread and may be exactly
        what is wedged, so ``close()`` here would hang the *next* thread too. Letting go
        of the references instead leaves the old process unreachable from Python, and lets
        the next probe launch a fresh one.

        The *process* is then killed outright — which is not the same thing as closing it,
        and is the only option left. Playwright's close path is what we cannot use here;
        SIGKILL needs no cooperation from the wedged browser and no thread affinity. This
        is what bounds the leak: without it every stall permanently cost a node driver and
        a Chromium tree, and 224 of them ate a 32 GiB host. Killing it also unblocks the
        abandoned worker's protocol read, so the thread parks idle instead of blocked.
        Non-blocking (a ``/proc`` scan and a signal) and never raises, as the contract
        requires.
        """
        self._browser = None
        self._pw = None
        self._driver_pid = None
        try:
            reaped = browser_procs.reap_orphans()
            self._reaped += int(reaped.get("reaped") or 0)
        except Exception:  # noqa: BLE001 — abandon must never raise
            log.warning("Reaping an abandoned browser tree failed", exc_info=True)

    def cleanup_stats(self) -> dict:
        """Closes that did not free their process tree, and trees reaped since start."""
        return {
            "cleanup_failures": self._cleanup_failures,
            "reaped": self._reaped,
            "driver_pid": self._driver_pid,
        }

    def run(self, config: dict) -> PluginResult:
        urls: list[str] = config.get("urls", [])
        if not urls:
            return PluginResult(self.name, success=False, error="No browser URLs configured")

        try:
            browser = self._ensure_browser(config)
        except Exception as exc:  # noqa: BLE001 — ImportError or env issue
            return PluginResult(self.name, success=False, error=f"{_INSTALL_HINT} ({exc})")

        timeout_ms = float(config.get("timeout_s", 30.0)) * 1000.0
        # The `networkidle` settle gets its own (short) cap instead of reusing the full
        # nav timeout: a page with trackers/long-poll may never go idle, and reusing the
        # 30s nav timeout made every such URL pay up to 30s of dead waiting. The wait is
        # still useful (lets late resources land for the smoothness metrics), just bounded.
        idle_timeout_ms = float(config.get("networkidle_timeout_s", 5.0)) * 1000.0
        wait_until = config.get("wait_until", "load")
        # Screenshot + HAR feed only the artifacts UI (no scored metric), so they're OFF
        # by default now — opt in for debugging a specific run.
        want_screenshot = bool(config.get("screenshot", False))
        want_har = bool(config.get("har", False))
        # The CDP screencast filmstrip (per-frame JPEG) is CPU-intensive and only
        # feeds the pixel-based Speed Index / paint-cadence diagnostics. Off by
        # default — scored SOPS smoothness now comes from the byte-arrival metrics.
        want_filmstrip = bool(config.get("filmstrip", False))

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

            # ``browser`` is the run-scoped Chromium (reused across iterations); each URL
            # still gets a fresh context so loads stay isolated. It's closed in teardown().
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
                    cdp = (
                        _start_screencast(context, page, frames, run_dir, stamp, slug, t0)
                        if want_filmstrip
                        else None
                    )

                    page.goto(url, wait_until=wait_until, timeout=timeout_ms)
                    try:
                        page.wait_for_load_state("networkidle", timeout=idle_timeout_ms)
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

                    # Resource Timing + LoAF for the smoothness instrument.
                    # Read after the synthetic interaction so the full
                    # initial-load resource set is captured. Best-effort.
                    resources = None
                    try:
                        resources = page.evaluate(_RESOURCE_JS)
                    except Exception:  # noqa: BLE001 — optional
                        pass
                    loaf = None
                    try:
                        loaf = page.evaluate(_LOAF_READ_JS)
                    except Exception:  # noqa: BLE001 — optional
                        pass

                    if want_screenshot and shot_path:
                        page.screenshot(path=shot_path)

                    urls_raw[url] = {
                        "nav": nav,
                        "paint": paint,
                        "total_render_ms": total_render_ms,
                        "filmstrip": frames,
                        "resources": resources,
                        "loaf": loaf,
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

            return {
                "raw": {"urls": urls_raw},
                "details": {"per_url": per_url_display, "artifact_dir": run_dir},
            }

        return self.timed(work)
