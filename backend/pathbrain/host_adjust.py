"""Host-adjusted duel margins: take the measuring machine's own speed out of a round.

The research export (phase 2 of ``docs/plan-learning-engine.md``) showed where the ring's
round noise comes from. Two legs of the same profile a couple of legs apart differ by
~0.9 Overall points; regressing that difference on the change in the **host's own
readings** between the two legs — browser context setup, context close, the timing reads
and render-to-first-paint (``instrument_health.HOST_QUANTITIES``), none of which the shaper
can move — explained **33% of it on held-out pairs**, while the network weather covariates
explained 5%. Every night from 07:00 UTC the host runs ~30% slower for five hours and every
profile loses 3-4 points with it; adjacent legs share most of that, but not all, and what
they don't share lands in the margin as noise.

So a round's margin is adjusted the standard way (a regression adjustment on a covariate
the treatment cannot move, "CUPED"):

    adjusted = raw − β · (x_challenger − mean(x_belt legs))

where ``x`` is a leg's log host readings and ``β`` is fitted on the ledger's own pure-noise
pairs — two legs of the **same** profile within :data:`MAX_LEG_GAP` legs of each other in
one session, whose true difference is exactly zero, so everything the covariates explain
there is noise by construction. Because the covariate is immune to the profile, the
adjustment cannot remove a real edge; it only removes the part of the margin the machine
put there. And it is **fitted, then checked**: β is used only when it reduces held-out
variance by at least :data:`MIN_GAIN` on :data:`MIN_PAIRS` or more pairs, otherwise margins
are left raw and the record says why. A round whose legs lack a reading is left raw too —
never a guessed covariate.

The raw margin is kept beside the adjusted one on every record (``raw_deltas``), so the
adjustment is auditable and reversible: every downstream reader (the stopping rules, the
ratings fit, the fused ranking, decidability's round noise) consumes the adjusted series,
and the raw one is there to prove what it changed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import select

from .instrument_health import HOST_QUANTITIES, readings_from_results
from .logging_config import get_logger

log = get_logger("host_adjust")

#: The host readings, in a fixed order (the covariate vector).
KEYS: tuple[str, ...] = tuple(HOST_QUANTITIES)
#: Same-profile legs at most this many legs apart form a noise pair. The ring's belt recurs
#: every ``belt_every`` (2-3) legs; further apart the pair starts carrying drift the margin
#: itself never sees.
MAX_LEG_GAP = 4
#: Fewer noise pairs than this and β is not trusted.
MIN_PAIRS = 60
#: β is used only if it removes at least this share of held-out variance.
MIN_GAIN = 0.05
#: A log-ratio is clipped to ±this, so one wedged phase (a 10 s timing read) cannot steer β.
CLIP = 1.0
#: Ridge penalty on the normal equations — keeps a collinear set of phases well-posed.
RIDGE = 1e-3
#: Duel sessions the noise pairs are read from (newest first).
SESSIONS = 60


def _log_vector(readings: dict | None) -> list[float] | None:
    """The covariate vector of one leg: log of each host reading, or None if any is missing."""
    if not readings:
        return None
    out = []
    for k in KEYS:
        v = readings.get(k)
        if not isinstance(v, (int, float)) or v <= 0:
            return None
        out.append(math.log(float(v)))
    return out


def _diff(a: list[float], b: list[float]) -> list[float]:
    return [max(-CLIP, min(CLIP, x - y)) for x, y in zip(a, b)]


def _solve(xtx: list[list[float]], xty: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting on a small dense system."""
    n = len(xty)
    m = [row[:] + [xty[i]] for i, row in enumerate(xtx)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit_beta(xs: list[list[float]], ys: list[float]) -> list[float] | None:
    """Ridge least squares through the origin (a noise pair has no intercept: swapping its
    legs flips both sides)."""
    if not xs:
        return None
    n = len(xs[0])
    xtx = [[sum(x[i] * x[j] for x in xs) + (RIDGE * len(xs) if i == j else 0.0) for j in range(n)]
           for i in range(n)]
    xty = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(n)]
    return _solve(xtx, xty)


def _var(v: list[float]) -> float:
    if len(v) < 2:
        return 0.0
    m = sum(v) / len(v)
    return sum((x - m) ** 2 for x in v) / len(v)


def _predict(beta: list[float], x: list[float]) -> float:
    return sum(b * xi for b, xi in zip(beta, x))


@dataclass
class HostModel:
    """A fitted adjustment, or the reason there isn't one (``beta`` None)."""

    beta: list[float] | None
    pairs: int
    gain: float | None
    why: str

    @property
    def active(self) -> bool:
        return self.beta is not None

    def adjust(self, delta: float, leg: list[float] | None, flanks: list[list[float] | None]) -> float | None:
        """The adjusted margin, or None when it cannot be adjusted (no model, or a leg
        without a full set of readings) — the caller then keeps the raw margin."""
        if not self.active or leg is None or not flanks or any(f is None for f in flanks):
            return None
        mean = [sum(f[i] for f in flanks) / len(flanks) for i in range(len(KEYS))]
        return float(delta) - _predict(self.beta, _diff(leg, mean))

    def describe(self) -> dict:
        return {
            "active": self.active,
            "beta": {k: round(b, 3) for k, b in zip(KEYS, self.beta)} if self.beta else None,
            "pairs": self.pairs,
            "heldout_gain": round(self.gain, 3) if self.gain is not None else None,
            "why": self.why,
        }


def evaluate(xs: list[list[float]], ys: list[float]) -> HostModel:
    """Fit β on the noise pairs and keep it only if it earns its place out of sample.

    Two-fold, interleaved (pairs are in time order, so alternating keeps both folds across
    the same nights): fit on one half, measure the variance it removes on the other, and
    the reverse; the gain is the mean of the two. Pure."""
    n = len(ys)
    if n < MIN_PAIRS:
        return HostModel(None, n, None, f"only {n} same-profile leg pairs on record (needs {MIN_PAIRS})")
    gains = []
    for fold in (0, 1):
        tr = [i for i in range(n) if i % 2 != fold]
        te = [i for i in range(n) if i % 2 == fold]
        b = fit_beta([xs[i] for i in tr], [ys[i] for i in tr])
        if b is None:
            return HostModel(None, n, None, "the host readings were too collinear to fit")
        before = _var([ys[i] for i in te])
        after = _var([ys[i] - _predict(b, xs[i]) for i in te])
        gains.append(1.0 - after / before if before > 0 else 0.0)
    gain = sum(gains) / 2
    if gain < MIN_GAIN:
        return HostModel(None, n, gain, (
            f"host readings explained only {gain:.0%} of held-out leg noise (needs {MIN_GAIN:.0%})"
        ))
    beta = fit_beta(xs, ys)
    if beta is None:
        return HostModel(None, n, gain, "the host readings were too collinear to fit")
    return HostModel(beta, n, gain, f"host readings remove {gain:.0%} of held-out leg noise ({n} pairs)")


def run_readings(session, run_ids: list[int]) -> dict[int, list[float]]:
    """``{run_id: log host vector}`` from each run's stamped instrument health."""
    from .models import Run

    out: dict[int, list[float]] = {}
    ids = [i for i in run_ids if i is not None]
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for rid, health in session.execute(
            select(Run.id, Run.instrument_health).where(Run.id.in_(chunk))
        ).all():
            vec = _log_vector((health or {}).get("readings"))
            if vec is not None:
                out[rid] = vec
    return out


def leg_vector(session, run_id: int | None) -> list[float] | None:
    """One leg's covariates: the run's stamped readings, else read from its results."""
    from .models import Run

    if run_id is None:
        return None
    run = session.get(Run, run_id)
    if run is None:
        return None
    vec = _log_vector((run.instrument_health or {}).get("readings"))
    if vec is None:
        vec = _log_vector(readings_from_results(run.results))
    return vec


def noise_pairs(session, methodology_version: str, sessions: int = SESSIONS) -> tuple[list[list[float]], list[float]]:
    """The ledger's pure-noise pairs: two legs of the SAME profile within
    :data:`MAX_LEG_GAP` legs of each other in one session, as ``(Δ log readings, Δ Overall)``."""
    from .models import Duel, Run, Score

    xs: list[list[float]] = []
    ys: list[float] = []
    for (run_ids,) in session.execute(
        select(Duel.run_ids).where(Duel.run_ids.is_not(None)).order_by(Duel.id.desc()).limit(sessions)
    ).all():
        ids = [i for i in (run_ids or []) if isinstance(i, int)]
        if len(ids) < 2:
            continue
        fps = dict(session.execute(select(Run.id, Run.settings_fingerprint).where(Run.id.in_(ids))).all())
        overall = {
            rid: (axes or {}).get("overall")
            for rid, axes in session.execute(
                select(Score.run_id, Score.axis_scores).where(
                    Score.run_id.in_(ids), Score.methodology_version == methodology_version,
                    Score.comparability != "incomparable",
                )
            ).all()
        }
        host = run_readings(session, ids)
        for i, a in enumerate(ids):
            for b in ids[i + 1:i + 1 + MAX_LEG_GAP]:
                if fps.get(a) is None or fps.get(a) != fps.get(b):
                    continue
                oa, ob = overall.get(a), overall.get(b)
                if not isinstance(oa, (int, float)) or not isinstance(ob, (int, float)):
                    continue
                if a not in host or b not in host:
                    continue
                xs.append(_diff(host[b], host[a]))
                ys.append(float(ob) - float(oa))
    return xs, ys


def model_for(session, methodology_version: str) -> HostModel:
    """The adjustment a session should use, best-effort: any failure is no adjustment."""
    try:
        xs, ys = noise_pairs(session, methodology_version)
        model = evaluate(xs, ys)
    except Exception:  # noqa: BLE001 — a failed fit costs the adjustment, never the session
        log.warning("Host adjustment: could not fit; margins stay raw", exc_info=True)
        return HostModel(None, 0, None, "the fit failed; margins left raw")
    log.info("Host adjustment: %s", model.why)
    return model
