"""Self-update: trigger a container update via a Watchtower sidecar's HTTP API.

PathBrain holds **no** Docker access. Watchtower holds the socket; PathBrain only POSTs to
its ``--http-api-update`` endpoint with a bearer token, and Watchtower pulls the newest
``:latest`` image and recreates the labeled PathBrain container. A container can't cleanly
recreate *itself* (removing it kills the process doing the removal), so the external agent
is what makes self-update possible — and the bearer token is a genuinely limited-permission
key: it can only ask Watchtower to run an update cycle, nothing else.

The trigger is **disarmed** unless both ``watchtower_url`` and ``watchtower_token`` are set,
and (like the version check) it never raises — any failure is reported, not thrown.
"""
from __future__ import annotations

import urllib.error
import urllib.request

from .config import get_settings
from .logging_config import get_logger

log = get_logger("self_update")


def self_update_status() -> dict:
    """Whether the self-update trigger is configured (both URL + token set).

    The token is a secret and is never returned — only whether it's present and the URL,
    so the UI can show/hide the "Update container" control and explain how to enable it.
    """
    s = get_settings()
    url = (s.watchtower_url or "").strip()
    token = (s.watchtower_token or "").strip()
    return {"available": bool(url and token), "url": url or None}


def trigger_update() -> dict:
    """Ask the Watchtower sidecar to pull + recreate our container. Never raises.

    Returns ``{"triggered": bool, "status": int | None, "detail": str}``. When disarmed
    (URL or token unset) returns ``triggered=False`` with an explanatory detail. Note that
    a *successful* self-recreate tears down this process, so the caller should fire this in
    the background and not depend on the return value — it's logged here for the record.
    """
    s = get_settings()
    url = (s.watchtower_url or "").strip()
    token = (s.watchtower_token or "").strip()
    if not url or not token:
        return {
            "triggered": False,
            "status": None,
            "detail": "self-update not configured (set PATHBRAIN_WATCHTOWER_URL + PATHBRAIN_WATCHTOWER_TOKEN)",
        }
    req = urllib.request.Request(
        url,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "PathBrain-self-update"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — operator-configured URL
            code = resp.getcode()
            log.info("Self-update triggered via Watchtower (%s) → HTTP %s", url, code)
            return {
                "triggered": True,
                "status": code,
                "detail": "update requested; Watchtower will pull :latest and recreate this container",
            }
    except urllib.error.HTTPError as exc:
        detail = f"Watchtower returned HTTP {exc.code} (check the token / http-api-token match)"
        log.warning("Self-update trigger failed: %s", detail)
        return {"triggered": False, "status": exc.code, "detail": detail}
    except Exception as exc:  # noqa: BLE001 — best-effort; report, don't raise
        # A connection reset here can actually mean success: Watchtower stopped us mid-request
        # while recreating the container. We can't distinguish that from a real failure, so we
        # report it; the UI treats "app went away then came back on a new build" as the signal.
        detail = f"{type(exc).__name__}: {exc}"
        log.warning("Self-update trigger error (may be the container restarting): %s", detail)
        return {"triggered": False, "status": None, "detail": detail}
