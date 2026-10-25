"""Crown agreement: do two methodology versions rank the profiles alike? (`crown_agreement.py`,
`GET /api/methodologies/crown-agreement`). The audit the v17 publish is read with."""
from __future__ import annotations

from datetime import datetime, timezone

from pathbrain import crown_agreement
from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.methodology import CURRENT_METHODOLOGY, METHODOLOGY_REGISTRY, build_definition_from_spec
from pathbrain.models import BenchmarkResult, Run, RunStatus

V16 = build_definition_from_spec(METHODOLOGY_REGISTRY["speed-smoothness-v16"])
V17 = build_definition_from_spec(METHODOLOGY_REGISTRY["speed-smoothness-v17"])


def _profile(values: dict, n: int = 5) -> list[dict]:
    return [dict(values) for _ in range(n)]


def test_the_default_pair_is_the_prior_registry_version_and_current():
    assert crown_agreement.prior_version("speed-smoothness-v17") == "speed-smoothness-v16"
    assert crown_agreement.prior_version("speed-smoothness-v17+sites-abc123") == "speed-smoothness-v16"
    assert crown_agreement.prior_version(crown_agreement.registry_versions()[0]) is None
    assert crown_agreement.prior_version("no-such-version") is None
    assert crown_agreement.prior_version(CURRENT_METHODOLOGY) is not None


def test_two_versions_that_crown_on_different_legs_disagree_when_the_shape_leg_decides():
    """A profile whose dead-air is low but lurching (even stall total, uneven cadence) wins the
    v16 crown and loses the v17 one — exactly the stall-then-lurch case the shape leg was
    added to see. The audit says so, names both #1s and the crown's two ranks."""
    # fcp/lcp identical: only the smoothness leg can separate them.
    lurch = {"fcp": 300.0, "lcp": 350.0, "network_stall_all": 60.0, "cadence_cov": 1.6}
    even = {"fcp": 300.0, "lcp": 350.0, "network_stall_all": 120.0, "cadence_cov": 0.3}
    slow = {"fcp": 1200.0, "lcp": 1500.0, "network_stall_all": 400.0, "cadence_cov": 0.9}
    profiles = {"lurch": _profile(lurch), "even": _profile(even), "slow": _profile(slow), "thin": _profile(even, n=2)}
    out = crown_agreement.assess(
        profiles, a="speed-smoothness-v16", b="speed-smoothness-v17", a_definition=V16, b_definition=V17,
        min_runs=5, crown_fp="lurch", names={"lurch": "Lurch", "even": "Even"},
    )
    assert out["verdict"] == "disagree"
    assert out["top_a"]["name"] == "Lurch" and out["top_b"]["name"] == "Even"
    assert out["crown"] == {"fingerprint": "lurch", "a_rank": 1, "b_rank": 2}
    assert out["a_crown_metrics"] == ["fcp", "lcp", "network_stall_all"]
    assert out["b_crown_metrics"] == ["fcp", "lcp", "cadence_cov"]
    assert out["profiles"] == 3 and out["thin_profiles"] == 1
    rows = {r["fingerprint"]: r for r in out["rows"]}
    assert rows["slow"]["a_rank"] == 3 and rows["slow"]["b_rank"] == 3
    # The versions sit on different scales (a different leg at a different weight), so only
    # the DIRECTION of the move is meaningful: the even profile gains on the lurching one.
    assert rows["even"]["b_minus_a"] > rows["lurch"]["b_minus_a"]
    assert {m["fingerprint"] for m in out["movers"]} == {"lurch", "even"}
    assert "v16" in out["text"] and "v17" in out["text"] and "#1 under v16 and #2 under v17" in out["text"]


def test_versions_that_order_the_field_alike_agree():
    a = {"fcp": 250.0, "lcp": 300.0, "network_stall_all": 40.0, "cadence_cov": 0.3}
    b = {"fcp": 400.0, "lcp": 500.0, "network_stall_all": 80.0, "cadence_cov": 0.6}
    c = {"fcp": 900.0, "lcp": 1200.0, "network_stall_all": 200.0, "cadence_cov": 1.2}
    out = crown_agreement.assess(
        {"a": _profile(a), "b": _profile(b), "c": _profile(c)},
        a="speed-smoothness-v16", b="speed-smoothness-v17", a_definition=V16, b_definition=V17, min_runs=5,
    )
    assert out["verdict"] == "agree" and out["rho"] == 1.0 and out["movers"] == []
    thin = crown_agreement.assess(
        {"a": _profile(a), "b": _profile(b)},
        a="speed-smoothness-v16", b="speed-smoothness-v17", a_definition=V16, b_definition=V17, min_runs=5,
    )
    assert thin["verdict"] == "insufficient" and "needs three" in thin["text"]


def test_the_route_sits_above_the_version_catch_all_and_an_unknown_version_404s(client):
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        for fp, vals in (("ca-fast", (250.0, 300.0, 40.0, 0.3)), ("ca-mid", (400.0, 500.0, 80.0, 0.6)), ("ca-slow", (900.0, 1200.0, 200.0, 1.2))):
            for _ in range(5):
                run = Run(status=RunStatus.COMPLETE, created_at=now, settings_fingerprint=fp, iterations=1, iterations_completed=1)
                s.add(run)
                s.flush()
                s.add(BenchmarkResult(run_id=run.id, plugin="browser", success=True, details={}, raw={}, metrics={
                    "fcp_ms": vals[0], "lcp_ms": vals[1], "network_stall_all_ms": vals[2], "cadence_cov": vals[3]}))
    with session_scope() as s:
        out = crown_agreement.crown_agreement(s, get_config(s), min_runs=5)
    assert out["b"] == out["methodology"] and out["a"] == crown_agreement.prior_version(out["b"])
    assert out["runs_compared"] >= 15 and out["profiles"] >= 3
    body = client.get("/api/methodologies/crown-agreement?a=speed-smoothness-v16&b=speed-smoothness-v17&min_runs=5").json()
    assert body["a"] == "speed-smoothness-v16" and body["b"] == "speed-smoothness-v17"
    assert body["verdict"] in {"insufficient", "agree", "same_top", "disagree"} and "rows" in body
    assert body["a_crown_metrics"] == ["fcp", "lcp", "network_stall_all"]
    assert client.get("/api/methodologies/crown-agreement?a=no-such-version").status_code == 404
    # A stored fork (not in the registry) resolves from the database.
    with session_scope() as s:
        assert crown_agreement.resolve_definition(s, "no-such-version") is None
        assert crown_agreement.resolve_definition(s, "speed-smoothness-v16")["overall"]["metrics"] == ["fcp", "lcp", "network_stall_all"]
