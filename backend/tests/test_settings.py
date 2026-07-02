"""Tests for settings fingerprinting and the correlation endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain.database import session_scope
from pathbrain.methodology import (
    CURRENT_METHODOLOGY,
    METHODOLOGY_REGISTRY,
    build_definition_from_spec,
    overall_metrics,
)
from pathbrain.models import BenchmarkResult, Run, RunStatus, Score, ScoreResult
from pathbrain.settings_profile import diff_profiles, fingerprint, normalize, summarize
from pathbrain.providers.mock import MockProvider


def _crown_metrics() -> list[str]:
    """The *current* methodology's crown metric set. Heirs (and their tests) always corner
    over the current crown, so we derive it here instead of hardcoding a set that silently
    drifts when the methodology changes."""
    metrics, _required = overall_metrics(
        build_definition_from_spec(METHODOLOGY_REGISTRY[CURRENT_METHODOLOGY])
    )
    return metrics


def test_diff_profiles_reports_direction():
    a = [{"label": "wan", "target": "10ms", "quantum": 1514, "download_bandwidth": "880Mbit", "ecn": False}]
    b = [{"label": "wan", "target": "5ms", "quantum": 2640, "download_bandwidth": "1Gbit", "ecn": True}]
    changes = {c["field"]: c for c in diff_profiles(a, b)}
    # CoDel target lowered 10ms -> 5ms (the kind of win that should seed experiments)
    assert changes["target"]["direction"] == "lower"
    assert changes["target"]["from_value"] == "10ms"
    assert changes["target"]["to_value"] == "5ms"
    assert changes["quantum"]["direction"] == "higher"  # 1514 -> 2640
    assert changes["download_bandwidth"]["direction"] == "higher"  # 880Mbit -> 1Gbit
    assert changes["ecn"]["direction"] == "higher"  # off -> on
    assert "scheduler" not in changes  # unchanged fields are omitted


def test_diff_profiles_identical_is_empty():
    a = [{"label": "wan", "target": "5ms", "quantum": 1514}]
    assert diff_profiles(a, a) == []


def test_fingerprint_stable_and_distinct():
    base = normalize(MockProvider().discover())
    fp1 = fingerprint(base)
    fp2 = fingerprint(list(reversed(base)))  # order-independent
    assert fp1 == fp2
    changed = [dict(p) for p in base]
    changed[0]["quantum"] = 6000
    assert fingerprint(changed) != fp1


def test_summarize_is_human_readable():
    s = summarize(normalize(MockProvider().discover()))
    assert "q" in s and ":" in s


def _seed_run(
    fp: str,
    sops: float,
    when: datetime,
    completion: float | None = None,
    completion_metrics: dict | None = None,
    metric_values: dict | None = None,
    iterations: int = 1,
    speed: float | None = None,
    responsiveness: float | None = None,
    result_metrics: dict | None = None,
    crown_subscores: dict | None = None,
    overall: float | None = None,
) -> None:
    with session_scope() as session:
        run = Run(
            status=RunStatus.COMPLETE,
            created_at=when,
            settings_fingerprint=fp,
            settings=[{"label": "wan", "quantum": 1514}],
            iterations=iterations,
        )
        session.add(run)
        session.flush()
        session.add(
            ScoreResult(
                run_id=run.id, sops=sops, subscores={}, weights_used={},
                metric_values=metric_values if metric_values is not None
                else {"longest_stall": 1500.0, "fcp": 500.0, "lcp": 800.0},
                completion=completion, completion_metric_values=completion_metrics,
            )
        )
        # Optional per-plugin derived metrics (the cache the profiles endpoint reads
        # for its per-metric medians), e.g. {"icmp": {"latency_ms": 12.0}}.
        for plugin, metrics in (result_metrics or {}).items():
            session.add(BenchmarkResult(run_id=run.id, plugin=plugin, success=True, metrics=metrics))
        # Settings now reads the (run × methodology) Score: smoothness is the ranking
        # axis. A legacy run (metric_values={}) gets an *incomparable* Score so it's
        # excluded; everything else is comparable with smoothness = the seeded score.
        comparable = metric_values is None or len(metric_values) > 0
        # A comparable run under speed-smoothness-v4 carries all three headline axes;
        # responsiveness defaults to the seeded smoothness score for fixtures.
        resp_val = responsiveness if responsiveness is not None else sops
        speed_val = speed if speed is not None else sops
        axes = {
            "responsiveness": resp_val,
            "speed": speed_val,
            "smoothness": sops,
        }
        if completion is not None:
            axes["completion"] = completion
        # The methodology persists a first-class Overall into axis_scores; inject it when a
        # test wants to exercise the persisted-Overall path (else compute_profiles falls
        # back to the live feel-trinity corner over the subscores below).
        if overall is not None:
            axes["overall"] = overall
        # The crown (v7) corners over fcp × lcp × total_stall. By default map them from the
        # axis fixture values (fcp←responsiveness, lcp←speed, total_stall←smoothness) so
        # corner-based assertions read naturally; load_event is kept too (still a scored
        # Speed metric) for the tests that target it. Tests override via ``crown_subscores``.
        subs = (
            crown_subscores
            if crown_subscores is not None
            else {
                "fcp": resp_val, "lcp": speed_val, "total_stall": sops, "load_event": speed_val,
            }
        )
        session.add(
            Score(
                run_id=run.id, methodology_version=CURRENT_METHODOLOGY, is_at_measure=True,
                comparability="exact" if comparable else "incomparable",
                axis_scores=axes if comparable else {},
                subscores=subs if comparable else {},
                weights_used={}, metric_values=completion_metrics or {},
            )
        )


def test_profiles_and_impact(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # Older profile "aaa" ~70, then a change to "bbb" ~85. 6 runs of 3 iterations
    # each => 18 total iterations, so both clear the default min_iterations=15
    # confidence threshold (confidence is iteration-based, not run-count-based).
    for i, s in enumerate([70, 72, 68, 71, 69, 70]):
        _seed_run("aaaaaaaaaaaa", s, t0 - timedelta(minutes=120 - i), iterations=3)
    for i, s in enumerate([84, 86, 85, 83, 87, 85]):
        _seed_run("bbbbbbbbbbbb", s, t0 - timedelta(minutes=30 - i), iterations=3)

    body = client.get("/api/settings/profiles").json()
    profiles = body["profiles"]
    fps = {p["fingerprint"] for p in profiles}
    assert {"aaaaaaaaaaaa", "bbbbbbbbbbbb"} <= fps
    assert profiles[0]["fingerprint"] == "bbbbbbbbbbbb"  # higher median first
    assert all(p["confident"] for p in profiles)  # 18 iterations each >= min_iterations
    assert body["min_iterations"] == 15
    # Each profile tracks total iterations (6 runs * 3 iterations -> 18).
    assert all(p["iterations"] == 18 for p in profiles)

    # best_diff compares the best profile to the next-ranked one.
    bd = body["best_diff"]
    assert bd is not None
    assert bd["best"]["fingerprint"] == "bbbbbbbbbbbb"
    assert bd["comparison"]["fingerprint"] == "aaaaaaaaaaaa"
    assert bd["delta_abs"] > 0
    # These two profiles use identical seeded settings, so no field changes.
    assert bd["changes"] == []

    impact = client.get("/api/settings/impact").json()
    assert impact["changed"] is True
    assert impact["enough_data"] is True
    assert impact["before"]["fingerprint"] == "aaaaaaaaaaaa"
    assert impact["after"]["fingerprint"] == "bbbbbbbbbbbb"
    assert impact["delta_abs"] > 0
    assert impact["significant"] is True  # ~70 -> ~85 over 5%, both confident


def test_confidence_is_iteration_based(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # 10 runs of 1 iteration => 10 total iterations (< min 15): NOT confident,
    # even though it's many runs. Run-count would have called this confident.
    for i in range(10):
        _seed_run("itersmall0x", 80, t0 - timedelta(minutes=200 - i), iterations=1)
    # 2 runs of 8 iterations => 16 total iterations (>= 15): confident, on far
    # fewer runs.
    for i in range(2):
        _seed_run("iterbig000x", 80, t0 - timedelta(minutes=100 - i), iterations=8)

    body = client.get("/api/settings/profiles").json()
    by_fp = {p["fingerprint"]: p for p in body["profiles"]}
    assert by_fp["itersmall0x"]["iterations"] == 10
    assert by_fp["itersmall0x"]["confident"] is False
    assert by_fp["iterbig000x"]["iterations"] == 16
    assert by_fp["iterbig000x"]["confident"] is True


def test_heirs_are_limited_data_profiles_that_can_beat_the_crown(client, monkeypatch):
    # NB: the test DB is shared across the module, so assert only on our own fingerprints
    # (not the global crown / heir totals). The heir is given a near-perfect ceiling so it
    # is guaranteed to out-rank any incidental accumulated contender and surface in the top.
    # This test is about ceiling ranking, not reachability — make every profile reachable.
    import pathbrain.api.routes_settings as rs

    monkeypatch.setattr(rs, "environment_signature", lambda norm: "env")
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    crown = _crown_metrics()  # follow the methodology's crown set, don't hardcode it
    # A confident, fresh profile (kept low so it can't disturb the module's global crown) —
    # must NEVER appear as an heir, because the crown only lists profiles it can't yet trust.
    for i in range(3):
        _seed_run("hcrown0000x", 60, t0 - timedelta(minutes=120 - i),
                  crown_subscores={m: 60 for m in crown}, iterations=6)
    # Heir: limited data (3 iters < 15) but near-perfect subscores — its optimistic ceiling
    # (median + margin, capped at 100) corners to ~100, clearing any plausible crown.
    _seed_run("heir000000x", 97, t0 - timedelta(minutes=50),
              crown_subscores={m: 97 for m in crown}, iterations=3)
    # Not an heir: limited data AND a ceiling that can't reach the crown even optimistically.
    _seed_run("nohope0000x", 40, t0 - timedelta(minutes=40),
              crown_subscores={m: 40 for m in crown}, iterations=3)

    body = client.get("/api/settings/profiles").json()
    heirs = body["heirs"]
    fps = [h["fingerprint"] for h in heirs["items"]]
    assert "heir000000x" in fps                 # promising limited-data profile surfaces
    assert "nohope0000x" not in fps             # can't beat the crown even optimistically
    assert "hcrown0000x" not in fps             # confident + fresh → never an heir
    heir = next(h for h in heirs["items"] if h["fingerprint"] == "heir000000x")
    assert heir["margin"] > 0                    # ceiling above the crown's Overall
    assert heir["reason"] == "limited-data"
    assert heir["iterations_to_min"] == 12       # 15 - 3 still to go
    assert heirs["total"] >= 1


def test_heirs_exclude_unreachable_profiles(monkeypatch):
    # _compute_heirs must drop profiles the race could never apply (their non-writable env
    # differs from the live config), so the card matches what the race would actually run.
    import pathbrain.api.routes_settings as rs
    from pathbrain.providers.base import FqCodelConfig

    class _FakeProvider:
        def discover(self):
            return [FqCodelConfig(scheduler="fq_codel", queues=1)]

    monkeypatch.setattr(rs, "get_provider", lambda: _FakeProvider())

    def _spread(med, p75):
        return {"median": med, "p25": med, "p75": p75, "min": med, "max": p75, "n": 3}

    crown = _crown_metrics()  # follow the methodology, not a hardcoded crown set
    high = {m: _spread(97, 97) for m in crown}
    result = {
        "best_fingerprint": "crown",
        "overall_metrics": crown,
        "overall_required": crown,
        "min_iterations": 15,
        "profiles": [
            {"fingerprint": "crown", "label": "crown", "confident": True, "overall": 80.0,
             "last_seen": None, "iterations": 30, "crown_spreads": {},
             "settings": [{"scheduler": "fq_codel", "queues": 1}]},
            {"fingerprint": "reach", "label": "R", "confident": False, "overall": None,
             "last_seen": None, "iterations": 3, "crown_spreads": high,
             "settings": [{"scheduler": "fq_codel", "queues": 1}]},
            {"fingerprint": "unreach", "label": "U", "confident": False, "overall": None,
             "last_seen": None, "iterations": 3, "crown_spreads": high,
             "settings": [{"scheduler": "fq_pie", "queues": 1}]},  # different scheduler
        ],
    }
    with session_scope() as s:
        heirs = rs._compute_heirs(result, s)
    fps = {h["fingerprint"] for h in heirs["items"]}
    assert "reach" in fps       # same environment as live → reachable
    assert "unreach" not in fps  # different scheduler → unreachable, excluded


def test_metric_thresholds_expose_effective_v6_anchors(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    _seed_run("thr000000x", 80, t0 - timedelta(minutes=30), iterations=6)
    body = client.get("/api/settings/profiles").json()
    thresholds = body["metric_thresholds"]
    # v6 re-anchors fcp's "best" to 150ms (vs the catalog default of 1800) — the saturation
    # check must use the methodology's effective threshold, not the registry default.
    assert thresholds["fcp"]["best"] == 150.0
    assert thresholds["load_event"]["best"] == 800.0
    assert thresholds["fcp"]["higher_is_better"] is False


def test_saturation_flags_too_lenient_threshold(client):
    # load_event "best" is 800ms; seed profiles whose page-load mostly clears it (so the
    # metric pins at ~100 and can't rank them). >50% saturated must flag the metric and
    # suggest re-anchoring 'best' down to the fastest profile measured.
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    for fp, ms in [("sat0000001x", 500.0), ("sat0000002x", 600.0),
                   ("sat0000003x", 650.0), ("sat0000004x", 700.0)]:
        _seed_run(fp, 80, t0 - timedelta(minutes=60), iterations=6,
                  result_metrics={"browser": {"load_event_ms": ms}})
    # One profile genuinely slower than 'best' (does not saturate).
    _seed_run("sat0000005x", 80, t0 - timedelta(minutes=55), iterations=6,
              result_metrics={"browser": {"load_event_ms": 1200.0}})

    body = client.get("/api/settings/profiles").json()
    le = next(s for s in body["saturation"] if s["key"] == "load_event")
    assert le["flagged"] is True
    assert le["saturated_fraction"] == 0.8        # 4 of 5 profiles already past 'best'
    assert le["best"] == 800.0
    assert le["suggested_best"] == 500.0          # re-anchor to the fastest measured
    # total_stall has best=0 (a physical floor) — never flagged/re-anchored even if "saturated".
    assert all(s["key"] != "total_stall" for s in body["saturation"])


def test_reanchor_forks_a_new_version_and_makes_it_current(client, monkeypatch):
    # Don't run the heavy background re-grade in the test; just confirm the publish.
    import pathbrain.api.routes_methodology as rm

    monkeypatch.setattr(rm.jobs, "start", lambda *a, **k: "job-test")

    r = client.post("/api/methodologies/reanchor", json={"metric_key": "load_event", "best": 500})
    assert r.status_code == 202
    out = r.json()
    assert out["version"] == "speed-smoothness-v7+load_event-best500"
    assert out["job_id"] == "job-test"

    # The fork is now current: only load_event's 'best' changed; fcp and the Overall crown
    # spec carry over from v6 untouched (append-only — a new version, not an edit).
    cur = client.get("/api/methodologies/current").json()
    assert cur["version"] == "speed-smoothness-v7+load_event-best500"
    metrics = {m["key"]: m for m in cur["definition"]["metrics"]}
    assert metrics["load_event"]["best"] == 500.0
    assert metrics["fcp"]["best"] == 150.0  # untouched
    assert cur["definition"]["overall"]["metrics"] == ["fcp", "lcp", "total_stall"]

    # Guard: 'best' can't cross to the wrong side of 'worst' (would invert the curve).
    bad = client.post("/api/methodologies/reanchor", json={"metric_key": "load_event", "best": 99999})
    assert bad.status_code == 400

    # Restore the stock current methodology so the rest of the shared-DB suite sees it.
    from pathbrain.config_store import get_config, save_config
    from pathbrain.methodology import CURRENT_METHODOLOGY, ensure_current_methodology

    with session_scope() as s:
        save_config(s, {"methodology_version": CURRENT_METHODOLOGY})
        ensure_current_methodology(s, get_config(s))


def test_best_is_closest_to_top_right_corner(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # Profile A: least dead-air (total_stall subscore 90) but slow first response *and*
    # page-load (fcp/load_event 70). Profile B: a bit more stall (80) but fast to start
    # *and* finish (fcp/load_event 95) — so it sits closer to the perfect (100,100,100)
    # corner. "Best" must follow the feel-trinity corner, not one metric alone.
    for i in range(3):
        _seed_run("smoothA0000x", 90, t0 - timedelta(minutes=120 - i),
                  speed=70, responsiveness=70, iterations=6)
    for i in range(3):
        _seed_run("cornerB0000x", 80, t0 - timedelta(minutes=60 - i),
                  speed=95, responsiveness=95, iterations=6)

    body = client.get("/api/settings/profiles").json()
    by_fp = {p["fingerprint"]: p for p in body["profiles"]}
    # Corner profile B wins despite A having the higher smoothness.
    assert body["best_fingerprint"] == "cornerB0000x"
    assert by_fp["cornerB0000x"]["overall"] > by_fp["smoothA0000x"]["overall"]
    assert by_fp["smoothA0000x"]["median"] > by_fp["cornerB0000x"]["median"]  # A still smoother
    # Each profile exposes its per-axis scores for the dynamic chart.
    assert by_fp["cornerB0000x"]["scores"]["speed"] == 95
    assert by_fp["cornerB0000x"]["scores"]["smoothness"] == 80
    # The response advertises selectable numeric fields for the UI.
    assert any(f["key"] == "overall" for f in body["fields"])


def test_crown_rewards_balance_over_specialism(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # SPEC: aces first-response + perceived-time but is sluggish to interact (inp 40).
    # Its *mean* feel subscore (100+100+40)/3 ≈ 80 beats BAL's 76 — so a weighted average
    # would rank SPEC higher. The corner is an *intersection*: SPEC's one weak metric
    # drags it to ~65 while balanced BAL sits at ~76, so the feel Overall picks BAL.
    # Proves the crown corner ≠ a mean. (Asserted per-profile; the module DB accumulates
    # other fixtures' profiles, so we don't assert the global crown here.)
    for i in range(3):
        _seed_run("specialist0x", 80, t0 - timedelta(minutes=120 - i), iterations=6,
                  crown_subscores={"fcp": 100, "total_stall": 100, "lcp": 40})
    for i in range(3):
        _seed_run("balanced000x", 80, t0 - timedelta(minutes=60 - i), iterations=6,
                  crown_subscores={"fcp": 76, "total_stall": 76, "lcp": 76})

    by_fp = {p["fingerprint"]: p for p in client.get("/api/settings/profiles").json()["profiles"]}
    spec = by_fp["specialist0x"]["crown_scores"]
    assert sum(spec.values()) / 3 > 76  # SPEC's mean really is higher (not trivial)…
    assert by_fp["balanced000x"]["overall"] > by_fp["specialist0x"]["overall"]  # …yet BAL's corner wins


def test_crown_corners_over_the_trinity(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # All three crown metrics present and equal → the corner (√k-normalized) == that value.
    for i in range(3):
        _seed_run("trinity000x", 80, t0 - timedelta(minutes=60 - i), iterations=6,
                  crown_subscores={"fcp": 80, "lcp": 80, "total_stall": 80, "load_event": 80})

    by_fp = {p["fingerprint"]: p for p in client.get("/api/settings/profiles").json()["profiles"]}
    prof = by_fp["trinity000x"]
    assert prof["overall"] == 80.0  # corner over {80, 80, 80} == 80
    assert set(prof["crown_scores"]) >= {"fcp", "lcp", "total_stall"}
    assert prof["confident"]


def test_crown_skips_run_missing_required_metric(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # A run missing required crown metrics (only fcp; no total_stall/load_event) can't be
    # cornered, so it contributes no Overall and can't be crowned — but still aggregates.
    for i in range(3):
        _seed_run("nopt000000x", 80, t0 - timedelta(minutes=60 - i), iterations=6,
                  crown_subscores={"fcp": 90})

    body = client.get("/api/settings/profiles").json()
    by_fp = {p["fingerprint"]: p for p in body["profiles"]}
    prof = by_fp["nopt000000x"]
    assert prof["overall"] is None  # no feel corner → no Overall
    assert body["best_fingerprint"] != "nopt000000x"  # no Overall → can't be crowned


def test_overall_uses_persisted_value_over_live_corner(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # Persist a first-class Overall of 42 while the subscores would *live*-corner to ~80.
    # compute_profiles must report the persisted value (grading and crowning share it),
    # not recompute from subscores.
    for i in range(3):
        _seed_run("persisted00x", 80, t0 - timedelta(minutes=60 - i), iterations=6,
                  overall=42.0, crown_subscores={"fcp": 80, "lcp": 80, "total_stall": 80, "load_event": 80})

    by_fp = {p["fingerprint"]: p for p in client.get("/api/settings/profiles").json()["profiles"]}
    assert by_fp["persisted00x"]["overall"] == 42.0


def test_custom_crown_corners_selected_betterments(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # X aces first response (fcp 100) but slow to finish (load_event 50); Y is the reverse.
    for i in range(3):
        _seed_run("xcustom0000x", 80, t0 - timedelta(minutes=120 - i), iterations=6,
                  crown_subscores={"fcp": 100, "lcp": 50, "total_stall": 80, "load_event": 50})
    for i in range(3):
        _seed_run("ycustom0000x", 80, t0 - timedelta(minutes=60 - i), iterations=6,
                  crown_subscores={"fcp": 50, "lcp": 100, "total_stall": 80, "load_event": 100})

    # Crown on first-response only → X's corner beats Y's.
    body = client.get("/api/settings/profiles?crown_metrics=fcp").json()
    by = {p["fingerprint"]: p for p in body["profiles"]}
    assert body["crown_metrics"] == ["fcp"]
    assert by["xcustom0000x"]["custom_overall"] == 100.0
    assert by["xcustom0000x"]["custom_overall"] > by["ycustom0000x"]["custom_overall"]

    # Crown on page-load time only → Y wins the same comparison.
    by2 = {p["fingerprint"]: p for p in
           client.get("/api/settings/profiles?crown_metrics=load_event").json()["profiles"]}
    assert by2["ycustom0000x"]["custom_overall"] > by2["xcustom0000x"]["custom_overall"]

    # No selection → no custom corner at all (canonical Overall untouched).
    plain = client.get("/api/settings/profiles").json()
    assert plain["crown_metrics"] is None
    assert all(p["custom_overall"] is None for p in plain["profiles"])


def test_profiles_expose_per_metric_medians(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # A run whose plugin caches carry a display-only metric (latency) + a scored one.
    for i in range(3):
        _seed_run(
            "metricsfp00x",
            80,
            t0 - timedelta(minutes=30 - i),
            iterations=6,
            result_metrics={"icmp": {"latency_ms": 12.0}, "http": {"ttfb_ms": 200.0}},
        )
    body = client.get("/api/settings/profiles").json()
    prof = next(p for p in body["profiles"] if p["fingerprint"] == "metricsfp00x")
    # "Any numeric value we collect" — incl. the display-only latency — is aggregated.
    assert prof["metrics"]["latency"] == 12.0
    assert prof["metrics"]["ttfb"] == 200.0


def test_impact_not_significant_without_enough_runs(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    _seed_run("cccccccccccc", 60, t0 - timedelta(minutes=20))
    _seed_run("dddddddddddd", 90, t0 - timedelta(minutes=5))  # only 1 run each
    impact = client.get("/api/settings/impact").json()
    # A change is detected, but it must NOT be flagged significant on 1+1 runs.
    assert impact["changed"] is True
    assert impact["enough_data"] is False
    assert impact["significant"] is False


def test_backfill_stamps_null_runs(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # A run with no captured settings (NULL fingerprint).
    with session_scope() as session:
        run = Run(status=RunStatus.COMPLETE, created_at=t0)
        session.add(run)
        session.flush()
        session.add(ScoreResult(run_id=run.id, sops=77, subscores={}, weights_used={}, metric_values={}))

    resp = client.post("/api/settings/backfill")
    assert resp.status_code == 200
    assert resp.json()["updated"] >= 1
    assert resp.json()["fingerprint"]  # mock provider yields a stable fingerprint


# Kept last: seeds a distinct profile and only queries by its own fingerprint, so
# it can't perturb the order-sensitive profile/impact assertions above.
def test_profiles_expose_completion_axis(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(6):
        _seed_run(
            "completionfp1",
            80 + i,
            t0 - timedelta(minutes=60 - i),
            completion=70 + i,
            completion_metrics={"dns": 12.0, "tcp": 30.0, "tls": 40.0},
            iterations=3,
        )
    body = client.get("/api/settings/profiles").json()
    prof = next(p for p in body["profiles"] if p["fingerprint"] == "completionfp1")
    # Completion aggregates as its own axis, gated like SOPS (on iterations).
    assert prof["completion"] is not None
    assert prof["completion"]["count"] == 6
    assert prof["completion"]["confident"] is True  # 18 iterations >= min_iterations (15)
    # Raw infra metric medians are exposed per profile.
    assert prof["completion_metrics"]["tls"]["median"] == 40.0
    assert prof["completion_metrics"]["dns"]["count"] == 6


# Kept last: only queries by its own fingerprints, so it can't perturb the
# order-sensitive profile/impact assertions above.
def test_complete_only_filters_legacy_runs(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # 6 legacy runs (no paint metrics) that read artificially high...
    for i in range(6):
        _seed_run("legacyonly9x", 95 + (i % 2), t0 - timedelta(minutes=90 - i), metric_values={})
    # ...and 6 latest-rubric runs with paint metrics.
    for i in range(6):
        _seed_run(
            "latestdata0x",
            70 + i,
            t0 - timedelta(minutes=40 - i),
            metric_values={"longest_stall": 1500.0, "fcp": 480.0, "lcp": 600.0, "ttfb": 200.0},
        )

    default = client.get("/api/settings/profiles").json()  # complete_only defaults true
    fps = {p["fingerprint"] for p in default["profiles"]}
    assert default["complete_only"] is True
    assert "latestdata0x" in fps
    # Incomparable runs have no axis score, so a legacy-only profile can't be ranked
    # — it's absent whether or not complete_only filters it.
    assert "legacyonly9x" not in fps

    allruns = client.get("/api/settings/profiles?complete_only=false").json()
    fps_all = {p["fingerprint"] for p in allruns["profiles"]}
    assert "latestdata0x" in fps_all
    assert "legacyonly9x" not in fps_all


# ── one-click "Apply this profile" (firewall write) ──────────────────────────


def _apply_target_profile():
    """A normalized profile that differs from the mock firewall's current state:
    the download pipe wants quantum 4000 / target 3ms; the upload pipe (no uuid)
    asks for a change too, to exercise the unwritable-pipe warning."""
    return [
        {
            "download_bandwidth": "900Mbit", "upload_bandwidth": "40Mbit",
            "quantum": 4000, "limit": 10240, "target": "3ms", "interval": "100ms",
            "ecn": True, "flows": 1024, "queues": 1, "scheduler": "fq_codel",
            "label": "wan-download",
        },
        {
            "download_bandwidth": "40Mbit", "upload_bandwidth": "40Mbit",
            "quantum": 999, "limit": 10240, "target": "5ms", "interval": "100ms",
            "ecn": True, "flows": 1024, "queues": 1, "scheduler": "fq_codel",
            "label": "wan-upload",
        },
    ]


def _seed_profile_run(fp: str, settings: list[dict]) -> None:
    with session_scope() as session:
        session.add(Run(status=RunStatus.COMPLETE, settings_fingerprint=fp, settings=settings))


def test_apply_profile_preview_lists_exact_changes(client):
    from pathbrain.providers.mock import _OVERRIDES

    _OVERRIDES.clear()  # back to mock defaults (quantum 1514, target 5ms)
    _seed_profile_run("applyfp01", _apply_target_profile())

    resp = client.post("/api/settings/apply-profile", json={"fingerprint": "applyfp01", "preview": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["preview"] is True and body["already_applied"] is False
    by_field = {(c["label"], c["param"]): c for c in body["changes"]}
    # The writable download pipe shows from→to for the two differing fields...
    assert by_field[("wan-download", "quantum")]["from"] == 1514
    assert by_field[("wan-download", "quantum")]["to"] == 4000
    assert by_field[("wan-download", "target")]["to"] == "3ms"
    # ...and nothing was written (preview only).
    assert _OVERRIDES == {}
    # The upload pipe has no uuid in the mock → flagged, not applied.
    assert any("wan-upload" in w for w in body["warnings"])
    assert not any(c["label"] == "wan-upload" for c in body["changes"])


def test_apply_profile_writes_to_firewall(client):
    from pathbrain.providers.mock import _OVERRIDES

    _OVERRIDES.clear()
    _seed_profile_run("applyfp02", _apply_target_profile())

    resp = client.post(
        "/api/settings/apply-profile",
        json={"fingerprint": "applyfp02", "run_benchmark": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    # The write went through the provider apply path.
    assert _OVERRIDES.get("quantum") == 4000
    assert _OVERRIDES.get("target") == "3ms"
    applied_fields = {a["field_label"] for a in body["applied"]}
    assert {"Quantum", "CoDel target"} <= applied_fields

    # Re-applying the same profile is now a no-op (firewall already matches).
    again = client.post(
        "/api/settings/apply-profile",
        json={"fingerprint": "applyfp02", "run_benchmark": False},
    ).json()
    assert again["already_applied"] is True and again["applied"] == []
    _OVERRIDES.clear()


def test_apply_profile_kicks_benchmark_when_requested(client, monkeypatch):
    from pathbrain.providers.mock import _OVERRIDES
    import pathbrain.api.routes_run as routes_run

    _OVERRIDES.clear()
    _seed_profile_run("applyfp03", _apply_target_profile())
    kicked: list[int] = []
    # Stub the background execute so the test doesn't run a real benchmark.
    monkeypatch.setattr(routes_run, "_locked_execute", lambda rid: kicked.append(rid))

    resp = client.post(
        "/api/settings/apply-profile",
        json={"fingerprint": "applyfp03", "run_benchmark": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    # A single-iteration benchmark run was created and kicked in the background.
    assert body["run_id"] is not None
    assert kicked == [body["run_id"]]
    _OVERRIDES.clear()


def test_apply_profile_unknown_fingerprint_404(client):
    resp = client.post("/api/settings/apply-profile", json={"fingerprint": "does-not-exist-xyz"})
    assert resp.status_code == 404


def test_apply_profile_requires_fingerprint(client):
    resp = client.post("/api/settings/apply-profile", json={})
    assert resp.status_code == 400


def test_crown_is_highest_overall_among_confident(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # "lucky": typically ~82 but occasionally spikes to 99 — high variance, few runs.
    for s in [78, 99, 82, 99, 80]:
        _seed_run("luckythin0x", s, t0 - timedelta(minutes=200), iterations=3)
    # "proven": many consistent iterations at a clearly higher typical Overall (92), the
    # highest median among all confident profiles seeded so far.
    for i in range(10):
        _seed_run("provenwide0x", 92, t0 - timedelta(minutes=150 - i), iterations=3)

    body = client.get("/api/settings/profiles").json()
    by_fp = {p["fingerprint"]: p for p in body["profiles"]}
    assert by_fp["luckythin0x"]["confident"] and by_fp["provenwide0x"]["confident"]
    # The crown is simply the highest median Overall among confident profiles. The lucky
    # profile's occasional 99 spikes don't help (median is robust) and variance is no longer
    # rewarded, so the steadily-higher proven profile wins — and a thin, spiky profile can
    # never dethrone a proven one on its upper tail (the bug this replaced).
    assert by_fp["provenwide0x"]["overall"] > by_fp["luckythin0x"]["overall"]
    assert body["best_fingerprint"] == "provenwide0x"


def test_profiles_expose_time_adjusted_overall(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(8):
        _seed_run("reloverall0x", 88, t0 - timedelta(minutes=80 - i), iterations=3)

    by_fp = {p["fingerprint"]: p for p in client.get("/api/settings/profiles").json()["profiles"]}
    p = by_fp["reloverall0x"]
    # The time-adjusted Overall ("vs typical") is exposed for the table's "vs typical"
    # column — informational only; it no longer feeds the crown.
    assert "relative_overall" in p
    if p["relative_overall"] is not None:
        assert set(p["relative_overall"]) >= {"delta_median", "p25", "p75", "count"}


# ── Tie-aware crown ────────────────────────────────────────────────────────────────
# The crown is no longer a bare argmax of the median Overall. A challenger only *clearly*
# beats the incumbent when its median pulls ahead by more than run-to-run noise (an
# absolute floor AND a share of the pooled Overall IQR). Otherwise the two are co-leaders
# (a statistical tie), broken by hysteresis (keep the active profile) then steadiness
# (tightest IQR). These are exercised as pure-function unit tests (no DB) plus one
# end-to-end wiring test, so the shared module DB can't perturb the assertions.

from pathbrain.api.routes_settings import _clearly_better, _select_crown  # noqa: E402


def _prof(fp: str, overall: float, iqr: float = 0.0, iterations: int = 30) -> dict:
    """A minimal confident-profile dict for the pure crown selector: an Overall median
    with a symmetric IQR band of the given width."""
    return {
        "fingerprint": fp,
        "overall": overall,
        "overall_p25": overall - iqr / 2,
        "overall_p75": overall + iqr / 2,
        "iterations": iterations,
        "confident": True,
    }


def test_clearly_better_requires_more_than_noise():
    tight_hi = _prof("hi", 90.0, iqr=0.0)
    tight_lo = _prof("lo", 70.0, iqr=0.0)
    # A 20-point lead with no spread is a real win.
    assert _clearly_better(tight_hi, tight_lo, 0.5, 0.5) is True
    # A 1-point lead over a wide (±5 → width 10) band is inside the noise: 1 < 0.5*10.
    wide = _prof("wide", 89.0, iqr=10.0)
    assert _clearly_better(tight_hi, wide, 0.5, 0.5) is False
    # A hair of median with both bands ~0 is still not "clear" — the absolute floor guards
    # against crowning on rounding.
    near = _prof("near", 89.7, iqr=0.0)
    assert _clearly_better(tight_hi, near, 0.5, 0.5) is False  # gap 0.3 < 0.5 floor


def test_crown_breaks_a_statistical_tie_by_steadiness():
    # jittery has the marginally higher median (96 vs 95) but a wide band; steady is a hair
    # lower but rock-steady. The gap (1) is inside the pooled noise, so they're co-leaders —
    # and the crown goes to the steadier profile, not the higher-median one.
    steady = _prof("steady", 95.0, iqr=0.0)
    jittery = _prof("jittery", 96.0, iqr=12.0)
    best, co = _select_crown([jittery, steady], current_fingerprint=None,
                             min_margin=0.5, iqr_fraction=0.5)
    assert best["fingerprint"] == "steady"
    assert set(co) == {"steady", "jittery"}  # both are co-leaders


def test_crown_hysteresis_keeps_the_active_profile_among_co_leaders():
    # Same tie, but the jittery profile is the one currently deployed. Hysteresis keeps the
    # crown on it rather than churning the firewall for a noise-level difference.
    steady = _prof("steady", 95.0, iqr=0.0)
    jittery = _prof("jittery", 96.0, iqr=12.0)
    best, co = _select_crown([jittery, steady], current_fingerprint="jittery",
                             min_margin=0.5, iqr_fraction=0.5)
    assert best["fingerprint"] == "jittery"
    assert set(co) == {"steady", "jittery"}


def test_crown_clear_winner_is_not_a_tie():
    # A decisive, well-separated lead crowns the higher profile with no co-leaders, even if
    # the loser happens to be the active one (hysteresis never overrides a *clear* winner).
    hi = _prof("hi", 92.0, iqr=1.0)
    lo = _prof("lo", 74.0, iqr=1.0)
    best, co = _select_crown([lo, hi], current_fingerprint="lo",
                             min_margin=0.5, iqr_fraction=0.5)
    assert best["fingerprint"] == "hi"
    assert co == ["hi"]  # only the crown; "lo" is clearly beaten, not a co-leader


def test_profiles_endpoint_exposes_co_leaders(client, monkeypatch):
    # End-to-end: two confident profiles a hair apart in median but overlapping bands should
    # come back as co-leaders. Seed them as the module's top profiles (Overall ~96) so they
    # are the global crown contenders regardless of other fixtures' lower-scoring profiles.
    import pathbrain.api.routes_settings as rs

    monkeypatch.setattr(rs, "_current_fingerprint", lambda: None)
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # steady: six runs all at Overall 96 → tight band.
    for i in range(6):
        _seed_run("tiesteady0x", 96, t0 - timedelta(minutes=200 - i), iterations=3, overall=96)
    # jittery: median 96 too, but a wide run-to-run spread (90..100).
    for i, o in enumerate([90, 100, 90, 100, 96, 96]):
        _seed_run("tiejitter0x", o, t0 - timedelta(minutes=150 - i), iterations=3, overall=o)

    body = client.get("/api/settings/profiles").json()
    co = set(body["co_leaders"])
    assert {"tiesteady0x", "tiejitter0x"} <= co | {body["best_fingerprint"]}
    # The crown is one of our two tied profiles, and the *other* is listed as a co-leader.
    assert body["best_fingerprint"] in {"tiesteady0x", "tiejitter0x"}
    # The steadier profile wins the tie (no active profile to make it sticky).
    assert body["best_fingerprint"] == "tiesteady0x"
    assert "tiejitter0x" in co
