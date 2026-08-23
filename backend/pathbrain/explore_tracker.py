"""The recommendation ledger: what Explore predicted, what the link actually did, and why.

``explore.py`` proposes. This module remembers the proposal and then *marks its own
homework* — the one thing that decides whether the exploration model is worth reading at
all. A prediction nobody scores is a horoscope: it costs a night of benchmarking either
way, and without a record of how the last ten turned out there is no way to know whether
"predicted 71.2 ± 3.1" means anything.

Two halves, deliberately separated:

* **The claim is stored** (``models.ExploreRecommendation``), written the moment a
  candidate is sent to be measured — before the measurement exists, so it cannot be
  quietly revised afterwards. It records what the model asserted (``predicted`` ±
  ``uncertainty``, the ``upside`` it was ranked on, the ``best_overall`` it was claiming
  to beat) **and what that assertion rested on**: a controlled matched pair, the parent's
  own neighbourhood, or a marginal curve already flagged confounded.
* **The verdict is derived**, never stored — recomputed from the measured field on every
  read, exactly like every other score in PathBrain. A re-grade, or fresh runs on the same
  profile, move the verdict instead of leaving a frozen answer behind.

The payoff isn't the individual row, it's the aggregate: *which kind of evidence actually
predicts*. If matched-pair-priced candidates land inside their band and marginal-curve ones
consistently overshoot, that is a measured statement about the model's own failure mode —
the same claim ``CONFOUNDED_SHRINK`` was written on a hunch to handle, now with a number
behind it. The evidence bucket is assigned **weakest-link**: a two-lever candidate priced
one leg from a matched pair and the other from a confounded curve is only as trustworthy as
the confounded leg, so that is the bucket it is scored in.

One rule keeps the comparison honest: a prediction is a number on **one rubric's scale**.
Measured under a different methodology it isn't wrong, it's *incomparable* — so a
recommendation made under an older methodology is reported as such and excluded from the
calibration summary rather than graded against a yardstick it never claimed.
"""
from __future__ import annotations

from sqlalchemy import select

from .config_store import get_config
from .database import session_scope
from .logging_config import get_logger
from .methodology import ensure_current_methodology, overall_metrics, overall_weights
from .models import ExploreRecommendation, Run, RunStatus
from .profile_names import names_for

log = get_logger("explore_tracker")

# The default for "Test now": enough iterations to see whether a recommendation is in the
# right neighbourhood, cheap enough to run several in an evening. It is deliberately one
# ``runner.CHUNK_ITERATIONS`` block, so a quick test is a single persisted chunk.
QUICK_ITERATIONS = 5

# How close counts as "the model got it right". The band is the candidate's own stated
# uncertainty — the model is allowed to be wrong by exactly as much as it said it might be
# — with a floor, because a prediction claiming ±0.1 on a link whose runs wobble by more
# than that is claiming a precision the measurement cannot refute either way.
MIN_BAND = 1.0

# Evidence classes, strongest first. The bucket a candidate is scored in is the *weakest*
# of the classes its legs were priced from.
EVIDENCE_ORDER = ["matched_pair", "conditioned", "marginal", "confounded", "unknown"]
EVIDENCE_LABELS = {
    "matched_pair": "controlled matched pair",
    "conditioned": "the parent's own neighbourhood",
    "marginal": "the marginal curve",
    "confounded": "a confounded marginal curve",
    "unknown": "no stated basis",
}
# What the model wrote in ``explore._predict`` → the class it belongs to.
_EVIDENCE_MATCH = [
    ("matched pair", "matched_pair"),
    ("own neighbourhood", "conditioned"),
    ("confounded", "confounded"),
    ("marginal curve", "marginal"),
]


def evidence_kind(notes) -> str:
    """The weakest evidence class among a candidate's per-lever notes.

    Weakest-link, not best-of: a prediction is only as good as its shakiest leg, and
    scoring a part-confounded candidate in the matched-pair bucket would flatter exactly
    the class the shrink factor exists to distrust.
    """
    kinds = set()
    for note in notes or []:
        text = str(note).lower()
        for needle, kind in _EVIDENCE_MATCH:
            if needle in text:
                kinds.add(kind)
                break
    if not kinds:
        return "unknown"
    return max(kinds, key=EVIDENCE_ORDER.index)


def record(
    *,
    fingerprint: str,
    parent_fingerprint: str | None = None,
    parent_overall: float | None = None,
    label: str | None = None,
    summary: str | None = None,
    changes=None,
    evidence=None,
    multi_lever: bool = False,
    predicted: float | None = None,
    uncertainty: float | None = None,
    upside: float | None = None,
    best_overall: float | None = None,
    methodology_version: str | None = None,
    iterations_requested: int = 0,
    baseline_iterations: int = 0,
    profile_test_id: int | None = None,
    note: str | None = None,
) -> int:
    """Write down a claim before it is measured. Returns the recommendation id.

    Opens its **own** session (like ``profile_names``): the caller is a request handler
    whose session comes from the read-only ``get_session`` dependency, which closes without
    committing — a claim written there would evaporate, and an unrecorded claim is exactly
    the state this module exists to end.
    """
    with session_scope() as session:
        rec = ExploreRecommendation(
            fingerprint=fingerprint,
            parent_fingerprint=parent_fingerprint,
            parent_overall=parent_overall,
            label=str(label)[:255] if label else None,
            summary=summary,
            changes=changes,
            evidence=list(evidence or []),
            multi_lever=bool(multi_lever),
            predicted=predicted,
            uncertainty=uncertainty,
            upside=upside,
            best_overall=best_overall,
            methodology_version=methodology_version,
            iterations_requested=int(iterations_requested or 0),
            baseline_iterations=int(baseline_iterations or 0),
            profile_test_id=profile_test_id,
            note=note,
        )
        session.add(rec)
        session.flush()
        rec_id = rec.id
    log.info(
        "Explore recommendation %s recorded: %s (predicted %s, evidence %s)",
        rec_id, fingerprint, predicted, evidence_kind(evidence),
    )
    return rec_id


def _verdict(
    rec: ExploreRecommendation,
    actual: float | None,
    iterations: int,
    min_iterations: int,
    current_version: str,
) -> dict:
    """Grade one claim against what the link actually did.

    Five outcomes, and the two that aren't a grade matter as much as the three that are:
    a claim with no data yet is **pending**, and one made under a different methodology is
    **incomparable** — its number was never a claim about today's scale.
    """
    band = max(float(rec.uncertainty or 0.0), MIN_BAND)
    stale = bool(rec.methodology_version and rec.methodology_version != current_version)
    error = None if (actual is None or rec.predicted is None) else round(actual - rec.predicted, 2)

    if actual is None or iterations <= rec.baseline_iterations:
        verdict = "pending"
    elif stale:
        verdict = "incomparable"
    elif rec.predicted is None:
        verdict = "unscored"
    elif error is not None and error > band:
        verdict = "better"
    elif error is not None and error < -band:
        verdict = "worse"
    else:
        verdict = "on_target"

    return {
        "verdict": verdict,
        "band": round(band, 2),
        "error": error,
        # Provisional while the profile is still short of the confidence bar: the number is
        # real, it just rests on too few iterations to argue with.
        "provisional": verdict not in ("pending", "incomparable") and iterations < min_iterations,
        "stale_methodology": stale,
    }


def _why(rec: ExploreRecommendation, kind: str, graded: dict, actual: float | None,
         iterations: int, min_iterations: int) -> str:
    """One sentence on *why* the data was right or wrong — the point of the whole ledger.

    It joins the two things a bare "off by 6.4" never says: what the prediction was priced
    from, and what that class of evidence is known to do.
    """
    verdict = graded["verdict"]
    basis = EVIDENCE_LABELS.get(kind, kind)
    # The note is a caveat on the measurement itself (e.g. the levers landed on the live
    # profile rather than the proposed parent), so it belongs on every verdict — most of
    # all on the ones that would otherwise read as a clean answer.
    note = f" Note: {rec.note}" if rec.note else ""
    if verdict == "pending":
        return (
            "No comparable runs on this profile yet — the test may still be queued, running, "
            "or its runs may have been quarantined as incomparable." + note
        )
    if verdict == "incomparable":
        return (
            f"Proposed under methodology {rec.methodology_version} and measured under a "
            "different one. The prediction was a number on the old scale, so scoring it "
            "against today's Overall would compare two different yardsticks." + note
        )
    if verdict == "unscored":
        return (
            "No prediction was recorded with this recommendation, so there is nothing to grade."
            + note
        )

    err = graded["error"]
    off = abs(err) if err is not None else 0.0
    lead = (
        f"Predicted {rec.predicted:.1f} ± {graded['band']:.1f} from {basis}; "
        f"measured {actual:.1f} over {iterations} iteration{'' if iterations == 1 else 's'}"
    )
    if verdict == "on_target":
        tail = (
            f" — inside the band ({err:+.1f}). The model priced this move and the controlled "
            "measurement agreed."
            if kind == "matched_pair"
            else f" — inside the band ({err:+.1f}). The estimate held up."
        )
    elif verdict == "worse":
        tail = f" — {off:.1f} below the band."
        if kind == "confounded":
            tail += (
                " It was priced off a curve we had already flagged as confounded: part of that "
                "curve's shape belonged to another lever, and the controlled test is where that "
                "shows up. This is the failure mode the discount exists for — evidently not "
                "discounted enough."
            )
        elif kind == "marginal":
            tail += (
                " A marginal curve averages over profiles that differ in other levers, so it "
                "answers 'how do profiles with this value score?' — not 'what happens if I "
                "change this profile'. Here those had different answers."
            )
        elif kind == "matched_pair":
            tail += (
                " That move was measured directly on a matched pair, so the miss isn't "
                "confounding — either the effect doesn't transfer to this parent (the levers "
                "are coupled), or the pair itself was thin."
            )
        else:
            tail += " The neighbourhood the estimate came from didn't extend this far."
    else:  # better
        tail = f" — {off:.1f} above the band."
        if kind in ("marginal", "confounded"):
            tail += (
                " The curve understated this move; averaged across other levers, this parent's "
                "neighbourhood is better than the field's."
            )
        else:
            tail += " The estimate was conservative — the untested value is genuinely stronger here."
    extra = ""
    if graded["provisional"]:
        extra = (
            f" Provisional: {iterations} of the {min_iterations} iterations that make a profile "
            "confident, so treat the standing as an early reading."
        )
    return lead + tail + extra + note


def _claim_key(row: dict) -> tuple:
    """The identity of a *proposal*, so re-testing one idea doesn't read as new evidence.

    Pressing "Test now" twice on the same candidate writes two claims — which is correct as
    a record of what was run, and wrong as calibration: both resolve to the same profile and
    the same measurement, so counting them separately states the model has been checked twice
    when it has been checked once. Since the whole point of the ledger is an honest count of
    how often the model is right, that inflation is the one error it cannot afford.

    Two claims are the same proposal when they move the same levers to the same values from
    the same parent, under the same methodology, **and resolved to the same measured
    profile**. That last clause matters: two attempts that landed on *different* profiles
    measured different things and stay separate — a disagreement worth seeing, not hiding.
    """
    moves = tuple(sorted(
        (ch.get("key"), ch.get("to"))
        for ch in (row.get("changes") or [])
        if isinstance(ch, dict) and ch.get("key") is not None
    ))
    if not moves:
        # Nothing describes the move (a bare settings test): fall back to the profile itself,
        # and never merge across methodologies — those predictions aren't on one scale.
        return ("profile", row.get("fingerprint"), row.get("methodology_version"))
    return (
        "moves",
        (row.get("parent") or {}).get("fingerprint"),
        moves,
        row.get("methodology_version"),
        row.get("fingerprint"),
    )


def _collapse(rows: list[dict]) -> list[dict]:
    """One row per proposal, carrying how many times it was tested.

    ``rows`` arrives newest-first, so the representative is the most recent claim — the
    model's current belief about that point — and the group's oldest timestamp is kept as
    ``first_proposed_at``. Nothing is discarded: the attempt ids stay on the row, and the
    measurement was never per-attempt anyway (it is the profile's own pooled Overall, so a
    second 5-iteration test shows up as more iterations behind one verdict, which is exactly
    what it is).
    """
    by_key: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in rows:
        key = _claim_key(row)
        head = by_key.get(key)
        if head is None:
            by_key[key] = {
                **row,
                "attempts": 1,
                "attempt_ids": [row["id"]],
                "first_proposed_at": row.get("created_at"),
            }
            order.append(key)
            continue
        head["attempts"] += 1
        head["attempt_ids"].append(row["id"])
        head["first_proposed_at"] = row.get("created_at")  # newest-first, so this ends oldest
        # A note earned by any attempt belongs on the surviving row — most often the
        # "the firewall settled elsewhere" correction, which explains the whole group.
        if row.get("note") and not head.get("note"):
            head["note"] = row["note"]
        if row.get("predicted") is not None and row["predicted"] != head.get("predicted"):
            head.setdefault("other_predictions", []).append(row["predicted"])

    out = [by_key[k] for k in order]
    for row in out:
        if row["attempts"] > 1:
            extra = (
                f" Proposed and tested {row['attempts']} times; the measurement pools every run "
                "on the profile, so it counts once here rather than once per attempt."
            )
            if row.get("other_predictions"):
                seen = ", ".join(f"{p:.1f}" for p in row["other_predictions"])
                extra += f" Earlier attempts predicted {seen}."
            row["why"] = (row.get("why") or "") + extra
    return out


def _summarize(rows: list[dict]) -> dict:
    """Aggregate calibration: is the model's number worth anything, and *which* number?

    Only graded rows count — pending and incomparable ones are excluded rather than
    counted as successes, which is the difference between a calibration statistic and a
    flattering one.
    """
    graded = [r for r in rows if r["verdict"] in ("on_target", "better", "worse")]
    out = {
        "recorded": len(rows),
        "graded": len(graded),
        "pending": sum(1 for r in rows if r["verdict"] == "pending"),
        "incomparable": sum(1 for r in rows if r["verdict"] == "incomparable"),
        "on_target": sum(1 for r in graded if r["verdict"] == "on_target"),
        "better": sum(1 for r in graded if r["verdict"] == "better"),
        "worse": sum(1 for r in graded if r["verdict"] == "worse"),
        "mean_error": None,
        "mean_abs_error": None,
        "hit_rate": None,
        "beat_best": None,
        "beat_best_claimed": None,
        "by_evidence": [],
    }
    if not graded:
        return out
    errors = [r["error"] for r in graded if r["error"] is not None]
    if errors:
        out["mean_error"] = round(sum(errors) / len(errors), 2)
        out["mean_abs_error"] = round(sum(abs(e) for e in errors) / len(errors), 2)
    out["hit_rate"] = round(out["on_target"] / len(graded), 3)

    # The headline claim a candidate is *ranked* on is its upside beating the field's best.
    # Scoring only the ones that made that claim keeps the statistic about the claim.
    claimed = [
        r for r in graded
        if r["best_overall"] is not None and r["upside"] is not None and r["upside"] > r["best_overall"]
    ]
    if claimed:
        out["beat_best_claimed"] = len(claimed)
        out["beat_best"] = sum(1 for r in claimed if r["actual"] > r["best_overall"])

    by_kind: dict[str, list[dict]] = {}
    for r in graded:
        by_kind.setdefault(r["evidence_kind"], []).append(r)
    for kind in EVIDENCE_ORDER:
        group = by_kind.get(kind)
        if not group:
            continue
        errs = [g["error"] for g in group if g["error"] is not None]
        out["by_evidence"].append({
            "kind": kind,
            "label": EVIDENCE_LABELS[kind],
            "graded": len(group),
            "on_target": sum(1 for g in group if g["verdict"] == "on_target"),
            "mean_error": round(sum(errs) / len(errs), 2) if errs else None,
            "mean_abs_error": round(sum(abs(e) for e in errs) / len(errs), 2) if errs else None,
        })
    return out


def _measured_fingerprint(session, rows: list[ExploreRecommendation]) -> dict[int, str]:
    """``{recommendation id: the fingerprint its benchmark ACTUALLY ran under}``.

    The fingerprint recorded when a test starts is a *prediction*: it is
    ``fingerprint(target)``, hashed from the profile we intend to drive the firewall to.
    The fingerprint a run is filed under is ``fingerprint(normalize(discover()))``, read back
    off the firewall while the benchmark runs. Those are two different numbers whenever the
    firewall doesn't land exactly where we asked — a field on a pipe the provider can't write
    (see ``settings_profile.unwritable_diffs``), or a value the firewall echoes back in its
    own representation. Nothing detects it: the apply succeeds, the verify passes, the
    read-before/read-after check sees no drift, and the runs are filed — correctly — under
    the profile that really ran. Only the *claim* points somewhere else, and the
    recommendation reads as having no history at all.

    So the ledger never trusts the prediction. Every chunk of a profile test carries
    ``job_group = "profile_test-<id>"``, and each run's ``settings_fingerprint`` is the very
    column ``compute_profiles`` groups profiles on — so resolving through it makes the
    correlation correct **by construction**, whatever the firewall did.
    """
    by_group = {
        f"profile_test-{r.profile_test_id}": r.id for r in rows if r.profile_test_id is not None
    }
    if not by_group:
        return {}
    found: dict[int, str] = {}
    for group, fp in session.execute(
        select(Run.job_group, Run.settings_fingerprint)
        .where(
            Run.job_group.in_(list(by_group)),
            Run.status == RunStatus.COMPLETE,
            Run.settings_fingerprint.is_not(None),
        )
        .order_by(Run.id)
    ).all():
        # First completed chunk wins: every chunk of one test benchmarks the same applied
        # firewall state, and a later chunk differing would mean mid-test drift, which
        # ``execute_run`` fails the run for anyway.
        found.setdefault(by_group[group], fp)
    return found


def _reconcile(rows: list[ExploreRecommendation], actual: dict[int, str]) -> None:
    """Correct any recorded fingerprint the benchmark contradicts, and say so on the row.

    Written back rather than corrected on the fly so the profile *link* is right too, and so
    a row that has already been resolved costs nothing on later reads — the same
    resolve-it-once-afterwards pattern ``updates.verify_pending_updates`` uses for an update
    attempt whose outcome only becomes knowable later. Like that one (and like
    ``profile_names``) the write takes its **own** session: the caller's comes from the
    read-only ``get_session`` dependency, which closes without committing, so a correction
    written there would evaporate and every read would redo it. The in-memory rows are
    updated to match, so the response that triggered the fix already reflects it.
    """
    stale = [(rec, fp) for rec in rows if (fp := actual.get(rec.id)) and fp != rec.fingerprint]
    if not stale:
        return
    with session_scope() as session:
        for rec, fp in stale:
            row = session.get(ExploreRecommendation, rec.id)
            if row is None:
                continue
            note = (
                f"The firewall settled on {fp}, not the {row.fingerprint} this was recorded "
                "against — the requested profile was not fully reachable (a field the provider "
                "cannot write, or a value the firewall stores in its own form). Graded against "
                "the profile that actually ran."
            )
            log.warning(
                "Explore recommendation %s: recorded %s but the benchmark ran %s; re-pointing",
                row.id, row.fingerprint, fp,
            )
            row.note = f"{row.note} {note}" if row.note else note
            row.fingerprint = fp
            rec.fingerprint, rec.note = fp, row.note


# How many recorded claims to consider when de-duplicating candidates. Generous — the point
# is not to re-propose something already paid for — but bounded, so the landscape's cost
# doesn't grow without limit as the ledger fills.
CLAIM_HISTORY = 500


def claimed_moves(session, limit: int = CLAIM_HISTORY) -> list[dict]:
    """``[{parent_fingerprint, moves: {axis key: value}}]`` — every proposal already paid for.

    The exploration page proposes *untested* profiles, and it decides what is untested by
    looking at the measured field. That check has a hole a benchmark can fall straight
    through: a proposal the firewall cannot be driven exactly to settles on a neighbour, and
    the runs are filed under **that** profile's coordinates — so the point that was proposed
    never appears in the field, and gets proposed again every time the page is opened.

    The ledger is the record of what was actually attempted, which is the question that
    matters ("have we already spent a night on this?"). A claim stores its parent and the
    levers it moved, so the point it proposed reconstructs exactly: the parent's coordinates
    with those moves applied.
    """
    rows = session.scalars(
        select(ExploreRecommendation)
        .order_by(ExploreRecommendation.id.desc())
        .limit(max(1, limit))
    ).all()
    out: list[dict] = []
    for rec in rows:
        if not rec.parent_fingerprint:
            continue
        moves = {
            ch["key"]: ch["to"]
            for ch in (rec.changes or [])
            if isinstance(ch, dict) and ch.get("key") is not None and ch.get("to") is not None
        }
        if moves:
            out.append({"parent_fingerprint": rec.parent_fingerprint, "moves": moves})
    return out


def recommendations(session, limit: int = 50) -> dict:
    """The ledger with every claim graded against what the link actually did.

    Costs one indexed query for the rows plus **one** batched query for every referenced
    profile's Overall (``crown_follower.profile_overalls``) — deliberately not a
    ``compute_profiles`` pass, so the page can load this without the landscape's cost.
    """
    from .crown_follower import profile_overalls

    rows = session.scalars(
        select(ExploreRecommendation).order_by(ExploreRecommendation.id.desc()).limit(max(1, limit))
    ).all()
    methodology = ensure_current_methodology(session, get_config(session))
    definition = methodology.definition or {}
    crown_metrics, crown_required = overall_metrics(definition)
    weights = overall_weights(definition)
    min_iterations = int(
        (get_config(session).get("correlation", {}) or {}).get("min_iterations", 15) or 15
    )

    # Re-point any row whose benchmark contradicts the fingerprint it was recorded against,
    # BEFORE anything is read off it — otherwise the ledger grades a claim against a profile
    # with no runs and reports it pending forever.
    _reconcile(rows, _measured_fingerprint(session, rows))

    fps = [r.fingerprint for r in rows] + [r.parent_fingerprint for r in rows if r.parent_fingerprint]
    measured: dict = {}
    if fps:
        try:
            measured = profile_overalls(
                session, fps, methodology.version, crown_metrics, crown_required, weights
            )
        except Exception:  # noqa: BLE001 — a scoring hiccup must not empty the ledger
            log.debug("Recommendation ledger: could not compute Overalls", exc_info=True)
    names = names_for(session, [fp for fp in fps if fp]) if fps else {}

    out: list[dict] = []
    for rec in rows:
        actual, iterations = measured.get(rec.fingerprint, (None, 0))
        kind = evidence_kind(rec.evidence)
        graded = _verdict(rec, actual, iterations, min_iterations, methodology.version)
        parent_actual, _ = measured.get(rec.parent_fingerprint or "", (None, 0))
        out.append({
            "id": rec.id,
            "created_at": rec.created_at.isoformat() if rec.created_at else None,
            "fingerprint": rec.fingerprint,
            "name": names.get(rec.fingerprint),
            "label": rec.label,
            "summary": rec.summary,
            "parent": {
                "fingerprint": rec.parent_fingerprint,
                "name": names.get(rec.parent_fingerprint or ""),
                "overall": rec.parent_overall,
                # What the parent scores *now* — a candidate that beat its parent's
                # old number but not its current one moved nothing.
                "overall_now": None if parent_actual is None else round(parent_actual, 2),
            },
            "changes": rec.changes or [],
            "evidence": rec.evidence or [],
            "evidence_kind": kind,
            "evidence_label": EVIDENCE_LABELS.get(kind, kind),
            "multi_lever": bool(rec.multi_lever),
            "predicted": rec.predicted,
            "uncertainty": rec.uncertainty,
            "upside": rec.upside,
            "best_overall": rec.best_overall,
            "methodology_version": rec.methodology_version,
            "iterations_requested": rec.iterations_requested,
            "profile_test_id": rec.profile_test_id,
            "note": rec.note,
            "actual": None if actual is None else round(actual, 2),
            "iterations": iterations,
            **graded,
            "beat_best": (
                None if (actual is None or rec.best_overall is None or graded["verdict"] in ("pending", "incomparable"))
                else bool(actual > rec.best_overall)
            ),
            "beat_parent": (
                None if (actual is None or rec.parent_overall is None or graded["verdict"] in ("pending", "incomparable"))
                else bool(actual > rec.parent_overall)
            ),
        })
        out[-1]["why"] = _why(rec, kind, graded, actual, iterations, min_iterations)

    # One row per proposal. Testing the same candidate twice is two records of what was run
    # and one piece of evidence about the model — the summary must count the latter.
    collapsed = _collapse(out)
    return {
        "recommendations": collapsed,
        "summary": _summarize(collapsed),
        "attempts_recorded": len(out),
        "methodology_version": methodology.version,
        "min_iterations": min_iterations,
        "quick_iterations": QUICK_ITERATIONS,
    }


__all__ = ["record", "recommendations", "claimed_moves", "evidence_kind", "QUICK_ITERATIONS"]
