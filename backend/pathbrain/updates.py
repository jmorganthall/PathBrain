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
import time
import urllib.request

from . import __version__
from .config import get_settings
from .logging_config import get_logger

log = get_logger("updates")

# Cache the upstream lookup so the frontend can poll freely without hammering GitHub.
_CACHE_TTL_S = 3600.0
_cache: dict = {"at": 0.0, "latest_sha": None, "error": None}


def _fetch_latest_sha(repo: str, branch: str) -> str:
    """Newest commit SHA on ``repo``'s ``branch`` (public GitHub API). Raises on failure."""
    url = f"https://api.github.com/repos/{repo}/commits/{branch}"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "PathBrain-update-check"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 — fixed https GitHub URL
        return str(json.load(resp)["sha"])


def _latest_sha_cached(repo: str, branch: str) -> tuple[str | None, str | None]:
    """``(latest_sha, error)`` with a TTL cache; never raises."""
    now = time.monotonic()
    if _cache["latest_sha"] is not None and (now - _cache["at"]) < _CACHE_TTL_S:
        return _cache["latest_sha"], None
    try:
        sha = _fetch_latest_sha(repo, branch)
        _cache.update({"at": now, "latest_sha": sha, "error": None})
        return sha, None
    except Exception as exc:  # noqa: BLE001 — best-effort; report, don't raise
        err = f"{type(exc).__name__}: {exc}"
        log.info("Update check failed: %s", err)
        _cache.update({"at": now, "error": err})
        return _cache["latest_sha"], err  # serve a stale sha if we have one


def version_info() -> dict:
    """Current build identity + (best-effort) whether a newer build is available."""
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
        "error": None,
    }
    if not settings.update_check:
        return info

    latest, err = _latest_sha_cached(settings.update_repo, settings.update_branch)
    info["error"] = err
    if latest:
        info["latest_sha"] = latest
        info["latest_sha_short"] = latest[:7]
        # Only claim an update when we know our own build SHA and it differs.
        if git_sha and latest != git_sha and not latest.startswith(git_sha):
            info["update_available"] = True
            info["compare_url"] = f"https://github.com/{settings.update_repo}/compare/{git_sha}...{latest}"
    return info
