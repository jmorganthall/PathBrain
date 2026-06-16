"""Tests for the browser (Playwright) benchmark plugin.

These cover the pure navigation-timing math and graceful degradation. They do
not require Playwright or Chromium to be installed.
"""
from __future__ import annotations

import importlib.util

from pathbrain.plugins import get_plugin
from pathbrain.plugins.benchmark_browser import (
    compute_navigation_metrics,
    extract_paint_metrics,
)

_HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None


def test_navigation_metrics_typical():
    nav = {
        "startTime": 0,
        "domainLookupStart": 5,
        "domainLookupEnd": 12,
        "connectStart": 12,
        "secureConnectionStart": 20,
        "connectEnd": 40,
        "requestStart": 40,
        "responseStart": 95,
        "responseEnd": 120,
        "domContentLoadedEventEnd": 300,
        "loadEventEnd": 520,
    }
    m = compute_navigation_metrics(nav)
    assert m["dns_ms"] == 7
    assert m["tcp_ms"] == 28
    assert m["tls_ms"] == 20  # connectEnd - secureConnectionStart
    assert m["ttfb_ms"] == 55
    assert m["dom_content_loaded_ms"] == 300
    assert m["load_event_ms"] == 520


def test_navigation_metrics_no_tls():
    nav = {
        "startTime": 0,
        "domainLookupStart": 0,
        "domainLookupEnd": 0,
        "connectStart": 0,
        "secureConnectionStart": 0,
        "connectEnd": 3,
        "requestStart": 3,
        "responseStart": 10,
        "loadEventEnd": 50,
    }
    m = compute_navigation_metrics(nav)
    assert m["tls_ms"] == 0.0
    assert m["ttfb_ms"] == 7


def test_navigation_metrics_empty():
    m = compute_navigation_metrics(None)
    assert m["dns_ms"] is None
    assert m["tls_ms"] == 0.0
    assert m["load_event_ms"] is None


def test_extract_paint_metrics_typical():
    m = extract_paint_metrics({"fcp": 812.5, "lcp": 1340.0, "inp": 48.0})
    assert m == {"fcp_ms": 812.5, "lcp_ms": 1340.0, "inp_ms": 48.0}


def test_extract_paint_metrics_missing_inp():
    # No interaction observed -> INP is None, FCP/LCP still captured.
    m = extract_paint_metrics({"fcp": 900.0, "lcp": 1500.0, "inp": None})
    assert m["fcp_ms"] == 900.0 and m["lcp_ms"] == 1500.0
    assert m["inp_ms"] is None


def test_extract_paint_metrics_empty():
    m = extract_paint_metrics(None)
    assert m == {"fcp_ms": None, "lcp_ms": None, "inp_ms": None}


def test_browser_plugin_registered():
    plugin = get_plugin("browser")
    assert plugin is not None
    assert plugin.name == "browser"


def test_browser_no_urls_fails_gracefully():
    plugin = get_plugin("browser")
    result = plugin.run({"urls": []})
    assert result.success is False
    assert "URL" in (result.error or "")


def test_browser_missing_playwright_is_graceful():
    if _HAS_PLAYWRIGHT:
        import pytest

        pytest.skip("Playwright installed; missing-dependency path not exercised")
    plugin = get_plugin("browser")
    result = plugin.run({"urls": ["https://example.com/"]})
    assert result.success is False
    assert "Playwright" in (result.error or "")
