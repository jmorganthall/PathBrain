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
    """Build the configured provider from environment settings.

    Every engine gets it behind ``session_runtime.resilient``: a firewall call that times
    out or drops is retried before anything fails, and a call that is beyond saving raises
    a typed ``FirewallUnavailable`` rather than a bare transport error. This is the one seam
    every ``discover``/``apply`` in the codebase passes through, which is why the policy is
    installed here and not remembered per engine.
    """
    from ..session_runtime import resilient

    settings = get_settings()
    if settings.config_provider.lower() == "opnsense":
        return resilient(OPNsenseProvider(
            base_url=settings.opnsense_url,
            api_key=settings.opnsense_api_key,
            api_secret=settings.opnsense_api_secret,
            verify_tls=settings.opnsense_verify_tls,
            timeout=float(settings.opnsense_timeout_s),
        ))
    return resilient(MockProvider())
