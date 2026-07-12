"""The read-only derivation-integrity audit (`GET /api/runs/{id}/verify-derivation`).

Proves the bronze guarantee end-to-end: a metric's stored value must reproduce **exactly** from
the run's immutable raw under the current derivation. When it doesn't, the run is carrying a
stale-formula value (captured under an older DERIVATION_VERSION and never re-derived) — the
"we're not keeping the same data the same" break — and the audit flags it as drift.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pathbrain.database import session_scope
from pathbrain.interpret import derive
from pathbrain.models import BenchmarkResult, Run, RunStatus
from pathbrain.runner import _median_values

_NAV = {"responseStart": 165.0, "responseEnd": 300.0, "loadEventEnd": 597.0}
_PAINT = {"fcp": 250.0, "lcp": 260.0}
_RES = [{"responseEnd": t} for t in (180.0, 210.0, 300.0, 560.0)]
_LOAF = {"source": "longtask", "entries": [{"startTime": 300.0, "duration": 200.0}]}
_PER_ITER = {"urls": {"https://a/": {"nav": _NAV, "paint": _PAINT, "resources": _RES, "loaf": _LOAF}}}


def _persist_run(stored_metrics: dict) -> int:
    raw = {"iterations": [_PER_ITER, _PER_ITER]}
    with session_scope() as session:
        run = Run(
            status=RunStatus.COMPLETE,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            iterations=2,
            settings_fingerprint="verifyderiv1",
            settings=[{"label": "wan", "quantum": 1514}],
        )
        session.add(run)
        session.flush()
        session.add(
            BenchmarkResult(run_id=run.id, plugin="browser", success=True, metrics=stored_metrics, raw=raw)
        )
        return run.id


def _fresh_median() -> dict:
    """The metrics a fresh re-derivation from the raw produces (what 'stored' should equal)."""
    return _median_values([derive("browser", _PER_ITER), derive("browser", _PER_ITER)])


def test_consistent_run_reproduces_exactly_from_raw(client):
    # Store exactly what a fresh derivation yields → the audit must report like-for-like.
    run_id = _persist_run(_fresh_median())
    body = client.get(f"/api/runs/{run_id}/verify-derivation").json()

    assert body["consistent"] is True
    assert body["drift"] == []
    assert body["checked"] > 0
    assert body["current_derivation"]  # the version everything was checked against


def test_stale_formula_value_is_flagged_as_drift(client):
    # Simulate a run captured under an OLD formula: its stored fcp_ms no longer reproduces from raw.
    stored = _fresh_median()
    assert "fcp_ms" in stored
    stored["fcp_ms"] = stored["fcp_ms"] + 999.0  # a value the current derivation would never produce
    run_id = _persist_run(stored)

    body = client.get(f"/api/runs/{run_id}/verify-derivation").json()
    assert body["consistent"] is False
    drift_keys = {d["key"] for d in body["drift"]}
    assert "fcp_ms" in drift_keys
    row = next(d for d in body["drift"] if d["key"] == "fcp_ms")
    assert row["match"] is False
    assert row["stored"] != row["rederived"]
    # The delta points at how far the stale value is from the true re-derived one.
    assert row["delta"] is not None and abs(row["delta"]) > 900


def test_missing_stored_metric_that_raw_now_produces_is_drift(client):
    # A metric the current derivation emits but the stored cache lacks (a metric added by a newer
    # derive-vN) is also drift — the run needs re-deriving to gain it.
    stored = _fresh_median()
    stored.pop("lcp_ms", None)
    run_id = _persist_run(stored)

    body = client.get(f"/api/runs/{run_id}/verify-derivation").json()
    assert body["consistent"] is False
    assert any(d["key"] == "lcp_ms" and d["stored"] is None for d in body["drift"])


def test_verify_unknown_run_404(client):
    assert client.get("/api/runs/99999999/verify-derivation").status_code == 404


def _persist_at(fingerprint: str, created: datetime, stored_metrics: dict) -> int:
    raw = {"iterations": [_PER_ITER, _PER_ITER]}
    with session_scope() as session:
        run = Run(
            status=RunStatus.COMPLETE,
            created_at=created,
            iterations=2,
            settings_fingerprint=fingerprint,
            settings=[{"label": "wan", "quantum": 1514}],
        )
        session.add(run)
        session.flush()
        session.add(
            BenchmarkResult(run_id=run.id, plugin="browser", success=True, metrics=stored_metrics, raw=raw)
        )
        return run.id


def test_profile_rollup_flags_stale_history(client):
    # The exact scenario under investigation: OLD runs carry a value the current derivation would
    # never produce (a formula changed under them); NEW runs are freshly, correctly derived.
    fp = "profverifstale"
    fresh = _fresh_median()
    stale = {**fresh, "fcp_ms": fresh["fcp_ms"] + 500.0}
    base = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(3):  # old cohort — stale formula
        _persist_at(fp, base.replace(day=1 + i), stale)
    for i in range(3):  # new cohort — consistent
        _persist_at(fp, base.replace(month=6, day=1 + i), fresh)

    body = client.get(f"/api/settings/profiles/{fp}/verify-derivation?sample=3").json()
    assert body["total_runs"] == 6
    assert body["oldest"]["consistent"] is False and "fcp_ms" in body["oldest"]["drift_metrics"]
    assert body["newest"]["consistent"] is True
    assert body["stale_history"] is True     # historical drift, fresh clean — the smoking gun
    assert body["consistent"] is False


def test_profile_rollup_all_consistent(client):
    fp = "profverifclean"
    fresh = _fresh_median()
    base = datetime(2026, 2, 1, 12, 0, 0)
    for i in range(3):
        _persist_at(fp, base.replace(day=1 + i), fresh)
    body = client.get(f"/api/settings/profiles/{fp}/verify-derivation").json()
    assert body["consistent"] is True and body["stale_history"] is False


def test_profile_rollup_unknown_404(client):
    assert client.get("/api/settings/profiles/nope/verify-derivation").status_code == 404
