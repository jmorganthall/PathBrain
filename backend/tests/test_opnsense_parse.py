"""Parsing of an OPNsense dnpipe into a normalized FqCodelConfig.

Guards the field-name mapping (fq_codel exposes quantum/limit/flows as
``fqcodel_*``) so changing quantum actually shows up in the settings profile.
"""
from __future__ import annotations

from pathbrain.providers.opnsense import _pipe_to_config

# Shape mirrors OPNsense's /api/trafficshaper/settings/get pipe entry: <select>
# fields are {optionKey: {value, selected}}, scalars are plain strings.
SAMPLE_PIPE = {
    "enabled": "1",
    "bandwidth": "900",
    "bandwidthMetric": {
        "bit": {"value": "Bit/s", "selected": 0},
        "Mbit": {"value": "Mbit/s", "selected": 1},
    },
    "queue": "",
    "scheduler": {
        "fifo": {"value": "FIFO", "selected": 0},
        "fq_codel": {"value": "FlowQueue-CoDel", "selected": 1},
    },
    "codel_target": "5",
    "codel_interval": "100",
    "codel_ecn_enable": "1",
    "fqcodel_quantum": "3000",
    "fqcodel_limit": "10240",
    "fqcodel_flows": "1024",
    "description": "WAN download",
}


def test_pipe_to_config_reads_fqcodel_fields():
    cfg = _pipe_to_config("abc-uuid", SAMPLE_PIPE)
    assert cfg.quantum == 3000  # was previously missed (read as codel_quantum)
    assert cfg.limit == 10240
    assert cfg.flows == 1024
    assert cfg.target == "5"
    assert cfg.interval == "100"
    assert cfg.ecn is True
    assert cfg.scheduler == "fq_codel"
    assert cfg.download_bandwidth == "900Mbit"
    assert cfg.extra["uuid"] == "abc-uuid"


def test_quantum_change_changes_fingerprint():
    from pathbrain.settings_profile import fingerprint, normalize

    base = normalize([_pipe_to_config("u", SAMPLE_PIPE)])
    changed_pipe = {**SAMPLE_PIPE, "fqcodel_quantum": "6000"}
    changed = normalize([_pipe_to_config("u", changed_pipe)])
    assert fingerprint(base) != fingerprint(changed)


class _FakeResp:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeClient:
    """Context-manager stand-in for httpx.Client returning a scripted status per path."""

    def __init__(self, by_path: dict):
        self._by_path = by_path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, path: str):
        status = self._by_path.get(path)
        if status is None:
            raise RuntimeError("connection refused")
        return _FakeResp(status)


def test_probe_classifies_access_by_status():
    """The access probe maps HTTP status → allowed / denied / indeterminate so the UI can
    tell 'the key can read this' from 'the key lacks the privilege' from 'not on this build'."""
    from pathbrain.providers.opnsense import OPNsenseProvider

    prov = OPNsenseProvider(base_url="https://fw", api_key="k", api_secret="s")
    prov._client = lambda: _FakeClient(  # type: ignore[method-assign]
        {"/ok": 200, "/forbidden": 403, "/missing": 404, "/boom": 500}
    )

    ok, _ = prov._probe("/ok")
    assert ok is True
    denied, detail = prov._probe("/forbidden")
    assert denied is False and "privilege" in detail
    unknown, detail = prov._probe("/missing")
    assert unknown is None and "not present" in detail
    err, _ = prov._probe("/boom")
    assert err is False
    unreachable, detail = prov._probe("/never-configured")
    assert unreachable is None and "unreachable" in detail
