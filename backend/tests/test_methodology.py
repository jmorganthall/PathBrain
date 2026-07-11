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


def test_supersede_stale_methodology_pin_drops_reanchor_fork():
    # A GUI re-anchor pins config.methodology_version to a fork of a *superseded* base
    # (v6+…). On the next deploy — which ships a newer CURRENT_METHODOLOGY — startup must
    # drop that stale pin so the code-published version becomes current, instead of the
    # instance staying frozen on the fork forever.
    from pathbrain.config_store import save_config
    from pathbrain.methodology import (
        current_version,
        supersede_stale_methodology_pin,
    )

    with session_scope() as s:
        original = get_config(s).get("methodology_version")
        try:
            stale = "speed-smoothness-v6+fcp-best150"  # base v6 ≠ current v7 → stale
            save_config(s, {"methodology_version": stale})
            assert current_version(get_config(s)) == stale  # pinned before reconcile

            cleared = supersede_stale_methodology_pin(s, get_config(s))
            assert cleared == stale
            # Pin gone → current_version falls back to the code-published methodology.
            assert current_version(get_config(s)) == CURRENT_METHODOLOGY
        finally:
            save_config(s, {"methodology_version": original})


def test_supersede_keeps_current_base_fork_and_bare_pins():
    # A re-anchor of the *current* methodology is a legitimate live fork — keep it until a
    # newer methodology ships. A bare (non-fork) pin is a deliberate operator hold — respect
    # it. Neither writes to config; both return None (no-op).
    from pathbrain.methodology import supersede_stale_methodology_pin

    with session_scope() as s:
        current_fork = {"methodology_version": f"{CURRENT_METHODOLOGY}+fcp-best120"}
        assert supersede_stale_methodology_pin(s, current_fork) is None

        bare = {"methodology_version": "speed-smoothness-v5"}
        assert supersede_stale_methodology_pin(s, bare) is None

        assert supersede_stale_methodology_pin(s, {}) is None  # unset → no-op


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


def test_current_methodology_is_v11_rubric():
    # The published-now methodology is speed-smoothness-v11: the crown's smoothness leg becomes
    # worst_void_fraction (the "pregnant pause" index — the longest void within the FCP→LCP window
    # as a fraction of that window). Scale-free, so it measures the *evenness* of the journey to
    # main content decoupled from how long it took (that's LCP's job), fixing v10's stall_energy
    # double-count with LCP. Crown = FCP × LCP × worst_void_fraction; stall_energy → display-only.
    assert CURRENT_METHODOLOGY == "speed-smoothness-v11"
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
        # speed — time-to-last + interactive + page-load
        "lcp": ("speed", 40, 150.0, 4000.0),
        "render": ("speed", 20, 500.0, 8000.0),
        "inp": ("speed", 40, 50.0, 500.0),
        "load_event": ("speed", 20, 800.0, 8000.0),
        # stability — CLS only
        "cls": ("stability", 50, 0.0, 0.25),
        # smoothness — worst_void_fraction (pregnant-pause index) takes the scored stall slot
        # (stall_energy → display-only)
        "longest_stall": ("smoothness", 40, 25.0, 2000.0),
        "worst_void_fraction": ("smoothness", 30, 0.0, 0.6),
        "cadence_cov": ("smoothness", 15, 0.2, 2.5),
        "delivery_gini": ("smoothness", 15, 0.1, 0.7),
    }
    for key, (axis, weight, best, worst) in expected.items():
        m = by_key[key]
        assert (m["axis"], m["weight"], m["best"], m["worst"]) == (axis, weight, best, worst), key

    # perceived_time, total_stall, stall_time, stall_energy — and v9's reverted legs — display-only.
    for k in ("perceived_time", "total_stall", "stall_time", "stall_energy", "jank_fraction", "nav_response"):
        assert by_key[k]["axis"] is None, k

    # The universal `required` field is materialized onto every metric that defines the
    # Overall/crown (v11: FCP × LCP × worst_void_fraction) plus the flagged longest_stall.
    for key in ("fcp", "lcp", "worst_void_fraction", "longest_stall"):
        assert by_key[key]["required"] is True, key
    # A scored-but-optional metric is NOT required (it redistributes when missing → partial).
    assert by_key["byte_earliness"]["required"] is False
    assert by_key["render"]["required"] is False
    assert {a["key"] for a in d["axes"]} == {
        "responsiveness", "speed", "smoothness", "stability", "completion"
    }
    # Display-only metrics carry no axis (e.g. latency, transfer, speed_index, stall_energy).
    for k in ("latency", "transfer", "speed_index", "network_stall", "stall_energy"):
        assert by_key[k]["axis"] is None

    # v11's crown: the corner over FCP × LCP × worst_void_fraction — initial content × main content
    # loaded × how *evenly* the fill progressed between them (the scale-free pregnant-pause index).
    assert d["overall"] == {
        "method": "corner",
        "metrics": ["fcp", "lcp", "worst_void_fraction"],
        "required": ["fcp", "lcp", "worst_void_fraction"],
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


def test_comparability_gates_on_crown_metrics():
    from pathbrain.methodology import comparability

    d = build_definition_from_spec(METHODOLOGY_REGISTRY["speed-smoothness-v6"])
    # Every scored metric present → exact.
    full = {m["key"]: 1.0 for m in d["metrics"] if m.get("axis")}
    assert comparability(d, full)[0] == "exact"
    # A run missing a crown metric (no total_stall) → incomparable: the crown set is part of
    # the single universal required field, so it gates comparability.
    no_stall = {k: v for k, v in full.items() if k != "total_stall"}
    tag, missing = comparability(d, no_stall)
    assert tag == "incomparable" and "total_stall" in missing
    # All crown metrics present but an optional axis metric missing → partial.
    no_byte = {k: v for k, v in full.items() if k != "byte_earliness"}
    assert comparability(d, no_byte)[0] == "partial"


def test_required_metric_keys_is_the_single_required_set():
    from pathbrain.methodology import required_metric_keys

    d = build_definition_from_spec(METHODOLOGY_REGISTRY["speed-smoothness-v6"])
    req = required_metric_keys(d)
    # Overall == Crown == required: the crown metrics plus the flagged longest_stall, and
    # nothing else (optional scored metrics like byte_earliness are not required).
    assert set(req) == {"fcp", "total_stall", "load_event", "longest_stall"}
    assert "byte_earliness" not in req and "inp" not in req

    # The accessor stays correct for a snapshot that predates the materialized flag: even if
    # only longest_stall carries required:True, the crown (from the overall spec) is unioned in.
    legacy_snapshot = {
        "metrics": [{**m, "required": m["key"] == "longest_stall"} for m in d["metrics"]],
        "overall": d["overall"],
    }
    assert set(required_metric_keys(legacy_snapshot)) == set(req)

    # A pre-Overall methodology (no crown spec) → just the flagged markers.
    d4 = build_definition_from_spec(METHODOLOGY_REGISTRY["speed-smoothness-v4"])
    assert required_metric_keys(d4) == ["longest_stall"]


def test_summarize_reports_the_full_required_set():
    # The Methodology page reads `required_metrics`; it must list the crown, not just the
    # one historically-flagged metric — the transparency gap this change closes.
    from pathbrain.methodology import build_definition_from_spec, summarize
    from pathbrain.models import Methodology

    d = build_definition_from_spec(METHODOLOGY_REGISTRY["speed-smoothness-v6"])
    row = Methodology(version="speed-smoothness-v6", rubric_version="speed-smoothness-v6",
                      derivation_version="derive-v4", definition=d)
    assert set(summarize(row)["required_metrics"]) == {
        "fcp", "total_stall", "load_event", "longest_stall"
    }


def test_v3_methodology_still_frozen():
    # v3 is preserved append-only (its blended Speed axis lives on for old at-measure
    # scores), even though v4 is now current.
    spec = METHODOLOGY_REGISTRY["speed-smoothness-v3"]
    by_key = {m["key"]: m for m in build_definition_from_spec(spec)["metrics"]}
    assert by_key["lcp"]["axis"] == "speed"
    assert by_key["inp"]["axis"] == "stability"
