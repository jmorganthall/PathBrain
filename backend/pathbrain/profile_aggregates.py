"""The per-profile aggregate layer: read a profile's numbers without re-reading its runs.

PathBrain's governing principle is *derive, don't store* — every verdict re-derives from
immutable raw, which is why a methodology change re-grades history instead of needing it
re-collected. That principle is right about **provenance** and silent about **latency**,
and the two had collapsed into one rule: because every number must *be* derivable, every
number was *re-derived on every request*.

Those are separable, and this module separates them. The rule that decides which side a
computation belongs on:

    Derive-on-read is correct when the input is bounded by the question.
    It is wrong when the input is bounded only by time.

The duel ladder's verdicts (the lineal belt, the Bradley-Terry ratings) are bounded by the
ledger — fifty sessions, however many years pass — so they re-derive on every read and cost
milliseconds. A profile's pooled Overall is bounded by *all history*: the question ("what do
these ~150 profiles score?") never grew, but the scan behind it grew every night, until the
answer took longer than the client would wait.

So the aggregate is materialized, and the discipline that keeps it honest is that it is
**never trusted blind**:

* ``stamps()`` re-reads what every profile's rollup *should* have been built from — newest
  run, run count, iteration total, newest scoring timestamp — in **one grouped query** that
  returns a row per profile rather than a row per run. Against a 120k-run table that is
  ~80ms and ~150 rows, versus ~22s and 120k rows (plus ~840k JSON documents) to recompute.
* A stored row is used only where all four numbers match. Anything else is recomputed from
  that profile's own runs and written back.

The failure mode of a stale row is therefore *slower*, never *wrong* — which is the property
that makes this safe to put under the crown, since the number it feeds decides which profile
gets written to the firewall.

Invalidation is **per profile**, which is the other half of the fix. The field-wide memo in
``routes_settings`` keys on a global stamp (max/count over every run), so a single completed
run invalidates the entire field — and during a duel a run lands every minute, so the cache
was coldest exactly when the system was busiest. Here a run touches exactly one profile's
stamp, so a duel invalidates the two profiles it measured and nothing else.
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import median, quantiles

from sqlalchemy import delete, func, select

from .database import session_scope
from .logging_config import get_logger
from .models import ProfileAggregate, Run, RunStatus, Score

log = get_logger("profile_aggregates")

#: One profile's identity-of-inputs: (newest run id, runs, iterations, newest scored_at).
#: Everything a rollup was built from, in four numbers cheap enough to re-read every time.
Stamp = tuple[int | None, int, int, datetime | None]

_EMPTY: Stamp = (None, 0, 0, None)

#: Fingerprints per ``IN (...)`` clause. Every query here takes a caller-supplied list
#: straight into a bound-parameter list, and SQLite's variable ceiling is a **build-time**
#: constant — 250000 on the build these tests run against, 999 on an older or more
#: conservatively compiled one. At today's ~150 profiles no build comes close, which is
#: precisely why an unchunked query would go unnoticed until some future field crossed
#: whichever limit the deployed build happened to have, and then fail as an opaque driver
#: error with nothing to do with profiles. Chunking makes the size of the field stop being
#: a correctness question at all.
_CHUNK = 500


def _chunks(items: list[str]) -> list[list[str]]:
    return [items[i:i + _CHUNK] for i in range(0, len(items), _CHUNK)] or [[]]


def _comparable_runs(version: str):
    """The runs a profile's rollup is built from: completed, fingerprinted, scored under
    this methodology and not quarantined. One definition, used by both the stamp and the
    recompute — so the cache can never be validated against a different set of rows than it
    was built from."""
    return (
        Run.status == RunStatus.COMPLETE,
        Run.settings_fingerprint.is_not(None),
        Score.methodology_version == version,
        Score.comparability != "incomparable",
    )


def stamps(session, version: str, fingerprints: list[str] | None = None) -> dict[str, Stamp]:
    """``{fingerprint: Stamp}`` — what each profile's rollup *should* be built from, now.

    One grouped query returning a row per profile. This is the whole reason the cache is
    safe to use: verifying is orders of magnitude cheaper than computing, so there is never
    a reason to skip it.
    """
    base = (
        select(
            Run.settings_fingerprint,
            func.max(Run.id),
            func.count(Run.id),
            func.sum(func.coalesce(Run.iterations, 1)),
            func.max(Score.computed_at),
        )
        .join(Score, Score.run_id == Run.id)
        .where(*_comparable_runs(version))
        .group_by(Run.settings_fingerprint)
    )
    queries = (
        [base.where(Run.settings_fingerprint.in_(chunk))
         for chunk in _chunks(list(dict.fromkeys(fingerprints)))]
        if fingerprints else [base]
    )
    out: dict[str, Stamp] = {}
    for q in queries:
        for fp, max_id, runs, iters, scored_at in session.execute(q):
            if fp:
                out[fp] = (max_id, int(runs or 0), int(iters or 0), scored_at)
    return out


def _stored_stamp(row: ProfileAggregate) -> Stamp:
    return (row.source_max_run_id, int(row.source_run_count or 0),
            int(row.iterations or 0), row.source_scored_at)


def _quartiles(values: list[float]) -> tuple[float | None, float | None]:
    """(p25, p75) by exactly the convention ``routes_settings._spread`` already uses —
    ``statistics.quantiles(n=4)``, degenerating to the median on a single sample. Matched
    rather than reinvented so a spread read from the rollup and one computed from the runs
    are the same number, not two conventions that agree most of the time."""
    if not values:
        return None, None
    if len(values) < 2:
        return values[0], values[0]
    q = quantiles(values, n=4)
    return q[0], q[2]


def _recompute(session, version: str, fingerprints: list[str]) -> dict[str, dict]:
    """Aggregate these profiles' runs from the Score rows — the one place that reads runs.

    Scoped to the fingerprints handed in, so a duel that measured two profiles re-reads two
    profiles' runs rather than the field's. The subscores are decoded here (one document per
    run) because a rollup needs **every** metric the run scored, not a known few — but it is
    paid once per profile per new run, not once per read.
    """
    if not fingerprints:
        return {}
    rows = [
        row
        for chunk in _chunks(fingerprints)
        for row in session.execute(
            select(Run.settings_fingerprint, Run.iterations, Run.id, Score.subscores)
            .join(Score, Score.run_id == Run.id)
            .where(*_comparable_runs(version), Run.settings_fingerprint.in_(chunk))
        )
    ]

    samples: dict[str, dict[str, list[float]]] = {}
    totals: dict[str, dict] = {}
    for fp, iterations, run_id, subscores in rows:
        agg = totals.setdefault(fp, {"iterations": 0, "runs": 0, "max_run_id": None})
        agg["iterations"] += int(iterations or 1)
        agg["runs"] += 1
        if agg["max_run_id"] is None or run_id > agg["max_run_id"]:
            agg["max_run_id"] = run_id
        by_metric = samples.setdefault(fp, {})
        for metric, value in (subscores or {}).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            by_metric.setdefault(metric, []).append(float(value))

    out: dict[str, dict] = {}
    for fp in fingerprints:
        by_metric = samples.get(fp) or {}
        agg = totals.get(fp) or {"iterations": 0, "runs": 0, "max_run_id": None}
        metrics = {}
        for metric, values in by_metric.items():
            if not values:
                continue
            p25, p75 = _quartiles(values)
            metrics[metric] = {"n": len(values), "median": median(values), "p25": p25, "p75": p75}
        out[fp] = {
            "metrics": metrics,
            "iterations": agg["iterations"],
            "run_count": agg["runs"],
            "max_run_id": agg["max_run_id"],
        }
    return out


def _persist(version: str, computed: dict[str, dict], fresh_stamps: dict[str, Stamp]) -> None:
    """Write the recomputed rollups back, in their **own** transaction.

    Deliberately a separate ``session_scope``: request sessions come from the read-only
    ``get_session`` dependency, which closes without committing, so a row written on the
    caller's session would evaporate and every request would recompute forever — the same
    trap ``profile_names`` and ``explore_tracker._reconcile`` open their own sessions to
    avoid. Best-effort: this is a cache, and failing to *save* an answer must never fail the
    request that already has it.
    """
    if not computed:
        return
    try:
        with session_scope() as session:
            existing = {
                row.settings_fingerprint: row
                for chunk in _chunks(list(computed))
                for row in session.scalars(
                    select(ProfileAggregate).where(
                        ProfileAggregate.methodology_version == version,
                        ProfileAggregate.settings_fingerprint.in_(chunk),
                    )
                )
            }
            now = datetime.now(timezone.utc)
            for fp, agg in computed.items():
                max_run_id, run_count, iterations, scored_at = fresh_stamps.get(fp, _EMPTY)
                row = existing.get(fp)
                if row is None:
                    row = ProfileAggregate(settings_fingerprint=fp, methodology_version=version)
                    session.add(row)
                row.metrics = agg["metrics"]
                row.iterations = agg["iterations"]
                row.run_count = agg["run_count"]
                row.source_max_run_id = max_run_id
                row.source_run_count = run_count
                row.source_scored_at = scored_at
                row.computed_at = now
    except Exception:  # noqa: BLE001 — a cache write can't be why a read fails
        log.debug("Could not persist profile aggregates", exc_info=True)


def aggregates(
    session, version: str, fingerprints: list[str] | None = None, *, persist: bool = True
) -> dict[str, dict]:
    """``{fingerprint: {metrics, iterations, run_count}}``, verified fresh.

    Reads the stored rollups, checks each against what it *should* have been built from,
    recomputes only the profiles that disagree, and (best-effort) writes those back. A
    caller can therefore treat the result as if it had scanned every run itself, because
    for any profile where that would have given a different answer, it did.
    """
    wanted = [fp for fp in dict.fromkeys(fingerprints) if fp] if fingerprints else None
    fresh = stamps(session, version, wanted)
    if wanted is None:
        wanted = list(fresh)

    stored = {
        row.settings_fingerprint: row
        for chunk in _chunks(wanted)
        for row in session.scalars(
            select(ProfileAggregate).where(
                ProfileAggregate.methodology_version == version,
                ProfileAggregate.settings_fingerprint.in_(chunk),
            )
        )
    }

    out: dict[str, dict] = {}
    stale: list[str] = []
    for fp in wanted:
        want = fresh.get(fp, _EMPTY)
        row = stored.get(fp)
        if want == _EMPTY:
            # No comparable runs at all. Answerable without touching the cache — and a
            # stored row here is a leftover (its runs were re-graded out of comparability),
            # so it must not be served.
            out[fp] = {"metrics": {}, "iterations": 0, "run_count": 0}
            continue
        if row is not None and _stored_stamp(row) == want:
            out[fp] = {
                "metrics": row.metrics or {},
                "iterations": int(row.iterations or 0),
                "run_count": int(row.run_count or 0),
            }
        else:
            stale.append(fp)

    if stale:
        computed = _recompute(session, version, stale)
        for fp, agg in computed.items():
            out[fp] = {
                "metrics": agg["metrics"],
                "iterations": agg["iterations"],
                "run_count": agg["run_count"],
            }
        if persist:
            _persist(version, computed, fresh)
    return out


def medians(
    session, version: str, fingerprints: list[str] | None = None
) -> dict[str, tuple[dict[str, float], int]]:
    """``{fingerprint: ({metric: median}, iterations)}`` — the shape a crown grade wants."""
    return {
        fp: ({m: v["median"] for m, v in (agg["metrics"] or {}).items() if v.get("median") is not None},
             agg["iterations"])
        for fp, agg in aggregates(session, version, fingerprints).items()
    }


def invalidate(fingerprints: list[str] | None = None, version: str | None = None) -> None:
    """Drop stored rollups so the next read rebuilds them.

    Correctness never depends on this — the stamp check catches everything it would, which
    is exactly why it exists as a *tidy-up* rather than a guarantee. It is called on the
    paths that rewrite scores wholesale (re-grade, re-derive, refingerprint) so the rebuild
    happens once at a known moment instead of being discovered profile by profile.
    """
    try:
        with session_scope() as session:
            stmt = delete(ProfileAggregate)
            if version:
                stmt = stmt.where(ProfileAggregate.methodology_version == version)
            if fingerprints:
                for chunk in _chunks(list(fingerprints)):
                    session.execute(
                        stmt.where(ProfileAggregate.settings_fingerprint.in_(chunk))
                    )
                return
            session.execute(stmt)
    except Exception:  # noqa: BLE001 — best-effort tidy-up
        log.debug("Could not invalidate profile aggregates", exc_info=True)
