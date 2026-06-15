"""Mock config provider for development and tests.

Returns plausible FQ-CoDel parameters so the whole pipeline (discover →
snapshot → store) runs without a live firewall.
"""
from __future__ import annotations

from .base import ConfigProvider, FqCodelConfig

# Module-level so applied changes persist across get_provider() calls and are
# reflected by discover() — lets the experiment engine be exercised end-to-end.
_OVERRIDES: dict[str, object] = {}


class MockProvider(ConfigProvider):
    name = "mock"

    def discover(self) -> list[FqCodelConfig]:
        first_quantum = int(_OVERRIDES.get("quantum", 1514))
        first = {
            "download_bandwidth": str(_OVERRIDES.get("download_bandwidth", "900Mbit")),
            "quantum": first_quantum,
            "limit": int(_OVERRIDES.get("limit", 10240)),
            "target": str(_OVERRIDES.get("target", "5ms")),
            "interval": str(_OVERRIDES.get("interval", "100ms")),
        }
        return [
            FqCodelConfig(
                download_bandwidth=first["download_bandwidth"],
                upload_bandwidth="40Mbit",
                quantum=first["quantum"],
                limit=first["limit"],
                target=first["target"],
                interval=first["interval"],
                ecn=True,
                flows=1024,
                queues=1,
                scheduler="fq_codel",
                extra={"pipe": "wan-download", "direction": "download", "uuid": "mock-download"},
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

    def apply(self, changes: dict) -> dict:
        param = changes.get("param")
        value = changes.get("value")
        if not param:
            raise ValueError("apply() requires a 'param'")
        _OVERRIDES[param] = value
        return {"provider": self.name, "applied": {param: value}, "ok": True}
