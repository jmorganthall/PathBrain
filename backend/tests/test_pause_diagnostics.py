"""Round-trip tests for the "Where's the pause?" diagnostic.

These persist a real ``Run`` + browser ``BenchmarkResult`` with the **actual stored raw shape**
(``{"iterations": [{"urls": {...}}]}``) and hit ``/api/results/{id}``, so what's verified is the
real serialization path — not a hand-mocked structure. The original bug (reading ``raw["urls"]``
at the top level, one nesting level too shallow) emptied the card for every run yet passed a
pure-function test that mocked the raw at the wrong level; a round-trip test is what catches that.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from pathbrain.database import session_scope
from pathbrain.models import BenchmarkResult, Run, RunStatus
from pathbrain.raw_access import browser_url_observations, stored_iterations


def _persist_browser_run(raw: dict, fingerprint: str | None = None) -> int:
    with session_scope() as session:
        run = Run(
            status=RunStatus.COMPLETE,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            iterations=2,
            settings_fingerprint=fingerprint,
            settings=[{"label": "wan", "quantum": 1514}],
        )
        session.add(run)
        session.flush()
        session.add(
            BenchmarkResult(run_id=run.id, plugin="browser", success=True, metrics={"fcp_ms": 250.0}, raw=raw)
        )
        return run.id


_NAV = {"responseStart": 165.0, "responseEnd": 300.0, "loadEventEnd": 597.0}
_PAINT = {"fcp": 250.0, "lcp": 260.0}
_RES = [{"responseEnd": t} for t in (180.0, 210.0, 300.0, 560.0)]  # a 260ms void 300→560, post-LCP
_LOAF = {"source": "longtask", "entries": [{"startTime": 300.0, "duration": 200.0}]}  # covers it → render
_PER_ITER = {"urls": {"https://a/": {"nav": _NAV, "paint": _PAINT, "resources": _RES, "loaf": _LOAF}}}


def test_results_endpoint_populates_pause_card_from_the_real_stored_raw(client):
    # The stored shape is {"iterations": [<per-iter {"urls": ...}>, ...]} — the exact nesting the
    # original bug read one level too shallow.
    run_id = _persist_browser_run({"iterations": [_PER_ITER, _PER_ITER]})
    body = client.get(f"/api/results/{run_id}").json()

    assert body["pause_diagnostics"], "the pause card must populate from a real browser run"
    d = body["pause_diagnostics"][0]
    assert d["url"] == "https://a/"
    assert d["phase"] == "lcp_load"       # the felt pause is the post-LCP settle
    assert d["attribution"] == "render"   # main-thread, not byte delivery
    assert d["duration_ms"] == 260.0


def test_results_endpoint_survives_a_list_shaped_loaf(client):
    # Some browsers hand back a bare list for LoAF; it must degrade to "unknown", never 500.
    bad_iter = {"urls": {"x": {"nav": _NAV, "paint": _PAINT, "resources": _RES, "loaf": [1, 2, 3]}}}
    run_id = _persist_browser_run({"iterations": [bad_iter]})
    resp = client.get(f"/api/results/{run_id}")
    assert resp.status_code == 200
    assert resp.json()["pause_diagnostics"][0]["attribution"] == "unknown"


def test_results_endpoint_no_browser_raw_hides_the_card(client):
    run_id = _persist_browser_run({"iterations": []})
    assert client.get(f"/api/results/{run_id}").json()["pause_diagnostics"] is None


def test_profile_pause_rollup_aggregates_across_runs(client):
    # The profile-level roll-up of the run-detail card: per URL, the median void + dominant phase +
    # network/render split across the profile's runs.
    fp = "pauseroll01x"
    try:
        for _ in range(3):
            _persist_browser_run({"iterations": [_PER_ITER]}, fingerprint=fp)  # 260ms lcp_load render void

        body = client.get(f"/api/results/profile/{fp}/pauses").json()
        assert body["fingerprint"] == fp and body["runs"] == 3
        u = next(x for x in body["urls"] if x["url"] == "https://a/")
        assert u["runs"] == 3
        assert u["median_void_ms"] == 260.0
        assert u["phase"] == "lcp_load" and u["phase_fraction"] == 1.0
        assert u["attribution"] == "render" and u["render_fraction"] == 1.0 and u["network_fraction"] == 0.0

        # A fingerprint with no browser runs → empty roll-up, not an error.
        empty = client.get("/api/results/profile/nope/pauses").json()
        assert empty["runs"] == 0 and empty["urls"] == []
    finally:
        # Clean up the fingerprinted rows so this test can't pollute the shared test DB's
        # profile/diagnostic counts that other suites assert on.
        with session_scope() as s:
            for run in list(s.scalars(select(Run).where(Run.settings_fingerprint == fp))):
                for r in list(run.results):
                    s.delete(r)
                s.delete(run)


def test_raw_access_readers_agree_on_the_stored_nesting():
    # The accessors ARE the contract: stored_iterations unwraps "iterations";
    # browser_url_observations walks (iteration, url, observation).
    raw = {"iterations": [_PER_ITER, _PER_ITER]}
    assert len(stored_iterations(raw)) == 2
    obs = list(browser_url_observations(raw))
    assert [(i, url) for i, url, _ in obs] == [(0, "https://a/"), (1, "https://a/")]
    # Back-compat: a bare per-iteration payload reads as one iteration (not silently empty).
    assert len(stored_iterations(_PER_ITER)) == 1
    assert len(list(browser_url_observations(_PER_ITER))) == 1
    # Junk in → empty out, never raises.
    assert stored_iterations(None) == [] and list(browser_url_observations({"iterations": [7, "x"]})) == []
