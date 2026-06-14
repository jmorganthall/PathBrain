"""Configuration discovery providers (firewall / shaper integrations)."""
from __future__ import annotations

from ..config import get_settings
from .base import ConfigProvider, FqCodelConfig
from .mock import MockProvider
from .opnsense import OPNsenseProvider

__all__ = [
    "ConfigProvider",
    "FqCodelConfig",
    "MockProvider",
    "OPNsenseProvider",
    "get_provider",
]


def get_provider() -> ConfigProvider:
    """Build the configured provider from environment settings."""
    settings = get_settings()
    if settings.config_provider.lower() == "opnsense":
        return OPNsenseProvider(
            base_url=settings.opnsense_url,
            api_key=settings.opnsense_api_key,
            api_secret=settings.opnsense_api_secret,
            verify_tls=settings.opnsense_verify_tls,
        )
    return MockProvider()
