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


def test_force_refresh_bypasses_the_cache(monkeypatch):
    # The "Check now" path must re-fetch even when the cache is warm, so a stale "up to date"
    # can be corrected on demand instead of waiting out the TTL.
    _reset_cache()
    get_settings.cache_clear()
    monkeypatch.setenv("PATHBRAIN_GIT_SHA", "a" * 40)
    calls = {"n": 0}
    seq = ["a" * 40, "b" * 40]  # upstream moves between the cached check and the forced re-check

    def fetch(repo, branch):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return seq[i]

    monkeypatch.setattr(updates, "_fetch_latest_sha", fetch)

    first = updates.version_info()          # fetches "aaaa…" → up to date
    assert first["update_available"] is False
    assert first["checked_at"] is not None  # we record when we looked
    assert first["update_repo"] and first["update_branch"]

    cached = updates.version_info()         # served from cache → no new fetch
    assert cached["update_available"] is False
    assert calls["n"] == 1

    forced = updates.version_info(force=True)  # bypasses cache → sees "bbbb…"
    assert calls["n"] == 2
    assert forced["update_available"] is True
    assert forced["latest_sha_short"] == "bbbbbbb"
    get_settings.cache_clear()


def test_version_refresh_endpoint(client, monkeypatch):
    monkeypatch.setattr(updates, "_fetch_latest_sha", lambda repo, branch: "c" * 40)
    monkeypatch.setenv("PATHBRAIN_GIT_SHA", "d" * 40)
    get_settings.cache_clear()
    body = client.post("/api/version/refresh").json()
    assert body["update_available"] is True
    assert body["latest_sha_short"] == "ccccccc"
    assert body["checked_at"] is not None
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
    monkeypatch.delenv("WATCHTOWER_URL", raising=False)
    assert updates.version_info()["self_update"] is False
    get_settings.cache_clear()
    # URL set → the UI offers the button.
    monkeypatch.setenv("WATCHTOWER_URL", "http://192.168.2.6:8998")
    assert updates.version_info()["self_update"] is True
    get_settings.cache_clear()


def test_trigger_update_not_configured(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.delenv("WATCHTOWER_URL", raising=False)
    out = updates.trigger_update()
    assert out["triggered"] is False and "not configured" in out["error"]
    get_settings.cache_clear()


def test_trigger_update_success_sends_bearer_token(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("WATCHTOWER_URL", "http://192.168.2.6:8998/")  # trailing slash trimmed
    monkeypatch.setenv("WATCHTOWER_TOKEN", "s3cr3t")
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
    monkeypatch.setenv("WATCHTOWER_URL", "http://192.168.2.6:8998")
    monkeypatch.setenv("WATCHTOWER_TOKEN", "wrong")

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
    monkeypatch.setenv("WATCHTOWER_URL", "http://192.168.2.6:8998")

    def fake_urlopen(req, timeout=0):
        raise urllib.error.URLError(ConnectionResetError("connection reset by peer"))

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    out = updates.trigger_update()
    assert out["triggered"] is True
    get_settings.cache_clear()


def test_trigger_update_unreachable_is_an_error(monkeypatch):
    # A refused connection means Watchtower isn't listening → real, surfaced failure.
    get_settings.cache_clear()
    monkeypatch.setenv("WATCHTOWER_URL", "http://192.168.2.6:8998")

    def fake_urlopen(req, timeout=0):
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    out = updates.trigger_update()
    assert out["triggered"] is False and "Could not reach" in out["error"]
    get_settings.cache_clear()


def test_test_connection_not_configured(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.delenv("WATCHTOWER_URL", raising=False)
    out = updates.test_update_connection()
    assert out["configured"] is False and out["status"] == "not_configured"
    assert out["reachable"] is False
    get_settings.cache_clear()


def test_test_connection_probes_root_not_update_endpoint(monkeypatch):
    # The test must NEVER hit /v1/update (that would perform an update) — only the API root.
    get_settings.cache_clear()
    monkeypatch.setenv("WATCHTOWER_URL", "http://192.168.2.6:8998")
    monkeypatch.setenv("WATCHTOWER_TOKEN", "s3cr3t")
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["method"] = req.get_method()
        return _FakeResp(status=404)  # Watchtower returns 404 at the root → still reachable

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    out = updates.test_update_connection()
    assert seen["url"] == "http://192.168.2.6:8998/"
    assert "/v1/update" not in seen["url"]
    assert out["status"] == "ok" and out["reachable"] is True and out["token_set"] is True
    get_settings.cache_clear()


def test_test_connection_reports_unreachable(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("WATCHTOWER_URL", "http://192.168.2.6:8998")

    def fake_urlopen(req, timeout=0):
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    out = updates.test_update_connection()
    assert out["status"] == "unreachable" and out["reachable"] is False
    assert "Could not reach" in out["detail"]
    get_settings.cache_clear()


def test_test_connection_http_error_still_reachable(monkeypatch):
    # An HTTP error status at the root (e.g. 401) still proves the server is up → reachable.
    get_settings.cache_clear()
    monkeypatch.setenv("WATCHTOWER_URL", "http://192.168.2.6:8998")

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    out = updates.test_update_connection()
    assert out["status"] == "ok" and out["reachable"] is True
    get_settings.cache_clear()


# ── the self-update ledger ───────────────────────────────────────────────────
#
# Self-update is the one operation that destroys its own evidence: a successful update
# recreates the container mid-response, so the request looks exactly like a dropped
# connection and the container log the user would grep is replaced along with it. These
# tests pin the invariant that makes "it never seems to work" answerable — every attempt
# is recorded before the call, and the verdict is decided by comparing builds afterwards.


def _attempts():
    from sqlalchemy import select

    from pathbrain.database import session_scope
    from pathbrain.models import UpdateAttempt

    with session_scope() as session:
        rows = session.scalars(select(UpdateAttempt).order_by(UpdateAttempt.id)).all()
        return [
            {
                "id": r.id,
                "outcome": r.outcome,
                "verdict": r.verdict,
                "http_status": r.http_status,
                "token_sent": r.token_sent,
                "url": r.url,
                "git_sha_before": r.git_sha_before,
                "git_sha_after": r.git_sha_after,
                "response_body": r.response_body,
                "detail": r.detail,
                "error": r.error,
            }
            for r in rows
        ]


def _clear_attempts():
    from pathbrain.database import session_scope
    from pathbrain.models import UpdateAttempt

    with session_scope() as session:
        for row in session.query(UpdateAttempt).all():
            session.delete(row)


def test_every_attempt_is_recorded_with_the_build_it_ran_on(monkeypatch):
    _clear_attempts()
    get_settings.cache_clear()
    monkeypatch.setenv("WATCHTOWER_URL", "http://192.168.2.6:8998")
    monkeypatch.setenv("WATCHTOWER_TOKEN", "s3cr3t")
    monkeypatch.setenv("PATHBRAIN_GIT_SHA", "a" * 40)
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda req, timeout=0: _FakeResp())

    out = updates.trigger_update()
    assert out["triggered"] is True

    (row,) = _attempts()
    assert row["outcome"] == "accepted"
    assert row["http_status"] == 200
    assert row["token_sent"] is True
    assert row["url"] == "http://192.168.2.6:8998/v1/update"
    # The build at press time is the whole basis of the later verdict.
    assert row["git_sha_before"] == "a" * 40
    assert row["verdict"] == "pending"  # "accepted" is not "it worked"
    assert row["response_body"] == "Updated PathBrain"
    assert out["attempt_id"] == row["id"]
    get_settings.cache_clear()


def test_accepted_but_unchanged_build_is_reported_as_no_change(monkeypatch):
    # The failure the user actually hit: Watchtower says 200, nothing updates. Left to the
    # old code this was indistinguishable from success, because "triggered" was the only
    # thing ever recorded.
    _clear_attempts()
    get_settings.cache_clear()
    monkeypatch.setenv("WATCHTOWER_URL", "http://192.168.2.6:8998")
    monkeypatch.setenv("PATHBRAIN_GIT_SHA", "a" * 40)
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda req, timeout=0: _FakeResp())
    updates.trigger_update()

    # Not yet — a pull + recreate takes a moment, so an immediate check stays pending.
    updates.verify_pending_updates()
    assert _attempts()[0]["verdict"] == "pending"

    # …but once the grace period has passed on the SAME build, it never happened.
    monkeypatch.setattr(updates, "VERIFY_AFTER_SECONDS", -1)
    assert updates.verify_pending_updates() == 1
    row = _attempts()[0]
    assert row["verdict"] == "no_change"
    assert "scope" in (row["detail"] or "")  # names the usual cause
    get_settings.cache_clear()


def test_a_changed_build_confirms_the_update(monkeypatch):
    _clear_attempts()
    get_settings.cache_clear()
    monkeypatch.setenv("WATCHTOWER_URL", "http://192.168.2.6:8998")
    monkeypatch.setenv("PATHBRAIN_GIT_SHA", "a" * 40)
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda req, timeout=0: _FakeResp())
    updates.trigger_update()

    # Restart onto the new image — which is exactly when verify runs (app startup).
    get_settings.cache_clear()
    monkeypatch.setenv("PATHBRAIN_GIT_SHA", "b" * 40)
    assert updates.verify_pending_updates() == 1
    row = _attempts()[0]
    assert row["verdict"] == "confirmed"
    assert row["git_sha_after"] == "b" * 40
    get_settings.cache_clear()


def test_a_dropped_connection_stays_pending_until_the_build_is_compared(monkeypatch):
    # A severed response is what a successful update looks like from inside — and also what a
    # hung Watchtower looks like. It must not be recorded as a success on its own.
    _clear_attempts()
    get_settings.cache_clear()
    monkeypatch.setenv("WATCHTOWER_URL", "http://192.168.2.6:8998")
    monkeypatch.setenv("PATHBRAIN_GIT_SHA", "a" * 40)

    def fake_urlopen(req, timeout=0):
        raise urllib.error.URLError(ConnectionResetError("connection reset by peer"))

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    assert updates.trigger_update()["triggered"] is True
    row = _attempts()[0]
    assert row["outcome"] == "dropped"
    assert row["verdict"] == "pending"
    get_settings.cache_clear()


def test_a_request_that_never_landed_is_recorded_as_failed(monkeypatch):
    _clear_attempts()
    get_settings.cache_clear()
    monkeypatch.setenv("WATCHTOWER_URL", "http://192.168.2.6:8998")

    def fake_urlopen(req, timeout=0):
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    updates.trigger_update()
    row = _attempts()[0]
    assert row["outcome"] == "unreachable"
    assert row["verdict"] == "failed"  # nothing to wait for — it never got out
    get_settings.cache_clear()


def test_a_rejected_token_is_recorded_as_failed(monkeypatch):
    _clear_attempts()
    get_settings.cache_clear()
    monkeypatch.setenv("WATCHTOWER_URL", "http://192.168.2.6:8998")
    monkeypatch.setenv("WATCHTOWER_TOKEN", "wrong")

    def fake_urlopen(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(updates.urllib.request, "urlopen", fake_urlopen)
    updates.trigger_update()
    row = _attempts()[0]
    assert row["outcome"] == "rejected" and row["http_status"] == 401
    assert row["verdict"] == "failed"
    assert "TOKEN" in (row["detail"] or "")
    get_settings.cache_clear()


def test_update_log_endpoint_lists_attempts_newest_first(client, monkeypatch):
    _clear_attempts()
    get_settings.cache_clear()
    monkeypatch.setenv("WATCHTOWER_URL", "http://192.168.2.6:8998")
    monkeypatch.setenv("PATHBRAIN_GIT_SHA", "a" * 40)
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda req, timeout=0: _FakeResp())
    updates.trigger_update()
    updates.trigger_update()

    body = client.get("/api/update/log").json()
    assert [a["id"] for a in body["attempts"]] == sorted(
        (a["id"] for a in body["attempts"]), reverse=True
    )
    assert len(body["attempts"]) == 2
    assert body["running_sha"] == "a" * 40
    assert body["attempts"][0]["outcome"] == "accepted"
    get_settings.cache_clear()


def test_an_unconfigured_press_is_still_recorded(monkeypatch):
    # "I clicked it and nothing happened" has to leave a trace even when there was nothing
    # to call — otherwise the absence of a record is itself ambiguous.
    _clear_attempts()
    get_settings.cache_clear()
    monkeypatch.delenv("WATCHTOWER_URL", raising=False)
    updates.trigger_update()
    row = _attempts()[0]
    assert row["outcome"] == "not_configured" and row["verdict"] == "failed"
    get_settings.cache_clear()
