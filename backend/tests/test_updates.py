"""Tests for the version / update-awareness check (best-effort, cached)."""
from __future__ import annotations

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
