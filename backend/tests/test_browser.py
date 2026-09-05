"""Tests for the browser (Playwright) benchmark plugin.

These cover the pure navigation-timing math and graceful degradation. They do
not require Playwright or Chromium to be installed.
"""
from __future__ import annotations

import importlib.util

from pathbrain.plugins import get_plugin
from pathbrain.plugins.benchmark_browser import (
    build_chromium_args,
    compute_navigation_metrics,
    context_options,
    extract_paint_metrics,
    launch_headless,
    realistic_user_agent,
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


def test_build_chromium_args_default_is_a_normal_browser():
    # HTTP/3 off by default -> no QUIC flags. The defaults that ARE on are the "look like a
    # normal browser" ones: Chromium's new headless mode, and navigator.webdriver cleared.
    args = build_chromium_args({"urls": ["https://example.com/"]})
    assert args == ["--headless=new", "--disable-blink-features=AutomationControlled"]
    assert build_chromium_args({}) == args
    assert not any(a.startswith("--enable-quic") for a in args)


def test_headless_mode_legacy_goes_through_playwrights_own_switch():
    # Legacy mode: no --headless=new of our own; Playwright's headless=True adds the old flag.
    cfg = {"headless_mode": "legacy"}
    assert "--headless=new" not in build_chromium_args(cfg)
    assert launch_headless(cfg) is True
    # New mode: WE pass the flag, so Playwright must not add the legacy one beside it.
    assert launch_headless({"headless_mode": "new"}) is False
    assert launch_headless({}) is False
    # Headed: no headless flag at all, whatever the mode says.
    assert "--headless=new" not in build_chromium_args({"headless": False})
    assert launch_headless({"headless": False}) is False


def test_hide_automation_can_be_switched_off():
    args = build_chromium_args({"hide_automation": False})
    assert "--disable-blink-features=AutomationControlled" not in args


def test_realistic_user_agent_tracks_the_bundled_chromium_major():
    ua = realistic_user_agent("125.0.6422.26")
    assert ua.startswith("Mozilla/5.0 (X11; Linux x86_64)")
    assert "Chrome/125.0.0.0" in ua and "Headless" not in ua
    # No version to read -> a sane fallback, never a broken string.
    assert "Chrome/" in realistic_user_agent(None) and "Chrome/." not in realistic_user_agent("")


def test_context_options_default_to_a_persons_desktop_browser():
    opts = context_options({}, "126.0.1")
    assert opts == {"user_agent": realistic_user_agent("126.0.1")}
    # The shipped defaults (config_store) add the viewport + locale.
    from pathbrain.config_store import DEFAULT_CONFIG

    opts = context_options(DEFAULT_CONFIG["browser"], "126.0.1")
    assert opts["viewport"] == {"width": 1920, "height": 1080}
    assert opts["locale"] == "en-US"
    assert "timezone_id" not in opts  # "" = the container's clock
    assert "Chrome/126.0.0.0" in opts["user_agent"]


def test_context_options_respect_explicit_and_empty_values():
    cfg = {"user_agent": "Custom/1.0", "viewport": {"width": 0, "height": 800},
           "locale": " ", "timezone_id": "America/Chicago"}
    opts = context_options(cfg, "126.0.1")
    assert opts == {"user_agent": "Custom/1.0", "timezone_id": "America/Chicago"}
    # "" keeps Playwright's default user agent (the HeadlessChrome string).
    assert "user_agent" not in context_options({"user_agent": ""}, "126.0.1")


def test_build_chromium_args_http3_derives_origins():
    args = build_chromium_args(
        {
            "http3": True,
            "urls": ["https://www.google.com/", "https://github.com/path"],
        }
    )
    assert "--enable-quic" in args
    assert (
        "--origin-to-force-quic-on=www.google.com:443,github.com:443" in args
    )


def test_build_chromium_args_http3_explicit_origins():
    args = build_chromium_args(
        {
            "http3": True,
            "urls": ["https://example.com/"],
            "force_quic_origins": ["cloudflare.com:443"],
        }
    )
    assert "--origin-to-force-quic-on=cloudflare.com:443" in args


def test_build_chromium_args_http3_dedupes_and_handles_ports():
    args = build_chromium_args(
        {
            "http3": True,
            "urls": [
                "https://example.com/a",
                "https://example.com/b",
                "http://plain.test:8080/",
            ],
        }
    )
    assert (
        "--origin-to-force-quic-on=example.com:443,plain.test:8080" in args
    )


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
