"""Tests for the methodology layer (Phase 1): snapshot + read API."""
from __future__ import annotations

from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.interpret import DERIVATION_VERSION
from pathbrain.methodology import (
    CURRENT_METHODOLOGY,
    METHODOLOGY_REGISTRY,
    build_definition,
    build_definition_from_spec,
    ensure_current_methodology,
)
from pathbrain.models import Methodology


def test_build_definition_snapshots_effective_rubric():
    with session_scope() as s:
        config = get_config(s)
    d = build_definition(config)
    by_key = {m["key"]: m for m in d["metrics"]}
    # Axes + every registry metric are captured.
    assert {a["key"] for a in d["axes"]} == {"sops", "completion"}
    assert {"byte_earliness", "longest_stall", "dns", "latency"} <= set(by_key)
    # Effective weight + thresholds are frozen onto each scored metric.
    assert by_key["byte_earliness"]["axis"] == "sops"
    assert by_key["byte_earliness"]["weight"] == config["weights"]["byte_earliness"]
    assert by_key["byte_earliness"]["best"] == config["thresholds"]["byte_earliness"]["best"]
    # longest_stall is the current-rubric marker → required (drives comparability).
    assert by_key["longest_stall"]["required"] is True
    # Display-only metrics are present but not scored.
    assert by_key["latency"]["axis"] is None
    assert by_key["latency"]["weight"] == 0.0


def test_ensure_current_is_idempotent_and_immutable():
    # Use a throwaway version so we don't disturb the real current methodology that
    # other tests rely on. ensure() takes an explicit config.
    fake = {"methodology_version": "test-immutable-v0", "weights": {}, "thresholds": {},
            "completion_weights": {}, "completion_thresholds": {}}
    with session_scope() as s:
        first = ensure_current_methodology(s, fake)
        assert first.definition["metrics"]  # snapshotted on first sight
        # Tamper, then ensure again: an existing version must NOT be rebuilt
        # (methodologies are append-only / immutable once recorded).
        first.definition = {"axes": [], "metrics": []}
        s.commit()
    with session_scope() as s:
        again = ensure_current_methodology(s, fake)
        assert again.definition == {"axes": [], "metrics": []}  # untouched
        # Restore the real current methodology for the rest of the suite.
        ensure_current_methodology(s, get_config(s))


def test_methodologies_endpoint_lists_current(client):
    resp = client.get("/api/methodologies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    current = next(m for m in body["methodologies"] if m["is_current"])
    assert current["derivation_version"] == DERIVATION_VERSION
    assert current["scored_metric_count"] >= 1
    assert "longest_stall" in current["required_metrics"]


def test_methodology_current_returns_full_definition(client):
    resp = client.get("/api/methodologies/current")
    assert resp.status_code == 200
    body = resp.json()
    assert "definition" in body and body["definition"]["metrics"]
    version = body["version"]
    # Fetchable by explicit version too.
    by_version = client.get(f"/api/methodologies/{version}")
    assert by_version.status_code == 200
    assert by_version.json()["version"] == version


def test_unknown_methodology_404(client):
    assert client.get("/api/methodologies/no-such-version").status_code == 404


def test_current_methodology_is_v6_rubric():
    # The published-now methodology is speed-smoothness-v6: v5's axes/thresholds, but the
    # crown is decomposed — perceived_time is no longer scored (display-only), total_stall
    # joins Smoothness, and the built-in load_event is scored on Speed.
    assert CURRENT_METHODOLOGY == "speed-smoothness-v6"
    spec = METHODOLOGY_REGISTRY[CURRENT_METHODOLOGY]
    d = build_definition_from_spec(spec)
    by_key = {m["key"]: m for m in d["metrics"]}

    expected = {
        # completion (unchanged)
        "dns": ("completion", 10, 1.0, 150.0),
        "tcp": ("completion", 15, 5.0, 250.0),
        "tls": ("completion", 20, 5.0, 500.0),
        "jitter": ("completion", 5, 0.5, 30.0),
        "packet_loss": ("completion", 5, 0.0, 2.5),
        # responsiveness — time-to-first (v5 aspirational-floor anchors carried over)
        "ttfb": ("responsiveness", 15, 30.0, 1800.0),
        "fcp": ("responsiveness", 25, 150.0, 3000.0),
        "byte_earliness": ("responsiveness", 30, 150.0, 5000.0),
        # speed — time-to-last + interactive + the newly-scored built-in page-load time
        "lcp": ("speed", 40, 150.0, 4000.0),
        "render": ("speed", 20, 500.0, 8000.0),
        "inp": ("speed", 40, 50.0, 500.0),
        "load_event": ("speed", 20, 800.0, 8000.0),
        # stability — CLS only
        "cls": ("stability", 50, 0.0, 0.25),
        # smoothness — perceived_time replaced by cumulative total_stall
        "longest_stall": ("smoothness", 40, 25.0, 2000.0),
        "total_stall": ("smoothness", 30, 0.0, 3000.0),
        "cadence_cov": ("smoothness", 15, 0.2, 2.5),
        "delivery_gini": ("smoothness", 15, 0.1, 0.7),
    }
    for key, (axis, weight, best, worst) in expected.items():
        m = by_key[key]
        assert (m["axis"], m["weight"], m["best"], m["worst"]) == (axis, weight, best, worst), key

    # perceived_time is retained but no longer scored (display-only diagnostic).
    assert by_key["perceived_time"]["axis"] is None

    # longest_stall is the required marker; the five axes are present.
    assert by_key["longest_stall"]["required"] is True
    assert {a["key"] for a in d["axes"]} == {
        "responsiveness", "speed", "smoothness", "stability", "completion"
    }
    # Display-only metrics carry no axis (e.g. latency, transfer, speed_index).
    for k in ("latency", "transfer", "speed_index", "network_stall"):
        assert by_key[k]["axis"] is None

    # v6's crown: the decomposed corner over FCP × total_stall × load_event.
    assert d["overall"] == {
        "method": "corner",
        "metrics": ["fcp", "total_stall", "load_event"],
        "required": ["fcp", "total_stall", "load_event"],
    }


def test_overall_from_definition_corners_the_crown_trinity():
    from pathbrain.methodology import overall_from_definition, overall_metrics

    d = build_definition_from_spec(METHODOLOGY_REGISTRY["speed-smoothness-v6"])
    assert overall_metrics(d) == (["fcp", "total_stall", "load_event"],
                                  ["fcp", "total_stall", "load_event"])
    # All three present → corner over {90, 80, 70}.
    full = overall_from_definition(d, {"fcp": 90, "total_stall": 80, "load_event": 70})
    assert full is not None and 70 < full < 90
    # All equal → the √k-normalized corner equals that value.
    assert overall_from_definition(d, {"fcp": 80, "total_stall": 80, "load_event": 80}) == 80.0
    # A required metric missing → no Overall.
    assert overall_from_definition(d, {"fcp": 80, "total_stall": 80}) is None
    # v4 has no overall spec → None (pre-first-class).
    d4 = build_definition_from_spec(METHODOLOGY_REGISTRY["speed-smoothness-v4"])
    assert overall_from_definition(d4, {"fcp": 80, "total_stall": 80, "load_event": 80}) is None


def test_v3_methodology_still_frozen():
    # v3 is preserved append-only (its blended Speed axis lives on for old at-measure
    # scores), even though v4 is now current.
    spec = METHODOLOGY_REGISTRY["speed-smoothness-v3"]
    by_key = {m["key"]: m for m in build_definition_from_spec(spec)["metrics"]}
    assert by_key["lcp"]["axis"] == "speed"
    assert by_key["inp"]["axis"] == "stability"
