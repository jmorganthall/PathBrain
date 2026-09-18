"""**One ranking from both kinds of evidence** — the Overall page.

PathBrain has two ways of measuring which profile is best, and until this module they
were two answers with a switch between them.

* The **pooled** Overall: every run a profile ever produced, medianed. Thousands of
  iterations, so its error bar is tiny — and every one of those runs was taken under
  whatever the weather was that hour, with the firewall sitting on one profile for hours
  at a time. Precise, and biased by an amount nobody could read off it.
* The **ring**: a duel round is one leg on profile A and one on profile B back to back, so
  the *difference* between them was measured under the same conditions. Unbiased by
  weather, by construction — and a night produces a handful of rounds, each carrying
  ±1.5 points of noise, so its error bar is wide.

Neither is the truth. Both are measurements of the same thing — each profile's true
Overall — and the right thing to do with two noisy measurements of one quantity is to fit
them **together**, weighted by how much each can be trusted. That is what this does, and
the whole of it is one weighted least-squares problem:

    minimise   Σ_i (θ_i − μ_i)² / (SE_i² + τ²)   +   Σ_rounds (θ_b − θ_a − d)² / σ²

`θ` is the unknown true Overall per profile. The first sum is the pooled anchors: each
profile's median `μ_i` with its own standard error, **widened by τ** — the *pooled slack*,
how far a pooled median can sit from the truth for reasons no amount of iterations fixes
(weather, time of day, the instrument drifting under it). The second sum is every ring
round: the margin `d` (challenger minus reference, in Overall points, already stored on
each match record) against the ring's own measured round noise `σ`
(`decidability.round_noise`).

Three properties fall out of that one line, and the tests pin each:

* **No rounds → the pooled ranking, exactly.** A profile the ring never fought lands on
  its pooled median with its pooled bar widened by τ. Nothing invented.
* **τ is the whole argument, as a number.** At τ = 0 a three-thousand-iteration median
  has a near-zero bar and the ring can never move it (today's pooled crown). At τ → ∞ the
  anchors vanish and only the ring speaks (today's duel champion). The two verdicts the
  user had been choosing between are the two corners of this fit.
* **τ is measured, not chosen.** Where the ring has fought a pair, the pooled difference
  and the ring's difference are two readings of the same gap, and the ring's is unbiased.
  How much they disagree *beyond what both error bars explain* is exactly the bias in the
  pooled medians — `Var(pooled Δ − ring Δ) = SE_a² + SE_b² + σ²/n + 2τ²`, solved for τ
  robustly over every fought pair (`pooled_slack`). It needs no weather covariate and no
  opinion: it is the ledger grading the pooled record. **There is no setting for it**:
  a human weight on the evidence is exactly what this fit replaces, so the page says
  whether τ was measured or defaulted, and nothing lets anyone choose it.

The fit is solved only over the profiles the ring has actually fought (a few dozen);
everyone else is diagonal and drops straight out as pooled-plus-slack, so the linear
algebra is tiny and dependency-free. The inverse of the normal matrix gives every fused
Overall a standard error and every pair a covariance, which is what makes "tied" and
"rounds to separate" real numbers here rather than two opinions.

Read-only: the rollup and the duel ledger in, one ranking out. Nothing here changes a
score. What automation does with the answer is `crowning.resolve`'s decision, and under
the `"fused"` policy that is this module's crown.

**The ring is pointed at this ranking's open question** (`ring_target`, read by
`duel.select_incumbent` / `duel.contender_order` under the fused policy): the fused #1
defends, and the profiles the fit cannot yet separate from it — its `tied` set, most
ambiguous first — are seated before anything else. A round between the #1 and a tied
rival is the one measurement that moves this ranking where it is undecided, so the
ladder spends its nights exactly there; once the tie clears, the ordinary tiers resume.
That is what "the duel arbitrates the Overall" means operationally: pooled seeds the
question, the ring answers it, and the fit reads the answer back.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from . import decidability
from .config_store import get_config
from .logging_config import get_logger
from .methodology import ensure_current_methodology, overall_method
from .settings_profile import SQM_OFF_FINGERPRINT

log = get_logger(__name__)

#: Pooled slack (τ, Overall points) when the ledger holds too few fought pairs to measure
#: it. Half a point: the crown's own practical tie floor (`crown_tie_min_margin`), and the
#: order of the weather-attributable spread a fiber link shows. Stated on the page as a
#: default, never as a measurement.
DEFAULT_SLACK = 0.5

#: Fought pairs needed before the measured slack is trusted over the default. Under this,
#: one odd night's disagreement would set the trust in every pooled median.
MIN_SLACK_PAIRS = 5

#: A round's margin variance when the ledger cannot measure its own noise yet. Wide, so an
#: unmeasured ring never overrules a measured pooled record.
FALLBACK_ROUND_SIGMA = 3.0

#: Ring rounds that make a profile eligible for the crown without pooled confidence.
#: Eight rounds is `duel.streak_to_decide` under the balanced preset — the ring's own bar
#: for a verdict.
MIN_RING_ROUNDS_FOR_CROWN = 8

#: Anchor variance for a profile the ring fought that has no pooled median at all. Flat
#: enough to be a formality (it keeps the normal matrix positive-definite), tight enough
#: that a profile connected to nothing else still has a finite answer.
UNANCHORED_VARIANCE = 400.0

#: `median(z²)` for a centred normal is `0.4549 · Var(z)`; used to read a variance off a
#: median so one enormous margin (a failed leg) cannot set the slack on its own.
MEDIAN_SQ_TO_VAR = 1.0 / 0.4549

#: Past this many head-to-head rounds "rounds to separate" is reported as None — the
#: answer is "they are the same profile for every practical purpose".
MAX_SEPARATING_ROUNDS = 200

#: How many held-out sessions the predictive check scores. Each is one small fit.
BACKTEST_SESSIONS = 25

#: The fit counts as "within noise" of its better parent when its held-out error is within
#: this many points, or this share, of the parent's — the check's own resolution.
BACKTEST_CLOSE_ABS = 0.05
BACKTEST_CLOSE_REL = 0.10

#: Rows listed as tied with the leader; the count is always reported in full.
MAX_TIED_LISTED = 8

#: The biggest rank movements listed under "what the ring changed".
MAX_MOVERS = 6


# ── Inputs ──────────────────────────────────────────────────────────────────────────────


def ring_rounds(sessions_data: list[dict], methodology: str | None) -> dict:
    """Every usable round on the ledger as ``(a, b, margin)`` with ``margin = θ_b − θ_a``.

    ``a`` is the match's reference (the belt), ``b`` the challenger, so the stored
    ``deltas`` (challenger minus reference) are already the right sign. A match fought
    under another methodology is excluded — its margins are Overalls on a different scale
    — and counted; one recorded before the ledger stamped methodologies is kept and
    counted as unstamped, because the alternative is discarding every round from before
    the stamp existed. Aborted matches produced no rounds and contribute nothing.
    """
    rounds: list[dict] = []
    matches = 0
    excluded_methodology = 0
    unstamped = 0
    shifted = 0
    sessions = 0
    for sess in sessions_data or []:
        used_here = False
        for m in sess.get("matchups") or []:
            if not m or not m.get("incumbent") or not m.get("challenger"):
                continue
            from .duel import ABORTED, outcome

            if outcome(m) == ABORTED:
                continue
            deltas = decidability._deltas_of(m)
            if not deltas:
                continue
            fought_under = m.get("methodology")
            if fought_under is None:
                unstamped += 1
            elif methodology is not None and fought_under != methodology:
                excluded_methodology += 1
                continue
            a, b = str(m["incumbent"]), str(m["challenger"])
            if a == b:
                continue
            matches += 1
            used_here = True
            shifts = m.get("weather_shifts") or []
            threshold = m.get("weather_shift_threshold")
            for i, d in enumerate(deltas):
                w = shifts[i] if i < len(shifts) else None
                is_shifted = bool(
                    w is not None and threshold is not None and float(w) >= float(threshold)
                )
                shifted += int(is_shifted)
                rounds.append({
                    "a": a, "b": b, "margin": float(d),
                    "session_id": sess.get("id"), "weather_shifted": is_shifted,
                })
        sessions += int(used_here)
    return {
        "rounds": rounds,
        "matches": matches,
        "sessions": sessions,
        "excluded_methodology": excluded_methodology,
        "unstamped_matches": unstamped,
        "weather_shifted_rounds": shifted,
    }


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def pair_summaries(rounds: list[dict]) -> dict[tuple[str, str], dict]:
    """Per unordered pair: the mean ring margin oriented as ``θ_b − θ_a`` with ``a < b``,
    and how many rounds it rests on."""
    acc: dict[tuple[str, str], list[float]] = {}
    for r in rounds:
        a, b, d = r["a"], r["b"], r["margin"]
        if a > b:
            a, b, d = b, a, -d
        acc.setdefault((a, b), []).append(d)
    return {
        key: {"n": len(v), "mean": sum(v) / len(v)}
        for key, v in acc.items()
    }


def pooled_slack(
    pooled: dict[str, dict], pairs: dict[tuple[str, str], dict], sigma_round: float,
) -> dict:
    """τ, measured: how far pooled medians sit from the truth beyond their own error bars.

    For every pair the ring has fought, the pooled difference ``μ_b − μ_a`` and the ring's
    mean margin are two readings of one gap, and the ring's is unbiased. Their
    disagreement ``z`` has variance ``SE_a² + SE_b² + σ²/n + 2τ²`` — everything but the
    last term is known — so τ is the excess, read robustly (a median of ``z²``, since one
    failed leg's margin would otherwise set it) and never negative.

    Returns ``{tau, pairs, basis, excess_var, expected_var}``; ``basis`` is ``measured``
    or ``default``.
    """
    z2: list[float] = []
    expected: list[float] = []
    for (a, b), s in pairs.items():
        pa, pb = pooled.get(a), pooled.get(b)
        if not pa or not pb or pa.get("overall") is None or pb.get("overall") is None:
            continue
        z = (float(pb["overall"]) - float(pa["overall"])) - float(s["mean"])
        var = (
            (float(pa.get("se") or 0.0) ** 2)
            + (float(pb.get("se") or 0.0) ** 2)
            + (sigma_round ** 2) / max(1, int(s["n"]))
        )
        z2.append(z * z)
        expected.append(var)
    if len(z2) < MIN_SLACK_PAIRS:
        return {
            "tau": DEFAULT_SLACK, "pairs": len(z2), "basis": "default",
            "excess_var": None, "expected_var": None,
            "needed_pairs": MIN_SLACK_PAIRS,
        }
    total_var = float(_median(z2) or 0.0) * MEDIAN_SQ_TO_VAR
    exp_var = float(_median(expected) or 0.0)
    excess = max(0.0, (total_var - exp_var) / 2.0)
    return {
        "tau": round(math.sqrt(excess), 3), "pairs": len(z2), "basis": "measured",
        "excess_var": round(total_var, 4), "expected_var": round(exp_var, 4),
        "needed_pairs": MIN_SLACK_PAIRS,
    }


# ── The fit ─────────────────────────────────────────────────────────────────────────────


def _cholesky_solve_inverse(a: list[list[float]]) -> list[list[float]]:
    """Inverse of a symmetric positive-definite matrix, via Cholesky. Pure Python; the
    matrix is the ring-rated profiles only, so n is a few dozen at most."""
    n = len(a)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = a[i][j] - sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                if s <= 0:
                    raise ValueError("normal matrix is not positive-definite")
                L[i][i] = math.sqrt(s)
            else:
                L[i][j] = s / L[j][j]
    # Inverse column by column: solve L y = e_j, then Lᵀ x = y.
    inv = [[0.0] * n for _ in range(n)]
    for j in range(n):
        y = [0.0] * n
        for i in range(n):
            s = (1.0 if i == j else 0.0) - sum(L[i][k] * y[k] for k in range(i))
            y[i] = s / L[i][i]
        x = [0.0] * n
        for i in range(n - 1, -1, -1):
            s = y[i] - sum(L[k][i] * x[k] for k in range(i + 1, n))
            x[i] = s / L[i][i]
        for i in range(n):
            inv[i][j] = x[i]
    return inv


def fuse(
    pooled: dict[str, dict],
    rounds: list[dict],
    sigma_round: float,
    tau: float,
    *,
    anchor_scale: float = 1.0,
) -> dict[str, dict]:
    """The weighted least-squares fit. ``pooled[fp] = {overall, se}``; ``rounds`` as
    `ring_rounds` returns them. Returns ``{fp: {fused, se, rounds, opponents, anchored,
    cov_index}}`` for every profile in either input, plus the covariance the
    "tied"/"separate" arithmetic needs (``_cov`` on the returned dict's ``__meta__`` key
    is avoided; `fused_field` keeps it beside).

    ``anchor_scale`` multiplies every anchor's weight — 1 is the fit, 0 (with a floor)
    is the ring-only corner the page shows for comparison.
    """
    in_ring: dict[str, int] = {}
    for r in rounds:
        for fp in (r["a"], r["b"]):
            if fp not in in_ring:
                in_ring[fp] = len(in_ring)
    out: dict[str, dict] = {}
    # Diagonal profiles: never fought, so the fit is the anchor itself.
    for fp, p in pooled.items():
        if fp in in_ring or p.get("overall") is None:
            continue
        var = float(p.get("se") or 0.0) ** 2 + tau ** 2
        out[fp] = {
            "fused": float(p["overall"]), "se": math.sqrt(var), "rounds": 0,
            "opponents": 0, "anchored": True, "cov_index": None,
        }
    if not in_ring:
        return out
    n = len(in_ring)
    A = [[0.0] * n for _ in range(n)]
    b = [0.0] * n
    w_round = 1.0 / (sigma_round ** 2)
    counts = {fp: 0 for fp in in_ring}
    opps: dict[str, set[str]] = {fp: set() for fp in in_ring}
    for r in rounds:
        i, j, d = in_ring[r["a"]], in_ring[r["b"]], r["margin"]
        A[i][i] += w_round
        A[j][j] += w_round
        A[i][j] -= w_round
        A[j][i] -= w_round
        b[i] -= w_round * d
        b[j] += w_round * d
        counts[r["a"]] += 1
        counts[r["b"]] += 1
        opps[r["a"]].add(r["b"])
        opps[r["b"]].add(r["a"])
    anchored: dict[str, bool] = {}
    for fp, i in in_ring.items():
        p = pooled.get(fp) or {}
        if p.get("overall") is not None:
            var = float(p.get("se") or 0.0) ** 2 + tau ** 2
            w = anchor_scale / var
            # A vanishing anchor still needs a floor, or a component of the graph with no
            # other anchor is singular.
            w = max(w, 1.0 / UNANCHORED_VARIANCE)
            A[i][i] += w
            b[i] += w * float(p["overall"])
            anchored[fp] = True
        else:
            w = 1.0 / UNANCHORED_VARIANCE
            mean = _median([float(q["overall"]) for q in pooled.values()
                            if q.get("overall") is not None]) or 0.0
            A[i][i] += w
            b[i] += w * mean
            anchored[fp] = False
    cov = _cholesky_solve_inverse(A)
    theta = [sum(cov[i][k] * b[k] for k in range(n)) for i in range(n)]
    for fp, i in in_ring.items():
        out[fp] = {
            "fused": theta[i], "se": math.sqrt(max(0.0, cov[i][i])),
            "rounds": counts[fp], "opponents": len(opps[fp]), "anchored": anchored[fp],
            "cov_index": i,
        }
    out["__cov__"] = cov  # type: ignore[assignment]
    return out


def diff_se(fit: dict, a: str, b: str) -> float | None:
    """Standard error of ``θ_a − θ_b`` under the fit, covariance included."""
    pa, pb = fit.get(a), fit.get(b)
    if not pa or not pb:
        return None
    ia, ib = pa.get("cov_index"), pb.get("cov_index")
    cov = fit.get("__cov__")
    if ia is not None and ib is not None and cov is not None:
        v = cov[ia][ia] + cov[ib][ib] - 2.0 * cov[ia][ib]
    else:
        v = pa["se"] ** 2 + pb["se"] ** 2
    return math.sqrt(max(0.0, v))


def rounds_to_separate(gap: float, se: float, sigma_round: float, tie_sigma: float) -> int | None:
    """Head-to-head rounds that would take the SE of this gap under ``gap / tie_sigma`` if
    the gap holds — each round adds ``1/σ²`` of precision to the difference."""
    if gap <= 0 or se <= 0:
        return None
    target_var = (gap / tie_sigma) ** 2
    if se ** 2 <= target_var:
        return 0
    n = (sigma_round ** 2) * (1.0 / target_var - 1.0 / (se ** 2))
    n = int(math.ceil(n))
    return n if n <= MAX_SEPARATING_ROUNDS else None


# ── The ranking ────────────────────────────────────────────────────────────────────────


def _pooled_field(session, version: str) -> list[dict]:
    from .verdict import _graded_field

    return _graded_field(session, version)


def _live_fingerprint() -> str | None:
    try:
        from .providers import get_provider
        from .settings_profile import fingerprint, normalize

        return fingerprint(normalize(get_provider().discover()))
    except Exception:  # noqa: BLE001 — a firewall that will not answer is not this page's failure
        log.debug("Overall ranking: could not read the live profile", exc_info=True)
        return None


def _name(p: dict) -> str:
    return p.get("name") or p.get("label") or str(p.get("fingerprint", ""))[:8]


def _sentence(best: dict, runner: dict | None, lead: float | None, bar: float | None,
              tied_count: int, need: int | None, slack: dict, moved: bool) -> str:
    who = _name(best)
    out = f"Run {who} — Overall {best['fused']:.1f} ± {best['fused_se']:.2f}."
    if best["rounds"] and best["pooled"] is not None:
        pull = best["fused"] - best["pooled"]
        out += (
            f" It measured {best['pooled']:.1f} on the pooled record over {best['iterations']} "
            f"iterations, and {best['rounds']} head-to-head round{'s' if best['rounds'] != 1 else ''} "
            f"in the ring moved that by {pull:+.2f}."
        )
    elif best["rounds"] == 0:
        out += (
            f" That is its pooled median over {best['iterations']} iterations; the ring has "
            f"never fought it, so nothing head-to-head has tested the claim."
        )
    if lead is None:
        out += " Nothing else is measured well enough to compare it against."
    elif bar is not None and lead > bar:
        out += (
            f" It leads {_name(runner)} by {lead:.2f} points, clear of the ±{bar:.2f} both "
            f"kinds of evidence allow together — a real lead."
        )
    else:
        out += (
            f" Its {lead:.2f}-point lead over {_name(runner)} is inside the ±{bar:.2f} the "
            f"evidence allows, and {tied_count} profile{'s are' if tied_count != 1 else ' is'} "
            f"tied with it."
        )
        if need:
            out += f" About {need} more rounds between the top two would settle it."
        elif need is None and lead > 0:
            out += " More rounds would not settle it within any practical night."
    if moved:
        out += " The ring changed who is on top."
    if slack.get("basis") == "measured":
        out += (
            f" Pooled medians are trusted to ±{slack['tau']:.2f}, measured from "
            f"{slack['pairs']} pairs where the ring and the pooled record read the same gap."
        )
    return out


def ranking(session, *, backtest: bool = True, live: bool = True) -> dict:
    """The fused ranking, the crown it names, and everything a reader needs to see why.

    τ is always the measured slack (or the stated default under `MIN_SLACK_PAIRS`); there
    is deliberately no override. ``backtest=False`` skips the held-out predictive check
    and ``live=False`` the firewall read (the crown follower needs neither — it reads the
    firewall itself).
    """
    cfg = get_config(session)
    corr = cfg.get("correlation") or {}
    min_iterations = int(corr.get("min_iterations") or 15)
    tie_sigma = float(corr.get("crown_tie_sigma") or 2.0)
    min_margin = float(corr.get("crown_tie_min_margin") or 0.0)
    duel_cfg = cfg.get("duel") or {}

    methodology = ensure_current_methodology(session, cfg)
    version = methodology.version
    method = overall_method(methodology.definition or {})

    out: dict = {
        "methodology": version,
        "overall_method": method,
        "min_iterations": min_iterations,
        "tie_sigma": tie_sigma,
        "best": None,
        "runner_up": None,
        "lead": None,
        "noise_bar": None,
        "clear": None,
        "tied": [],
        "tied_count": 0,
        "rounds_to_settle": None,
        "vs_sqm_off": None,
        "sqm_off_overall": None,
        "live": None,
        "on_firewall": None,
        "confident_profiles": 0,
        "verdict": "",
        "profiles": [],
        "inputs": {},
        "corners": {},
        "movers": [],
        "backtest": None,
        "computed_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    }
    if method != "weighted":
        out["verdict"] = (
            f"This methodology ({version}) ranks profiles against each other rather than on "
            f"their own, so pooled medians cannot be read per profile and there is nothing "
            f"for the ring to be fused with. The standings on Settings Impact are the answer."
        )
        return out

    # ── Inputs ─────────────────────────────────────────────────────────────────────
    from .duel import _ledger_sessions, ledger_ratings, ledger_leader, rank_sigma

    field = _pooled_field(session, version)
    pooled = {p["fingerprint"]: p for p in field}
    sessions_data = _ledger_sessions(session, 200)
    rr = ring_rounds(sessions_data, version)
    rounds = rr["rounds"]
    noise = decidability.round_noise(sessions_data)
    sigma_round = float(noise["sigma"]) if noise and noise.get("sigma") else FALLBACK_ROUND_SIGMA
    pairs = pair_summaries(rounds)
    slack = pooled_slack(pooled, pairs, sigma_round)
    tau = float(slack["tau"])

    # ── The fit ───────────────────────────────────────────────────────────────────
    fit = fuse(pooled, rounds, sigma_round, tau)
    ring_only = fuse(pooled, rounds, sigma_round, tau, anchor_scale=0.0)

    rows: list[dict] = []
    for fp, f in fit.items():
        if fp == "__cov__":
            continue
        p = pooled.get(fp) or {}
        rows.append({
            "fingerprint": fp,
            "name": p.get("name"),
            "label": p.get("label"),
            "fused": round(float(f["fused"]), 2),
            "fused_se": round(float(f["se"]), 3),
            "pooled": None if p.get("overall") is None else round(float(p["overall"]), 2),
            "pooled_se": None if p.get("se") is None else round(float(p["se"]), 3),
            "iterations": int(p.get("iterations") or 0),
            "rounds": int(f["rounds"]),
            "opponents": int(f["opponents"]),
            "ring_pull": None if p.get("overall") is None else round(float(f["fused"]) - float(p["overall"]), 2),
            "ring_only": None if fp not in ring_only or not f["rounds"] else round(float(ring_only[fp]["fused"]), 2),
            "confident": int(p.get("iterations") or 0) >= min_iterations,
            "is_sqm_off": fp == SQM_OFF_FINGERPRINT,
        })
    # Names for profiles the ring fought that the field has no row for.
    unnamed = [r["fingerprint"] for r in rows if not r["name"]]
    if unnamed:
        try:
            from .profile_names import names_for

            names = names_for(session, unnamed)
            for r in rows:
                if not r["name"]:
                    r["name"] = names.get(r["fingerprint"])
        except Exception:  # noqa: BLE001 — naming is never a reason the page fails
            log.debug("Overall ranking: could not resolve call signs", exc_info=True)

    # Eligible for the crown: measured enough on either record. SQM off is excluded (the
    # baseline test's supervised job) but supplies the "vs no shaper" reading.
    for r in rows:
        r["eligible"] = (not r["is_sqm_off"]) and (
            r["confident"] or r["rounds"] >= MIN_RING_ROUNDS_FOR_CROWN
        )
    rows.sort(key=lambda r: (r["fused"], r["iterations"], r["rounds"]), reverse=True)
    eligible = [r for r in rows if r["eligible"]]
    pooled_rank = {
        r["fingerprint"]: i + 1
        for i, r in enumerate(sorted(
            [r for r in rows if r["pooled"] is not None and not r["is_sqm_off"]],
            key=lambda r: (r["pooled"], r["iterations"]), reverse=True,
        ))
    }
    fused_rank = {r["fingerprint"]: i + 1 for i, r in enumerate([r for r in rows if not r["is_sqm_off"]])}
    for r in rows:
        r["pooled_rank"] = pooled_rank.get(r["fingerprint"])
        r["fused_rank"] = fused_rank.get(r["fingerprint"])
        r["moved"] = (
            None if r["pooled_rank"] is None or r["fused_rank"] is None
            else r["pooled_rank"] - r["fused_rank"]
        )

    out["confident_profiles"] = len(eligible)
    out["sqm_off_overall"] = next((r["pooled"] for r in rows if r["is_sqm_off"]), None)
    out["inputs"] = {
        "pooled_profiles": len([r for r in rows if r["pooled"] is not None]),
        "pooled_iterations": sum(r["iterations"] for r in rows),
        "ring_rounds": len(rounds),
        "ring_matches": rr["matches"],
        "ring_sessions": rr["sessions"],
        "ring_profiles": len([r for r in rows if r["rounds"]]),
        "excluded_other_methodology": rr["excluded_methodology"],
        "unstamped_matches": rr["unstamped_matches"],
        "weather_shifted_rounds": rr["weather_shifted_rounds"],
        "sigma_round": round(sigma_round, 3),
        "sigma_basis": "measured" if noise and noise.get("sigma") else "fallback",
        "noise": noise,
        "slack": slack,
    }

    # ── The three corners: what each kind of evidence says alone, and together ────
    pooled_best = next(
        (r for r in sorted(
            [r for r in rows if r["confident"] and not r["is_sqm_off"]],
            key=lambda r: (r["pooled"], r["iterations"]), reverse=True,
        )),
        None,
    )
    ratings = ledger_ratings(session)
    ring_best_fp = ledger_leader(ratings, sigma=rank_sigma(duel_cfg))
    ring_best = next((r for r in rows if r["fingerprint"] == ring_best_fp), None)
    best = eligible[0] if eligible else None
    out["corners"] = {
        "pooled": None if pooled_best is None else _corner(pooled_best, "pooled"),
        "ring": None if ring_best is None else {
            **_corner(ring_best, "ring"),
            "rating": (ratings.get(ring_best_fp) or {}).get("rating"),
            "rating_se": (ratings.get(ring_best_fp) or {}).get("rating_se"),
        },
        "fused": None if best is None else _corner(best, "fused"),
        "agree": bool(
            best is not None and pooled_best is not None and ring_best is not None
            and best["fingerprint"] == pooled_best["fingerprint"] == ring_best["fingerprint"]
        ),
    }

    if best is None:
        out["verdict"] = (
            f"No profile has reached {min_iterations} iterations on the pooled record or "
            f"{MIN_RING_ROUNDS_FOR_CROWN} rounds in the ring yet, so there is nothing to crown."
        )
        out["profiles"] = rows
        return out

    runner = eligible[1] if len(eligible) > 1 else None
    lead = None if runner is None else round(best["fused"] - runner["fused"], 2)
    bar = None
    need = None
    if runner is not None:
        se = diff_se(fit, best["fingerprint"], runner["fingerprint"]) or 0.0
        bar = round(max(min_margin, tie_sigma * se), 2)
        need = rounds_to_separate(lead or 0.0, se, sigma_round, tie_sigma)
    tied: list[dict] = []
    for r in eligible[1:]:
        se = diff_se(fit, best["fingerprint"], r["fingerprint"]) or 0.0
        gap = best["fused"] - r["fused"]
        r["tied_with_leader"] = gap <= max(min_margin, tie_sigma * se)
        r["rounds_to_separate"] = rounds_to_separate(gap, se, sigma_round, tie_sigma)
        r["gap_to_leader"] = round(gap, 2)
        r["gap_se"] = round(se, 3)
        if r["tied_with_leader"]:
            tied.append(r)
    best["tied_with_leader"] = False
    best["gap_to_leader"] = 0.0

    live_fp = _live_fingerprint() if live else None
    by_fp = {r["fingerprint"]: r for r in rows}
    sqm_off = out["sqm_off_overall"]
    moved_top = bool(pooled_best is not None and pooled_best["fingerprint"] != best["fingerprint"])
    out.update({
        "best": best,
        "runner_up": runner,
        "lead": lead,
        "noise_bar": bar,
        "clear": None if (lead is None or bar is None) else bool(lead > bar),
        "tied": tied[:MAX_TIED_LISTED],
        "tied_count": len(tied),
        "rounds_to_settle": need,
        "vs_sqm_off": (
            round((best["fused"] - sqm_off) / sqm_off * 100, 1) if sqm_off else None
        ),
        "live": by_fp.get(live_fp) if live_fp else None,
        "on_firewall": None if live_fp is None else bool(live_fp == best["fingerprint"]),
        "verdict": _sentence(best, runner, lead, bar, len(tied), need, slack, moved_top),
        "profiles": rows,
        "movers": sorted(
            [r for r in rows if r["rounds"] and r["moved"]],
            key=lambda r: (abs(r["moved"] or 0), r["rounds"]), reverse=True,
        )[:MAX_MOVERS],
    })
    if backtest:
        try:
            out["backtest"] = predictive_check(pooled, rounds, sigma_round, tau)
        except Exception:  # noqa: BLE001 — a check, never a reason the ranking fails
            log.warning("Overall ranking: the predictive check failed", exc_info=True)
    return out


def _corner(r: dict, kind: str) -> dict:
    return {
        "fingerprint": r["fingerprint"], "name": r["name"], "label": r["label"],
        "kind": kind, "fused": r["fused"], "pooled": r["pooled"],
        "iterations": r["iterations"], "rounds": r["rounds"],
    }


def crown(session) -> dict | None:
    """The profile the fused ranking names, for the crowning policy — ``{fingerprint,
    name, label, fused, fused_se, lead, clear, tied_count}`` or None. No backtest, no
    live read: the follower reads the firewall itself."""
    r = ranking(session, backtest=False, live=False)
    best = r.get("best")
    if not best:
        return None
    return {
        "fingerprint": best["fingerprint"], "name": best.get("name"), "label": best.get("label"),
        "fused": best["fused"], "fused_se": best["fused_se"],
        "lead": r.get("lead"), "clear": r.get("clear"), "tied_count": r.get("tied_count"),
        "computed_at": r.get("computed_at"),
    }


def ring_target(session) -> dict | None:
    """What the ring should fight next, read off this ranking: the fused #1 and every
    profile the fit cannot yet separate from it, most ambiguous first.

    Returns ``{best, best_fused, best_se, lead, noise_bar, clear, rivals: [{fingerprint,
    name, gap, gap_se, z, rounds_to_separate}], tied_count}`` or None when the ranking
    names nobody. ``rivals`` is the ``tied`` set ordered by ``z`` = gap / SE of the gap
    ascending — the profile the fit is *least* able to tell from the #1 first, since one
    round between those two is the measurement that moves this ranking where it is
    undecided. The runner-up is included even when the lead is clear, so the ring keeps
    re-checking the crown's margin rather than going quiet the moment a tie resolves.
    No backtest and no firewall read: this is read before every cycle of a session.

    Only an **anchored** best is a target: a profile the fit places from ring rounds
    alone, with no pooled median at all, has a fused Overall that is relative to its
    opponents and not on the pooled scale, and seating it would hand the ring to a number
    the pooled record has never confirmed. In production every duel leg is also a pooled
    run, so this only bites on a quarantined or foreign ledger — and there it returns
    None, so the ladder falls back to the belt exactly as before.
    """
    r = ranking(session, backtest=False, live=False)
    best = r.get("best")
    if not best or best.get("pooled") is None:
        return None
    eligible = [p for p in r.get("profiles") or [] if p.get("eligible") and p["fingerprint"] != best["fingerprint"]]
    rivals: list[dict] = []
    for p in eligible:
        se = float(p.get("gap_se") or 0.0)
        gap = float(p.get("gap_to_leader") or 0.0)
        tied = bool(p.get("tied_with_leader"))
        if not tied and (r.get("runner_up") or {}).get("fingerprint") != p["fingerprint"]:
            continue
        rivals.append({
            "fingerprint": p["fingerprint"],
            "name": p.get("name"),
            "gap": round(gap, 2),
            "gap_se": round(se, 3),
            "z": (gap / se) if se > 0 else float("inf"),
            "rounds_to_separate": p.get("rounds_to_separate"),
            "tied": tied,
        })
    rivals.sort(key=lambda x: (not x["tied"], x["z"], -(x["rounds_to_separate"] or 0)))
    for x in rivals:
        x["z"] = None if x["z"] == float("inf") else round(x["z"], 2)
    return {
        "best": best["fingerprint"],
        "best_name": best.get("name"),
        "best_fused": best["fused"],
        "best_se": best["fused_se"],
        "lead": r.get("lead"),
        "noise_bar": r.get("noise_bar"),
        "clear": r.get("clear"),
        "tied_count": r.get("tied_count"),
        "rivals": rivals,
    }


# ── The predictive check ───────────────────────────────────────────────────────────────


def predictive_check(pooled: dict[str, dict], rounds: list[dict], sigma_round: float,
                     tau: float) -> dict | None:
    """Which verdict predicts the next night better? Leave-one-session-out.

    For each of the newest `BACKTEST_SESSIONS` sessions with rounds, refit on every other
    session's rounds and predict that session's mean margin per match three ways: the
    pooled record alone (``μ_b − μ_a``), the ring alone, and the fused fit. The score is
    the round-weighted mean absolute error against what the night actually measured.
    Lower is better; a verdict that predicts the ring's own next reading worse than the
    pooled record does is not the one to crown by. Returns None below two sessions.
    """
    by_session: dict = {}
    for r in rounds:
        by_session.setdefault(r["session_id"], []).append(r)
    ids = sorted(by_session, key=lambda s: (s is None, -(s or 0)))[:BACKTEST_SESSIONS]
    if len(ids) < 2:
        return None
    err = {"pooled": 0.0, "ring": 0.0, "fused": 0.0}
    weight = 0
    scored = 0
    for sid in ids:
        held = by_session[sid]
        train = [r for r in rounds if r["session_id"] != sid]
        if not train:
            continue
        fit = fuse(pooled, train, sigma_round, tau)
        ring = fuse(pooled, train, sigma_round, tau, anchor_scale=0.0)
        summaries = pair_summaries(held)
        for (a, b), s in summaries.items():
            pa, pb = pooled.get(a), pooled.get(b)
            if fit.get(a) is None or fit.get(b) is None:
                continue
            n = int(s["n"])
            actual = float(s["mean"])
            preds = {
                "pooled": (None if not pa or not pb or pa.get("overall") is None
                           or pb.get("overall") is None
                           else float(pb["overall"]) - float(pa["overall"])),
                "ring": ring[b]["fused"] - ring[a]["fused"],
                "fused": fit[b]["fused"] - fit[a]["fused"],
            }
            if preds["pooled"] is None:
                continue
            for k, v in preds.items():
                err[k] += n * abs(v - actual)
            weight += n
            scored += 1
    if not weight:
        return None
    mae = {k: round(v / weight, 3) for k, v in err.items()}
    best = min(mae, key=mae.get)
    # A few hundredths between the fit and its better parent is the noise of the check
    # itself, not a finding; only a clear loss is reported as one.
    parent = min(mae["pooled"], mae["ring"])
    if mae["fused"] <= parent:
        standing = "best"
    elif mae["fused"] - parent <= max(BACKTEST_CLOSE_ABS, BACKTEST_CLOSE_REL * parent):
        standing = "close"
    else:
        standing = "worse"
    sentence = (
        f"Over the last {len(ids)} sessions, predicting each night's margins from the "
        f"other nights: pooled alone missed by {mae['pooled']:.2f} points a match, the "
        f"ring alone by {mae['ring']:.2f}, together by {mae['fused']:.2f}."
    )
    if standing == "best":
        sentence += " Fusing the two records predicts the next night better than either alone."
    elif standing == "close":
        sentence += (
            f" The fit is within noise of the {'pooled record' if best == 'pooled' else 'ring'} "
            f"alone here — it loses nothing, and it ranks every profile rather than only "
            f"the ones {'the ring fought' if best == 'ring' else 'with pooled data'}."
        )
    else:
        sentence += (
            f" On this ledger the fit predicts worse than the "
            f"{'pooled record' if best == 'pooled' else 'ring'} alone, so read the crown with "
            f"that in mind; more fought pairs sharpen the slack estimate and this check."
        )
    return {
        "sessions": len(ids), "matches": scored, "rounds": weight,
        "mae": mae, "best": best, "standing": standing,
        "sentence": sentence,
    }


__all__ = [
    "DEFAULT_SLACK", "MIN_SLACK_PAIRS", "MIN_RING_ROUNDS_FOR_CROWN",
    "ring_rounds", "pair_summaries", "pooled_slack", "fuse", "diff_se",
    "rounds_to_separate", "ranking", "crown", "ring_target", "predictive_check",
]
