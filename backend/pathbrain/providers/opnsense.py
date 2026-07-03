"""OPNsense configuration discovery provider.

Talks to the OPNsense REST API (``/api/trafficshaper/settings/get``) using API
key/secret basic auth to discover FQ-CoDel / dummynet shaper parameters.

OPNsense returns ``<select>`` fields as ``{optionKey: {"value": ..., "selected":
0|1}}`` maps and booleans as ``"0"``/``"1"`` strings; the helpers here normalize
both into plain Python values.

NOTE: this is written against the documented OPNsense API shape. Endpoint/
credentials are supplied via environment settings; point ``PATHBRAIN_*`` at a
live firewall to exercise it. The mock provider covers offline development.
"""
from __future__ import annotations

import httpx

from ..logging_config import get_logger
from .base import ConfigProvider, FqCodelConfig

log = get_logger("providers.opnsense")

_SETTINGS_GET = "/api/trafficshaper/settings/get"
_SET_PIPE = "/api/trafficshaper/settings/setPipe"
_RECONFIGURE = "/api/trafficshaper/service/reconfigure"

# Read-only diagnostics/telemetry endpoints to probe for *access* — the perf-data
# context (firewall CPU, uplink utilization, memory/load) we'd want to capture around a
# run. These live under OPNsense's Diagnostics privileges, which a trafficshaper-only API
# user does NOT automatically hold — so probing them tells the operator whether the
# existing key can read performance data or needs extra grants. Any 2xx proves the
# privilege; 401/403 = the API user lacks it; 404 = the endpoint isn't on this build. Same
# "written against the documented API, validate against live hardware" caveat as apply();
# ``getCPUType`` is a cheap privilege canary (small JSON GET) rather than the SSE cpu stream.
_DIAG_PROBES = (
    ("cpu", "Read CPU telemetry", "/api/diagnostics/cpu_usage/getCPUType"),
    ("traffic", "Read interface throughput", "/api/diagnostics/traffic/interface"),
    ("sysres", "Read system resources (memory/load)", "/api/diagnostics/system/systemInformation"),
)

# Map PathBrain's normalized parameter names to OPNsense pipe field names. Must cover every
# ``shaper_fields.WRITABLE_FIELDS`` entry (enforced by test_shaper_fields, not just this
# comment) — that's the relationship whose silent drift broke the challenger race.
_PARAM_FIELD = {
    "quantum": "fqcodel_quantum",
    "limit": "fqcodel_limit",
    "flows": "fqcodel_flows",
    "target": "codel_target",
    "interval": "codel_interval",
    "ecn": "codel_ecn_enable",
    "bandwidth": "bandwidth",
    "download_bandwidth": "bandwidth",
}


def _selected(field: object) -> str | None:
    """Extract the selected option key from an OPNsense select field."""
    if isinstance(field, dict):
        for key, opt in field.items():
            if isinstance(opt, dict) and str(opt.get("selected")) == "1":
                return key
        return None
    if field in (None, ""):
        return None
    return str(field)


def _as_bool(field: object) -> bool | None:
    val = _selected(field)
    if val is None:
        return None
    return val in ("1", "true", "True", "on")


def _as_int(field: object) -> int | None:
    val = _selected(field)
    if val in (None, ""):
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _pick(pipe: dict, *keys: str) -> object:
    """Return the first present key's value (field names vary by scheduler)."""
    for k in keys:
        if k in pipe:
            return pipe[k]
    return None


def _pipe_to_config(uuid: str, pipe: dict) -> FqCodelConfig:
    """Parse one OPNsense dnpipe into a normalized FqCodelConfig.

    fq_codel pipes expose quantum/limit/flows as ``fqcodel_*`` and the CoDel
    knobs as ``codel_*`` — read those first, with fallbacks for other schedulers.
    """
    bandwidth = _selected(pipe.get("bandwidth"))
    metric = _selected(pipe.get("bandwidthMetric")) or ""
    bw = f"{bandwidth}{metric}" if bandwidth else None
    return FqCodelConfig(
        download_bandwidth=bw,
        upload_bandwidth=None,  # OPNsense pipes are directional via rules
        quantum=_as_int(_pick(pipe, "fqcodel_quantum", "codel_quantum", "quantum")),
        limit=_as_int(_pick(pipe, "fqcodel_limit", "codel_limit", "queue")),
        target=_selected(pipe.get("codel_target")),
        interval=_selected(pipe.get("codel_interval")),
        ecn=_as_bool(pipe.get("codel_ecn_enable")),
        flows=_as_int(_pick(pipe, "fqcodel_flows", "codel_flows", "flows")),
        queues=_as_int(pipe.get("queue")),
        scheduler=_selected(pipe.get("scheduler")),
        extra={
            "uuid": uuid,
            "description": _selected(pipe.get("description")),
            "enabled": _as_bool(pipe.get("enabled")),
            "mask": _selected(pipe.get("mask")),
        },
    )


class OPNsenseProvider(ConfigProvider):
    name = "opnsense"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        verify_tls: bool = False,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.verify_tls = verify_tls
        self.timeout = timeout

    # -- HTTP --------------------------------------------------------------
    def _client(self) -> httpx.Client:
        if not self.base_url:
            raise RuntimeError("OPNsense URL is not configured (PATHBRAIN_OPNSENSE_URL)")
        if not (self.api_key and self.api_secret):
            raise RuntimeError("OPNsense API key/secret not configured")
        return httpx.Client(
            base_url=self.base_url,
            auth=(self.api_key, self.api_secret),
            verify=self.verify_tls,
            timeout=self.timeout,
        )

    def _get(self, path: str) -> dict:
        with self._client() as client:
            resp = client.get(path)
            resp.raise_for_status()
            return resp.json()

    # -- ConfigProvider ----------------------------------------------------
    def discover(self) -> list[FqCodelConfig]:
        data = self._get(_SETTINGS_GET)
        pipes = (((data or {}).get("ts") or {}).get("pipes") or {}).get("pipe") or {}

        configs = [
            _pipe_to_config(uuid, pipe)
            for uuid, pipe in pipes.items()
            if isinstance(pipe, dict)
        ]
        if not configs:
            log.warning("OPNsense discover() found no shaper pipes")
        return configs

    def snapshot(self) -> dict:
        data = self._get(_SETTINGS_GET)
        return {"provider": self.name, "base_url": self.base_url, "trafficshaper": data}

    def apply(self, changes: dict) -> dict:
        """Set one pipe field and reconfigure the shaper.

        NOTE: written against the documented OPNsense API but not yet exercised
        against live hardware — validate with the experiment engine's dry-run mode
        before arming it for real.
        """
        param = changes.get("param")
        value = changes.get("value")
        field = _PARAM_FIELD.get(param or "")
        if not field:
            raise ValueError(f"Unknown/unsupported param '{param}'")

        data = self._get(_SETTINGS_GET)
        pipes = (((data or {}).get("ts") or {}).get("pipes") or {}).get("pipe") or {}
        uuid = changes.get("pipe_uuid") or (next(iter(pipes)) if pipes else None)
        if not uuid or uuid not in pipes:
            raise RuntimeError("Target shaper pipe not found")

        pipe = pipes[uuid]
        # Flatten OPNsense's select fields ({key:{selected}}) to settable scalars.
        payload = {k: (_selected(v) if isinstance(v, dict) else v) or "" for k, v in pipe.items()}
        payload[field] = str(value)

        with self._client() as client:
            resp = client.post(f"{_SET_PIPE}/{uuid}", json={"pipe": payload})
            resp.raise_for_status()
            rc = client.post(_RECONFIGURE, json={})
            rc.raise_for_status()
        log.info("OPNsense applied %s=%s to pipe %s", field, value, uuid)
        return {"provider": self.name, "ok": True, "uuid": uuid, "applied": {field: value}}

    def _probe(self, path: str) -> tuple[bool | None, str]:
        """GET ``path`` purely to classify *access*, never raising: 2xx → have the
        privilege; 401/403 → the API user lacks it; 404 → not present on this build
        (indeterminate); anything else → surfaced as an error string."""
        try:
            with self._client() as client:
                resp = client.get(path)
        except Exception as exc:  # noqa: BLE001
            return None, f"unreachable: {type(exc).__name__}: {exc}"
        code = resp.status_code
        if 200 <= code < 300:
            return True, f"HTTP {code} — accessible"
        if code in (401, 403):
            return False, f"HTTP {code} — the API user lacks this privilege"
        if code == 404:
            return None, "HTTP 404 — endpoint not present on this OPNsense build"
        return False, f"HTTP {code}"

    def access_checks(self) -> list[dict]:
        """Config reads (from the base) plus the read-only diagnostics/perf probes, so the
        UI can show exactly what this credential can and cannot read on the firewall."""
        checks = super().access_checks()
        for key, label, path in _DIAG_PROBES:
            ok, detail = self._probe(path)
            checks.append(
                {
                    "key": f"diag_{key}",
                    "label": label,
                    "category": "diagnostics",
                    "ok": ok,
                    "optional": True,
                    "detail": detail,
                    "endpoint": path,
                }
            )
        return checks

    def health(self) -> dict:
        try:
            self._get(_SETTINGS_GET)
            return {"provider": self.name, "ok": True, "base_url": self.base_url}
        except Exception as exc:  # noqa: BLE001
            return {
                "provider": self.name,
                "ok": False,
                "base_url": self.base_url,
                "error": f"{type(exc).__name__}: {exc}",
            }
