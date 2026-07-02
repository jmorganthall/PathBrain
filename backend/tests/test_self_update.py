"""Tests for the Watchtower-backed container self-update trigger.

PathBrain holds no Docker access — it only POSTs to a Watchtower sidecar's token-guarded
HTTP endpoint. The trigger is disarmed unless both the URL and token are configured, and the
token is never surfaced back to the UI.
"""
from __future__ import annotations

import time

from pathbrain import self_update
from pathbrain.config import get_settings


def _clear():
    get_settings.cache_clear()


def test_status_disarmed_without_url_or_token(monkeypatch):
    _clear()
    monkeypatch.delenv("PATHBRAIN_WATCHTOWER_URL", raising=False)
    monkeypatch.delenv("PATHBRAIN_WATCHTOWER_TOKEN", raising=False)
    assert self_update.self_update_status() == {"available": False, "url": None}
    _clear()


def test_status_needs_both_url_and_token(monkeypatch):
    _clear()
    monkeypatch.setenv("PATHBRAIN_WATCHTOWER_URL", "http://watchtower:8080/v1/update")
    monkeypatch.delenv("PATHBRAIN_WATCHTOWER_TOKEN", raising=False)
    # URL without a token is still disarmed (Watchtower's http-api requires the token).
    assert self_update.self_update_status()["available"] is False
    _clear()


def test_status_available_hides_the_token(monkeypatch):
    _clear()
    monkeypatch.setenv("PATHBRAIN_WATCHTOWER_URL", "http://watchtower:8080/v1/update")
    monkeypatch.setenv("PATHBRAIN_WATCHTOWER_TOKEN", "secret-token")
    st = self_update.self_update_status()
    assert st["available"] is True
    assert st["url"] == "http://watchtower:8080/v1/update"
    assert "secret-token" not in str(st)  # the token is a secret — never returned
    _clear()


def test_trigger_disarmed_returns_not_triggered(monkeypatch):
    _clear()
    monkeypatch.delenv("PATHBRAIN_WATCHTOWER_URL", raising=False)
    monkeypatch.delenv("PATHBRAIN_WATCHTOWER_TOKEN", raising=False)
    r = self_update.trigger_update()
    assert r["triggered"] is False and r["status"] is None
    _clear()


def test_version_endpoint_exposes_self_update_availability(client, monkeypatch):
    _clear()
    monkeypatch.setenv("PATHBRAIN_WATCHTOWER_URL", "http://watchtower:8080/v1/update")
    monkeypatch.setenv("PATHBRAIN_WATCHTOWER_TOKEN", "secret-token")
    monkeypatch.setenv("PATHBRAIN_UPDATE_CHECK", "false")  # skip the GitHub call
    body = client.get("/api/version").json()
    assert body["self_update"]["available"] is True
    assert body["self_update"]["url"] == "http://watchtower:8080/v1/update"
    _clear()


def test_apply_endpoint_400_when_disarmed(client, monkeypatch):
    _clear()
    monkeypatch.delenv("PATHBRAIN_WATCHTOWER_URL", raising=False)
    monkeypatch.delenv("PATHBRAIN_WATCHTOWER_TOKEN", raising=False)
    assert client.post("/api/update/apply").status_code == 400
    _clear()


def test_apply_endpoint_202_fires_trigger_in_background(client, monkeypatch):
    _clear()
    monkeypatch.setenv("PATHBRAIN_WATCHTOWER_URL", "http://watchtower:8080/v1/update")
    monkeypatch.setenv("PATHBRAIN_WATCHTOWER_TOKEN", "secret-token")
    called = {}
    # Replace the real (network) trigger so the test never hits Watchtower.
    monkeypatch.setattr(self_update, "trigger_update", lambda: called.setdefault("hit", True))

    r = client.post("/api/update/apply")
    assert r.status_code == 202
    assert r.json()["requested"] is True
    # The trigger runs in a daemon thread — give it a beat to fire.
    for _ in range(50):
        if called.get("hit"):
            break
        time.sleep(0.02)
    assert called.get("hit") is True
    _clear()
