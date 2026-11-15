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


# ── S11: the rollup's raw metrics ──────────────────────────────────────────────────────

#: The literal `profile_aggregates.RAW_METRICS` held before S11, for the current methodology.
OLD_PROFILE_AGGREGATES_RAW_METRICS = ("fcp", "lcp")


def test_raw_metrics_read_equals_the_old_literal_for_the_current_methodology():
    from pathbrain import profile_aggregates

    assert CURRENT_METHODOLOGY == "speed-smoothness-v17", "re-check this literal on a new current"
    assert profile_aggregates.RAW_METRICS == OLD_PROFILE_AGGREGATES_RAW_METRICS


def test_raw_metrics_are_the_crowns_millisecond_legs():
    from pathbrain import profile_aggregates

    assert profile_aggregates.RAW_METRICS_UNIT == "ms"
    every = methodology.crown_metric_keys()
    assert every == ("fcp", "lcp", "cadence_cov")
    assert profile_aggregates.RAW_METRICS == tuple(k for k in every if k != "cadence_cov")


def test_crown_metric_keys_follow_each_registry_versions_overall_spec():
    from pathbrain.methodology import overall_metrics

    for version, spec in METHODOLOGY_REGISTRY.items():
        d = build_definition_from_spec(spec)
        listed = [k for k in overall_metrics(d)[0] if k in {m["key"] for m in d["metrics"]}]
        assert methodology.crown_metric_keys(version) == tuple(listed), version
    assert methodology.crown_metric_keys("not-a-version") == ()
