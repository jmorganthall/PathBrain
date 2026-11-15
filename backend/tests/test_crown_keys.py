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


# ── S13: the leg-first crowns ──────────────────────────────────────────────────────────

#: The literal `pooled_field.LEG_FIRST` held before S13, for the current methodology.
OLD_POOLED_FIELD_LEG_FIRST = {
    "lcp_first": {"lead": "lcp", "then": "fcp", "config": "lcp_band_pct", "default": 10.0},
    "fcp_first": {"lead": "fcp", "then": "lcp", "config": "fcp_band_pct", "default": 10.0},
}


def test_leg_first_read_equals_the_old_literal_for_the_current_methodology():
    from pathbrain import pooled_field

    assert CURRENT_METHODOLOGY == "speed-smoothness-v17", "re-check this literal on a new current"
    assert pooled_field.LEG_FIRST == OLD_POOLED_FIELD_LEG_FIRST


def test_leg_first_takes_its_legs_from_the_crown_and_only_the_lead_from_policy():
    from pathbrain import pooled_field

    policies = pooled_field.LEG_FIRST_POLICIES
    assert {k: v["lead"] for k, v in policies.items()} == {"lcp_first": "lcp", "fcp_first": "fcp"}
    assert all("then" not in v for v in policies.values())
    # A crown whose millisecond legs differ re-points `then` with no edit here.
    assert pooled_field._leg_first(policies, ("lcp", "ttfb"))["lcp_first"]["then"] == "ttfb"
    # A policy that cannot lead is left out rather than guessed.
    assert "fcp_first" not in pooled_field._leg_first(policies, ("lcp", "ttfb"))
    assert pooled_field._leg_first(policies, ("fcp", "lcp", "ttfb")) == {}


def test_crowning_falls_back_to_the_pooled_crown_when_a_policy_has_no_legs(monkeypatch):
    from pathbrain import crowning, pooled_field

    from pathbrain.database import session_scope

    monkeypatch.setattr(pooled_field, "LEG_FIRST", {})
    with session_scope() as s:
        out = crowning._resolve_policy(s, "pooled-best", "lcp_first")
    assert out["source"] == "pooled" and out["fingerprint"] == "pooled-best"
    assert out["lcp_first"] is None
