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


@lru_cache
def get_settings() -> Settings:
    return Settings()
