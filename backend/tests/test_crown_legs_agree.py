"""S12: the settings ranking names no crown leg of its own, and the Overall page, the
settings ranking and the duel all corner over the same legs for the current methodology."""
from __future__ import annotations

from pathlib import Path

from pathbrain import duel as duel_mod, methodology, pooled_field, profile_aggregates, refresh
from pathbrain.api import routes_settings
from pathbrain.config_store import get_config
from pathbrain.database import session_scope


def test_the_overall_page_the_settings_ranking_and_the_duel_name_the_same_crown_legs(monkeypatch):
    seen: list[list[str]] = []
    real = pooled_field.overall_metrics

    def recording(definition):
        out = real(definition)
        seen.append(list(out[0]))
        return out

    monkeypatch.setattr(pooled_field, "overall_metrics", recording)
    monkeypatch.setattr(refresh, "list_profiles", lambda session: [{"fingerprint": "x", "settings": []}])
    monkeypatch.setattr(profile_aggregates, "aggregates", lambda session, version, fps: {})

    with session_scope() as s:
        row = methodology.ensure_current_methodology(s, get_config(s))
        pooled_field.graded_field(s, row.version)  # the Overall page's pooled field
        settings_legs = routes_settings.compute_profiles(s, include_weather=False)["overall_metrics"]
        duel_legs = duel_mod._crown_keys(s, row.version)

    assert seen, "the Overall page read its crown"
    overall_page_legs = seen[-1]
    assert overall_page_legs == list(settings_legs) == list(duel_legs)
    assert overall_page_legs == list(methodology.crown_metric_keys(row.version))


def test_the_settings_module_holds_no_crown_fallback():
    src = Path(routes_settings.__file__).read_text()
    assert "CROWN_METRICS" not in src and "CROWN_REQUIRED" not in src
    assert not hasattr(routes_settings, "CROWN_METRICS")


def test_a_definition_without_a_crown_still_ranks_by_the_legacy_legs():
    """The case the old fallback covered: a pinned pre-v5 version (v1–v4) or a version id
    the registry does not hold. Its legs come from the methodology now, unchanged."""
    assert methodology.LEGACY_CROWN_VERSION == "speed-smoothness-v6"
    legacy = (["fcp", "total_stall", "load_event"], ["fcp", "total_stall", "load_event"])
    assert methodology.ranking_crown({}) == legacy
    assert methodology.ranking_crown(None) == legacy
    v3 = methodology.build_definition_from_spec(methodology.METHODOLOGY_REGISTRY["speed-smoothness-v3"])
    assert methodology.ranking_crown(v3) == legacy
    v17 = methodology.build_definition_from_spec(methodology.METHODOLOGY_REGISTRY["speed-smoothness-v17"])
    assert methodology.ranking_crown(v17) == methodology.overall_metrics(v17)
