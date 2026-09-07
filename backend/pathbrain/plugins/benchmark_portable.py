"""Portable-test plugin: the Away test's recipe, run at home by PathBrain's own Chromium.

The Away test (``portable.py``) is what a phone can measure from a browser tab, compared
"vs home". Its home reference used to depend on someone having run it at home first. This
plugin removes that dependency: it runs the **same recipe with the same in-browser code**
as an ordinary member of the suite, so every monitoring run, duel leg and profile test
carries a portable reading already stamped with the firewall profile and the time — a
per-profile, per-hour home baseline with no dedicated schedule.

One runner, not two: rather than re-implementing the waterfall in Python, the plugin loads
the app's own Away page (``/away?embedded=1``) in the browser plugin's Chromium and calls
the page's ``window.__pathbrainPortable.run(recipe)`` — the function the phone runs —
so the recipe and the measurement code cannot drift apart. The recipe is fetched from the
app's own API first, which also makes the plugin fail fast (no Chromium launched) when the
server isn't reachable, e.g. under the test suite.

It is a pure sensor like every plugin: it returns one portable **iteration** of raw per
call (the suite iterates), and ``interpret.derive`` derives the metrics. The runner also
files a ``PortableRun`` row for the run (``portable.record_server_run``) so the "vs home"
comparison can read it as a home sample from the device ``pathbrain-server`` — a
*different device* from any phone, and labelled as such.
"""
from __future__ import annotations

import json
import urllib.request

from ..logging_config import get_logger
from .base import BenchmarkPlugin, PluginResult, get_plugin, register

log = get_logger("plugins.portable")

SERVER_DEVICE_ID = "pathbrain-server"
DEFAULT_SELF_URL = "http://127.0.0.1:8000"
EMBED_PATH = "/away?embedded=1"


def fetch_recipe(self_url: str, timeout: float = 5.0) -> dict:
    """The current recipe from the app's own API (raises on any failure)."""
    req = urllib.request.Request(f"{self_url}/api/portable/recipe", headers={"User-Agent": "PathBrain/portable-plugin"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — our own server
        body = json.loads(resp.read().decode("utf-8"))
    if not isinstance(body, dict) or not body.get("instrument_version") or not body.get("resources"):
        raise ValueError("recipe endpoint returned no usable recipe")
    return body


@register
class PortableBenchmark(BenchmarkPlugin):
    name = "portable"
    description = (
        "The Away test's synthetic CDN waterfall, streamed download and round trips, run from "
        "PathBrain's own Chromium — the per-profile home reference for away runs"
    )

    def run(self, config: dict) -> PluginResult:
        if config.get("enabled") is False:
            return PluginResult(self.name, success=False, error="portable plugin disabled")
        self_url = str(config.get("self_url") or DEFAULT_SELF_URL).rstrip("/")
        timeout_ms = float(config.get("page_timeout_s", 30.0)) * 1000.0

        def work() -> dict:
            # 1. The recipe, from the same endpoint the phone reads. Done before any browser
            #    work so an unreachable server is a fast, cheap failure.
            recipe = fetch_recipe(self_url)
            body = {k: recipe[k] for k in ("resources", "stream", "rtt") if k in recipe}

            # 2. Borrow the browser plugin's Chromium. Plugins run on the one probe worker
            #    thread, so the handle's owner thread is this one; the browser plugin closes
            #    it at run end and accounts for its process tree — one place for all of that.
            browser_plugin = get_plugin("browser")
            if browser_plugin is None or not hasattr(browser_plugin, "borrow_browser"):
                raise RuntimeError("browser plugin unavailable; the portable plugin rides its Chromium")
            browser = browser_plugin.borrow_browser()

            # 3. Load the app's own page and run exactly one iteration with its code.
            context = browser.new_context()
            try:
                page = context.new_page()
                page.goto(f"{self_url}{EMBED_PATH}", wait_until="load", timeout=timeout_ms)
                page.wait_for_function(
                    "() => !!(window.__pathbrainPortable && window.__pathbrainPortable.ready)",
                    timeout=timeout_ms,
                )
                # The SAME sequence a phone runs (`runPortableTest`: a warm-up fetch, then the
                # recipe's iterations in this one page), so the reference and a phone run carry
                # the same connection warmth. A single cold `runOne` measured against a phone's
                # warm tab read as the phone beating the wire on every setup-bound metric —
                # the handshake cost, not the link. `runOne` stays the fallback for a page
                # served from an older bundle.
                doc = page.evaluate(
                    "(recipe) => (window.__pathbrainPortable.run || window.__pathbrainPortable.runOne)(recipe)",
                    body,
                )
                try:
                    client = page.evaluate("() => window.__pathbrainPortable.clientInfo()")
                except Exception:  # noqa: BLE001 — cosmetic
                    client = {}
            finally:
                try:
                    context.close()
                except Exception:  # noqa: BLE001 — best-effort; the browser plugin reaps
                    pass

            if isinstance(doc, dict) and isinstance(doc.get("iterations"), list):
                iterations = [it for it in doc["iterations"] if isinstance(it, dict) and "waterfall" in it]
            elif isinstance(doc, dict) and "waterfall" in doc:
                iterations = [doc]  # an older page: one cold iteration
            else:
                iterations = []
            if not iterations:
                raise ValueError("the page returned no iteration document")
            first = iterations[0]
            resources = (first.get("waterfall") or {}).get("resources") or []
            failed = {r.get("id"): r.get("error") for r in resources if isinstance(r, dict) and not r.get("ok")}
            return {
                "raw": {
                    "iterations": iterations,
                    "instrument_version": recipe["instrument_version"],
                    "client": {**(client or {}), "device": SERVER_DEVICE_ID},
                },
                "details": {
                    "instrument_version": recipe["instrument_version"],
                    "self_url": self_url,
                    "iterations": len(iterations),
                    "warm_up": "run" if len(iterations) > 1 or isinstance(doc.get("iterations"), list) else "none",
                    "resources": len(resources),
                    "resources_failed": failed,
                    "stream_ok": bool((first.get("stream") or {}).get("ok")),
                    "rtt_samples": len((first.get("rtt") or {}).get("samples_ms") or []),
                },
            }

        return self.timed(work)
