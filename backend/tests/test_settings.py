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
    crown_raw: tuple | None = None,
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
        # A comparable run persists the first-class Overall (methodology v5+); default it to the
        # seeded smoothness score for fixtures unless a test sets it explicitly. Mirrors
        # production, where every comparable run carries axis_scores["overall"].
        if overall is not None:
            axes["overall"] = overall
        elif comparable:
            axes["overall"] = sops
        # The crown (v7) corners over fcp × lcp × total_stall. Subscores drive the *axis*
        # scores + the custom-crown lens; the canonical Overall now corners the *raw* browser
        # measurements. Tests override subscores via ``crown_subscores``.
        subs = (
            crown_subscores
            if crown_subscores is not None
            else {
                "fcp": resp_val, "lcp": speed_val, "total_stall": sops, "load_event": speed_val,
            }
        )
        # Raw crown metrics (browser plugin) — what the field-normalized Overall corners over.
        # ``crown_raw=(fcp_ms, lcp_ms, total_stall_ms)`` sets them explicitly; otherwise derive
        # a monotonic default from the crown subscores (higher subscore → faster raw), so a
        # fixture that only sets subscores still yields a matching raw-based crown ordering.
        result_metrics = {k: dict(v) for k, v in (result_metrics or {}).items()}
        browser = dict(result_metrics.get("browser") or {})
        if crown_raw is not None:
            # Explicit raw; a None entry means "this crown metric wasn't captured" (so the
            # profile has no Overall — for the missing-required-metric case).
            for key, val in zip(("fcp_ms", "lcp_ms", "total_stall_ms"), crown_raw):
                if val is not None:
                    browser.setdefault(key, val)
        else:
            browser.setdefault("fcp_ms", (100.0 - float(subs.get("fcp", sops))) * 10.0)
            browser.setdefault("lcp_ms", (100.0 - float(subs.get("lcp", sops))) * 10.0)
            browser.setdefault("total_stall_ms", (100.0 - float(subs.get("total_stall", sops))) * 10.0)
        result_metrics["browser"] = browser
        # Per-plugin derived metrics (the cache the profiles endpoint reads for its per-metric
        # medians), e.g. {"icmp": {"latency_ms": 12.0}} — plus the browser crown raw above.
        for plugin, metrics in result_metrics.items():
            session.add(BenchmarkResult(run_id=run.id, plugin=plugin, success=True, metrics=metrics))
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


def test_best_diff_is_computed_on_overall():
    # The best-vs-next diff must measure the gap on the Overall (the crown we rank on) and the
    # time-adjusted Overall — NOT the legacy smoothness median / relative_sops.
    from pathbrain.api.routes_settings import _best_diff

    profiles = [
        {"fingerprint": "win", "label": "win", "overall": 90.0,
         "relative_overall": {"delta_median": 5.0}, "completion": None, "confident": True,
         "settings": [{"label": "wan", "quantum": 3000}]},
        {"fingerprint": "runnerup", "label": "runnerup", "overall": 60.0,
         "relative_overall": {"delta_median": 1.0}, "completion": None, "confident": True,
         "settings": [{"label": "wan", "quantum": 1514}]},
    ]
    bd = _best_diff(profiles, "win")
    assert bd["best"]["overall"] == 90.0 and bd["comparison"]["overall"] == 60.0
    assert bd["delta_abs"] == 30.0                 # Overall gap (90 - 60)
    assert bd["best"]["relative_overall"] == 5.0
    assert bd["relative_delta"] == 4.0             # time-adjusted Overall gap (5 - 1)
    # A profile with no crown Overall yields a null delta, not a crash.
    bd2 = _best_diff([dict(profiles[0]), {**profiles[1], "overall": None}], "win")
    assert bd2["delta_abs"] is None and bd2["delta_pct"] is None


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


def test_overall_follows_the_crown_metrics_not_the_smoothness_axis(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # Profile A has the higher *smoothness axis* score but B is faster on every *crown metric*
    # (fcp/lcp/total_stall). The Overall follows the crown metrics, so B ranks above A — even
    # though A "looks" smoother on the axis column.
    for i in range(3):
        _seed_run("smoothA0000x", 90, t0 - timedelta(minutes=120 - i),
                  speed=70, responsiveness=70, iterations=6, crown_raw=(300.0, 300.0, 200.0))
    for i in range(3):
        _seed_run("cornerB0000x", 80, t0 - timedelta(minutes=60 - i),
                  speed=95, responsiveness=95, iterations=6, crown_raw=(150.0, 150.0, 100.0))

    body = client.get("/api/settings/profiles").json()
    by_fp = {p["fingerprint"]: p for p in body["profiles"]}
    # B faster on all three crown metrics → higher Overall (dominance holds under percentiles).
    assert by_fp["cornerB0000x"]["overall"] > by_fp["smoothA0000x"]["overall"]
    assert by_fp["smoothA0000x"]["median"] > by_fp["cornerB0000x"]["median"]  # A still smoother (axis)
    # Each profile exposes its per-axis scores for the dynamic chart.
    assert by_fp["cornerB0000x"]["scores"]["speed"] == 95
    assert by_fp["cornerB0000x"]["scores"]["smoothness"] == 80
    # The response advertises selectable numeric fields for the UI.
    assert any(f["key"] == "overall" for f in body["fields"])


def test_crown_rewards_balance_over_specialism():
    # The Overall corner is an *intersection*: a specialist that aces two crown metrics but is
    # dead-last on the third loses to an all-round profile, because the weak axis drags the
    # corner down more than the two strong axes lift it. Controlled 3-profile field so the
    # percentiles are deterministic. (Unit test — the corner shape, independent of the DB.)
    metrics = ["fcp", "lcp", "total_stall"]
    higher = {m: False for m in metrics}
    # SPEC is fastest on fcp/lcp but *slowest* on total_stall; BAL is consistently 2nd on all.
    profiles = [
        {"metrics": {"fcp": 10, "lcp": 10, "total_stall": 900}},   # SPEC: aces two, worst on one
        {"metrics": {"fcp": 20, "lcp": 20, "total_stall": 100}},   # BAL: 2nd on everything
        {"metrics": {"fcp": 30, "lcp": 30, "total_stall": 200}},
        {"metrics": {"fcp": 40, "lcp": 40, "total_stall": 300}},
        {"metrics": {"fcp": 50, "lcp": 50, "total_stall": 400}},
    ]
    field = _crown_field_values(profiles, metrics)
    spreads = {m: {"n": 1} for m in metrics}
    spec = _normalized_crown(profiles[0]["metrics"], spreads, field, higher, metrics, metrics)
    bal = _normalized_crown(profiles[1]["metrics"], spreads, field, higher, metrics, metrics)
    # SPEC beats BAL on fcp and lcp (it's fastest)…
    assert spec["norm"]["fcp"] > bal["norm"]["fcp"]
    assert spec["norm"]["lcp"] > bal["norm"]["lcp"]
    # …but is far worse on total_stall…
    assert spec["norm"]["total_stall"] < bal["norm"]["total_stall"]
    # …so the balanced profile wins the corner — one weak axis can't be averaged away.
    assert bal["overall"] > spec["overall"]


def test_crown_corners_over_the_raw_trinity(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # A profile with all three crown metrics captured gets a field-normalized Overall over the
    # *raw* measurements, with a per-metric normalized value for each crown metric.
    for i in range(3):
        _seed_run("trinity000x", 80, t0 - timedelta(minutes=60 - i), iterations=6,
                  crown_raw=(200.0, 250.0, 300.0))

    by_fp = {p["fingerprint"]: p for p in client.get("/api/settings/profiles").json()["profiles"]}
    prof = by_fp["trinity000x"]
    assert prof["overall"] is not None and 0.0 <= prof["overall"] <= 100.0
    assert set(prof["crown_norm"]) >= {"fcp", "lcp", "total_stall"}  # normalized raw, not grade
    assert prof["confident"]


def test_crown_skips_run_missing_required_metric(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    # A run missing a required *raw* crown metric (only fcp captured; no lcp/total_stall) can't
    # be cornered, so it contributes no Overall and can't be crowned — but still aggregates.
    for i in range(3):
        _seed_run("nopt000000x", 80, t0 - timedelta(minutes=60 - i), iterations=6,
                  crown_raw=(120.0, None, None))

    body = client.get("/api/settings/profiles").json()
    by_fp = {p["fingerprint"]: p for p in body["profiles"]}
    prof = by_fp["nopt000000x"]
    assert prof["overall"] is None  # no full raw corner → no Overall
    assert body["best_fingerprint"] != "nopt000000x"  # no Overall → can't be crowned


def test_overall_ranks_by_raw_measurements_dominance(client):
    # A profile faster on ALL three raw crown metrics must have a higher Overall (the corner is
    # monotonic in the raw values). Field-relative, so assert the ordering, not exact values.
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(3):
        _seed_run("rawfast000x", 80, t0 - timedelta(minutes=60 - i), iterations=6,
                  crown_raw=(180.0, 220.0, 100.0))   # faster on all three
    for i in range(3):
        _seed_run("rawslow000x", 80, t0 - timedelta(minutes=40 - i), iterations=6,
                  crown_raw=(300.0, 360.0, 400.0))   # slower on all three

    by = {p["fingerprint"]: p for p in client.get("/api/settings/profiles").json()["profiles"]}
    fast, slow = by["rawfast000x"], by["rawslow000x"]
    # Better raw on every crown metric → strictly higher normalized value on each…
    for m in ("fcp", "lcp", "total_stall"):
        assert fast["crown_norm"][m] > slow["crown_norm"][m]
    # …and therefore a higher Overall (dominance is preserved — grading can't reverse it).
    assert fast["overall"] > slow["overall"]
    # The Overall IQR brackets the point estimate (corner over normalized p25/p75 raw).
    assert fast["overall_p25"] <= fast["overall"] <= fast["overall_p75"]


# ── Percentile-normalized raw crown helpers (no grading, no thresholds) ──────────────

from pathbrain.api.routes_settings import (  # noqa: E402
    _crown_field_values,
    _normalized_crown,
    _percentile_norm,
)


def test_percentile_norm_is_rank_based_and_direction_aware():
    field = [100, 200, 300, 400]
    # Lower-is-better: smallest value beats the whole field → ~top percentile; largest → bottom.
    assert _percentile_norm(100, field, higher=False) > _percentile_norm(400, field, higher=False)
    assert _percentile_norm(200, field, higher=False) > _percentile_norm(300, field, higher=False)
    # Higher-is-better flips the ordering.
    assert _percentile_norm(400, field, higher=True) > _percentile_norm(100, field, higher=True)
    # A value's percentile does NOT depend on magnitude, only rank: compressing the field's
    # spread (one outlier) can't change the ordering.
    assert _percentile_norm(None, field, higher=False) is None
    assert _percentile_norm(50, [50], higher=False) == 100.0  # single-profile field


def test_percentile_norm_equalizes_spread_so_no_metric_dominates():
    # The point of rank normalization: a metric with a huge magnitude spread and one with a
    # tiny spread map to the SAME set of percentiles, so neither can dominate the corner.
    wide = [10, 500, 1000]      # total_stall-like: big magnitudes
    narrow = [300, 301, 302]    # fcp-like: tightly clustered
    assert (
        _percentile_norm(500, wide, higher=False)
        == _percentile_norm(301, narrow, higher=False)
    )  # the middle profile ranks the same on both, regardless of magnitude


def test_normalized_crown_is_monotonic_and_threshold_free():
    # The crown corner takes ONLY raw measurements + the field distribution + direction —
    # never a methodology best/worst threshold — so re-grading a metric cannot change it.
    metrics = ["fcp", "lcp", "total_stall"]
    higher = {m: False for m in metrics}  # all lower-is-better
    profiles = [
        {"metrics": {"fcp": 200, "lcp": 240, "total_stall": 100}},
        {"metrics": {"fcp": 300, "lcp": 360, "total_stall": 400}},
    ]
    field = _crown_field_values(profiles, metrics)
    spreads = {m: {"p25": None, "p75": None, "n": 1} for m in metrics}
    fast = _normalized_crown(profiles[0]["metrics"], spreads, field, higher, metrics, metrics)
    slow = _normalized_crown(profiles[1]["metrics"], spreads, field, higher, metrics, metrics)
    # Faster on all three → higher percentile on every axis → higher corner.
    assert fast["overall"] > slow["overall"]
    for m in metrics:
        assert fast["norm"][m] >= slow["norm"][m]
    # Optimistic ceiling ≥ the point Overall (benefit of the doubt for a thin sample).
    assert fast["optimistic"] >= fast["overall"]


def test_normalized_crown_missing_required_metric_is_none():
    metrics = ["fcp", "lcp", "total_stall"]
    higher = {m: False for m in metrics}
    field = {"fcp": [100, 300]}  # only fcp has a field distribution
    res = _normalized_crown({"fcp": 150}, {}, field, higher, metrics, metrics)
    assert res["overall"] is None  # lcp/total_stall absent → no corner


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
    # The write went through the provider apply path — target written as the bare number 3
    # (the firewall's option key), not "3ms".
    assert _OVERRIDES.get("quantum") == 4000
    assert _OVERRIDES.get("target") == 3
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


def test_apply_settings_preview_then_commit(client):
    """apply-settings applies arbitrary (AI-style) settings permanently — preview lists the
    exact writes without touching the firewall, commit writes them via the provider."""
    from pathbrain.providers.mock import _OVERRIDES

    _OVERRIDES.clear()
    # An AI-style "3ms" string is accepted but written to the firewall as the bare number 3
    # (the option key the firewall's duration select actually uses).
    settings = [{"label": "wan-download", "quantum": 4000, "target": "3ms"}]

    prev = client.post(
        "/api/settings/apply-settings", json={"settings": settings, "preview": True}
    ).json()
    assert prev["preview"] is True and prev["already_applied"] is False
    by_field = {(c["label"], c["param"]): c for c in prev["changes"]}
    assert by_field[("wan-download", "quantum")]["to"] == 4000
    # The planned write value is the bare number, not "3ms".
    assert by_field[("wan-download", "target")]["value"] == 3
    assert _OVERRIDES == {}  # preview wrote nothing

    resp = client.post(
        "/api/settings/apply-settings",
        json={"settings": settings, "run_benchmark": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert _OVERRIDES.get("quantum") == 4000 and _OVERRIDES.get("target") == 3
    _OVERRIDES.clear()


def test_apply_settings_canonicalizes_ai_format(client):
    """An AI's duration value ("3ms" / "3" / 3) is written as the firewall's bare number 3 —
    never "3ms", which the firewall's option-keyed select would reject."""
    from pathbrain.providers.mock import _OVERRIDES

    for raw in ("3ms", "3", 3, 3.0):
        _OVERRIDES.clear()
        resp = client.post(
            "/api/settings/apply-settings",
            json={"settings": [{"label": "wan-download", "target": raw}], "run_benchmark": False},
        )
        assert resp.status_code == 200, raw
        assert _OVERRIDES.get("target") == 3, raw
    _OVERRIDES.clear()


def test_apply_settings_rejects_no_op(client):
    from pathbrain.providers.mock import _OVERRIDES

    _OVERRIDES.clear()
    resp = client.post("/api/settings/apply-settings", json={"settings": {}})
    assert resp.status_code == 400
    assert _OVERRIDES == {}


def test_apply_settings_kicks_benchmark(client, monkeypatch):
    from pathbrain.providers.mock import _OVERRIDES
    import pathbrain.api.routes_run as routes_run

    _OVERRIDES.clear()
    kicked: list[int] = []
    monkeypatch.setattr(routes_run, "_locked_execute", lambda rid: kicked.append(rid))

    body = client.post(
        "/api/settings/apply-settings",
        json={"settings": [{"label": "wan-download", "quantum": 4000}], "run_benchmark": True},
    ).json()
    assert body["run_id"] is not None and kicked == [body["run_id"]]
    _OVERRIDES.clear()


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


# ── Crown = highest median Overall; ties are informational only ─────────────────────
# The crown follows the highest median Overall, full stop — the winner wins, by any margin.
# The per-run Overall IQR does NOT change who is crowned: it only labels a photo finish.
# ``co_leaders`` lists the profiles statistically indistinguishable from the crown (within
# run-to-run noise via ``_clearly_better``), purely so the UI can flag a "tied" chip. There
# is no hysteresis (stickiness to the active profile) and no steadiness override in the
# verdict. Exercised as pure-function unit tests (no DB) plus one end-to-end wiring test, so
# the shared module DB can't perturb the assertions.

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
        "last_seen": "2026-01-01T00:00:00",
    }


def test_clearly_better_requires_more_than_noise():
    # ``_clearly_better`` still powers the *co-leader* (tie) labelling, so it keeps its
    # noise-aware semantics — it just no longer changes who's crowned.
    tight_hi = _prof("hi", 90.0, iqr=0.0)
    tight_lo = _prof("lo", 70.0, iqr=0.0)
    assert _clearly_better(tight_hi, tight_lo, 0.5, 0.5) is True  # 20-pt lead is real
    wide = _prof("wide", 89.0, iqr=10.0)
    assert _clearly_better(tight_hi, wide, 0.5, 0.5) is False  # 1 < 0.5*10 → within noise
    near = _prof("near", 89.7, iqr=0.0)
    assert _clearly_better(tight_hi, near, 0.5, 0.5) is False  # gap 0.3 < 0.5 floor


def test_crown_follows_highest_median_even_by_a_hair():
    # jittery leads by a hair (96 vs 95) with a wide band; steady is a touch lower but
    # rock-steady. The winner is the highest median, period — jittery is crowned even though
    # the lead is inside the noise. Both are still returned as co-leaders (informational).
    steady = _prof("steady", 95.0, iqr=0.0)
    jittery = _prof("jittery", 96.0, iqr=12.0)
    best, co = _select_crown([steady, jittery], min_margin=0.5, iqr_fraction=0.5)
    assert best["fingerprint"] == "jittery"  # higher median wins, no steadiness override
    assert set(co) == {"steady", "jittery"}  # both flagged tied (within noise)


def test_crown_ignores_the_active_profile_no_stickiness():
    # There is no hysteresis: even if the marginally-lower profile is the one deployed, the
    # crown still moves to the highest median. (``_select_crown`` takes no active fingerprint.)
    deployed_lo = _prof("deployed", 95.0, iqr=0.0)
    higher = _prof("higher", 95.4, iqr=0.0)  # +0.4: within the 0.5 floor → a co-leader…
    best, co = _select_crown([deployed_lo, higher], min_margin=0.5, iqr_fraction=0.5)
    assert best["fingerprint"] == "higher"  # …yet still crowned: highest median wins
    assert set(co) == {"deployed", "higher"}  # both within noise → both tied


def test_crown_clear_winner_has_no_co_leaders():
    # A decisive, well-separated lead crowns the higher profile with no co-leaders.
    hi = _prof("hi", 92.0, iqr=1.0)
    lo = _prof("lo", 74.0, iqr=1.0)
    best, co = _select_crown([lo, hi], min_margin=0.5, iqr_fraction=0.5)
    assert best["fingerprint"] == "hi"
    assert co == ["hi"]  # only the crown; "lo" is clearly beaten, not a co-leader


def test_profiles_endpoint_flags_co_leaders_on_a_tie(client, monkeypatch):
    # End-to-end: two confident profiles with identical raw crown measurements → identical
    # Overall → a statistical tie. Seed them as the fastest in the field (raw ~1 ms) so they're
    # the global crown contenders; one is crowned (tie-break), the other flagged co-leader.
    import pathbrain.api.routes_settings as rs

    monkeypatch.setattr(rs, "_current_fingerprint", lambda: None)
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(6):
        _seed_run("tieaaa0000x", 80, t0 - timedelta(minutes=200 - i), iterations=3,
                  crown_raw=(1.0, 1.0, 1.0))
    for i in range(6):
        _seed_run("tiebbb0000x", 80, t0 - timedelta(minutes=100 - i), iterations=3,
                  crown_raw=(1.0, 1.0, 1.0))

    body = client.get("/api/settings/profiles").json()
    co = set(body["co_leaders"])
    best = body["best_fingerprint"]
    assert best in {"tieaaa0000x", "tiebbb0000x"}          # one of our (tied, fastest) pair wins
    other = "tiebbb0000x" if best == "tieaaa0000x" else "tieaaa0000x"
    assert other in co                                      # the tied twin is flagged co-leader
    assert best not in co                                   # the crown is excluded from its own co-leaders


def test_optimizer_export_has_settings_runs_and_raw_metrics(client):
    # The AI export is profile-centric: each profile carries its tunable settings, its runs, and
    # the raw scoring metrics per run — plus the methodology objective and the shaper field model.
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(3):
        _seed_run("optexp0000x", 80, t0 - timedelta(minutes=30 - i), iterations=6,
                  crown_raw=(210.0, 250.0, 140.0))

    body = client.get("/api/settings/export/optimizer").json()
    # Top-level: objective + the levers an AI may tune.
    assert "generated_at" in body and body["profile_count"] >= 1
    meth = body["methodology"]
    assert set(meth["crown_metrics"]) == {"fcp", "lcp", "total_stall"}
    assert "objective" in meth and meth["metrics"]["fcp"]["is_crown_metric"] is True
    shaper = body["shaper_model"]
    assert "quantum" in shaper["writable_fields"]        # a lever apply() can write
    assert any(f["key"] == "target" and f["suggested_range"] for f in shaper["fields"])

    prof = next(p for p in body["profiles"] if p["fingerprint"] == "optexp0000x")
    assert prof["settings"]                               # FULL shaper config (levers + identity)
    assert prof["confident"] and prof["runs"] == 3
    # Both scoring data AND full details are present per profile.
    assert "axis_scores" in prof and "overall_iqr" in prof
    assert prof["first_seen"] and prof["last_seen"]
    # Raw scoring metrics per run (most recent first), with the crown metrics present.
    assert len(prof["run_samples"]) == 3
    sample = prof["run_samples"][0]
    assert sample["metrics"]["fcp"] == 210.0 and sample["metrics"]["total_stall"] == 140.0
    # Percentile-normalized crown + raw medians summarize the profile.
    assert set(prof["crown_percentiles"]) >= {"fcp", "lcp", "total_stall"}
    assert prof["metric_medians"]["lcp"] == 250.0
    # Full-history spread per metric (n = every run) accompanies the capped raw samples.
    assert prof["metric_distribution"]["fcp"]["n"] == 3
    assert prof["metric_distribution"]["lcp"]["median"] == 250.0


def test_optimizer_export_caps_run_samples(client):
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(6):
        _seed_run("optcap0000x", 80, t0 - timedelta(minutes=60 - i), iterations=3,
                  crown_raw=(300.0, 340.0, 200.0))

    body = client.get("/api/settings/export/optimizer?runs_per_profile=2").json()
    prof = next(p for p in body["profiles"] if p["fingerprint"] == "optcap0000x")
    assert len(prof["run_samples"]) == 2          # capped to the requested limit
    assert prof["run_samples_truncated"] is True  # flagged as truncated (6 runs > 2)
    # The distribution is computed from ALL 6 runs, not just the 2 sampled — so variance
    # is always conveyed regardless of the cap.
    dist = prof["metric_distribution"]["fcp"]
    assert dist["n"] == 6
    assert dist["min"] == 300.0 and dist["max"] == 300.0 and dist["median"] == 300.0


def test_optimizer_export_distribution_spans_full_history(client):
    """metric_distribution reports the true spread (min < median < max) across all runs."""
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    for i, fcp in enumerate((100.0, 200.0, 300.0, 400.0, 500.0)):
        _seed_run("optdist000x", 80, t0 - timedelta(minutes=50 - i), iterations=3,
                  crown_raw=(fcp, 340.0, 200.0))

    body = client.get("/api/settings/export/optimizer?runs_per_profile=2").json()
    prof = next(p for p in body["profiles"] if p["fingerprint"] == "optdist000x")
    assert len(prof["run_samples"]) == 2          # samples capped
    dist = prof["metric_distribution"]["fcp"]
    assert dist["n"] == 5                          # distribution over the full history
    assert dist["min"] == 100.0 and dist["max"] == 500.0 and dist["median"] == 300.0
    assert dist["p25"] < dist["median"] < dist["p75"]


def test_optimizer_export_top_n_profiles(client):
    body = client.get("/api/settings/export/optimizer?profile_limit=1").json()
    assert body["profile_count"] == 1                       # only the top profile by Overall
    assert body["profile_limit"] == 1
    assert body["profiles_available"] >= 1                  # but more exist in the field
    # The one returned is the highest-Overall profile.
    assert body["profiles"][0]["overall"] is not None


def test_field_sensitivity_detects_monotonic_relationships():
    from pathbrain.api.routes_settings import _field_sensitivity

    meta = {"fcp": {"label": "FCP", "higher_is_better": False}}
    # quantum rises 1000→5000 while fcp falls 300→150 (perfect inverse → improves the crown);
    # target is constant so it can't correlate and must be dropped.
    profiles = [
        {"settings": [{"label": "Download", "quantum": q, "target": 5}], "metric_medians": {"fcp": f}}
        for q, f in [(1000, 300), (2000, 250), (3000, 200), (4000, 180), (5000, 150)]
    ]
    rows = _field_sensitivity(profiles, ["fcp"], meta)
    q_row = next(r for r in rows if r["field"] == "quantum" and r["metric"] == "fcp")
    assert q_row["pipe"] == "Download"
    assert q_row["spearman"] == -1.0
    assert q_row["metric_direction"] == "decreases" and q_row["effect"] == "improves"
    # A constant lever (target) never appears — no distinct values to correlate.
    assert not any(r["field"] == "target" for r in rows)


def test_field_sensitivity_correlates_against_overall():
    # The lever is also correlated against the Overall itself (the rank-corner we crown on),
    # not only the individual raw crown metrics — because that's where "overperformance" lives.
    from pathbrain.api.routes_settings import _field_sensitivity

    meta = {"fcp": {"label": "FCP", "higher_is_better": False}}
    # quantum rises while the (higher-is-better) Overall rises too → raising it improves the crown.
    profiles = [
        {"settings": [{"label": "Download", "quantum": q}], "metric_medians": {"fcp": f},
         "overall": ov}
        for q, f, ov in [(1000, 300, 40), (2000, 250, 55), (3000, 200, 70), (4000, 180, 85)]
    ]
    rows = _field_sensitivity(profiles, ["fcp"], meta)
    ov_row = next(r for r in rows if r["metric"] == "overall")
    assert ov_row["metric_label"] == "Overall"
    assert ov_row["spearman"] == 1.0                       # Overall rises with quantum
    assert ov_row["metric_direction"] == "increases" and ov_row["effect"] == "improves"


def test_lever_signature_finds_a_sweet_spot_correlation_misses():
    # The winners cluster quantum at ~3000 while the field ranges 800–6000 and the rest are
    # spread across it — a sweet spot a monotone correlation can't see (both extremes are worse).
    from pathbrain.api.routes_settings import _lever_signature

    top = [{"settings": [{"label": "Download", "quantum": q}], "overall": ov}
           for q, ov in [(2950, 92), (3000, 90), (3050, 88)]]
    rest = [{"settings": [{"label": "Download", "quantum": q}], "overall": ov}
            for q, ov in [(800, 60), (1500, 58), (6000, 55), (5000, 52), (1000, 50),
                          (4500, 48), (2000, 46), (5500, 44), (900, 42)]]
    sig = _lever_signature(top + rest)
    assert sig["available"] is True
    lev = next(l for l in sig["levers"] if l["field"] == "quantum")
    assert lev["pattern"] == "sweet_spot"
    assert 2900 <= lev["top_value"] <= 3100
    assert lev["field_range"] == [800, 6000]


def test_lever_signature_flags_a_higher_run_and_needs_enough_profiles():
    from pathbrain.api.routes_settings import _lever_signature

    # Winners run quantum systematically higher than the rest.
    top = [{"settings": [{"label": "Download", "quantum": q}], "overall": ov}
           for q, ov in [(5500, 92), (5800, 90), (6000, 88)]]
    rest = [{"settings": [{"label": "Download", "quantum": q}], "overall": ov}
            for q, ov in [(800, 60), (1000, 58), (1200, 55), (1500, 52), (2000, 50),
                          (900, 48), (1100, 46), (1300, 44), (1700, 42)]]
    lev = next(l for l in _lever_signature(top + rest)["levers"] if l["field"] == "quantum")
    assert lev["pattern"] == "higher" and (lev["shift"] or 0) > 0

    # Too few scored profiles to split → unavailable, not a crash.
    thin = _lever_signature(top[:2])
    assert thin["available"] is False and thin["levers"] == []


def test_field_sensitivity_flags_worsening_and_no_trend():
    from pathbrain.api.routes_settings import _field_sensitivity

    meta = {"fcp": {"label": "FCP", "higher_is_better": False}}
    # quantum rises while fcp ALSO rises → raising it worsens the crown.
    worse = [
        {"settings": [{"label": "Download", "quantum": q}], "metric_medians": {"fcp": f}}
        for q, f in [(1000, 150), (2000, 200), (3000, 250), (4000, 300)]
    ]
    r = next(x for x in _field_sensitivity(worse, ["fcp"], meta) if x["field"] == "quantum")
    assert r["spearman"] == 1.0 and r["effect"] == "worsens" and r["metric_direction"] == "increases"

    # No monotonic trend → reported as "none" (|ρ| below the trend threshold), not improves/worsens.
    flat = [
        {"settings": [{"label": "Download", "quantum": q}], "metric_medians": {"fcp": f}}
        for q, f in [(1000, 200), (2000, 205), (3000, 199), (4000, 202), (5000, 201)]
    ]
    fr = next(x for x in _field_sensitivity(flat, ["fcp"], meta) if x["field"] == "quantum")
    assert fr["effect"] == "none" and fr["metric_direction"] == "none"


def test_field_sensitivity_keeps_pipes_separate_and_needs_enough_points():
    from pathbrain.api.routes_settings import _field_sensitivity

    meta = {"fcp": {"label": "FCP", "higher_is_better": False}}
    # Only 3 points (< SENSITIVITY_MIN_POINTS=4) → nothing computed.
    thin = [
        {"settings": [{"label": "Download", "quantum": q}], "metric_medians": {"fcp": f}}
        for q, f in [(1000, 300), (2000, 200), (3000, 100)]
    ]
    assert _field_sensitivity(thin, ["fcp"], meta) == []

    # A Download and an Upload pipe with opposite trends stay independent rows.
    both = [
        {
            "settings": [
                {"label": "Download", "quantum": q},
                {"label": "Upload", "quantum": 6000 - q},
            ],
            "metric_medians": {"fcp": f},
        }
        for q, f in [(1000, 300), (2000, 250), (3000, 200), (4000, 150)]
    ]
    rows = _field_sensitivity(both, ["fcp"], meta)
    dl = next(r for r in rows if r["pipe"] == "Download" and r["field"] == "quantum")
    ul = next(r for r in rows if r["pipe"] == "Upload" and r["field"] == "quantum")
    assert dl["spearman"] == -1.0 and ul["spearman"] == 1.0  # mirror-image levers


def test_optimizer_export_carries_analysis_block(client):
    body = client.get("/api/settings/export/optimizer").json()
    assert "analysis" in body
    assert "note" in body["analysis"] and "field_sensitivity" in body["analysis"]
    assert isinstance(body["analysis"]["field_sensitivity"], list)


def test_apply_writable_overrides_only_touches_writable_fields():
    from pathbrain.api.routes_settings import _apply_writable_overrides

    live = [{"label": "wan-download", "quantum": 1514, "target": "5ms",
             "scheduler": "fq_codel", "queues": 1}]
    # A per-pipe override changing a writable (quantum) and a non-writable (scheduler) field.
    out = _apply_writable_overrides(live, [{"label": "wan-download", "quantum": 3000, "scheduler": "HFSC"}])
    assert out[0]["quantum"] == 3000            # writable → applied
    assert out[0]["scheduler"] == "fq_codel"    # non-writable → left as live (reachable)
    # A flat dict applies to every pipe; a duration is canonicalized to the firewall's bare
    # number (3), not "3ms" — the option key its select actually uses.
    out2 = _apply_writable_overrides(live, {"target": "3ms"})
    assert out2[0]["target"] == 3


def test_test_settings_rejects_a_no_op(client):
    from pathbrain.providers import mock as mock_mod
    mock_mod._OVERRIDES.clear()
    # Empty overrides → target == live → nothing to change → 400 (never starts a test).
    resp = client.post("/api/settings/test-settings", json={"settings": {}})
    assert resp.status_code == 400


def test_test_settings_starts_a_test_for_a_writable_change(client, monkeypatch):
    import pathbrain.api.routes_settings as rs
    from pathbrain.providers import mock as mock_mod
    mock_mod._OVERRIDES.clear()
    calls: dict = {}
    monkeypatch.setattr(rs.profile_test_mod, "start",
                        lambda fp, target, label, iters: calls.update(fp=fp, iters=iters) or 55)

    resp = client.post("/api/settings/test-settings",
                       json={"settings": {"quantum": 6000}, "label": "AI idea"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 55 and body["iterations"] >= 1
    assert calls["fp"] == body["fingerprint"]   # materialized fingerprint is what gets tested


def test_optimizer_export_includes_all_pipes(client, monkeypatch):
    # A profile with BOTH a download and an upload pipe must export both — not just download.
    import pathbrain.api.routes_settings as rs

    two_pipes = [
        {"label": "Download", "download_bandwidth": "880Mbit", "quantum": 6056, "target": "3ms",
         "interval": "60ms", "ecn": True, "scheduler": "fq_codel", "queues": 1},
        {"label": "Upload", "download_bandwidth": "880Mbit", "quantum": 500, "target": "3ms",
         "interval": "60ms", "ecn": True, "scheduler": "fq_codel", "queues": 1},
    ]
    # compute_profiles reads run.settings; patch the seed to carry both pipes for this fp.
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    for i in range(3):
        _seed_run("twopipe000x", 80, t0 - timedelta(minutes=30 - i), iterations=6,
                  crown_raw=(200.0, 240.0, 150.0))
    with session_scope() as s:
        from pathbrain.models import Run
        for run in s.query(Run).filter(Run.settings_fingerprint == "twopipe000x").all():
            run.settings = two_pipes

    body = client.get("/api/settings/export/optimizer").json()
    prof = next(p for p in body["profiles"] if p["fingerprint"] == "twopipe000x")
    labels = {pipe["label"] for pipe in prof["settings"]}
    assert labels == {"Download", "Upload"}                 # BOTH directions exported
    up = next(pipe for pipe in prof["settings"] if pipe["label"] == "Upload")
    assert up["quantum"] == 500                             # the upload pipe's own params
    # The export tells the model to tune both directions.
    assert "upload" in body["shaper_model"]["pipes_note"].lower()
