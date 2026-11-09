"""The evidence layer: one comparison primitive, instrument epochs, and one search space.

The 2026-11-08 review found the recommendation ledger grading ten Explore bets as misses by
7 to 21 points, because each was compared with a pooled best built before the instrument
stepped. Against what ran the same night they were level. These tests pin the three things
that stop that recurring: a comparison never crosses an instrument step, a claim is graded
as a margin over its parent measured in the same conditions, and nothing outside the
shared search space (a held pipe speed, an inert lever already tried) is ever proposed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import pathbrain.api.routes_settings as rs
from pathbrain import evidence, explore, explore_tracker

from . import test_explore_tracker as ledger_tests
from .test_settings import _crown_metrics, _seed_run


# ── epochs ───────────────────────────────────────────────────────────────────────────


def _day(i: int, lcp: float | None, runs: int = 30, fcp: float | None = 400.0) -> dict:
    key = (datetime(2026, 10, 1) + timedelta(days=i)).strftime("%Y-%m-%d")
    return {"key": key, "runs": runs, "medians": {"lcp_ms": lcp, "fcp_ms": fcp}}


def test_both_lcp_steps_are_epoch_boundaries_and_a_bad_day_is_not():
    """LCP stepped twice (654 ms → 2.0 s, later 1.3 s → 2.7 s). The drift card reports the
    largest step per quantity; epochs need every sustained one, and must ignore a spike."""
    lcp = [650] * 6 + [2000] * 6 + [1300] * 6 + [2700] * 6
    lcp[3] = 2400  # one bad day inside the first era
    found = evidence.epoch_boundaries([_day(i, v) for i, v in enumerate(lcp)])
    assert [b["at"] for b in found] == ["2026-10-07", "2026-10-19"]
    assert all(b["key"] == "lcp_ms" for b in found)
    # 2000 → 1300 is -35%: a drift worth watching, not a new instrument.
    assert found[0]["shift_pct"] > 200 and found[1]["shift_pct"] > 100


def test_thin_days_never_place_a_boundary():
    lcp = [650] * 6 + [2000] * 6
    days = [_day(i, v, runs=3) for i, v in enumerate(lcp)]
    assert evidence.epoch_boundaries(days) == []


def test_a_moment_belongs_to_the_epoch_its_day_starts():
    times = evidence.boundary_times([{"at": "2026-10-19"}, {"at": "2026-11-02"}])
    assert evidence.epoch_of(datetime(2026, 10, 18, 23, 59), times) == 0
    assert evidence.epoch_of(datetime(2026, 10, 19, 0, 0), times) == 1
    assert evidence.epoch_of(datetime(2026, 11, 8, tzinfo=timezone.utc), times) == 2
    assert evidence.epoch_of(datetime(2026, 11, 8), []) == 0


# ── the control ──────────────────────────────────────────────────────────────────────


def _row(at: datetime, iterations: int = 5, comparability: str | None = None) -> tuple:
    return (at, iterations, {"x": 1.0}, comparability)


def test_the_control_prefers_the_same_window_then_the_same_epoch_and_never_another_era():
    times = evidence.boundary_times([{"at": "2026-11-02"}])
    bet = [datetime(2026, 11, 8, 3, 0), datetime(2026, 11, 8, 3, 40)]
    window = [_row(datetime(2026, 11, 8, 4, 30))]
    epoch = [_row(datetime(2026, 11, 4, 12, 0))]
    old = [_row(datetime(2026, 10, 20, 12, 0), iterations=200)]

    picked = evidence.control(bet, window + epoch + old, times)
    assert picked["kind"] == "same_window" and picked["rows"] == window

    picked = evidence.control(bet, epoch + old, times)
    assert picked["kind"] == "same_epoch" and picked["rows"] == epoch

    # Two hundred iterations from before the step are not a control for a bet after it.
    assert evidence.control(bet, old, times)["kind"] is None


def test_an_incomparable_control_run_does_not_count_toward_the_minimum():
    bet = [datetime(2026, 11, 8, 3, 0)]
    rows = [_row(datetime(2026, 11, 8, 3, 30), iterations=10, comparability="incomparable")]
    assert evidence.control(bet, rows, [])["kind"] is None


def test_a_margin_cancels_a_field_that_moved_between_the_claim_and_the_test():
    """Predicted +0.4 over a parent anchored at 37.8; the field then fell eight points, and
    candidate and parent measured 28.1 and 27.9 the same night. The margin is +0.2: level
    with the claim, where the absolute grade calls it a ten-point miss."""
    m = evidence.margin(predicted=38.2, reference=37.8, actual=28.1, control_overall=27.9)
    assert m == {"predicted": 0.4, "measured": 0.2, "error": -0.2}
    assert evidence.margin(predicted=38.2, reference=37.8, actual=28.1, control_overall=None) is None


# ── the search space ─────────────────────────────────────────────────────────────────


def _axis(pipe: str, field: str) -> dict:
    return {"key": f"{pipe}::{field}", "pipe": pipe, "field": field}


def _axes(*pairs) -> dict:
    return {f"{p}::{f}": _axis(p, f) for p, f in pairs}


def test_bandwidth_on_either_pipe_is_never_in_the_search_space():
    axes = _axes(("Download", "download_bandwidth"), ("Upload", "download_bandwidth"),
                 ("Upload", "upload_bandwidth"), ("Download", "quantum"))
    space = evidence.search_space(axes)
    for key in ("Download::download_bandwidth", "Upload::download_bandwidth", "Upload::upload_bandwidth"):
        assert space[key]["status"] == "held" and not space[key]["proposable"]
    assert space["Download::quantum"]["status"] == "live" and space["Download::quantum"]["proposable"]


def test_an_inert_lever_gets_one_check_unless_the_ring_catches_it_moving():
    axes = _axes(("Upload", "target"), ("Upload", "interval"), ("Upload", "limit"))
    claimed = [
        {"parent_fingerprint": "p", "moves": {"Upload::target": 4.0}},
        {"parent_fingerprint": "p", "moves": {"Upload::limit": 2000}, "unreachable": True},
    ]
    ring = {"Upload::interval": {"transitions": [{"significant": True}]}}
    space = evidence.search_space(axes, ring, claimed)
    assert space["Upload::target"]["status"] == "inert_checked" and not space["Upload::target"]["proposable"]
    # The firewall never held the limit proposal, so its check is still owed.
    assert space["Upload::limit"]["status"] == "inert" and space["Upload::limit"]["proposable"]
    assert space["Upload::interval"]["status"] == "surprise" and space["Upload::interval"]["proposable"]


def test_a_candidate_is_in_the_space_only_if_every_lever_it_moves_is():
    space = evidence.search_space(_axes(("Download", "quantum"), ("Upload", "download_bandwidth")))
    ok = {"changes": [{"key": "Download::quantum", "to": 6300}]}
    mixed = {"changes": [{"key": "Download::quantum", "to": 6300},
                         {"key": "Upload::download_bandwidth", "to": 780}]}
    unknown = {"changes": [{"key": "Download::flows", "to": 512}]}
    assert evidence.in_search_space(ok, space)
    assert not evidence.in_search_space(mixed, space)
    assert not evidence.in_search_space(unknown, space)
    assert not evidence.in_search_space({"changes": []}, space)


def _bw_profile(fp: str, *, quantum: int, bw: int, overall: float) -> dict:
    return {
        "fingerprint": fp, "name": fp.title(), "label": f"q{quantum} bw{bw}",
        "overall": overall, "iterations": 30, "confident": True,
        "settings": [
            {"label": "Download", "quantum": quantum, "download_bandwidth": f"{bw}Mbit",
             "scheduler": "fq_codel", "queues": 1},
            {"label": "Upload", "quantum": 500, "download_bandwidth": f"{bw - 100}Mbit",
             "scheduler": "fq_codel", "queues": 1},
        ],
    }


def test_explore_never_proposes_a_pipe_speed_from_any_of_its_builders(monkeypatch):
    """Bandwidth varies across the field and is the strongest curve on it, which is exactly
    the case where the old page proposed it. Nothing runnable may move it: not a headline
    candidate, not a hole's variant, not a crown-leg move, not a bet."""
    profiles = [
        _bw_profile("a", quantum=3000, bw=600, overall=50.0),
        _bw_profile("b", quantum=4000, bw=700, overall=58.0),
        _bw_profile("c", quantum=5000, bw=800, overall=66.0),
        _bw_profile("d", quantum=6000, bw=880, overall=72.0),
        _bw_profile("e", quantum=7000, bw=880, overall=70.0),
    ]
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: {"profiles": profiles})
    out = explore.landscape(None, suggestions=20)
    held = {r["key"] for r in out["search_space"] if r["status"] == "held"}
    assert {"Download::download_bandwidth", "Upload::download_bandwidth"} <= held

    runnable = explore.runnable_candidates(out["candidates"], out["gaps"], out["crown_legs"])
    runnable += out["bets"]
    assert runnable, "the quantum axis should still produce proposals"
    for cand in runnable:
        assert all(ch["field"] != "download_bandwidth" for ch in cand["changes"]), cand["changes"]
    # The hole on a held axis is still reported, with the reason and no button.
    for gap in out["gaps"]:
        if gap["key"].endswith("download_bandwidth"):
            assert "candidate" not in gap and "outside the search space" in gap["blocked"]


# ── the ledger grades margins ────────────────────────────────────────────────────────


@pytest.fixture()
def _ledger_db():
    ledger_tests._clear_ledger()
    evidence.clear_cache()
    yield
    ledger_tests._clear_ledger()
    evidence.clear_cache()


def _runs(fp: str, overall: float, when: datetime, iterations: int = 5) -> None:
    _seed_run(fp, overall, when, iterations=iterations,
              crown_subscores={m: overall for m in _crown_metrics()})


def test_a_bet_level_with_its_parent_the_same_night_grades_on_target(client, _ledger_db):
    """The 2026-11-08 finding, as a test. The parent was 37.8 before the instrument moved and
    measures 27.9 tonight; the bet claimed +0.4 and measured 28.1. The absolute grade says
    a ten-point miss. The margin says +0.2 against +0.4, inside the band."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _runs("recparent01", 37.8, now - timedelta(days=10), iterations=40)
    ledger_tests._record(
        "recbet00001", predicted=38.2, uncertainty=1.5,
        parent_fingerprint="recparent01", parent_overall=37.8, anchor=37.8,
    )
    _runs("recparent01", 27.9, now + timedelta(minutes=30), iterations=5)
    _runs("recbet00001", 28.1, now + timedelta(minutes=5), iterations=5)

    row = ledger_tests._ledger(client, "recbet00001")
    assert row["grade_basis"] == "margin"
    assert row["control"]["kind"] == "same_window" and row["control"]["overall"] == 27.9
    assert row["margin"] == {"predicted": 0.4, "measured": 0.2, "error": -0.2, "reference": "anchor"}
    assert row["verdict"] == "on_target" and row["error"] == -0.2
    assert row["beat_parent"] is True
    assert "within two hours" in row["why"]

    summary = client.get("/api/explore/recommendations").json()["summary"]
    assert summary["graded_margin"] == 1
    assert [r["kind"] for r in summary["by_evidence_margin"]] == ["matched_pair"]


def test_a_claim_with_no_control_is_graded_absolutely_and_never_calibrates(client, _ledger_db):
    """Without a parent run in the same window or epoch the ledger still shows the absolute
    number, says so, and keeps it out of the calibration that widens bet bands."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(3):
        fp = f"recabs0000{i}"
        ledger_tests._record(fp, predicted=38.0, uncertainty=1.0,
                             parent_fingerprint="recnoparent", parent_overall=37.8)
        _runs(fp, 28.0, now + timedelta(minutes=5))
    row = ledger_tests._ledger(client, "recabs00000")
    assert row["grade_basis"] == "absolute" and row["verdict"] == "worse"
    assert "kept out of calibration" in row["why"]

    from pathbrain.database import session_scope

    with session_scope() as s:
        calib = explore_tracker.calibration(s)
    assert "matched_pair" not in calib
