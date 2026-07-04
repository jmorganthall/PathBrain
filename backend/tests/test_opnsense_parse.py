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


def test_plan_apply_matches_ms_string_to_bare_number_target():
    """Regression for the real OPNsense failure: the firewall reports ``codel_target`` as the
    bare number ``"5"`` (its option key), so a profile/AI value of ``"5ms"`` must NOT read as a
    change (no spurious write) and must NOT leave a phantom "did not accept" diff on verify."""
    from pathbrain.settings_profile import plan_apply

    live = [_pipe_to_config("u", SAMPLE_PIPE)]  # target "5", interval "100"
    # Same setting, expressed the way an old profile / AI reply might carry it.
    target = [{"label": "WAN download", "target": "5ms", "interval": "100ms"}]
    changes, _warnings = plan_apply(target, live)
    assert changes == []  # "5ms" == "5" — nothing to write, verify would see no remaining diff


def test_plan_apply_writes_bare_number_for_duration_change():
    """A genuine duration change is written as the firewall's bare option key (3), never "3ms"
    — writing the unit-suffixed string to an option-keyed select silently doesn't take."""
    from pathbrain.settings_profile import plan_apply

    live = [_pipe_to_config("u", SAMPLE_PIPE)]  # target "5"
    target = [{"label": "WAN download", "target": "3ms"}]
    changes, _warnings = plan_apply(target, live)
    assert len(changes) == 1
    assert changes[0]["param"] == "codel_target" or changes[0]["field"] == "target"
    assert changes[0]["value"] == 3  # bare number, not "3ms"
