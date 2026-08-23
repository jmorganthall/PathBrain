"""The recommendation ledger: does the exploration model's number mean anything?

The Explore page's output is a prediction, and a prediction nobody scores is a horoscope —
it costs the same night of benchmarking either way. These tests pin the loop that closes:
the claim is written down *before* it is measured, the verdict is derived from the measured
field on every read, and the aggregate is split by **what the prediction rested on**, which
is the only way "the curve was confounded" turns from a caveat into a measured fact.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import pathbrain.api.routes_settings as rs
from pathbrain import explore, explore_tracker
from pathbrain.database import session_scope
from pathbrain.models import ExploreRecommendation, Run, Score

from .test_settings import _crown_metrics, _seed_run

# The test database is shared across the whole session, so profiles seeded here would
# otherwise leak into other modules' views of the field — a thin one is enough to break a
# neighbouring test that asserts every profile is confident. Every fingerprint below starts
# with "rec", and each test cleans up after itself.
_FP_PREFIX = "rec"

_MATCHED = ["measured directly on a matched pair"]
_CONFOUNDED = ["estimated from a confounded marginal curve — discounted, and worth little"]
_MARGINAL = ["estimated from the marginal curve, which averages over other levers"]


def _clear_ledger() -> None:
    """Reset the ledger *and* the runs these tests seeded — the DB outlives the module."""
    with session_scope() as s:
        for row in s.query(ExploreRecommendation).all():
            s.delete(row)
        runs = s.query(Run).filter(Run.settings_fingerprint.like(f"{_FP_PREFIX}%")).all()
        for run in runs:
            for score in s.query(Score).filter(Score.run_id == run.id).all():
                s.delete(score)
            s.delete(run)   # cascades to its BenchmarkResults + ScoreResult


@pytest.fixture(autouse=True)
def _isolate():
    _clear_ledger()
    yield
    _clear_ledger()


def _record(fp: str, *, predicted=70.0, uncertainty=2.0, evidence=None, **kw) -> int:
    return explore_tracker.record(
        fingerprint=fp,
        predicted=predicted,
        uncertainty=uncertainty,
        evidence=evidence if evidence is not None else _MATCHED,
        methodology_version=kw.pop("methodology_version", None) or _current_version(),
        **kw,
    )


def _current_version() -> str:
    from pathbrain.config_store import get_config
    from pathbrain.methodology import ensure_current_methodology

    with session_scope() as s:
        return ensure_current_methodology(s, get_config(s)).version


def _seed_profile(fp: str, overall: float, iterations: int = 20, job_group: str | None = None) -> None:
    """A profile measuring ``overall`` — subscores are what the weighted crown grades.

    ``job_group`` stamps the runs the way a profile test's chunks are stamped, which is how
    the ledger finds the profile a recommendation's benchmark actually ran under."""
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    _seed_run(
        fp, overall, t0 - timedelta(minutes=5), iterations=iterations,
        crown_subscores={m: overall for m in _crown_metrics()},
    )
    if job_group:
        with session_scope() as s:
            for run in s.query(Run).filter(Run.settings_fingerprint == fp).all():
                run.job_group = job_group


def _ledger(client, fp: str) -> dict:
    body = client.get("/api/explore/recommendations").json()
    return next(r for r in body["recommendations"] if r["fingerprint"] == fp)


# ── the claim is written down before the measurement exists ────────────────────────────


def test_a_claim_is_recorded_before_it_is_measured_and_reads_back_pending(client):
    """The whole point: the prediction is stored up front, so it cannot be quietly revised
    once the answer is in. With no runs yet it grades as *pending* — not as a success."""
    _record("recpending01", predicted=71.2, uncertainty=3.1, upside=74.3, best_overall=70.0)
    row = _ledger(client, "recpending01")
    assert row["predicted"] == 71.2 and row["uncertainty"] == 3.1
    assert row["verdict"] == "pending" and row["actual"] is None
    assert "no comparable runs" in row["why"].lower()
    # A pending claim is never counted as a hit.
    summary = client.get("/api/explore/recommendations").json()["summary"]
    assert summary["graded"] == 0 and summary["pending"] >= 1


def test_the_verdict_is_derived_not_stored_so_fresh_runs_move_it(client):
    """No outcome column exists on purpose — the verdict recomputes from the measured field
    on every read, exactly like every other score here, so a re-grade moves it."""
    _record("recderive001", predicted=70.0, uncertainty=2.0)
    assert _ledger(client, "recderive001")["verdict"] == "pending"
    _seed_profile("recderive001", 70.5)
    row = _ledger(client, "recderive001")
    assert row["verdict"] == "on_target" and row["actual"] == 70.5


# ── grading ────────────────────────────────────────────────────────────────────────────


def test_a_prediction_is_graded_against_its_own_stated_band(client):
    """The model is allowed to be wrong by exactly as much as it said it might be. Inside
    the band is a hit; outside it in either direction is a named miss."""
    _record("recband0001", predicted=70.0, uncertainty=5.0)   # band 5.0
    _record("recband0002", predicted=70.0, uncertainty=1.0)   # band 1.0 (the floor)
    _seed_profile("recband0001", 73.0)   # +3.0, inside ±5
    _seed_profile("recband0002", 63.0)   # −7.0, well outside ±1
    assert _ledger(client, "recband0001")["verdict"] == "on_target"
    worse = _ledger(client, "recband0002")
    assert worse["verdict"] == "worse" and worse["error"] == -7.0


def test_a_tiny_stated_uncertainty_cannot_manufacture_a_miss(client):
    """A candidate claiming ±0.1 is claiming a precision run-to-run noise cannot refute, so
    the band has a floor. Without it every recommendation grades as a miss and the ledger
    says nothing."""
    _record("recfloor001", predicted=70.0, uncertainty=0.05)
    _seed_profile("recfloor001", 70.4)
    row = _ledger(client, "recfloor001")
    assert row["band"] == explore_tracker.MIN_BAND
    assert row["verdict"] == "on_target"


def test_a_claim_from_an_older_methodology_is_incomparable_not_wrong(client):
    """A prediction is a number on one rubric's scale. Graded under a different one it is
    not a miss — it is a comparison of two yardsticks, and the ledger says so instead of
    scoring it. The same discipline the run-comparability gate applies."""
    _record("recstale001", predicted=70.0, methodology_version="speed-smoothness-v1")
    _seed_profile("recstale001", 40.0)   # a huge "miss" that must NOT be scored
    row = _ledger(client, "recstale001")
    assert row["verdict"] == "incomparable" and row["stale_methodology"] is True
    assert "yardstick" in row["why"]
    summary = client.get("/api/explore/recommendations").json()["summary"]
    assert summary["graded"] == 0 and summary["incomparable"] >= 1


def test_a_thin_measurement_is_graded_but_flagged_provisional(client):
    """Five iterations is enough to see whether a recommendation went anywhere — which is
    what "Test now" is for — but not enough to argue with, so the standing is marked
    provisional rather than withheld or presented as settled."""
    _record("recthin0001", predicted=70.0, uncertainty=2.0)
    _seed_profile("recthin0001", 70.5, iterations=5)
    row = _ledger(client, "recthin0001")
    assert row["verdict"] == "on_target" and row["provisional"] is True
    assert "provisional" in row["why"].lower()


# ── evidence: the reason the ledger exists ─────────────────────────────────────────────


def test_the_weakest_evidence_decides_the_bucket():
    """A two-lever candidate priced one leg from a matched pair and the other from a
    confounded curve is only as trustworthy as the confounded leg. Scoring it in the
    matched-pair bucket would flatter exactly the class the shrink factor distrusts."""
    assert explore_tracker.evidence_kind(_MATCHED) == "matched_pair"
    assert explore_tracker.evidence_kind(_MATCHED + _CONFOUNDED) == "confounded"
    assert explore_tracker.evidence_kind(_MATCHED + _MARGINAL) == "marginal"
    assert explore_tracker.evidence_kind(["estimated from this profile's own neighbourhood"]) == "conditioned"
    assert explore_tracker.evidence_kind([]) == "unknown"


def test_the_summary_splits_calibration_by_evidence_class(client):
    """The payoff isn't the row, it's the aggregate: *which kind of evidence predicts*.
    Matched pairs landing while confounded curves overshoot is a measured statement about
    the model's own failure mode — the claim CONFOUNDED_SHRINK was written on a hunch."""
    for i, (fp, evidence, actual) in enumerate([
        ("recev000001", _MATCHED, 70.4),      # controlled → lands
        ("recev000002", _MATCHED, 69.6),      # controlled → lands
        ("recev000003", _CONFOUNDED, 61.0),   # confounded → overshoots badly
        ("recev000004", _CONFOUNDED, 62.0),
    ]):
        _record(fp, predicted=70.0, uncertainty=1.0, evidence=evidence)
        _seed_profile(fp, actual)
    summary = client.get("/api/explore/recommendations").json()["summary"]
    by_kind = {b["kind"]: b for b in summary["by_evidence"]}
    assert by_kind["matched_pair"]["on_target"] == 2
    assert by_kind["confounded"]["on_target"] == 0
    # The signed error is the direction of the failure: the confounded curve promised more
    # than the controlled test delivered.
    assert by_kind["confounded"]["mean_error"] < -5
    assert abs(by_kind["matched_pair"]["mean_error"]) < 1
    assert summary["graded"] == 4 and summary["hit_rate"] == 0.5


def test_a_confounded_miss_says_why_it_missed(client):
    """"Off by 6.4" is a number; "priced off a curve we had already flagged as confounded,
    and this is where that shows up" is the finding. The sentence is the deliverable."""
    _record("recwhy00001", predicted=70.0, uncertainty=1.0, evidence=_CONFOUNDED)
    _seed_profile("recwhy00001", 60.0)
    why = _ledger(client, "recwhy00001")["why"]
    assert "confounded" in why and "another lever" in why


def test_the_beat_the_best_claim_is_scored_only_where_it_was_made(client):
    """Candidates are *ranked* on upside beating the field's best, so that claim gets its
    own count — over the recommendations that actually made it, not over all of them."""
    # Claimed it could beat 70 and did.
    _record("recbeat0001", predicted=70.0, uncertainty=1.0, upside=74.0, best_overall=70.0)
    _seed_profile("recbeat0001", 71.0)
    # Claimed it could beat 70 and didn't.
    _record("recbeat0002", predicted=70.0, uncertainty=1.0, upside=74.0, best_overall=70.0)
    _seed_profile("recbeat0002", 69.5)
    # Never claimed to beat anything (a hole-filler) — must not dilute the statistic.
    _record("recbeat0003", predicted=60.0, uncertainty=1.0, upside=61.0, best_overall=70.0)
    _seed_profile("recbeat0003", 60.2)
    summary = client.get("/api/explore/recommendations").json()["summary"]
    assert summary["beat_best_claimed"] == 2 and summary["beat_best"] == 1


# ── measuring the candidate that was actually proposed ─────────────────────────────────


def test_a_candidate_is_materialized_on_its_parent_not_on_the_live_profile():
    """A candidate is "*that* profile with a lever moved". Sending only the moved lever
    measures the firewall as it stands with that lever moved — the proposed profile only
    when the firewall happens to already be on the parent — and since a profile test restores
    the baseline, that is only ever the baseline profile. The read-before/read-after integrity
    check can't catch it: a real, stable profile is applied and measured, it just isn't the one
    that was proposed."""
    parent = [
        {"label": "wan-download", "quantum": 3000, "target": "5ms", "scheduler": "fq_codel", "queues": 1},
        {"label": "wan-upload", "quantum": 300, "target": "5ms", "scheduler": "fq_codel", "queues": 1},
    ]
    merged = explore.full_overrides(parent, [{"label": "wan-download", "quantum": 7000}])
    by_label = {p["label"]: p for p in merged}
    assert by_label["wan-download"]["quantum"] == 7000   # the proposed move
    assert by_label["wan-download"]["target"] == "5ms"   # …the rest of the PARENT, not live
    assert by_label["wan-upload"]["quantum"] == 300
    # Non-writable topology is deliberately absent: it can't be applied, and the apply path
    # keeps the live environment's — the same reachability rule the race uses.
    assert "scheduler" not in by_label["wan-download"]


def test_test_now_runs_a_short_block_and_the_default_tops_up_to_the_minimum(client, monkeypatch):
    """Two questions, one path. "Test now" asks *did this go anywhere?* — a short block,
    cheap enough to try several in an evening. The default asks *is it confidently better?*"""
    from pathbrain.providers import mock as mock_mod
    mock_mod._OVERRIDES.clear()
    calls: list[int] = []
    monkeypatch.setattr(
        rs.profile_test_mod, "start",
        lambda fp, target, label, iters: calls.append(iters) or 77,
    )

    quick = client.post("/api/explore/test", json={
        "settings": {"quantum": 6100}, "label": "Explore: quick",
        "iterations": explore_tracker.QUICK_ITERATIONS,
        "predicted": 70.0, "uncertainty": 2.0, "evidence": _MATCHED,
    })
    assert quick.status_code == 200 and quick.json()["iterations"] == 5
    assert calls == [5]

    deep = client.post("/api/explore/test", json={
        "settings": {"quantum": 6200}, "label": "Explore: deep",
        "predicted": 70.0, "evidence": _MATCHED,
    })
    assert deep.status_code == 200
    # Untested target → the whole confidence minimum, which is more than a quick block.
    assert deep.json()["iterations"] > explore_tracker.QUICK_ITERATIONS


def test_starting_a_test_records_the_claim_against_the_fingerprint_it_will_measure(client, monkeypatch):
    """The recorded fingerprint is the *materialized* target — the profile the firewall is
    actually driven to — so the ledger grades the claim against the runs it produced."""
    from pathbrain.providers import mock as mock_mod
    mock_mod._OVERRIDES.clear()
    monkeypatch.setattr(rs.profile_test_mod, "start", lambda fp, target, label, iters: 78)

    resp = client.post("/api/explore/test", json={
        "settings": {"quantum": 6300}, "label": "Explore: recorded",
        "iterations": 5, "predicted": 71.5, "uncertainty": 2.5, "upside": 74.0,
        "best_overall": 70.0, "evidence": _CONFOUNDED, "multi_lever": False,
        "changes": [{"key": "wan-download::quantum", "from": 1514, "to": 6300}],
    })
    body = resp.json()
    assert body["recommendation_id"] is not None
    row = _ledger(client, body["fingerprint"])
    assert row["predicted"] == 71.5 and row["evidence_kind"] == "confounded"
    assert row["profile_test_id"] == 78 and row["iterations_requested"] == 5
    assert row["changes"][0]["to"] == 6300


def test_a_substituted_parent_is_noted_on_the_record_not_silently_swallowed(client, monkeypatch):
    """When the parent's stored settings can't be found the levers land on the live profile
    instead — which may not be the proposal. A caveat on the record beats a silent
    substitution that later grades as a miss for the wrong reason."""
    from pathbrain.providers import mock as mock_mod
    mock_mod._OVERRIDES.clear()
    monkeypatch.setattr(rs.profile_test_mod, "start", lambda fp, target, label, iters: 79)

    resp = client.post("/api/explore/test", json={
        "settings": {"quantum": 6400}, "iterations": 5, "predicted": 70.0,
        "parent_fingerprint": "nosuchprofile",
    })
    body = resp.json()
    assert body["note"] and "live profile" in body["note"]
    row = _ledger(client, body["fingerprint"])
    assert row["note"] and "live profile" in row["why"]


# ── the fingerprint a claim is recorded against must be the one that gets measured ─────
#
# The bug this section exists for: "the tests ran, but the profile shows no history."
# `fingerprint(target)` is a PREDICTION — the profile we intend to drive the firewall to.
# A run is filed under `fingerprint(normalize(discover()))`, read off the firewall while it
# measures. When the firewall can't be driven all the way to the target those are different
# numbers, and nothing in the pipeline notices: plan_apply drops the unappliable field as a
# *warning*, the apply succeeds, the verify re-plans and again sees no changes, and the
# read-before/read-after check sees no drift because the firewall genuinely never moved.
# The runs are filed correctly — against the profile that actually ran. Only the claim
# points elsewhere, at a fingerprint with no history.


def test_a_target_the_firewall_cannot_reach_is_reported_not_dropped():
    """`plan_apply` skips a pipe it can't write and says so only in `warnings`, which every
    caller discards. That's harmless when the pipe already matches and quietly wrong when it
    doesn't — so the difference itself has to be reportable."""
    from pathbrain.providers import get_provider
    from pathbrain.providers import mock as mock_mod
    from pathbrain.settings_profile import normalize, plan_apply, unwritable_diffs

    mock_mod._OVERRIDES.clear()
    live = get_provider().discover()
    target = [dict(p) for p in normalize(live)]
    # The mock's upload pipe deliberately has no uuid — "an OPNsense pipe apply() can't target".
    target[1]["quantum"] = 555

    changes, warnings = plan_apply(target, live)
    assert not any(c["label"] == target[1]["label"] for c in changes)   # silently not applied
    dropped = unwritable_diffs(target, live)
    assert [(d["label"], d["field"], d["to"]) for d in dropped] == [(target[1]["label"], "quantum", 555)]
    # …and a pipe that can't be written but doesn't need to be is NOT a finding.
    assert unwritable_diffs([dict(p) for p in normalize(live)], live) == []


def test_the_returned_fingerprint_is_the_profile_that_will_actually_be_measured(client, monkeypatch):
    """The fix at the source: unappliable fields are reverted, so the fingerprint handed back
    names a profile the firewall will really be on — instead of one that can never exist, whose
    runs therefore land under some other key."""
    from pathbrain.providers import get_provider
    from pathbrain.providers import mock as mock_mod
    from pathbrain.settings_profile import fingerprint, normalize, plan_apply

    mock_mod._OVERRIDES.clear()
    provider = get_provider()
    applied: dict = {}
    monkeypatch.setattr(
        rs.profile_test_mod, "start",
        lambda fp, target, label, iters: applied.update(fp=fp, target=target) or 91,
    )

    live_norm = normalize(provider.discover())
    resp = client.post("/api/explore/test", json={
        "settings": [
            {"label": live_norm[0]["label"], "quantum": 6500},   # writable — must take
            {"label": live_norm[1]["label"], "quantum": 555},    # no uuid — cannot take
        ],
        "iterations": 5, "predicted": 70.0,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["warnings"], "the caller must be told a requested field was not applied"

    # Drive the firewall exactly as the profile test would, then read back what it became.
    live = provider.discover()
    for change in plan_apply(applied["target"], live)[0]:
        provider.apply(change)
    reached = fingerprint(normalize(provider.discover()))
    assert body["fingerprint"] == reached, (
        "the recorded fingerprint must equal the one the runs will be filed under"
    )
    # The reachable part still happened — this is a trim, not a refusal.
    assert applied["target"][0]["quantum"] == 6500
    mock_mod._OVERRIDES.clear()


def test_a_claim_is_repointed_to_the_profile_its_benchmark_actually_ran(client):
    """The backstop, and what rescues data already collected: whatever the firewall did, the
    runs of a profile test carry `job_group = profile_test-<id>` and their own
    `settings_fingerprint` — the very column profiles are grouped on. Resolving through that
    makes the correlation right by construction, so a claim recorded against a fingerprint
    that never ran is re-pointed at the one that did instead of reading `pending` forever."""
    rec_id = _record("recwrongfp01", predicted=70.0, uncertainty=2.0, profile_test_id=4242)
    # The benchmark ran — but the firewall settled somewhere other than the recorded target.
    _seed_profile("recactual001", 66.0, job_group="profile_test-4242")

    body = client.get("/api/explore/recommendations").json()
    row = next(r for r in body["recommendations"] if r["id"] == rec_id)
    assert row["fingerprint"] == "recactual001"     # re-pointed at what really ran
    assert row["verdict"] == "worse" and row["actual"] == 66.0
    assert "settled on recactual001" in row["note"]
    # …and the correction is persisted, so the profile link is right on the next read too.
    with session_scope() as s:
        assert s.get(ExploreRecommendation, rec_id).fingerprint == "recactual001"


def test_a_claim_whose_benchmark_matches_is_left_alone(client):
    """The re-point must fire only on a genuine contradiction — rewriting a correct row would
    make the ledger unfalsifiable."""
    rec_id = _record("recrightfp01", predicted=70.0, uncertainty=2.0, profile_test_id=4243)
    _seed_profile("recrightfp01", 70.5, job_group="profile_test-4243")
    body = client.get("/api/explore/recommendations").json()
    row = next(r for r in body["recommendations"] if r["id"] == rec_id)
    assert row["fingerprint"] == "recrightfp01" and row["note"] is None
    assert row["verdict"] == "on_target"


def test_restating_a_field_in_another_notation_does_not_invent_a_profile(client, monkeypatch):
    """The dominant cause of the "no history" bug, and the subtlest.

    A candidate carries its parent's WHOLE writable set, so every field — not just the moved
    one — went through `coerce_value`, which canonicalizes to the firewall's wire form
    ("5ms" -> 5). For an unchanged field that is a purely textual difference: `plan_apply`
    compares numerically and plans no write, the firewall keeps reporting "5ms", and yet
    `fingerprint(target)` hashes a profile that will never exist. Every explore test was
    filed under the firewall's real fingerprint while the claim held the invented one.
    """
    from pathbrain.providers import get_provider
    from pathbrain.providers import mock as mock_mod
    from pathbrain.settings_profile import fingerprint, normalize, plan_apply

    mock_mod._OVERRIDES.clear()
    provider = get_provider()
    live_norm = normalize(provider.discover())
    # The firewall reports durations as "5ms"/"100ms"; a parent restates them as 5/100.
    assert live_norm[0]["target"] == "5ms"

    applied: dict = {}
    monkeypatch.setattr(
        rs.profile_test_mod, "start",
        lambda fp, target, label, iters: applied.update(target=target) or 92,
    )
    parent = [dict(p) for p in live_norm]
    overrides = explore.full_overrides(parent, [{"label": parent[0]["label"], "quantum": 7100}])
    resp = client.post("/api/explore/test", json={
        "settings": overrides, "iterations": 5, "predicted": 70.0,
    })
    assert resp.status_code == 200
    body = resp.json()

    # Only the genuinely moved lever is restated; the rest keep the firewall's own notation.
    assert applied["target"][0]["quantum"] == 7100
    assert applied["target"][0]["target"] == "5ms"

    for change in plan_apply(applied["target"], provider.discover())[0]:
        provider.apply(change)
    assert body["fingerprint"] == fingerprint(normalize(provider.discover()))
    mock_mod._OVERRIDES.clear()
