"""Tests for the version / update-awareness check (best-effort, cached)."""
from __future__ import annotations

import urllib.error

from pathbrain import updates
from pathbrain.config import get_settings


def _reset_cache():
    updates._cache.update({"at": 0.0, "latest_sha": None, "error": None})


def test_update_available_when_sha_differs(monkeypatch):
    _reset_cache()
    get_settings.cache_clear()
    monkeypatch.setenv("PATHBRAIN_GIT_SHA", "a" * 40)
    monkeypatch.setattr(updates, "_fetch_latest_sha", lambda repo, branch: "b" * 40)

    info = updates.version_info()
    assert info["update_available"] is True
    assert info["git_sha_short"] == "aaaaaaa"
    assert info["latest_sha_short"] == "bbbbbbb"
    assert info["compare_url"].endswith(f"{'a' * 40}...{'b' * 40}")
    get_settings.cache_clear()


def test_no_update_when_sha_matches(monkeypatch):
    _reset_cache()
    get_settings.cache_clear()
    sha = "c" * 40
    monkeypatch.setenv("PATHBRAIN_GIT_SHA", sha)
    monkeypatch.setattr(updates, "_fetch_latest_sha", lambda repo, branch: sha)

    info = updates.version_info()
    assert info["update_available"] is False
    assert info["latest_sha"] == sha
    get_settings.cache_clear()


def test_unknown_build_sha_never_claims_update(monkeypatch):
    # A dev build with no stamped SHA can't know it's behind → never alarms.
    _reset_cache()
    get_settings.cache_clear()
    monkeypatch.delenv("PATHBRAIN_GIT_SHA", raising=False)
    monkeypatch.setattr(updates, "_fetch_latest_sha", lambda repo, branch: "d" * 40)

    info = updates.version_info()
    assert info["update_available"] is False
    assert info["git_sha"] is None
    get_settings.cache_clear()


def test_check_is_best_effort_on_network_error(monkeypatch):
    _reset_cache()
    get_settings.cache_clear()
    monkeypatch.setenv("PATHBRAIN_GIT_SHA", "e" * 40)

    def boom(repo, branch):
        raise OSError("network unreachable")

    monkeypatch.setattr(updates, "_fetch_latest_sha", boom)
    info = updates.version_info()
    assert info["update_available"] is False
    assert info["error"] is not None  # reported, not raised
    get_settings.cache_clear()


def test_disabled_skips_network(monkeypatch):
    _reset_cache()
    get_settings.cache_clear()
    monkeypatch.setenv("PATHBRAIN_GIT_SHA", "f" * 40)
    monkeypatch.setenv("PATHBRAIN_UPDATE_CHECK", "false")

    def boom(repo, branch):  # must never be called when disabled
        raise AssertionError("network hit while update_check disabled")

    monkeypatch.setattr(updates, "_fetch_latest_sha", boom)
    info = updates.version_info()
    assert info["update_check"] is False
    assert info["update_available"] is False
    get_settings.cache_clear()


# ── one-click self-update via Watchtower ─────────────────────────────────────


class _FakeResp:
    """Minimal urlopen() context manager for a successful Watchtower response."""

    def __init__(self, status=200, body=b"Updated PathBrain"):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self._body


def test_self_update_flag_reflects_watchtower_config(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("PATHBRAIN_UPDATE_CHECK", "false")  # skip the network SHA check
    # No Watchtower configured → self_update false.
    monkeypatch.delenv("PATHBRAIN_WATCHTOWER_URL", raising=False)
    assert updates.version_info()["self_update"] is False
    get_settings.cache_clear()
    # URL set → the UI offers the button.
    monkeypatch.setenv("PATHBRAIN_WATCHTOWER_URL", "http://192.168.2.6:8998")
    assert updates.version_info()["self_update"] is True
    get_settings.cache_clear()


def test_trigger_update_not_configured(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.delenv("PATHBRAIN_WATCHTOWER_URL", raising=False)
    out = updates.trigger_update()
    assert out["triggered"] is False and "not configured" in out["error"]
    get_settings.cache_clear()


def test_trigger_update_success_sends_bearer_token(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("PATHBRAIN_WATCHTOWER_URL", "http://192.168.2.6:8998/")  # trailing slash trimmed
    monkeypatch.setenv("PATHBRAIN_WATCHTOWER_TOKEN", "s3cr3t")
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        seen["auth"] = req.get_header("Authorization")
        return _FakeResp()

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    out = updates.trigger_update()
    assert out["triggered"] is True
    assert seen["url"] == "http://192.168.2.6:8998/v1/update"  # no double slash
    assert seen["method"] == "POST"
    assert seen["auth"] == "Bearer s3cr3t"
    get_settings.cache_clear()


def test_trigger_update_bad_token_surfaces_auth_error(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("PATHBRAIN_WATCHTOWER_URL", "http://192.168.2.6:8998")
    monkeypatch.setenv("PATHBRAIN_WATCHTOWER_TOKEN", "wrong")

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    out = updates.trigger_update()
    assert out["triggered"] is False
    assert "401" in out["error"] and "TOKEN" in out["error"]
    get_settings.cache_clear()


def test_trigger_update_dropped_connection_is_treated_as_triggered(monkeypatch):
    # A successful update recreates *this* container, severing the response → not a failure.
    get_settings.cache_clear()
    monkeypatch.setenv("PATHBRAIN_WATCHTOWER_URL", "http://192.168.2.6:8998")

    def fake_urlopen(req, timeout=0):
        raise urllib.error.URLError(ConnectionResetError("connection reset by peer"))

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    out = updates.trigger_update()
    assert out["triggered"] is True
    get_settings.cache_clear()


def test_trigger_update_unreachable_is_an_error(monkeypatch):
    # A refused connection means Watchtower isn't listening → real, surfaced failure.
    get_settings.cache_clear()
    monkeypatch.setenv("PATHBRAIN_WATCHTOWER_URL", "http://192.168.2.6:8998")

    def fake_urlopen(req, timeout=0):
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    out = updates.trigger_update()
    assert out["triggered"] is False and "Could not reach" in out["error"]
    get_settings.cache_clear()
