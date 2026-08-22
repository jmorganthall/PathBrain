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
from sqlalchemy import select

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
        result["detail"] = "Watchtower is not configured — set WATCHTOWER_URL (and WATCHTOWER_TOKEN)."
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
        else "no token set — add WATCHTOWER_TOKEN if Watchtower requires one"
    )
    result["detail"] = (
        f"Reachable at {base} (HTTP {code} at the API root); {tok}. "
        'The token is verified for real when you click "Update now" (a bad token returns 401 '
        "without updating)."
    )
    return result


# How long to wait before judging an attempt that Watchtower accepted: a pull + recreate
# is quick, but give it room on a slow link before calling it a no-op.
VERIFY_AFTER_SECONDS = 600


def _record_attempt(**fields) -> int | None:
    """Persist an attempt row *before* the call, so a successful update — which destroys
    this container mid-response — can't take the evidence with it."""
    from .database import session_scope
    from .models import UpdateAttempt

    try:
        with session_scope() as session:
            row = UpdateAttempt(**fields)
            session.add(row)
            session.flush()
            return row.id
    except Exception:  # noqa: BLE001 — diagnostics must never break the operation
        log.exception("Could not record the update attempt")
        return None


def _finish_attempt(attempt_id: int | None, **fields) -> None:
    from .database import session_scope
    from .models import UpdateAttempt

    if attempt_id is None:
        return
    try:
        with session_scope() as session:
            row = session.get(UpdateAttempt, attempt_id)
            if row is not None:
                for k, v in fields.items():
                    setattr(row, k, v)
    except Exception:  # noqa: BLE001
        log.exception("Could not update the update attempt record")


def verify_pending_updates() -> int:
    """Resolve attempts whose outcome is still unknown by comparing builds.

    Called at startup (the moment an update would have landed) and whenever the log is
    read. The comparison is the only conclusive evidence available: the build that was
    running when the button was pressed, against the build running now.
    """
    from datetime import timedelta

    from .database import session_scope
    from .models import UpdateAttempt

    running = (get_settings().git_sha or "").strip() or None
    resolved = 0
    try:
        with session_scope() as session:
            rows = session.scalars(
                select(UpdateAttempt).where(UpdateAttempt.verdict == "pending")
            ).all()
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            for row in rows:
                if row.outcome in ("unreachable", "rejected", "not_configured"):
                    row.verdict, row.verdict_at = "failed", now
                    row.detail = row.error or "The request never reached Watchtower."
                    resolved += 1
                    continue
                before = (row.git_sha_before or "").strip() or None
                if running and before and running != before:
                    row.verdict, row.verdict_at = "confirmed", now
                    row.git_sha_after = running
                    row.detail = f"Build changed {before[:7]} → {running[:7]} — the update took effect."
                    log.info(
                        "Self-update CONFIRMED: attempt #%s moved the build %s → %s",
                        row.id, before[:7], running[:7],
                    )
                    resolved += 1
                    continue
                created = row.created_at
                if created is not None and created.tzinfo is not None:
                    created = created.astimezone(timezone.utc).replace(tzinfo=None)
                if created is not None and (now - created) > timedelta(seconds=VERIFY_AFTER_SECONDS):
                    row.verdict, row.verdict_at = "no_change", now
                    row.git_sha_after = running
                    row.detail = (
                        f"Watchtower accepted the request, but the build is still "
                        f"{(running or 'unknown')[:7]} after "
                        f"{int((now - created).total_seconds() // 60)} minutes. The usual causes: this "
                        "container isn't in Watchtower's scope (check its --scope / label filter and "
                        "that it's watching this container), the image was already current, or "
                        "Watchtower can't reach the registry."
                    )
                    log.warning(
                        "Self-update DID NOT TAKE EFFECT: attempt #%s still on build %s",
                        row.id, (running or "unknown")[:7],
                    )
                    resolved += 1
    except Exception:  # noqa: BLE001
        log.exception("Could not verify pending update attempts")
    return resolved


def update_log(limit: int = 10) -> dict:
    """Recent self-update attempts, newest first, each with its verdict.

    This is the answer to "did the update actually work?", which the container log can't
    give: a successful update replaces the very log you would read.
    """
    from .database import session_scope
    from .models import UpdateAttempt

    verify_pending_updates()
    try:
        with session_scope() as session:
            rows = session.scalars(
                select(UpdateAttempt).order_by(UpdateAttempt.id.desc()).limit(max(1, min(limit, 50)))
            ).all()
            return {
                "attempts": [
                    {
                        "id": r.id,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                        "url": r.url,
                        "token_sent": bool(r.token_sent),
                        "outcome": r.outcome,
                        "http_status": r.http_status,
                        "response_body": (r.response_body or "")[:500] or None,
                        "error": r.error,
                        "elapsed_ms": r.elapsed_ms,
                        "git_sha_before": r.git_sha_before,
                        "git_sha_after": r.git_sha_after,
                        "verdict": r.verdict,
                        "verdict_at": r.verdict_at.isoformat() if r.verdict_at else None,
                        "detail": r.detail,
                    }
                    for r in rows
                ],
                "running_sha": (get_settings().git_sha or "").strip() or None,
                "verify_after_seconds": VERIFY_AFTER_SECONDS,
            }
    except Exception:  # noqa: BLE001
        log.exception("Could not read the update log")
        return {"attempts": [], "running_sha": None, "verify_after_seconds": VERIFY_AFTER_SECONDS}


def trigger_update() -> dict:
    """Ask Watchtower to pull the newer image and recreate this container (one-click update).

    POSTs to ``{watchtower_url}/v1/update`` with the configured ``Bearer`` token — Watchtower's
    HTTP API. Returns ``{"triggered": bool, "detail"/"error": str, "attempt_id": int|None}``; never
    raises. Because a *successful* update recreates PathBrain's own container, Watchtower often
    severs the response mid-flight — a dropped/reset/timed-out connection is therefore reported as
    **triggered**, while a refused connection (Watchtower not listening) or an auth error (bad
    token) is a real failure surfaced to the caller. Idempotent from the user's side: Watchtower
    no-ops when the image is already current.

    Every attempt is written to ``update_attempts`` **before** the request goes out, because a
    successful update destroys the evidence of itself: the container (and its log) is replaced
    mid-response, and "triggered" only ever meant "Watchtower took the call", never "the build
    changed". The row is resolved afterwards — by ``verify_pending_updates`` at the next startup —
    into ``confirmed`` / ``no_change`` / ``failed``, which is the only conclusive answer to the
    user's actual question.
    """
    settings = get_settings()
    base = (settings.watchtower_url or "").strip().rstrip("/")
    token = (settings.watchtower_token or "").strip()
    running = (settings.git_sha or "").strip() or None
    if not base:
        log.warning("Self-update requested but WATCHTOWER_URL is not set")
        _record_attempt(
            url=None,
            token_sent=bool(token),
            outcome="not_configured",
            error="Watchtower is not configured (set WATCHTOWER_URL).",
            git_sha_before=running,
            verdict="failed",
            verdict_at=datetime.now(timezone.utc).replace(tzinfo=None),
            detail="Nothing was called — WATCHTOWER_URL is empty.",
        )
        return {"triggered": False, "error": "Watchtower is not configured (set WATCHTOWER_URL)."}

    url = f"{base}/v1/update"
    headers = {"User-Agent": "PathBrain-self-update"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method="POST", headers=headers, data=b"")

    attempt_id = _record_attempt(
        url=url, token_sent=bool(token), outcome="requested", git_sha_before=running
    )
    log.info(
        "Self-update attempt #%s: POST %s (token %s, running build %s)",
        attempt_id, url, "sent" if token else "NOT sent", (running or "unknown")[:7],
    )
    started = time.monotonic()

    def _elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — operator-configured URL
            body = resp.read(2048).decode("utf-8", "replace").strip()
            status = resp.status
        log.info(
            "Self-update attempt #%s: Watchtower accepted (HTTP %s in %s ms); body: %s",
            attempt_id, status, _elapsed(), body[:200] or "<empty>",
        )
        _finish_attempt(
            attempt_id,
            outcome="accepted",
            http_status=status,
            response_body=body or None,
            elapsed_ms=_elapsed(),
        )
        return {
            "triggered": True,
            "attempt_id": attempt_id,
            "detail": body or f"Watchtower accepted the update (HTTP {status}).",
        }
    except urllib.error.HTTPError as exc:
        # Watchtower answered with an error status — most commonly 401 (bad/missing token).
        try:
            body = exc.read(2048).decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001
            body = ""
        hint = " — check WATCHTOWER_TOKEN" if exc.code in (401, 403) else ""
        log.warning(
            "Self-update attempt #%s: Watchtower rejected it with HTTP %s%s; body: %s",
            attempt_id, exc.code, hint, body[:200] or "<empty>",
        )
        _finish_attempt(
            attempt_id,
            outcome="rejected",
            http_status=exc.code,
            response_body=body or None,
            error=f"HTTP {exc.code}{hint}",
            elapsed_ms=_elapsed(),
            verdict="failed",
            verdict_at=datetime.now(timezone.utc).replace(tzinfo=None),
            detail=f"Watchtower returned HTTP {exc.code}{hint}. No update was started.",
        )
        return {
            "triggered": False,
            "attempt_id": attempt_id,
            "error": f"Watchtower returned HTTP {exc.code}{hint}.",
        }
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, _DROPPED_MIDWAY):
            return _dropped(attempt_id, reason, _elapsed())
        log.warning(
            "Self-update attempt #%s: could not reach Watchtower at %s: %s", attempt_id, url, reason
        )
        _finish_attempt(
            attempt_id,
            outcome="unreachable",
            error=f"{reason}",
            elapsed_ms=_elapsed(),
            verdict="failed",
            verdict_at=datetime.now(timezone.utc).replace(tzinfo=None),
            detail=(
                f"Could not reach Watchtower at {base}: {reason}. The request never landed, so no "
                "update was started — check the URL/port and that Watchtower is reachable from "
                "inside the PathBrain container."
            ),
        )
        return {
            "triggered": False,
            "attempt_id": attempt_id,
            "error": f"Could not reach Watchtower at {base}: {reason}.",
        }
    except _DROPPED_MIDWAY as exc:  # bare socket timeout not wrapped in URLError
        return _dropped(attempt_id, exc, _elapsed())


def _dropped(attempt_id: int | None, reason: object, elapsed_ms: int) -> dict:
    """The connection was severed *after* the request landed.

    That is exactly what a successful self-update looks like from in here — Watchtower recreates
    our container mid-response — but it is equally what a Watchtower that hung looks like, and the
    two are indistinguishable at this moment. So it is reported as triggered (the frontend polls
    for the new build) and the row stays ``pending``: the next startup compares builds and writes
    the real verdict.
    """
    log.info(
        "Self-update attempt #%s: connection dropped after the request (%s, %s ms) — consistent "
        "with the update recreating this container; verdict pending until the build is compared",
        attempt_id, reason, elapsed_ms,
    )
    _finish_attempt(attempt_id, outcome="dropped", error=f"{reason}", elapsed_ms=elapsed_ms)
    return {
        "triggered": True,
        "attempt_id": attempt_id,
        "detail": "Update triggered; PathBrain is restarting.",
    }
