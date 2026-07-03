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

    def access_checks(self) -> list[dict]:
        """Probe what the configured credentials can actually *read*, as a list of
        capability results — a firewall-permissions self-test.

        Each entry is ``{key, label, category, ok, detail, optional?, endpoint?}``:
        ``category`` is ``"view"`` (config reads) or ``"diagnostics"`` (perf/telemetry
        reads); ``ok`` is ``True``/``False`` (or ``None`` when a probe can't decide, e.g.
        the endpoint isn't present on this firewall build). This method is **read-only /
        non-destructive** — the reversible *write* probe lives in the ``apply()`` round-trip
        the config route runs separately, so calling ``access_checks()`` never changes the
        firewall.

        The base implementation exercises the generic read path (``discover``/``snapshot``);
        providers with richer APIs (CPU / bandwidth / diagnostics endpoints) override to add
        those probes so the UI can show, per credential, exactly which reads succeed.
        """
        checks: list[dict] = []
        try:
            configs = self.discover()
            checks.append(
                {
                    "key": "read_shaper",
                    "label": "Read shaper config",
                    "category": "view",
                    "ok": True,
                    "detail": f"{len(configs)} shaper pipe(s) readable",
                }
            )
        except Exception as exc:  # noqa: BLE001 — a failed probe is a reportable result
            checks.append(
                {
                    "key": "read_shaper",
                    "label": "Read shaper config",
                    "category": "view",
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
        try:
            self.snapshot()
            checks.append(
                {
                    "key": "snapshot",
                    "label": "Snapshot full config",
                    "category": "view",
                    "ok": True,
                    "detail": "full configuration snapshot readable",
                }
            )
        except Exception as exc:  # noqa: BLE001
            checks.append(
                {
                    "key": "snapshot",
                    "label": "Snapshot full config",
                    "category": "view",
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}",
                }
            )
        return checks

    def apply(self, changes: dict) -> dict:
        """Apply a single shaper parameter change and reconfigure.

        ``changes`` = ``{"pipe_uuid": str|None, "param": str, "value": Any}``.
        ``pipe_uuid`` blank → the first discovered pipe. Returns a small status
        dict. Used by the experiment engine; always snapshot-before per the
        engine's safety flow.
        """
        raise NotImplementedError("This provider cannot apply changes")
