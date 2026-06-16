"""Tests for persisted config merge behavior."""
from __future__ import annotations

from pathbrain.config_store import DEFAULT_CONFIG, _deep_merge


def test_deep_merge_overrides_nested_without_dropping_siblings():
    merged = _deep_merge(DEFAULT_CONFIG, {"weights": {"render": 40}})
    assert merged["weights"]["render"] == 40
    # Sibling default weights preserved.
    assert merged["weights"]["lcp"] == DEFAULT_CONFIG["weights"]["lcp"]
    # Unrelated sections preserved.
    assert merged["icmp"]["targets"] == DEFAULT_CONFIG["icmp"]["targets"]


def test_deep_merge_replaces_lists_wholesale():
    merged = _deep_merge(DEFAULT_CONFIG, {"icmp": {"targets": ["192.168.1.1"]}})
    assert merged["icmp"]["targets"] == ["192.168.1.1"]
    assert merged["icmp"]["count"] == DEFAULT_CONFIG["icmp"]["count"]
