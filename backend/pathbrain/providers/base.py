"""Config discovery provider contract.

A provider knows how to talk to a firewall / shaper to *discover* current
FQ-CoDel parameters and to *snapshot* the full configuration for safety and
rollback. Applying changes is part of the experiment/autonomous phases; the
contract reserves a method for it but the foundation only reads.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field


@dataclass
class FqCodelConfig:
    """Normalized FQ-CoDel / shaper parameters across providers."""

    download_bandwidth: str | None = None
    upload_bandwidth: str | None = None
    quantum: int | None = None
    limit: int | None = None
    target: str | None = None
    interval: str | None = None
    ecn: bool | None = None
    flows: int | None = None
    queues: int | None = None
    scheduler: str | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class ConfigProvider(ABC):
    """Base class for firewall configuration providers."""

    name: str = ""

    @abstractmethod
    def discover(self) -> list[FqCodelConfig]:
        """Discover current FQ-CoDel settings (one per shaper pipe/queue)."""

    @abstractmethod
    def snapshot(self) -> dict:
        """Return a full configuration snapshot suitable for rollback."""

    def health(self) -> dict:
        """Lightweight connectivity / configuration check."""
        return {"provider": self.name, "ok": True}

    def writable_fields(self) -> list[str]:
        """Normalized shaper-field keys this provider's ``apply()`` can write — the single
        accessor for "what can we change". Defaults to the registry's writable set (the
        standard OPNsense capability: codel/bandwidth params, not scheduler/queues/upload
        bandwidth); a provider with a different capability may override."""
        from ..shaper_fields import WRITABLE_FIELDS

        return list(WRITABLE_FIELDS)

    def pipe_states(self) -> list[dict]:
        """The per-pipe on/off (SQM enabled) state, for the baseline "SQM off" test.

        Returns ``[{"uuid": str|None, "label": str, "enabled": bool}]`` — one entry per
        shaper pipe. This is deliberately *separate* from the shaper-field/writable model:
        the pipe ``enabled`` flag is not a profile-identity field (see ``shaper_fields``),
        so toggling it goes through :meth:`set_pipe_enabled`, not :meth:`apply`. The
        default derives from ``discover()`` (the enabled flag lives in ``extra``)."""
        states: list[dict] = []
        for cfg in self.discover():
            extra = cfg.extra or {}
            enabled = extra.get("enabled")
            states.append(
                {
                    "uuid": extra.get("uuid"),
                    "label": extra.get("description") or extra.get("pipe") or extra.get("direction"),
                    # Unknown (None) is treated as enabled — the normal live state.
                    "enabled": True if enabled is None else bool(enabled),
                }
            )
        return states

    def set_pipe_enabled(self, pipe_uuid: str | None, enabled: bool) -> dict:
        """Enable/disable one shaper pipe and reconfigure — the "turn SQM off/on" write.

        Used by the baseline test to disable shaping on every pipe, benchmark the
        unshaped link, then restore each pipe's prior state. Providers that can write the
        firewall override this; the default cannot."""
        raise NotImplementedError("This provider cannot toggle a shaper pipe on/off")

    def apply(self, changes: dict) -> dict:
        """Apply a single shaper parameter change and reconfigure.

        ``changes`` = ``{"pipe_uuid": str|None, "param": str, "value": Any}``.
        ``pipe_uuid`` blank → the first discovered pipe. Returns a small status
        dict. Used by the experiment engine; always snapshot-before per the
        engine's safety flow.
        """
        raise NotImplementedError("This provider cannot apply changes")
