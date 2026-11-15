"""S10: the crown keys the instrument audits use are read from the methodology."""
from __future__ import annotations

from pathbrain import evidence, instrument_drift, methodology
from pathbrain.methodology import CURRENT_METHODOLOGY, METHODOLOGY_REGISTRY, build_definition_from_spec
from pathbrain.warm_agreement import crown_sources

#: The literals these constants held before S10, for the current methodology.
OLD_INSTRUMENT_DRIFT_CROWN_KEYS = ("fcp_ms", "lcp_ms", "cadence_cov")
OLD_EVIDENCE_EPOCH_KEYS = ("fcp_ms", "lcp_ms")


def test_the_reads_equal_the_old_literals_for_the_current_methodology():
    assert CURRENT_METHODOLOGY == "speed-smoothness-v17", "re-check these literals on a new current"
    assert instrument_drift.CROWN_KEYS == OLD_INSTRUMENT_DRIFT_CROWN_KEYS
    assert evidence.EPOCH_KEYS == OLD_EVIDENCE_EPOCH_KEYS
    assert methodology.crown_keys() == OLD_INSTRUMENT_DRIFT_CROWN_KEYS


def test_epoch_keys_are_the_crown_minus_the_documented_exclusion():
    assert evidence.EPOCH_EXCLUDED_KEYS == {"cadence_cov"}
    assert set(evidence.EPOCH_KEYS) == set(methodology.crown_keys()) - evidence.EPOCH_EXCLUDED_KEYS


def test_crown_keys_agree_with_the_browser_sources_of_every_registry_version():
    for version, spec in METHODOLOGY_REGISTRY.items():
        expected = tuple(crown_sources(build_definition_from_spec(spec)).values())
        assert methodology.crown_keys(version) == expected, version


def test_an_unknown_version_has_no_crown_keys():
    assert methodology.crown_keys("not-a-version") == ()
