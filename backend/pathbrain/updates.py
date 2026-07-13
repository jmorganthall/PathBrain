"""Version awareness: is a newer build available to pull?

The image is stamped with the git commit it was built from (``PATHBRAIN_GIT_SHA``,
fed ``github.sha`` by CI). When ``update_check`` is enabled we do a **cached,
best-effort** comparison of that commit against the latest commit on the repo's
default branch via the public GitHub API — and since CI publishes ``:latest`` on every
push to that branch, "the branch moved past my build" ≈ "a newer image is available to
pull". The check never raises: any failure (offline, rate-limited, blocked by the
network policy) just leaves ``update_available`` false with an ``error`` note.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import __version__
from .config import get_settings
from .logging_config import get_logger

log = get_logger("updates")

# Cache the upstream lookup so the frontend can poll freely without hammering GitHub.
_CACHE_TTL_S = 3600.0
# ``at`` is monotonic (for the TTL); ``checked_at`` is wall-clock ISO (for the "checked 2:15 PM"
# readout), so the UI can show exactly *when* it last looked and the answer isn't a black box.
_cache: dict = {"at": 0.0, "latest_sha": None, "error": None, "checked_at": None}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fetch_latest_sha(repo: str, branch: str) -> str:
    """Newest commit SHA on ``repo``'s ``branch`` (public GitHub API). Raises on failure."""
    url = f"https://api.github.com/repos/{repo}/commits/{branch}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "PathBrain-update-check"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 — fixed https GitHub URL
        return str(json.load(resp)["sha"])


def _latest_sha_cached(repo: str, branch: str, *, force: bool = False) -> tuple[str | None, str | None]:
    """``(latest_sha, error)`` with a TTL cache; never raises. ``force`` bypasses the TTL so a
    user-triggered "Check now" always re-fetches (the whole point of a manual refresh is to not
    trust the possibly-stale cached answer)."""
    now = time.monotonic()
    if not force and _cache["latest_sha"] is not None and (now - _cache["at"]) < _CACHE_TTL_S:
        return _cache["latest_sha"], None
    try:
        sha = _fetch_latest_sha(repo, branch)
        _cache.update({"at": now, "latest_sha": sha, "error": None, "checked_at": _utcnow_iso()})
        return sha, None
    except Exception as exc:  # noqa: BLE001 — best-effort; report, don't raise
        err = f"{type(exc).__name__}: {exc}"
        log.info("Update check failed: %s", err)
        _cache.update({"at": now, "error": err, "checked_at": _utcnow_iso()})
        return _cache["latest_sha"], err  # serve a stale sha if we have one


def version_info(*, force: bool = False) -> dict:
    """Current build identity + (best-effort) whether a newer build is available. ``force``
    re-checks upstream immediately instead of serving the 1-hour cache (the "Check now" path)."""
    settings = get_settings()
    git_sha = (settings.git_sha or "").strip()
    info: dict = {
        "version": __version__,
        "git_sha": git_sha or None,
        "git_sha_short": git_sha[:7] or None,
        "update_check": settings.update_check,
        "update_available": False,
        "latest_sha": None,
        "latest_sha_short": None,
        "compare_url": None,
        # Whether a one-click self-update is wired up (Watchtower HTTP API configured). The UI
        # only offers the "Update now" button when this is true; otherwise the chip is a link.
        "self_update": bool((settings.watchtower_url or "").strip()),
        # What upstream we compare against, and when we last actually looked — so "up to date" is
        # a transparent statement ("running X · latest on <branch> is Y · checked <time>"), not a
        # black box the user has to trust.
        "update_repo": settings.update_repo,
        "update_branch": settings.update_branch,
        "checked_at": _cache.get("checked_at"),
        "error": None,
    }
    if not settings.update_check:
        return info

    latest, err = _latest_sha_cached(settings.update_repo, settings.update_branch, force=force)
    info["checked_at"] = _cache.get("checked_at")
    info["error"] = err
    if latest:
        info["latest_sha"] = latest
        info["latest_sha_short"] = latest[:7]
        # Only claim an update when we know our own build SHA and it differs.
        if git_sha and latest != git_sha and not latest.startswith(git_sha):
            info["update_available"] = True
            info["compare_url"] = f"https://github.com/{settings.update_repo}/compare/{git_sha}...{latest}"
    return info


# Connection-level failures that mean "the request reached Watchtower and it recreated *this*
# container out from under us" (expected on a successful self-update) rather than "Watchtower is
# unreachable". A reset/dropped/timed-out connection after the request was sent → treat as
# triggered; a refused connection or DNS failure → Watchtower isn't there → real error.
_DROPPED_MIDWAY = (ConnectionResetError, socket.timeout, TimeoutError)


def self_update_config() -> dict:
    """The Watchtower integration's configuration state — **no network call**. Powers the
    Plugins-page integration card's initial render. Never exposes the token itself, only whether
    one is set."""
    settings = get_settings()
    base = (settings.watchtower_url or "").strip().rstrip("/")
    return {
        "configured": bool(base),
        "url": base or None,
        "token_set": bool((settings.watchtower_token or "").strip()),
    }


def test_update_connection() -> dict:
    """Check the Watchtower self-update integration **without triggering an update**.

    Watchtower's only HTTP endpoint (``/v1/update``) *performs* the update, so a safe test can't
    call it. Instead this probes the API **root** — any HTTP response (even 404/401) proves the
    server is up and reachable from inside this container; only a connection-level failure
    (refused / DNS / timeout) means the URL, port, or network is wrong (the #1 real-world misconfig).
    The token is verified for real only by "Update now", which safely reports a bad token as HTTP 401
    without updating. Returns ``{configured, url, token_set, reachable, status, detail}``; never raises.
    ``status`` ∈ ``ok`` | ``unreachable`` | ``not_configured``."""
    cfg = self_update_config()
    result = {**cfg, "reachable": False, "status": "not_configured", "detail": ""}
    base = cfg["url"]
    if not base:
        result["detail"] = "Watchtower is not configured — set PATHBRAIN_WATCHTOWER_URL (and _TOKEN)."
        return result

    # Probe the root, NOT /v1/update — hitting the update endpoint would run an update.
    req = urllib.request.Request(
        base + "/", method="GET", headers={"User-Agent": "PathBrain-self-update-test"}
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:  # noqa: S310 — operator-configured URL
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code  # server answered with an error status → still reachable
    except urllib.error.URLError as exc:
        result["status"] = "unreachable"
        result["detail"] = (
            f"Could not reach Watchtower at {base}: {exc.reason}. Check the URL/port and that it's "
            "reachable from inside the PathBrain container."
        )
        return result
    except _DROPPED_MIDWAY as exc:  # bare socket timeout
        result["status"] = "unreachable"
        result["detail"] = f"Timed out reaching Watchtower at {base}: {exc}."
        return result

    result["reachable"] = True
    result["status"] = "ok"
    tok = (
        "a token is set"
        if cfg["token_set"]
        else "no token set — add PATHBRAIN_WATCHTOWER_TOKEN if Watchtower requires one"
    )
    result["detail"] = (
        f"Reachable at {base} (HTTP {code} at the API root); {tok}. "
        'The token is verified for real when you click "Update now" (a bad token returns 401 '
        "without updating)."
    )
    return result


def trigger_update() -> dict:
    """Ask Watchtower to pull the newer image and recreate this container (one-click update).

    POSTs to ``{watchtower_url}/v1/update`` with the configured ``Bearer`` token — Watchtower's
    HTTP API. Returns ``{"triggered": bool, "detail"/"error": str}``; never raises. Because a
    *successful* update recreates PathBrain's own container, Watchtower often severs the response
    mid-flight — a dropped/reset/timed-out connection is therefore reported as **triggered**, while
    a refused connection (Watchtower not listening) or an auth error (bad token) is a real failure
    surfaced to the caller. Idempotent from the user's side: Watchtower no-ops when the image is
    already current."""
    settings = get_settings()
    base = (settings.watchtower_url or "").strip().rstrip("/")
    token = (settings.watchtower_token or "").strip()
    if not base:
        return {"triggered": False, "error": "Watchtower is not configured (set PATHBRAIN_WATCHTOWER_URL)."}

    url = f"{base}/v1/update"
    headers = {"User-Agent": "PathBrain-self-update"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method="POST", headers=headers, data=b"")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — operator-configured URL
            body = resp.read(2048).decode("utf-8", "replace").strip()
        log.info("Watchtower update triggered (HTTP %s)", resp.status)
        return {"triggered": True, "detail": body or f"Watchtower accepted the update (HTTP {resp.status})."}
    except urllib.error.HTTPError as exc:
        # Watchtower answered with an error status — most commonly 401 (bad/missing token).
        hint = " — check PATHBRAIN_WATCHTOWER_TOKEN" if exc.code in (401, 403) else ""
        log.warning("Watchtower update rejected: HTTP %s%s", exc.code, hint)
        return {"triggered": False, "error": f"Watchtower returned HTTP {exc.code}{hint}."}
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, _DROPPED_MIDWAY):
            # The request landed and the update recreated us before the response came back.
            log.info("Watchtower connection dropped after request (%s) — treating as triggered", reason)
            return {"triggered": True, "detail": "Update triggered; PathBrain is restarting."}
        log.warning("Could not reach Watchtower at %s: %s", url, reason)
        return {"triggered": False, "error": f"Could not reach Watchtower at {base}: {reason}."}
    except _DROPPED_MIDWAY as exc:  # bare socket timeout not wrapped in URLError
        log.info("Watchtower connection dropped after request (%s) — treating as triggered", exc)
        return {"triggered": True, "detail": "Update triggered; PathBrain is restarting."}
