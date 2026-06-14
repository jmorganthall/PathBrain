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

    def apply(self, changes: dict) -> dict:  # pragma: no cover - Phase 3+
        """Apply configuration changes. Not implemented in the foundation."""
        raise NotImplementedError("Applying changes is not enabled in this phase")
