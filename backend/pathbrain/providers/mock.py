"""Mock config provider for development and tests.

Returns plausible FQ-CoDel parameters so the whole pipeline (discover →
snapshot → store) runs without a live firewall.
"""
from __future__ import annotations

from .base import ConfigProvider, FqCodelConfig


class MockProvider(ConfigProvider):
    name = "mock"

    def discover(self) -> list[FqCodelConfig]:
        return [
            FqCodelConfig(
                download_bandwidth="900Mbit",
                upload_bandwidth="40Mbit",
                quantum=1514,
                limit=10240,
                target="5ms",
                interval="100ms",
                ecn=True,
                flows=1024,
                queues=1,
                scheduler="fq_codel",
                extra={"pipe": "wan-download", "direction": "download"},
            ),
            FqCodelConfig(
                download_bandwidth="40Mbit",
                upload_bandwidth="40Mbit",
                quantum=300,
                limit=10240,
                target="5ms",
                interval="100ms",
                ecn=True,
                flows=1024,
                queues=1,
                scheduler="fq_codel",
                extra={"pipe": "wan-upload", "direction": "upload"},
            ),
        ]

    def snapshot(self) -> dict:
        return {
            "provider": self.name,
            "pipes": [c.to_dict() for c in self.discover()],
            "note": "Mock snapshot — no live firewall connected.",
        }
