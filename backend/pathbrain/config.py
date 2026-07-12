"""Application settings, sourced from environment variables.

These are *infrastructure* settings (where the database is, how to reach the
firewall). Runtime *benchmark* configuration (targets, weights, thresholds)
lives in the database and is managed by :mod:`pathbrain.config_store`.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PATHBRAIN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Storage
    database_url: str = "sqlite:///./data/pathbrain.db"
    # Where browser-engine artifacts (screenshots, HAR files) are written.
    artifact_dir: str = "./data/artifacts"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # Config discovery
    config_provider: str = "mock"  # "opnsense" | "mock"

    # OPNsense API
    opnsense_url: str = ""
    opnsense_api_key: str = ""
    opnsense_api_secret: str = ""
    opnsense_verify_tls: bool = False

    # Version / update awareness. ``git_sha`` is stamped into the image at build time
    # (Dockerfile ARG → ENV PATHBRAIN_GIT_SHA, fed github.sha by CI); empty in dev.
    # When ``update_check`` is on, the backend does a cached, best-effort comparison of
    # this build's commit against the latest commit on ``update_repo``'s default branch.
    git_sha: str = ""
    update_check: bool = True
    update_repo: str = "jmorganthall/PathBrain"
    update_branch: str = "main"

    # One-click self-update via Watchtower's HTTP API. When both are set, the "Update available"
    # chip offers an "Update now" button that POSTs to ``{watchtower_url}/v1/update`` with a
    # ``Bearer`` token, telling Watchtower to pull the newer image and recreate this container.
    # ``watchtower_url`` is the base URL of the Watchtower HTTP API (e.g. http://192.168.2.6:8998);
    # ``watchtower_token`` is its ``WATCHTOWER_HTTP_API_TOKEN``. Empty ``watchtower_url`` (default)
    # disables the button — the chip stays a link to the GitHub compare.
    watchtower_url: str = ""
    watchtower_token: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
