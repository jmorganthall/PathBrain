"""S6: the host-quiet gate in config and its pure function."""
from __future__ import annotations

from pathbrain import host_pressure as hp
from pathbrain.config_store import DEFAULT_CONFIG

HOST = {"host_cpu_pct": 90.0, "container_share_of_busy": 0.1}
SELF = {"host_cpu_pct": 90.0, "container_share_of_busy": 0.9}
QUIET = {"host_cpu_pct": 10.0}
UNREADABLE = {"host_cpu_pct": 90.0}  # busy, but no container share: attribution None


def test_the_gate_ships_off():
    assert DEFAULT_CONFIG["host_quiet"] == {"enabled": False, "max_host_share": 0.0}
    assert hp.measured_quiet([HOST, HOST], DEFAULT_CONFIG) is True


def test_host_share_counts_only_attributable_runs():
    assert hp.host_share([HOST, SELF, QUIET, None, UNREADABLE]) == 1 / 3
    assert hp.host_share([None, UNREADABLE]) is None
    assert hp.host_share([]) is None


def test_when_on_a_busy_host_leg_fails_the_gate_at_the_default_share():
    cfg = {"host_quiet": {"enabled": True, "max_host_share": 0.0}}
    assert hp.measured_quiet([QUIET, SELF], cfg) is True
    assert hp.measured_quiet([QUIET, HOST], cfg) is False


def test_the_share_is_inclusive():
    cfg = {"host_quiet": {"enabled": True, "max_host_share": 0.5}}
    assert hp.measured_quiet([HOST, QUIET], cfg) is True
    assert hp.measured_quiet([HOST, HOST, QUIET], cfg) is False


def test_no_reading_never_quarantines():
    cfg = {"host_quiet": {"enabled": True, "max_host_share": 0.0}}
    assert hp.measured_quiet([None, UNREADABLE], cfg) is True


def test_the_block_alone_or_a_partial_block_reads_over_the_defaults():
    assert hp.host_quiet_config(None) == {"enabled": False, "max_host_share": 0.0}
    assert hp.host_quiet_config({"enabled": True}) == {"enabled": True, "max_host_share": 0.0}
    assert hp.measured_quiet([HOST], {"enabled": True, "max_host_share": 1.0}) is True
