"""The belief layer: `fuse` generalised with instrument epochs and a quantum surface.

What is pinned here: with no surface and one epoch the belief model *is* today's fused fit
(so the backbone ships without moving the crown); its marginal likelihood is the true one
(so empirical Bayes chooses s, τ and λ honestly); an instrument step is absorbed as an
epoch offset rather than read as profiles getting worse; the surface finds the quantum
shape it was handed; and the backtest holds a ring session out of everything, its runs
included, before it scores which model predicts that night.
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pathbrain import beliefs, overall_ranking

UTC = timezone.utc
BOUNDS = [datetime(2026, 10, 19, tzinfo=UTC), datetime(2026, 11, 2, tzinfo=UTC)]


def _fixture(rnd: random.Random) -> tuple[dict, list[dict]]:
    n = rnd.randint(3, 12)
    fps = [f"p{i}" for i in range(n)]
    pooled = {
        fp: {"overall": rnd.uniform(50, 80), "se": rnd.choice([None, rnd.uniform(0.2, 3)])}
        for fp in fps if rnd.random() < 0.8
    }
    ring = rnd.sample(fps, rnd.randint(0, n))
    rounds = []
    if len(ring) >= 2:
        for _ in range(rnd.randint(1, 30)):
            a, b = rnd.sample(ring, 2)
            rounds.append({"a": a, "b": b, "margin": rnd.gauss(0, 3), "session_id": 1})
    return pooled, rounds


@pytest.mark.parametrize("scale", [1.0, 0.0])
def test_with_no_surface_and_one_epoch_it_is_the_fused_fit_exactly(scale):
    """Decision 20's premise: the generalisation changes nothing until it is switched on.
    Every profile's θ, its SE and every pair's difference SE match `fuse`, including the
    floors (an unanchored ring profile toward the median, a vanishing anchor's weight)."""
    rnd = random.Random(7)
    for _ in range(40):
        pooled, rounds = _fixture(rnd)
        tau, sigma = rnd.uniform(0.1, 2.0), rnd.uniform(1.0, 4.0)
        old = overall_ranking.fuse(pooled, rounds, sigma, tau, anchor_scale=scale)
        new = beliefs.fit_flat(pooled, rounds, sigma, tau, anchor_scale=scale)
        assert set(new.theta) == {k for k in old if k != "__cov__"}
        for fp, v in new.theta.items():
            assert v["theta"] == pytest.approx(old[fp]["fused"], abs=1e-9)
            assert v["se"] == pytest.approx(old[fp]["se"], abs=1e-9)
        ring = [k for k in old if k != "__cov__" and old[k]["cov_index"] is not None]
        for i, a in enumerate(ring):
            for b in ring[i + 1:]:
                assert new.diff_se(a, b) == pytest.approx(overall_ranking.diff_se(old, a, b), abs=1e-9)


def _direct_log_evidence(anchors, rounds, sigma, tau, x, s, lam, epochs):
    """log N(y; 0, C) with C built from the model's prior covariance directly: the slow,
    obviously-right computation the closed-form evidence must agree with."""
    centre = overall_ranking._median([a["overall"] for a in anchors])
    fps: list[str] = []
    for fp in [a["fingerprint"] for a in anchors] + [r[k] for r in rounds for k in ("a", "b")]:
        if fp not in fps:
            fps.append(fp)
    eps = sorted({a["epoch"] for a in anchors}) if epochs else []
    ref = eps[-1] if eps else None

    def row(fp):
        if x is None:
            return [1.0]
        return None if x.get(fp) is None else [1.0] + list(x[fp])

    def prior_cov(i, j):
        a, b = row(fps[i]), row(fps[j])
        if a is None or b is None:
            return beliefs.FREE_PROFILE_SD ** 2 if i == j else 0.0
        v = a[0] * b[0] * beliefs.INTERCEPT_SD ** 2 + sum(a[k] * b[k] * lam * lam for k in range(1, len(a)))
        return v + (s * s if i == j else 0.0)

    obs = [({fps.index(a["fingerprint"]): 1.0},
            a["epoch"] if epochs and a["epoch"] != ref else None,
            a["overall"] - centre, a["se"] ** 2 + tau ** 2) for a in anchors]
    obs += [({fps.index(r["a"]): -1.0, fps.index(r["b"]): 1.0}, None, r["margin"], sigma ** 2)
            for r in rounds]
    C = [[sum(wa * wb * prior_cov(i, j) for i, wa in ca.items() for j, wb in cb.items())
          + (beliefs.EPOCH_OFFSET_SD ** 2 if ea is not None and ea == eb else 0.0)
          + (va if p == q else 0.0)
          for q, (cb, eb, _yb, _vb) in enumerate(obs)]
         for p, (ca, ea, _ya, va) in enumerate(obs)]
    L = beliefs._cholesky(C)
    u: list[float] = []
    for i, (_c, _e, y, _v) in enumerate(obs):
        u.append((y - sum(L[i][k] * u[k] for k in range(i))) / L[i][i])
    return (-0.5 * sum(v * v for v in u) - sum(math.log(L[i][i]) for i in range(len(obs)))
            - 0.5 * len(obs) * math.log(2 * math.pi))


def test_the_marginal_likelihood_is_the_true_one():
    """Empirical Bayes is only as honest as its evidence. The closed form (Schur
    elimination of every never-fought profile, then one small Cholesky) must equal the
    Gaussian density of the readings under the model's own prior, with and without epochs
    and the surface."""
    rnd = random.Random(11)
    for _ in range(12):
        fps = [f"p{i}" for i in range(rnd.randint(3, 8))]
        x = {fp: ([rnd.uniform(-1, 1) for _ in range(3)] if rnd.random() < 0.85 else None) for fp in fps}
        anchors = [
            {"fingerprint": fp, "epoch": e, "overall": rnd.uniform(50, 80), "se": rnd.uniform(0.2, 2)}
            for fp in fps for e in rnd.sample([0, 1, 2], rnd.randint(1, 2))
        ]
        rounds = []
        for _ in range(rnd.randint(0, 12)):
            a, b = rnd.sample(fps, 2)
            rounds.append({"a": a, "b": b, "margin": rnd.gauss(0, 3), "session_id": 1})
        for epochs in (True, False):
            for xx in (x, None):
                tau, s, lam = rnd.uniform(0.1, 2), rnd.uniform(0.3, 5), rnd.uniform(0.3, 5)
                fit = beliefs.solve(anchors, rounds, 2.5, tau=tau, epochs=epochs, x=xx, s=s, lam=lam)
                want = _direct_log_evidence(anchors, rounds, 2.5, tau, xx, s, lam, epochs)
                assert fit.log_evidence == pytest.approx(want, abs=1e-8)


def _world(seed=1, n=120, ring_n=24, sessions=12, step=-6.0, surface=True):
    """A synthetic record: profiles on a known quantum surface, an instrument that reads
    ``step`` lower before each of two boundaries, solo runs across 100 days and ring
    sessions whose legs land in the pooled record under their duel's job group."""
    rnd = random.Random(seed)
    t0 = datetime(2026, 8, 1, tzinfo=UTC)
    prof = []
    for i in range(n):
        dq = math.exp(rnd.uniform(math.log(1500), math.log(10000)))
        uq = math.exp(rnd.uniform(math.log(300), math.log(3000)))
        zd = 2 * (math.log(dq) - math.log(1500)) / (math.log(10000) - math.log(1500)) - 1
        zu = 2 * (math.log(uq) - math.log(300)) / (math.log(3000) - math.log(300)) - 1
        f = 70 + ((-3 * (zd - 0.3) ** 2 - 2 * (zu + 0.2) ** 2 + zd * zu) if surface else 0.0)
        prof.append({"fingerprint": f"p{i}", "theta": f + rnd.gauss(0, 1.0),
                     "settings": [{"label": "Download", "quantum": round(dq)},
                                  {"label": "Upload", "quantum": round(uq)}]})

    def read(p, at):
        older = sum(1 for b in BOUNDS if at < b)
        return p["theta"] + step * older + rnd.gauss(0, 2.5)

    rows = []
    for p in prof:
        for _ in range(rnd.randint(1, 4)):
            at = t0 + timedelta(days=rnd.uniform(0, 100))
            rows.append((p["fingerprint"], at, 5, {"m": read(p, at)}, None))
    sessions_data = []
    for sid in range(1, sessions + 1):
        at = t0 + timedelta(days=100 * sid / sessions)
        matchups = []
        for _ in range(4):
            a, b = rnd.sample(prof[:ring_n], 2)
            deltas = [b["theta"] - a["theta"] + rnd.gauss(0, 3) for _ in range(rnd.randint(4, 10))]
            matchups.append({"incumbent": a["fingerprint"], "challenger": b["fingerprint"], "deltas": deltas})
            for p in (a, b):
                rows.append((p["fingerprint"], at, 1, {"m": read(p, at)}, f"duel-{sid}"))
        sessions_data.append({"id": sid, "matchups": matchups})
    rounds = [{"a": m["incumbent"], "b": m["challenger"], "margin": d, "session_id": s["id"]}
              for s in sessions_data for m in s["matchups"] for d in m["deltas"]]
    store = beliefs.RunStore.build(rows, BOUNDS, ["m"], ["m"], {"m": 1.0})
    return store, rounds, sessions_data, beliefs.lever_basis(prof), prof


def test_an_instrument_step_is_an_epoch_offset_and_the_surface_is_found():
    """Two steps of -6 points each are read as offsets of about -12 and -6 against the
    newest epoch, not as the old profiles being worse; the surface's curvature on both
    quanta comes out negative (an interior best, the mechanism's prediction) and an
    untested setting gets a finite band."""
    store, rounds, _sd, basis, prof = _world(seed=3, n=200)
    fit = beliefs.empirical_bayes(store.anchors(epochs=True), rounds, 3.0, epochs=True, x=basis["x"])
    assert fit.offsets[2]["offset"] == 0.0
    assert fit.offsets[0]["offset"] == pytest.approx(-12.0, abs=1.5)
    assert fit.offsets[1]["offset"] == pytest.approx(-6.0, abs=1.5)
    terms = dict(zip(["intercept"] + basis["terms"], fit.beta))
    assert terms["Download quantum²"]["mean"] < 0 and terms["Upload quantum²"]["mean"] < 0
    guess = beliefs.predict(fit, beliefs.coordinates(basis, {"Download": 5000, "Upload": 700}))
    assert guess is not None and 0 < guess["se"] < 5
    # θ is the instrument as it reads now: the fitted values track the truth, not the
    # era each profile happened to be measured in.
    err = [abs(fit.theta[p["fingerprint"]]["theta"] - p["theta"]) for p in prof]
    assert sorted(err)[len(err) // 2] < 1.5


def test_the_backtest_holds_a_session_out_of_everything_and_scores_every_arm():
    store, rounds, sessions_data, basis, _prof = _world(seed=5)
    bt = beliefs.backtest(store, rounds, sessions_data, 3.0, basis)
    assert bt is not None and set(bt["mae"]) == set(beliefs.ARMS)
    assert bt["sessions"] == 12 and bt["matches"] > 0
    # On a record with an instrument step the pooled medians alone are the worst reading
    # of the ring, and the belief model is no worse than today's fit.
    assert bt["mae"]["pooled"] > bt["mae"]["fuse"]
    assert bt["mae"]["beliefs"] <= bt["mae"]["fuse"] + 0.05
    assert bt["standing"] in {"better", "level", "worse"}


def test_a_held_out_session_s_runs_leave_the_anchors_too():
    rows = [
        ("a", datetime(2026, 11, 3, tzinfo=UTC), 1, {"m": 60.0}, None),
        ("a", datetime(2026, 11, 4, tzinfo=UTC), 1, {"m": 62.0}, None),
        ("a", datetime(2026, 11, 5, tzinfo=UTC), 1, {"m": 90.0}, "duel-9"),
    ]
    store = beliefs.RunStore.build(rows, BOUNDS, ["m"], ["m"], {"m": 1.0})
    every = {a["fingerprint"]: a for a in store.anchors(epochs=False)}
    held = {a["fingerprint"]: a for a in store.anchors(epochs=False, drop_group="duel-9")}
    assert every["a"]["runs"] == 3 and every["a"]["overall"] == 62.0
    assert held["a"]["runs"] == 2 and held["a"]["overall"] == 61.0


def test_the_surface_skips_shaping_off_and_constant_quanta():
    profiles = [
        {"fingerprint": "a", "settings": [{"label": "Download", "quantum": 3000}, {"label": "Upload", "quantum": 500}]},
        {"fingerprint": "b", "settings": [{"label": "Download", "quantum": 6000}, {"label": "Upload", "quantum": 500}]},
        {"fingerprint": "off", "is_sqm_off": True, "settings": []},
    ]
    basis = beliefs.lever_basis(profiles)
    assert basis["pipes"] == ["Download"]  # Upload never varies, so it is not a lever here
    assert basis["x"]["off"] is None
    assert basis["x"]["a"] == [-1.0, 1.0] and basis["x"]["b"] == [1.0, 1.0]


def test_the_crown_does_not_read_the_belief_layer_yet():
    """Decision 20: the crown moves to the belief model only through a publish, after the
    backtest shows it predicts the ring better."""
    root = Path(beliefs.__file__).parent
    for name in ("overall_ranking.py", "crown_follower.py", "crowning.py", "duel.py"):
        assert "beliefs" not in (root / name).read_text(), name


def test_the_read_answers_on_a_live_store(client):
    beliefs.clear_cache()
    res = client.get("/api/beliefs", params={"backtest": "false"})
    assert res.status_code == 200
    body = res.json()
    assert body["crown_reads"] == "fuse"
    assert {"profiles", "epochs", "surface", "hyper"} <= set(body)
