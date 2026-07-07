"""Mock config provider for development and tests.

Returns plausible FQ-CoDel parameters so the whole pipeline (discover →
snapshot → store) runs without a live firewall.
"""
from __future__ import annotations

from .base import ConfigProvider, FqCodelConfig

# Module-level so applied changes persist across get_provider() calls and are
# reflected by discover() — lets the experiment engine be exercised end-to-end.
_OVERRIDES: dict[str, object] = {}
# Per-pipe on/off (SQM enabled) state, keyed by the pipe uuid, so the baseline
# "SQM off" test can disable each pipe and see it reflected by discover().
_PIPE_ENABLED: dict[str, bool] = {}


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
                extra={
                    "pipe": "wan-download",
                    "direction": "download",
                    "uuid": "mock-download",
                    "enabled": _PIPE_ENABLED.get("mock-download", True),
                },
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
                # No uuid on purpose — mirrors an OPNsense pipe apply() can't target, and
                # exercises the "flagged, not applied" path in plan_apply / the tests.
                extra={
                    "pipe": "wan-upload",
                    "direction": "upload",
                    "enabled": _PIPE_ENABLED.get("mock-upload", True),
                },
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

    def set_pipe_enabled(self, pipe_uuid: str | None, enabled: bool) -> dict:
        # Key by uuid where present; a uuid-less pipe (the mock upload) can't be targeted,
        # mirroring apply()'s own limitation — so its toggle is a recorded no-op.
        uuid = pipe_uuid or "mock-upload"
        _PIPE_ENABLED[uuid] = bool(enabled)
        return {"provider": self.name, "ok": True, "uuid": uuid, "enabled": bool(enabled)}
