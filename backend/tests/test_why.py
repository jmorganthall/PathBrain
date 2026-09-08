"""Where a win lives (`why.py`, `GET /api/settings/profiles/{fp}/why`).

The crown says which profile wins and by how much; this says what the margin is made of.
Pinned here: the per-leg points add up to the gap exactly under the weighted crown and are
formed from the same rounded medians the grade uses; deltas are signed from the profile's
own side with a noise bar; the default reference is the unshaped link when it has runs;
per-site legs are re-derived from raw and priced on the methodology's own thresholds; a
corner crown is labelled inexact rather than pretending to add up.
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import median

import pytest
from sqlalchemy import delete

from pathbrain import profile_aggregates, profile_names, why
from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.methodology import ensure_current_methodology, overall_metrics, overall_weights
from pathbrain.models import BenchmarkResult, Run, RunStatus, Score
from pathbrain.scoring.engine import _normalize
from pathbrain.settings_profile import SQM_OFF_FINGERPRINT

A_FP, B_FP = "whyprofA001", "whyprofB001"
SITE1, SITE2 = "https://one.example/", "https://two.example/"

_RES = [{"responseEnd": t} for t in (180.0, 210.0, 300.0, 560.0)]
_LOAF = {"source": "longtask", "entries": [{"startTime": 300.0, "duration": 200.0}]}


def _nav(request_ms: float, render_ms: float, fcp: float) -> dict:
    """Navigation marks whose phases are: dns 3, tcp 12, tls 20, request `request_ms`,
    response 135, render `render_ms` (responseEnd → FCP)."""
    connect_end = 40.0
    response_start = connect_end + request_ms
    response_end = response_start + 135.0
    assert abs((fcp - response_end) - render_ms) < 1e-9, "fcp must equal responseEnd + render"
    return {
        "navigationStart": 0.0, "domainLookupStart": 5.0, "domainLookupEnd": 8.0,
        "connectStart": 8.0, "secureConnectionStart": 20.0, "connectEnd": connect_end,
        "requestStart": connect_end + 1.0, "responseStart": response_start,
        "responseEnd": response_end, "loadEventEnd": response_end + 300.0,
    }


def _definition():
    with session_scope() as s:
        m = ensure_current_methodology(s, get_config(s))
        return m.version, dict(m.definition or {})


def _thresholds(definition: dict) -> dict[str, tuple[float, float]]:
    return {m["key"]: (m["best"], m["worst"]) for m in definition["metrics"] if m.get("best") is not None}


def _seed(
    fp: str, n: int, *, request_ms: float, render_ms: float, lcp: dict[str, float], stall: float,
    version: str, definition: dict, settings=None,
) -> list[int]:
    """`n` comparable runs for one profile. Page loads on SITE1/SITE2 with the given per-site
    LCP; a ±1 ms alternation per run gives every series a small, finite IQR so the noise bar
    is computable (and small)."""
    thr = _thresholds(definition)
    crown, _ = overall_metrics(definition)
    ids: list[int] = []
    with session_scope() as s:
        for i in range(n):
            wobble = 1.0 if i % 2 else -1.0
            req = request_ms + wobble
            fcp = 40.0 + req + 135.0 + render_ms
            urls = {}
            for url in (SITE1, SITE2):
                urls[url] = {
                    "nav": _nav(req, render_ms, fcp),
                    "paint": {"fcp": fcp, "lcp": lcp[url] + wobble},
                    "resources": _RES, "loaf": _LOAF,
                }
            raw = {"iterations": [{"urls": urls}, {"urls": urls}]}
            raw_values = {"fcp": fcp, "lcp": median(lcp.values()) + wobble, "network_stall_all": stall + wobble}
            subscores = {m: _normalize(raw_values[m], *thr[m]) for m in crown}
            run = Run(
                status=RunStatus.COMPLETE,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                iterations=2, settings_fingerprint=fp,
                settings=settings if settings is not None else [{"label": "wan", "quantum": 1514, "enabled": True}],
            )
            s.add(run)
            s.flush()
            s.query(Score).filter(Score.run_id == run.id).delete()
            s.add(Score(run_id=run.id, methodology_version=version, comparability="exact",
                        subscores=subscores, metric_values=raw_values, axis_scores={}))
            s.add(BenchmarkResult(
                run_id=run.id, plugin="browser", success=True, raw=raw,
                metrics={
                    "fcp_ms": fcp, "lcp_ms": raw_values["lcp"], "network_stall_all_ms": raw_values["network_stall_all"],
                    "nav_dns_ms": 3.0, "nav_tcp_ms": 12.0, "nav_tls_ms": 20.0,
                    "nav_request_ms": req, "nav_response_ms": 135.0, "nav_render_ms": render_ms,
                },
            ))
            ids.append(run.id)
        s.commit()
    return ids


def _clear(ids: list[int], fps: list[str], version: str) -> None:
    with session_scope() as s:
        s.execute(delete(Score).where(Score.run_id.in_(ids)))
        s.execute(delete(BenchmarkResult).where(BenchmarkResult.run_id.in_(ids)))
        s.execute(delete(Run).where(Run.id.in_(ids)).execution_options(synchronize_session=False))
        s.commit()
    profile_aggregates.invalidate(fps, version)


@pytest.fixture()
def two_profiles():
    """A beats B: 50 ms less request wait (so FCP and LCP both ~50 ms sooner), 40 ms less
    network stall, and on SITE1 a further 100 ms LCP edge; render identical."""
    version, definition = _definition()
    ids = _seed(A_FP, 6, request_ms=100.0, render_ms=40.0, lcp={SITE1: 400.0, SITE2: 500.0}, stall=60.0,
                version=version, definition=definition)
    ids += _seed(B_FP, 6, request_ms=150.0, render_ms=40.0, lcp={SITE1: 550.0, SITE2: 550.0}, stall=100.0,
                 version=version, definition=definition)
    try:
        yield version, definition
    finally:
        _clear(ids, [A_FP, B_FP], version)


def _explain(fp: str, vs: str, limit: int = 30) -> dict:
    with session_scope() as s:
        return why.explain(s, fp, vs=vs, site_run_limit=limit)


def test_leg_points_add_up_to_the_gap_and_are_formed_from_the_graded_medians(two_profiles):
    version, definition = two_profiles
    out = _explain(A_FP, B_FP)
    assert out["method"] == "weighted" and out["exact"] is True
    weights = overall_weights(definition)
    crown, _ = overall_metrics(definition)
    total_w = sum(weights.get(m, 1.0) for m in crown)

    with session_scope() as s:
        rolled = profile_aggregates.aggregates(s, version, [A_FP, B_FP])
    for leg in out["legs"]:
        m = leg["metric"]
        med_a = round(rolled[A_FP]["metrics"][m]["median"], 2)
        med_b = round(rolled[B_FP]["metrics"][m]["median"], 2)
        assert leg["a"]["subscore"] == med_a and leg["b"]["subscore"] == med_b
        assert leg["points"] == pytest.approx(weights[m] * (med_a - med_b) / total_w, abs=0.011)
        assert leg["points"] > 0  # A is ahead on every leg
    assert out["gap"]["points"] == pytest.approx(sum(l["points"] for l in out["legs"]), abs=0.02)
    # The sides' Overalls are the crown grade (rounded to 0.1), so the gap agrees within that.
    assert out["a"]["overall"] - out["b"]["overall"] == pytest.approx(out["gap"]["points"], abs=0.11)
    assert out["gap"]["clear"] is True
    assert out["a"]["iterations"] == 12 and out["b"]["runs"] == 6


def test_deltas_are_signed_from_this_profiles_side_with_a_noise_bar(two_profiles):
    out = _explain(A_FP, B_FP)
    phases = {p["metric"]: p for p in out["phases"]}
    # A waits 50 ms less for the first byte: negative from A's side, and clear of noise.
    assert phases["nav_request"]["delta"] == pytest.approx(-50.0, abs=0.01)
    assert phases["nav_request"]["clear"] is True
    assert phases["nav_request"]["se"] is not None and phases["nav_request"]["se"] < 5
    # Render is the machine, identical on both sides: no delta, not clear.
    assert phases["nav_render"]["delta"] == 0.0 and phases["nav_render"]["clear"] is False
    # Raw crown values ride the legs too, in ms, signed the same way.
    legs = {l["metric"]: l for l in out["legs"]}
    assert legs["network_stall_all"]["delta_raw"] == pytest.approx(-40.0, abs=0.01)
    assert legs["fcp"]["delta_raw"] == pytest.approx(-50.0, abs=0.01)
    # Read from B's side the same numbers flip sign.
    flipped = _explain(B_FP, A_FP)
    assert flipped["gap"]["points"] == pytest.approx(-out["gap"]["points"], abs=0.02)
    assert {p["metric"]: p["delta"] for p in flipped["phases"]}["nav_request"] == pytest.approx(50.0, abs=0.01)


def test_sites_are_rederived_from_raw_and_priced_on_the_methodology_thresholds(two_profiles):
    version, definition = two_profiles
    out = _explain(A_FP, B_FP)
    assert out["site_runs"] == {"a": 6, "b": 6}
    sites = {s["url"]: s for s in out["sites"]}
    assert set(sites) == {SITE1, SITE2}
    # SITE1 carries the extra 100 ms LCP edge, so it prices the biggest gap and leads the table.
    assert out["sites"][0]["url"] == SITE1
    assert sites[SITE1]["points"] > sites[SITE2]["points"] > 0
    lcp1 = next(l for l in sites[SITE1]["legs"] if l["metric"] == "lcp")
    assert lcp1["a"] == pytest.approx(400.0, abs=0.01) and lcp1["b"] == pytest.approx(550.0, abs=0.01)
    assert lcp1["delta"] == pytest.approx(-150.0, abs=0.01) and lcp1["clear"] is True
    # Priced exactly as the crown would price that leg on this page alone.
    thr = _thresholds(definition)
    weights = overall_weights(definition)
    crown, _ = overall_metrics(definition)
    total_w = sum(weights.get(m, 1.0) for m in crown)
    expected = weights["lcp"] * (_normalize(400.0, *thr["lcp"]) - _normalize(550.0, *thr["lcp"])) / total_w
    assert lcp1["points"] == pytest.approx(expected, abs=0.011)
    # Every leg is priced from its own re-derived medians on that page, by the same rule.
    stall1 = next(l for l in sites[SITE1]["legs"] if l["metric"] == "network_stall_all")
    assert stall1["a"] is not None and stall1["b"] is not None
    expected_stall = (
        weights["network_stall_all"]
        * (_normalize(stall1["a"], *thr["network_stall_all"]) - _normalize(stall1["b"], *thr["network_stall_all"]))
        / total_w
    )
    assert stall1["points"] == pytest.approx(expected_stall, abs=0.011)
    # And the page-level phases name where the edge sits: request wait, not render.
    assert sites[SITE1]["top_phase"]["metric"] == "nav_request"
    assert sites[SITE1]["host"] == "one.example"


def test_the_verdict_names_the_winner_and_what_carries_it(two_profiles):
    out = _explain(A_FP, B_FP)
    with session_scope() as s:
        names = profile_names.names_for(s, [A_FP, B_FP])
    assert out["verdict"].startswith(f"{names[A_FP]} beats {names[B_FP]} by ")
    top = max(out["legs"], key=lambda l: l["points"])
    assert top["label"] in out["verdict"]
    assert "Request / TTFB wait" in out["verdict"]  # the phase the 50 ms sits in
    assert "one.example" in out["verdict"]
    # Read from the loser's side the sentence is still told from the winner's.
    assert _explain(B_FP, A_FP)["verdict"].startswith(f"{names[A_FP]} beats {names[B_FP]} by ")


def test_the_default_reference_is_the_unshaped_link_when_it_has_runs(two_profiles):
    version, definition = two_profiles
    ids = _seed(SQM_OFF_FINGERPRINT, 3, request_ms=160.0, render_ms=40.0, lcp={SITE1: 600.0, SITE2: 600.0},
                stall=120.0, version=version, definition=definition,
                settings=[{"label": "wan", "quantum": 1514, "enabled": False}])
    try:
        with session_scope() as s:
            out = why.explain(s, A_FP, site_run_limit=0)
        assert out["reference"]["why"] == "sqm_off"
        assert out["b"]["fingerprint"] == SQM_OFF_FINGERPRINT
        assert out["b"]["name"] == "No Shaper"
        assert out["sites"] == [] and out["site_runs"] == {"a": 0, "b": 0}  # the pass was skipped
        # The unshaped link itself compares against something else, never itself.
        with session_scope() as s:
            off = why.explain(s, SQM_OFF_FINGERPRINT, site_run_limit=0)
        assert off["b"]["fingerprint"] != SQM_OFF_FINGERPRINT
    finally:
        _clear(ids, [SQM_OFF_FINGERPRINT], version)
    with session_scope() as s:
        out = why.explain(s, A_FP, site_run_limit=0)
    assert out["reference"]["why"] in ("crown", "best_other")
    assert out["b"]["fingerprint"] != SQM_OFF_FINGERPRINT


def test_a_corner_crown_is_labelled_inexact_and_priced_by_one_leg_swaps():
    definition = {
        "overall": {"metrics": ["x", "y"], "required": ["x", "y"], "method": "corner"},
        "metrics": [],
    }
    a = {"x": {"n": 4, "median": 90.0, "p25": 89.0, "p75": 91.0}, "y": {"n": 4, "median": 80.0, "p25": 79.0, "p75": 81.0}}
    b = {"x": {"n": 4, "median": 70.0, "p25": 69.0, "p75": 71.0}, "y": {"n": 4, "median": 80.0, "p25": 79.0, "p75": 81.0}}
    out = why.decompose_legs(definition, a, b)
    assert out["method"] == "corner" and out["exact"] is False
    legs = {l["metric"]: l for l in out["legs"]}
    assert legs["y"]["points"] == 0.0  # swapping an identical leg changes nothing
    assert legs["x"]["points"] > 0  # swapping x to B's value lowers A's corner


def test_a_missing_leg_on_one_side_reports_no_gap_rather_than_a_partial_one():
    definition = {"overall": {"metrics": ["x", "y"], "required": ["x"], "method": "weighted",
                              "weights": {"x": 1, "y": 1}}, "metrics": []}
    a = {"x": {"n": 3, "median": 90.0, "p25": 89.0, "p75": 91.0}, "y": {"n": 3, "median": 80.0, "p25": 79.0, "p75": 81.0}}
    b = {"x": {"n": 3, "median": 70.0, "p25": 69.0, "p75": 71.0}}
    out = why.decompose_legs(definition, a, b)
    assert out["exact"] is False
    assert out["gap"]["points"] is None
    legs = {l["metric"]: l for l in out["legs"]}
    assert legs["y"]["missing"] is True and legs["y"]["points"] is None
    assert legs["x"]["points"] == pytest.approx(10.0)


def test_the_route_refuses_what_it_cannot_compare(client, two_profiles):
    assert client.get("/api/settings/profiles/nosuchprofile00/why").status_code == 404
    r = client.get(f"/api/settings/profiles/{A_FP}/why", params={"vs": A_FP})
    assert r.status_code == 404 and "itself" in r.json()["detail"]
    r = client.get(f"/api/settings/profiles/{A_FP}/why", params={"vs": B_FP, "limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["reference"]["why"] == "chosen" and body["site_run_limit"] == 5
    assert body["site_runs"] == {"a": 5, "b": 5}
    assert {l["metric"] for l in body["legs"]} == set(overall_metrics(two_profiles[1])[0])
