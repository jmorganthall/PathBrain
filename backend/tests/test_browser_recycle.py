"""The reused Chromium is recycled by age, so a browser that lives for a whole duel window
cannot bloat the machine the pages are rendered on."""
from __future__ import annotations

import sys
import threading
import types

from pathbrain import browser_procs
from pathbrain.plugins.benchmark_browser import BrowserBenchmark, should_recycle


def test_the_recycle_decision_is_pages_or_minutes_whichever_first():
    cfg = {"recycle_after_pages": 60, "recycle_after_minutes": 30}
    assert should_recycle(0, 0.0, cfg) is None
    assert should_recycle(59, 29 * 60.0, cfg) is None
    assert "60 page loads" in should_recycle(60, 0.0, cfg)
    assert "30 min" in should_recycle(1, 30 * 60.0, cfg)
    # 0 disables either bound; defaults apply when the keys are absent.
    assert should_recycle(10_000, 10.0, {"recycle_after_pages": 0, "recycle_after_minutes": 30}) is None
    assert should_recycle(1, 10 * 3600.0, {"recycle_after_pages": 60, "recycle_after_minutes": 0}) is None
    assert should_recycle(60, 0.0, {}) is not None and should_recycle(59, 0.0, {}) is None


class _FakeBrowser:
    def __init__(self, log: list[str], tag: str) -> None:
        self._log, self.tag = log, tag

    def is_connected(self) -> bool:
        return True

    def close(self) -> None:
        self._log.append(f"close:{self.tag}")


def _fake_playwright(monkeypatch, log: list[str]):
    launched = {"n": 0}

    class _FakePw:
        class chromium:  # noqa: N801 — mirrors playwright's attribute
            @staticmethod
            def launch(**kwargs):  # noqa: ARG004
                launched["n"] += 1
                return _FakeBrowser(log, f"b{launched['n']}")

        def stop(self):
            log.append("stop")

    class _FakeCtx:
        def start(self):
            return _FakePw()

    fake = types.ModuleType("playwright.sync_api")
    fake.sync_playwright = lambda: _FakeCtx()
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
    monkeypatch.setattr(browser_procs, "reap_orphans", lambda keep=(): {"reaped": 0, "pids": [], "strays": 0})
    monkeypatch.setattr(browser_procs, "driver_pids", lambda: [])
    monkeypatch.setattr(browser_procs, "wait_gone", lambda pid, timeout_s=2.0: True)
    return launched


def test_a_browser_past_its_page_bound_is_closed_and_relaunched_at_the_seam(monkeypatch):
    log: list[str] = []
    launched = _fake_playwright(monkeypatch, log)
    plugin = BrowserBenchmark()
    cfg = {"recycle_after_pages": 12, "recycle_after_minutes": 0}

    first = plugin._ensure_browser(cfg)
    assert launched["n"] == 1 and first.tag == "b1" and plugin._pages_since_launch == 0
    # Under the bound the same browser is handed back.
    plugin._pages_since_launch += 11
    assert plugin._ensure_browser(cfg).tag == "b1" and launched["n"] == 1
    # Six pages, cold + warm: twelve loads reach the bound, recycled at the next seam.
    plugin._pages_since_launch += 1
    second = plugin._ensure_browser(cfg)
    assert second.tag == "b2" and launched["n"] == 2
    assert log == ["close:b1", "stop"]
    assert plugin._pages_since_launch == 0 and plugin._owner_thread == threading.get_ident()
    stats = plugin.cleanup_stats()
    assert stats["recycled"] == 1 and stats["pages_since_launch"] == 0 and stats["browser_age_s"] is not None


def test_a_recycle_request_from_outside_is_honoured_once_at_the_next_seam(monkeypatch):
    log: list[str] = []
    launched = _fake_playwright(monkeypatch, log)
    plugin = BrowserBenchmark()
    cfg = {"recycle_after_pages": 0, "recycle_after_minutes": 0}  # no bounds at all
    plugin._ensure_browser(cfg)
    plugin.request_recycle("resource pressure")
    assert plugin.cleanup_stats()["recycle_requested"] == "resource pressure"
    assert plugin._ensure_browser(cfg).tag == "b2" and launched["n"] == 2
    # Cleared by the relaunch: no second recycle on the next call.
    assert plugin._ensure_browser(cfg).tag == "b2" and launched["n"] == 2
    assert plugin.cleanup_stats()["recycle_requested"] is None


def test_borrowing_the_browser_counts_toward_its_age(monkeypatch):
    log: list[str] = []
    _fake_playwright(monkeypatch, log)
    plugin = BrowserBenchmark()
    plugin.borrow_browser({})
    plugin.borrow_browser({})
    assert plugin._pages_since_launch == 2
