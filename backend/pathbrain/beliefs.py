"""**The belief layer** (gold): one posterior per profile and per lever effect, fitted from
the evidence the ring and the pooled record already hold.

This generalises `overall_ranking.fuse`. The fuse fit treats every profile as a free
number θ pinned by two kinds of reading, the ring's paired margins and the pooled median.
The belief model keeps both readings and adds the two things the 2026-11-08 review found
missing (docs/plan-learning-engine.md, "The belief model"):

    θ_i      = f(x_i) + u_i            u_i ~ N(0, s²)        profile residual
    ring     d_ab  = θ_b − θ_a + ε       ε ~ N(0, σ²)          σ = decidability.round_noise
    pooled   μ_i,e = θ_i + c_e + η       η ~ N(0, SE² + τ²)    c_e = epoch offset

* **Epochs.** A pooled median is read per instrument epoch (`evidence.instrument_epochs`),
  and each epoch carries an offset ``c_e`` against the newest one. A profile measured only
  before the LCP step is no longer compared with one measured only after it as if the
  instrument had not moved; the step is absorbed as an offset, learned from the profiles
  measured on both sides. θ is the Overall as the instrument measures it now.
* **Levers.** ``f`` is a quadratic surface over each pipe's quantum (log scale, normalised
  to the field's span): main effects, squares and the Download × Upload interaction, with a
  shrinkage prior. A profile the ring has never fought borrows strength from its
  neighbours on the surface, and an untested setting gets a prediction with an honest band
  (`predict`), which is what Explore proposes from in phase 4.

Everything is linear-Gaussian, so the posterior is closed form. ``s``, ``τ`` and the
lever prior scale are chosen by empirical Bayes (the marginal likelihood of the evidence),
not set by hand, and re-chosen on every read: a model that keeps missing widens itself.

**With f ≡ 0 and one epoch it is exactly ``fuse``** (`fit_flat`, pinned by a test). The
crown keeps reading ``fuse`` (decision 20) until `backtest` shows the belief model predicts
held-out ring margins better, and then only through a methodology-style publish.

Pure Python: the dense part of the system is the ring's profiles plus a handful of lever
and epoch terms. Every other profile touches only those terms, so it is eliminated in
closed form (a Schur complement) instead of being carried in the matrix.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

from .logging_config import get_logger

log = get_logger(__name__)

#: Prior sd of an epoch offset, in Overall points. Broad on purpose: the offsets are learned
#: from profiles measured on both sides of a step, and the prior only keeps an epoch no
#: profile spans from being singular.
EPOCH_OFFSET_SD = 10.0
#: Prior sd of the surface's intercept around the field median.
INTERCEPT_SD = 50.0
#: Prior sd of a profile with no lever coordinates (shaping off) around the field median.
#: It is not on the quantum surface, so it gets a broad prior of its own.
FREE_PROFILE_SD = 50.0
#: The empirical-Bayes grids, in Overall points.
S_GRID = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
LAMBDA_GRID = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
TAU_GRID = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0)
#: The lever whose surface ``f`` is fitted over, on every pipe that carries it.
SURFACE_FIELD = "quantum"
#: How long a full read (fit plus backtest) is reused.
CACHE_SECONDS = 15 * 60
#: The arms the backtest scores, in the order the page lists them.
ARMS = ("pooled", "fuse", "epochs", "levers", "beliefs")

_cache_lock = threading.Lock()
_cache: dict = {}


# ── linear algebra ──────────────────────────────────────────────────────────────────────


def _cholesky(a: list[list[float]]) -> list[list[float]]:
    n = len(a)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        Li = L[i]
        for j in range(i + 1):
            Lj = L[j]
            s = a[i][j]
            for k in range(j):
                s -= Li[k] * Lj[k]
            if i == j:
                if s <= 0:
                    raise ValueError("precision matrix is not positive-definite")
                Li[i] = math.sqrt(s)
            else:
                Li[j] = s / Lj[j]
    return L


def _inverse(L: list[list[float]]) -> list[list[float]]:
    """``(L Lᵀ)⁻¹`` from its Cholesky factor."""
    n = len(L)
    # Linv lower-triangular, then A⁻¹ = Linvᵀ Linv.
    Linv = [[0.0] * n for _ in range(n)]
    for j in range(n):
        Linv[j][j] = 1.0 / L[j][j]
        for i in range(j + 1, n):
            s = 0.0
            Li = L[i]
            for k in range(j, i):
                s += Li[k] * Linv[k][j]
            Linv[i][j] = -s / L[i][i]
    inv = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = 0.0
            for k in range(i, n):
                s += Linv[k][i] * Linv[k][j]
            inv[i][j] = s
            inv[j][i] = s
    return inv


# ── the model's inputs ──────────────────────────────────────────────────────────────────


def lever_basis(profiles: list[dict]) -> dict:
    """The surface's design: ``{"terms": [...], "pipes": [...], "x": {fp: [..] | None}}``.

    ``profiles`` are ``[{fingerprint, settings, is_sqm_off?}]``. Each pipe whose quantum
    varies across the field is one coordinate, on a log scale normalised to ``[-1, 1]`` over
    the field's span; the terms are each coordinate, its square, and every pairwise
    product. A profile missing a pipe's quantum sits at that coordinate's centre. A profile
    with shaping off has no place on the surface and gets ``None``.
    """
    from .settings_profile import _to_number

    coords: dict[str, dict[str, float]] = {}
    for p in profiles:
        if p.get("is_sqm_off"):
            continue
        for pipe in p.get("settings") or []:
            label = pipe.get("label") or "pipe"
            v = _to_number(SURFACE_FIELD, pipe.get(SURFACE_FIELD))
            if v is None or float(v) <= 0:
                continue
            coords.setdefault(p["fingerprint"], {})[label] = math.log(float(v))
    spans: dict[str, tuple[float, float]] = {}
    for c in coords.values():
        for label, v in c.items():
            lo, hi = spans.get(label, (v, v))
            spans[label] = (min(lo, v), max(hi, v))
    pipes = sorted(label for label, (lo, hi) in spans.items() if hi > lo)
    terms: list[str] = []
    for label in pipes:
        terms += [f"{label} {SURFACE_FIELD}", f"{label} {SURFACE_FIELD}²"]
    pairs = [(a, b) for i, a in enumerate(pipes) for b in pipes[i + 1:]]
    terms += [f"{a} × {b} {SURFACE_FIELD}" for a, b in pairs]
    x: dict[str, list[float] | None] = {}
    for p in profiles:
        fp = p["fingerprint"]
        if p.get("is_sqm_off") or not pipes:
            x[fp] = None
            continue
        c = coords.get(fp) or {}
        z = {}
        for label in pipes:
            lo, hi = spans[label]
            v = c.get(label)
            z[label] = 0.0 if v is None else (2.0 * (v - lo) / (hi - lo) - 1.0)
        row: list[float] = []
        for label in pipes:
            row += [z[label], z[label] ** 2]
        row += [z[a] * z[b] for a, b in pairs]
        x[fp] = row
    return {
        "terms": terms, "pipes": pipes, "x": x,
        "spans": {k: [round(math.exp(lo)), round(math.exp(hi))] for k, (lo, hi) in spans.items()},
    }


def coordinates(basis: dict, quanta: dict[str, float]) -> list[float]:
    """A setting's row on the surface, for a prediction: ``quanta`` is ``{pipe: quantum}``."""
    pipes = basis["pipes"]
    z = {}
    for label in pipes:
        lo, hi = (math.log(v) for v in basis["spans"][label])
        v = quanta.get(label)
        z[label] = 0.0 if not v or hi <= lo else (2.0 * (math.log(float(v)) - lo) / (hi - lo) - 1.0)
    row: list[float] = []
    for label in pipes:
        row += [z[label], z[label] ** 2]
    row += [z[a] * z[b] for i, a in enumerate(pipes) for b in pipes[i + 1:]]
    return row


def _grade_group(rows: list[tuple], crown: list[str], required: list[str], weights: dict) -> dict | None:
    """One profile's runs (in one epoch) graded the way `pooled_field.graded_field` grades
    the whole record: the Overall of the per-metric medians, the IQR between the Overalls
    of the per-metric quartiles, and the run count. ``rows`` are ``(iterations, subscores)``."""
    from statistics import median

    from .crown_follower import _grade_medians
    from .profile_aggregates import _quartiles

    samples: dict[str, list[float]] = {}
    iters = 0
    for iterations, subscores in rows:
        iters += int(iterations or 1)
        for m, v in (subscores or {}).items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            samples.setdefault(m, []).append(float(v))
    if not samples:
        return None
    med = {m: median(v) for m, v in samples.items()}
    overall = _grade_medians(med, iters, crown, required, weights)[0]
    if overall is None:
        return None
    q = {m: _quartiles(v) for m, v in samples.items()}
    p25 = _grade_medians({m: a for m, (a, _) in q.items()}, iters, crown, required, weights)[0]
    p75 = _grade_medians({m: b for m, (_, b) in q.items()}, iters, crown, required, weights)[0]
    return {
        "overall": round(float(overall), 2),
        "iterations": iters,
        "runs": len(rows),
        "iqr": None if p25 is None or p75 is None else max(0.0, float(p75) - float(p25)),
    }


@dataclass
class RunStore:
    """Every comparable run under the current methodology, grouped by profile and epoch,
    with each group's grade cached so a backtest fold regrades only the groups the held-out
    session touched."""

    groups: dict[tuple[str, int], list[tuple]]      # (fp, epoch) -> [(iters, subs, job_group)]
    crown: list[str]
    required: list[str]
    weights: dict
    _graded: dict = field(default_factory=dict)

    @classmethod
    def build(cls, rows: list[tuple], boundaries: list, crown, required, weights) -> "RunStore":
        """``rows`` are ``(fingerprint, created_at, iterations, subscores, job_group)``."""
        from .evidence import epoch_of

        groups: dict[tuple[str, int], list[tuple]] = {}
        for fp, at, iterations, subscores, group in rows:
            if not fp:
                continue
            groups.setdefault((fp, epoch_of(at, boundaries)), []).append(
                (iterations, subscores, group)
            )
        return cls(groups, list(crown), list(required), dict(weights))

    @property
    def epochs(self) -> list[int]:
        return sorted({e for _, e in self.groups})

    def _grade(self, key: tuple, rows: list[tuple], drop: str | None) -> dict | None:
        ck = (key, drop if drop and any(r[2] == drop for r in rows) else None)
        if ck not in self._graded:
            kept = [(i, s) for i, s, g in rows if not drop or g != drop]
            self._graded[ck] = _grade_group(kept, self.crown, self.required, self.weights) if kept else None
        return self._graded[ck]

    def anchors(self, *, epochs: bool, drop_group: str | None = None) -> list[dict]:
        """Pooled anchors ``[{fingerprint, epoch, overall, se, runs, iqr}]``: one per profile
        per epoch (``epochs=True``) or one per profile over the whole record. ``drop_group``
        leaves out one job group's runs (a held-out ring session's legs). Each SE is
        `shrinkage.profile_se` with the field's run noise, as `graded_field` computes it."""
        from .shrinkage import profile_se, run_noise

        if epochs:
            keyed = self.groups
        else:
            keyed = {}
            for (fp, _e), rows in self.groups.items():
                keyed.setdefault((fp, -1), []).extend(rows)
        out: list[dict] = []
        for key, rows in keyed.items():
            if not epochs:
                # The one-epoch grade spans groups, so it is cached under its own key.
                g = self._grade(("all", key[0]), rows, drop_group)
            else:
                g = self._grade(key, rows, drop_group)
            if g is None:
                continue
            out.append({"fingerprint": key[0], "epoch": max(0, key[1]) if epochs else 0, **g})
        noise, _n, _basis = run_noise([(a["iqr"], a["runs"]) for a in out])
        for a in out:
            a["se"] = profile_se(a["iqr"], a["runs"], noise)
        return out


# ── the fit ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Fit:
    """A posterior. ``theta[fp] = {theta, se}``; ``offsets[e] = {offset, se}`` against the
    newest epoch; ``beta`` the surface terms; ``log_evidence`` None for the flat (fuse)
    fit, whose priors are improper."""

    theta: dict[str, dict]
    offsets: dict[int, dict]
    beta: list[dict]
    log_evidence: float | None
    hyper: dict
    centre: float
    _cov: dict = field(default_factory=dict, repr=False)
    _beta_cov: list[list[float]] = field(default_factory=list, repr=False)

    def diff(self, a: str, b: str) -> float | None:
        ta, tb = self.theta.get(a), self.theta.get(b)
        if ta is None or tb is None:
            return None
        return tb["theta"] - ta["theta"]

    def diff_se(self, a: str, b: str) -> float | None:
        ta, tb = self.theta.get(a), self.theta.get(b)
        if ta is None or tb is None:
            return None
        cov = self._cov.get((a, b), self._cov.get((b, a), 0.0))
        return math.sqrt(max(0.0, ta["se"] ** 2 + tb["se"] ** 2 - 2.0 * cov))


def solve(
    anchors: list[dict],
    rounds: list[dict],
    sigma_round: float,
    *,
    tau: float,
    flat: bool = False,
    anchor_scale: float = 1.0,
    epochs: bool = True,
    x: dict[str, list[float] | None] | None = None,
    s: float = 2.0,
    lam: float = 2.0,
    kappa: float = EPOCH_OFFSET_SD,
    want_cov: bool = False,
) -> Fit:
    """The posterior for one set of hyperparameters.

    ``flat=True`` is the fuse fit: no surface, no residual prior, an unanchored ring profile
    floored toward the field median and an anchored one's weight floored, at
    `overall_ranking.UNANCHORED_VARIANCE`. Otherwise every profile has the proper prior
    ``θ ~ N(f(x), s²)`` (or the broad `FREE_PROFILE_SD` one off the surface), which is what
    makes the marginal likelihood, and so empirical Bayes, well defined.
    ``x`` (from `lever_basis`) switches the surface on; without it ``f`` is a constant.
    """
    from .overall_ranking import UNANCHORED_VARIANCE, _median

    floor = 1.0 / UNANCHORED_VARIANCE
    vals = [float(a["overall"]) for a in anchors if a.get("overall") is not None]
    centre = float(_median(vals) or 0.0)

    ring: dict[str, int] = {}
    for r in rounds:
        for fp in (r["a"], r["b"]):
            if fp not in ring:
                ring[fp] = len(ring)
    anchored = [a for a in anchors if a.get("overall") is not None]
    simple: dict[str, int] = {}
    for a in anchored:
        if a["fingerprint"] not in ring and a["fingerprint"] not in simple:
            simple[a["fingerprint"]] = len(simple)

    epoch_ids = sorted({int(a.get("epoch") or 0) for a in anchored}) if epochs else []
    ref_epoch = epoch_ids[-1] if epoch_ids else 0
    free_epochs = [e for e in epoch_ids if e != ref_epoch]
    n_terms = len(next((v for v in (x or {}).values() if v is not None), []) or [])
    use_surface = (not flat) and x is not None and n_terms > 0
    n_beta = 0 if flat else 1 + (n_terms if use_surface else 0)
    r_n = len(ring)
    beta_at = r_n
    c_at = {e: r_n + n_beta + k for k, e in enumerate(free_epochs)}
    m = r_n + n_beta + len(free_epochs)

    S = [[0.0] * m for _ in range(m)]
    hD = [0.0] * m
    sd: dict[str, float] = {}          # simple var -> diagonal precision
    sg: dict[str, dict[int, float]] = {}  # simple var -> coupling to dense tail
    sh: dict[str, float] = {}          # simple var -> information
    quad = 0.0       # Σ w y² over the data
    logw = 0.0       # Σ log w over the data
    count = 0

    def add_theta(fp: str, w: float, mean: float, epoch_index: int | None) -> None:
        """A Gaussian reading ``θ_fp (+ c_e) ≈ mean`` with weight ``w``."""
        if fp in ring:
            i = ring[fp]
            S[i][i] += w
            hD[i] += w * mean
            if epoch_index is not None:
                S[i][epoch_index] += w
                S[epoch_index][i] += w
        else:
            sd[fp] = sd.get(fp, 0.0) + w
            sh[fp] = sh.get(fp, 0.0) + w * mean
            if epoch_index is not None:
                g = sg.setdefault(fp, {})
                g[epoch_index] = g.get(epoch_index, 0.0) + w
        if epoch_index is not None:
            S[epoch_index][epoch_index] += w
            hD[epoch_index] += w * mean

    w_round = 1.0 / (sigma_round ** 2)
    for r in rounds:
        i, j, d = ring[r["a"]], ring[r["b"]], float(r["margin"])
        S[i][i] += w_round
        S[j][j] += w_round
        S[i][j] -= w_round
        S[j][i] -= w_round
        hD[i] -= w_round * d
        hD[j] += w_round * d
        quad += w_round * d * d
        logw += math.log(w_round)
        count += 1

    anchored_fp: set[str] = set()
    for a in anchored:
        fp = a["fingerprint"]
        var = float(a.get("se") or 0.0) ** 2 + tau ** 2
        y = float(a["overall"]) - centre
        if flat:
            # As `fuse`: a ring profile's anchor is scaled and floored; a never-fought
            # profile's is the anchor itself.
            w = max(anchor_scale / var, floor) if fp in ring else 1.0 / var
        else:
            w = 1.0 / var
        e = int(a.get("epoch") or 0)
        add_theta(fp, w, y, c_at.get(e) if epochs else None)
        quad += w * y * y
        logw += math.log(w)
        count += 1
        anchored_fp.add(fp)

    log_prior = 0.0  # log |P0|
    if flat:
        for fp in ring:
            if fp not in anchored_fp:
                add_theta(fp, floor, 0.0, None)  # toward the field median, as `fuse`
    else:
        prec_s = 1.0 / (s * s)
        for fp in list(ring) + list(simple):
            row = (x or {}).get(fp) if use_surface else []
            if use_surface and row is None:
                w = 1.0 / (FREE_PROFILE_SD ** 2)
                add_theta(fp, w, 0.0, None)
                log_prior += math.log(w)
                continue
            # (θ − x̃ᵀβ)²/s² with x̃ = [1, x...]
            xt = [1.0] + list(row or [])
            log_prior += math.log(prec_s)
            if fp in ring:
                i = ring[fp]
                S[i][i] += prec_s
                for k, xv in enumerate(xt):
                    S[i][beta_at + k] -= prec_s * xv
                    S[beta_at + k][i] -= prec_s * xv
            else:
                sd[fp] = sd.get(fp, 0.0) + prec_s
                g = sg.setdefault(fp, {})
                for k, xv in enumerate(xt):
                    g[beta_at + k] = g.get(beta_at + k, 0.0) - prec_s * xv
            for k, xk in enumerate(xt):
                for l, xl in enumerate(xt):
                    S[beta_at + k][beta_at + l] += prec_s * xk * xl
        for k in range(n_beta):
            w = 1.0 / (INTERCEPT_SD ** 2) if k == 0 else 1.0 / (lam * lam)
            S[beta_at + k][beta_at + k] += w
            log_prior += math.log(w)
    for e, idx in c_at.items():
        w = 1.0 / (kappa * kappa)
        S[idx][idx] += w
        log_prior += math.log(w)

    # Eliminate every profile the ring never fought: each touches only the tail.
    h_data = list(hD)  # before elimination, for the evidence's hᵀẑ
    logdet = 0.0
    for fp, d in sd.items():
        g = sg.get(fp) or {}
        hk = sh.get(fp, 0.0)
        logdet += math.log(d)
        items = list(g.items())
        for j, gj in items:
            hD[j] -= gj * hk / d
            for l, gl in items:
                S[j][l] -= gj * gl / d

    cov: list[list[float]] = []
    z: list[float] = []
    if m:
        L = _cholesky(S)
        logdet += 2.0 * sum(math.log(L[i][i]) for i in range(m))
        cov = _inverse(L)
        z = [sum(cov[i][k] * hD[k] for k in range(m)) for i in range(m)]

    theta: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for r in rounds:
        counts[r["a"]] = counts.get(r["a"], 0) + 1
        counts[r["b"]] = counts.get(r["b"], 0) + 1
    for fp, i in ring.items():
        theta[fp] = {"theta": z[i] + centre, "se": math.sqrt(max(0.0, cov[i][i])),
                     "rounds": counts.get(fp, 0), "anchored": fp in anchored_fp}
    for fp, d in sd.items():
        g = sg.get(fp) or {}
        mean = (sh.get(fp, 0.0) - sum(gv * z[j] for j, gv in g.items())) / d
        var = 1.0 / d + sum(gj * cov[j][l] * gl for j, gj in g.items() for l, gl in g.items()) / (d * d)
        theta[fp] = {"theta": mean + centre, "se": math.sqrt(max(0.0, var)),
                     "rounds": 0, "anchored": True}

    pair_cov: dict[tuple[str, str], float] = {}
    if want_cov:
        fps = list(ring)
        for i, a in enumerate(fps):
            for b in fps[i + 1:]:
                pair_cov[(a, b)] = cov[ring[a]][ring[b]]

    offsets = {ref_epoch: {"offset": 0.0, "se": 0.0, "reference": True}} if epoch_ids else {}
    for e, idx in c_at.items():
        offsets[e] = {"offset": z[idx], "se": math.sqrt(max(0.0, cov[idx][idx])), "reference": False}

    beta: list[dict] = []
    beta_cov: list[list[float]] = []
    if n_beta:
        for k in range(n_beta):
            beta.append({
                "term": "intercept" if k == 0 else f"term_{k}",
                "mean": z[beta_at + k] + (centre if k == 0 else 0.0),
                "sd": math.sqrt(max(0.0, cov[beta_at + k][beta_at + k])),
            })
        beta_cov = [[cov[beta_at + k][beta_at + l] for l in range(n_beta)] for k in range(n_beta)]

    log_ev = None
    if not flat:
        hz = sum(h_data[i] * z[i] for i in range(m)) + sum(
            sh.get(fp, 0.0) * (theta[fp]["theta"] - centre) for fp in sd
        )
        log_ev = 0.5 * (-quad + hz + log_prior - logdet + logw - count * math.log(2.0 * math.pi))
    return Fit(
        theta=theta, offsets=offsets, beta=beta, log_evidence=log_ev,
        hyper={"tau": tau, "s": None if flat else s, "lam": lam if use_surface else None,
               "sigma_round": sigma_round, "kappa": kappa if free_epochs else None},
        centre=centre, _cov=pair_cov, _beta_cov=beta_cov,
    )


def fit_flat(pooled: dict[str, dict], rounds: list[dict], sigma_round: float, tau: float,
             *, anchor_scale: float = 1.0) -> Fit:
    """`overall_ranking.fuse`'s fit, as this layer computes it: ``f ≡ 0``, one epoch."""
    anchors = [
        {"fingerprint": fp, "epoch": 0, "overall": p.get("overall"), "se": p.get("se")}
        for fp, p in pooled.items() if p.get("overall") is not None
    ]
    return solve(anchors, rounds, sigma_round, tau=tau, flat=True,
                 anchor_scale=anchor_scale, epochs=False, want_cov=True)


def empirical_bayes(
    anchors: list[dict], rounds: list[dict], sigma_round: float, *,
    epochs: bool, x: dict | None, want_cov: bool = False,
) -> Fit:
    """The fit at the hyperparameters the evidence itself prefers: ``s``, ``τ`` and (with a
    surface) the lever prior scale ``λ``, each chosen on a log grid by coordinate ascent of
    the marginal likelihood, two passes."""
    grids = {"s": S_GRID, "tau": TAU_GRID}
    if x is not None:
        grids["lam"] = LAMBDA_GRID
    cur = {"s": 2.0, "tau": 0.5, "lam": 2.0}
    seen: dict[tuple, float] = {}

    def score(h: dict) -> float:
        key = (h["s"], h["tau"], h["lam"])
        if key not in seen:
            try:
                f = solve(anchors, rounds, sigma_round, tau=h["tau"], epochs=epochs, x=x,
                          s=h["s"], lam=h["lam"])
                seen[key] = float(f.log_evidence)
            except (ValueError, ZeroDivisionError):
                seen[key] = -math.inf
        return seen[key]

    for _ in range(2):
        for name, grid in grids.items():
            best = max(grid, key=lambda v: score({**cur, name: v}))
            cur[name] = best
    fit = solve(anchors, rounds, sigma_round, tau=cur["tau"], epochs=epochs, x=x,
                s=cur["s"], lam=cur["lam"], want_cov=want_cov)
    fit.hyper["evaluated"] = len(seen)
    return fit


def predict(fit: Fit, row: list[float]) -> dict | None:
    """θ for a setting nobody has run: the surface at ``row`` (`coordinates`), with the
    band from the surface's own uncertainty and the profile residual ``s``."""
    if not fit.beta or len(fit.beta) != 1 + len(row):
        return None
    xt = [1.0] + list(row)
    mean = sum(b["mean"] * xv for b, xv in zip(fit.beta, xt))
    var = sum(xt[k] * fit._beta_cov[k][l] * xt[l] for k in range(len(xt)) for l in range(len(xt)))
    s = float(fit.hyper.get("s") or 0.0)
    return {"theta": mean, "se": math.sqrt(max(0.0, var + s * s))}


# ── the backtest ────────────────────────────────────────────────────────────────────────


def backtest(
    store: RunStore, rounds: list[dict], sessions_data: list[dict], sigma_round: float,
    basis: dict | None,
) -> dict | None:
    """Which model predicts the next night's ring margins best? Leave-one-session-out.

    For each of the newest `overall_ranking.BACKTEST_SESSIONS` sessions, every model is
    refitted without that session: its rounds, **and its runs** (a duel's legs land in the
    pooled record too, so leaving only the rounds out would let the pooled anchors see the
    night being predicted). τ, σ and the hyperparameters are re-read per fold. Each held-out
    pair's mean margin is predicted by:

    * ``pooled``: ``μ_b − μ_a`` over the whole record,
    * ``fuse``: today's fit (``f ≡ 0``, one epoch, τ from `pooled_slack`),
    * ``epochs``: per-epoch anchors with offsets, no surface,
    * ``levers``: the quantum surface, one epoch,
    * ``beliefs``: both.

    Scored on the pairs every model can predict, round-weighted mean absolute error.
    """
    from . import decidability
    from .overall_ranking import (
        BACKTEST_CLOSE_ABS, BACKTEST_CLOSE_REL, BACKTEST_SESSIONS, pair_summaries, pooled_slack,
    )

    by_session: dict = {}
    for r in rounds:
        by_session.setdefault(r["session_id"], []).append(r)
    ids = sorted(by_session, key=lambda s: (s is None, -(s or 0)))[:BACKTEST_SESSIONS]
    if len(ids) < 2:
        return None
    x = basis["x"] if basis and basis.get("terms") else None
    err = {k: 0.0 for k in ARMS}
    weight = scored = folds = 0
    chosen: list[dict] = []
    for sid in ids:
        train = [r for r in rounds if r["session_id"] != sid]
        if not train:
            continue
        sigma_f = sigma_round
        noise_f = decidability.round_noise([s for s in sessions_data if s.get("id") != sid])
        if noise_f and noise_f.get("sigma"):
            sigma_f = float(noise_f["sigma"])
        drop = f"duel-{sid}"
        one = store.anchors(epochs=False, drop_group=drop)
        per = store.anchors(epochs=True, drop_group=drop)
        pooled = {a["fingerprint"]: a for a in one}
        tau_f = float(pooled_slack(pooled, pair_summaries(train), sigma_f)["tau"])
        try:
            fits = {
                "fuse": fit_flat(pooled, train, sigma_f, tau_f),
                "epochs": empirical_bayes(per, train, sigma_f, epochs=True, x=None),
                "levers": empirical_bayes(one, train, sigma_f, epochs=False, x=x) if x else None,
                "beliefs": empirical_bayes(per, train, sigma_f, epochs=True, x=x) if x else None,
            }
        except ValueError:
            log.debug("Belief backtest: a fold could not be solved", exc_info=True)
            continue
        if fits["levers"] is None:
            # No surface to fit: the arm is the constant-f model on one epoch.
            fits["levers"] = empirical_bayes(one, train, sigma_f, epochs=False, x=None)
        if fits["beliefs"] is None:
            fits["beliefs"] = fits["epochs"]
        folds += 1
        chosen.append({k: v.hyper for k, v in fits.items()})
        for (a, b), s in pair_summaries(by_session[sid]).items():
            pa, pb = pooled.get(a), pooled.get(b)
            if not pa or not pb:
                continue
            preds = {"pooled": float(pb["overall"]) - float(pa["overall"])}
            for k, f in fits.items():
                preds[k] = f.diff(a, b)
            if any(v is None for v in preds.values()):
                continue
            n = int(s["n"])
            for k, v in preds.items():
                err[k] += n * abs(v - float(s["mean"]))
            weight += n
            scored += 1
    if not weight:
        return None
    mae = {k: round(v / weight, 3) for k, v in err.items()}
    gap = mae["beliefs"] - mae["fuse"]
    if gap < 0 and -gap > max(BACKTEST_CLOSE_ABS, BACKTEST_CLOSE_REL * mae["fuse"]):
        standing = "better"
    elif abs(gap) <= max(BACKTEST_CLOSE_ABS, BACKTEST_CLOSE_REL * mae["fuse"]):
        standing = "level"
    else:
        standing = "worse"
    sentence = (
        f"Over the last {folds} ring sessions, predicting each night's margins from the "
        f"other nights: today's fit missed by {mae['fuse']:.2f} points a match, the belief "
        f"model by {mae['beliefs']:.2f} (epochs alone {mae['epochs']:.2f}, the quantum "
        f"surface alone {mae['levers']:.2f}, pooled medians alone {mae['pooled']:.2f})."
    )
    sentence += {
        "better": " The belief model predicts the ring better; the crown can move to it through a publish.",
        "level": " The two are level within the check's own noise, so the crown stays on today's fit.",
        "worse": " Today's fit predicts the ring better, so the crown stays on it.",
    }[standing]
    return {
        "sessions": folds, "matches": scored, "rounds": weight, "mae": mae,
        "best": min(mae, key=mae.get), "standing": standing, "sentence": sentence,
        "hyper_per_fold": chosen,
    }


# ── the read ────────────────────────────────────────────────────────────────────────────


def _load(session) -> dict:
    """Everything the model reads, from the stores the crown already reads."""
    from sqlalchemy import select

    from . import decidability, evidence
    from .config_store import get_config
    from .duel import _ledger_sessions
    from .methodology import ensure_current_methodology, overall_metrics, overall_weights
    from .models import Run, Score
    from .overall_ranking import FALLBACK_ROUND_SIGMA, ring_rounds
    from .profile_aggregates import _comparable_runs
    from .refresh import list_profiles
    from .settings_profile import is_sqm_off_profile

    methodology = ensure_current_methodology(session, get_config(session))
    version = methodology.version
    definition = methodology.definition or {}
    crown, required = overall_metrics(definition)
    weights = overall_weights(definition)
    boundaries = evidence.instrument_epochs(session)["times"]
    rows = session.execute(
        select(Run.settings_fingerprint, Run.created_at, Run.iterations, Score.subscores, Run.job_group)
        .join(Score, Score.run_id == Run.id)
        .where(*_comparable_runs(version))
    ).all()
    store = RunStore.build([tuple(r) for r in rows], boundaries, crown, required, weights)
    profiles = list_profiles(session)
    for p in profiles:
        p["is_sqm_off"] = is_sqm_off_profile(p["fingerprint"], p.get("settings"))
    sessions_data = _ledger_sessions(session, 200)
    rr = ring_rounds(sessions_data, version)
    noise = decidability.round_noise(sessions_data)
    sigma = float(noise["sigma"]) if noise and noise.get("sigma") else FALLBACK_ROUND_SIGMA
    return {
        "version": version, "store": store, "profiles": profiles, "rounds": rr["rounds"],
        "sessions_data": sessions_data, "sigma": sigma,
        "boundaries": [t.date().isoformat() for t in boundaries],
    }


def beliefs(session, *, with_backtest: bool = True, refresh: bool = False) -> dict:
    """The belief model fitted on everything, beside today's fit, with the backtest that
    says which predicts the ring better. Read-only and cached; the crown does not read it."""
    key = ("beliefs", bool(with_backtest))
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not refresh and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    started = time.monotonic()
    data = _load(session)
    store, rounds, sigma = data["store"], data["rounds"], data["sigma"]
    basis = lever_basis(data["profiles"])
    x = basis["x"] if basis["terms"] else None
    per = store.anchors(epochs=True)
    fit = empirical_bayes(per, rounds, sigma, epochs=True, x=x, want_cov=True)
    names = {p["fingerprint"]: p.get("name") or p.get("label") for p in data["profiles"]}
    epochs_runs: dict[str, set] = {}
    for a in per:
        epochs_runs.setdefault(a["fingerprint"], set()).add(a["epoch"])
    ranked = sorted(fit.theta.items(), key=lambda kv: -kv[1]["theta"])
    profiles = [
        {
            "fingerprint": fp, "name": names.get(fp),
            "theta": round(t["theta"], 2), "se": round(t["se"], 2),
            "rounds": t["rounds"], "epochs": sorted(epochs_runs.get(fp, ())),
            "on_surface": (x or {}).get(fp) is not None,
        }
        for fp, t in ranked
    ]
    terms = ["intercept"] + list(basis["terms"])
    surface = [
        {"term": terms[k] if k < len(terms) else b["term"], "mean": round(b["mean"], 3), "sd": round(b["sd"], 3)}
        for k, b in enumerate(fit.beta)
    ]
    out = {
        "methodology": data["version"],
        "model": "θ = f(quantum) + u; ring margins and per-epoch pooled medians, empirical Bayes",
        "hyper": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in fit.hyper.items()},
        "epochs": {
            "boundaries": data["boundaries"],
            "offsets": {
                str(e): {"offset": round(o["offset"], 2), "se": round(o["se"], 2), "reference": o["reference"]}
                for e, o in sorted(fit.offsets.items())
            },
        },
        "surface": {"pipes": basis["pipes"], "spans": basis["spans"], "terms": surface},
        "profiles": profiles,
        "crown_reads": "fuse",
        "backtest": None,
    }
    if with_backtest:
        try:
            out["backtest"] = backtest(store, rounds, data["sessions_data"], sigma, basis)
        except Exception:  # noqa: BLE001 — the check is a report, never a reason the read fails
            log.warning("Belief backtest failed", exc_info=True)
    out["seconds"] = round(time.monotonic() - started, 2)
    with _cache_lock:
        _cache[key] = (time.monotonic(), out)
    return out


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


__all__ = [
    "ARMS", "Fit", "RunStore", "backtest", "beliefs", "clear_cache", "coordinates",
    "empirical_bayes", "fit_flat", "lever_basis", "predict", "solve",
]
