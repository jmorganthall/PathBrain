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

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from statistics import median, quantiles

from sqlalchemy import delete, func, select

from .database import session_scope
from .logging_config import get_logger
from .methodology import CURRENT_METHODOLOGY, crown_metric_keys, latest_score
from .settings_overrides import effective_fingerprint, overrides_stamp
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
        effective_fingerprint().is_not(None),  # a run's own fingerprint, or its override (S22)
        Score.methodology_version == version,
        Score.comparability != "incomparable",
        latest_score(),
    )


def median_se(iqr: float | None, runs: int | None) -> float | None:
    """Standard error of a profile's median Overall ≈ IQR/√runs — the ONE arithmetic.

    The rollup's quartiles are over **per-run** values (one Score per run), so the sample
    size is the run count, never the iteration total. The standings
    (`routes_settings._overall_se`), the pooled field (`pooled_field.se`) and through it the
    fused ranking's pooled anchors all read this; two of them used to divide the same IQR by
    different counts, which understated a five-iteration-a-run profile's SE by √5 on the card
    and over-trusted its anchor in the fit. ``None`` when the spread or the count is unknown
    (callers choose their own sentinel for that).
    """
    if iqr is None or runs is None or int(runs) < 1:
        return None
    return max(0.0, float(iqr)) / (int(runs) ** 0.5)


def stamps(
    session, version: str, fingerprints: list[str] | None = None, *,
    after: int | None = None, upto: int | None = None,
) -> dict[str, Stamp]:
    """``{fingerprint: Stamp}`` — what each profile's rollup *should* be built from, now.

    One grouped query returning a row per profile. This is the whole reason the cache is
    safe to use: verifying is orders of magnitude cheaper than computing, so there is never
    a reason to skip it. ``after`` and ``upto`` bound it to runs in that id range (what
    :func:`_field_stamps` reads and merges into stamps it already holds).
    """
    fp_col = effective_fingerprint()
    filters = list(_comparable_runs(version))
    if after is not None:
        filters.append(Run.id > after)
    if upto is not None:
        filters.append(Run.id <= upto)
    base = (
        select(
            fp_col,
            func.max(Run.id),
            func.count(Run.id),
            func.sum(func.coalesce(Run.iterations, 1)),
            func.max(Score.computed_at),
        )
        .join(Score, Score.run_id == Run.id)
        .where(*filters)
        .group_by(fp_col)
    )
    queries = (
        [base.where(fp_col.in_(chunk))
         for chunk in _chunks(list(dict.fromkeys(fingerprints)))]
        if fingerprints else [base]
    )
    out: dict[str, Stamp] = {}
    for q in queries:
        for fp, max_id, runs, iters, scored_at in session.execute(q):
            if fp:
                out[fp] = (max_id, int(runs or 0), int(iters or 0), scored_at)
    return out


# The whole field's stamps, kept between reads. The grouped query behind them scans every
# comparable run, and on the NAS that was most of the fused ranking's pooled step on a warm
# Profiles load (about 4s of 6). They are kept until something under the runs they were read
# from moves, and a new completed run is merged in rather than re-scanned for.
_STAMPS_LOCK = threading.Lock()
_STAMPS_CACHE: dict = {"base": None, "upto": None, "check": None, "stamps": None}

#: A request for at least this many profiles reads through the field's kept stamps; a
#: smaller one (a duel's two profiles, `why`'s one) keeps its own scoped query, which on a
#: miss is far cheaper than reading the field to answer it.
_FIELD_MIN = 20


def invalidate_stamps_cache() -> None:
    """Drop the kept field stamps (the in-place rewrite paths call this with the rest)."""
    with _STAMPS_LOCK:
        _STAMPS_CACHE.update({"base": None, "upto": None, "check": None, "stamps": None})


def newest_score_under(session, upto: int):
    """When the newest Score on a run up to ``upto`` was written. An in-place re-grade moves
    nothing else (`upsert_score` refreshes the at-present row and its ``computed_at``), so
    this is the one thing that tells a cache keyed on ids and counts that its scores moved.
    Walks the ``computed_at`` index from the newest down and stops at the first old run,
    which in practice is past only the scores of runs newer than ``upto``."""
    return session.execute(
        select(Score.computed_at)
        # ``+ 0`` keeps SQLite off the run_id index, which it otherwise picks for this
        # range (nearly every row) and then sorts them all to find the newest.
        .where(Score.run_id + 0 <= upto)
        .order_by(Score.computed_at.desc())
        .limit(1)
    ).scalar_one_or_none()


def _stamps_check(session, upto: int) -> tuple:
    """What must not have moved under the runs up to ``upto`` for kept stamps to stand."""
    complete = session.execute(
        select(func.count(Run.id)).where(Run.status == RunStatus.COMPLETE, Run.id <= upto)
    ).scalar_one()
    scores = tuple(session.execute(
        select(func.max(Score.id), func.count(Score.id)).where(Score.run_id <= upto)
    ).one())
    # The newest run itself: counts alone can't tell a deleted run from a new one that
    # reused its id (SQLite hands out the freed top id again).
    newest = tuple(session.execute(
        select(Run.created_at, effective_fingerprint()).where(Run.id == upto)
    ).one_or_none() or ())
    return (complete, scores, newest_score_under(session, upto), newest)


def _merge(held: dict[str, Stamp], new: dict[str, Stamp]) -> dict[str, Stamp]:
    """Stamps over two disjoint sets of runs, combined: what one grouped query over both
    would have returned (max, count, sum, max)."""
    out = dict(held)
    for fp, (max_id, runs, iters, scored_at) in new.items():
        old = out.get(fp)
        if old is None:
            out[fp] = (max_id, runs, iters, scored_at)
            continue
        ids = [i for i in (old[0], max_id) if i is not None]
        times = [t for t in (old[3], scored_at) if t is not None]
        out[fp] = (max(ids) if ids else None, old[1] + runs, old[2] + iters,
                   max(times) if times else None)
    return out


def _field_stamps(session, version: str) -> dict[str, Stamp]:
    """Every profile's stamp, from the kept copy where nothing under it moved."""
    from .phase_timing import laps

    lap = laps("aggregates")
    # The engine itself, compared by identity, so a second database in one process (the
    # tests' private ones) can never be served the first one's stamps.
    base = (session.get_bind(), version, overrides_stamp(session))
    newest = session.execute(
        select(func.max(Run.id)).where(Run.status == RunStatus.COMPLETE)
    ).scalar_one() or 0
    with _STAMPS_LOCK:
        held = dict(_STAMPS_CACHE)
    if (held["base"] == base and held["stamps"] is not None and held["upto"] <= newest
            and _stamps_check(session, held["upto"]) == held["check"]):
        if newest == held["upto"]:
            lap("stamps_cached")
            return dict(held["stamps"])
        # The check is read before the runs, and the runs are bounded by ``newest``: a run
        # landing or a score moving meanwhile then shows up as a change on the next read,
        # rather than being counted twice or filed under a check that already includes it.
        check = _stamps_check(session, newest)
        merged = _merge(held["stamps"],
                        stamps(session, version, after=held["upto"], upto=newest))
        lap("stamps_appended")
        _store_stamps(base, newest, check, merged)
        return dict(merged)
    check = _stamps_check(session, newest)
    fresh = stamps(session, version, upto=newest)
    lap("stamps")
    _store_stamps(base, newest, check, fresh)
    return dict(fresh)


def _store_stamps(base: tuple, upto: int, check: tuple, held: dict) -> None:
    with _STAMPS_LOCK:
        _STAMPS_CACHE.update({"base": base, "upto": upto, "check": check, "stamps": held})


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


#: Metrics whose RAW medians (milliseconds, off `Score.metric_values`) the rollup keeps
#: beside the subscores: the current crown's millisecond legs. A deliberate filter, not
#: the whole crown: the raw-millisecond crowns (`pooled_field`: fastest median FCP × LCP,
#: the leg-first bands) compare times, and a unitless leg like ``cadence_cov`` has no
#: millisecond median to keep. Read in SQL as one scalar per metric per run, so they cost
#: nothing like the subscore document decode.
RAW_METRICS_UNIT = "ms"
RAW_METRICS: tuple[str, ...] = crown_metric_keys(CURRENT_METHODOLOGY, unit=RAW_METRICS_UNIT)


def _summary(values: list[float]) -> dict:
    p25, p75 = _quartiles(values)
    return {"n": len(values), "median": median(values), "p25": p25, "p75": p75}


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
            select(effective_fingerprint(), Run.iterations, Run.id,
                   Score.computed_at, Score.subscores,
                   *[Score.metric_values[m].as_float() for m in RAW_METRICS])
            .join(Score, Score.run_id == Run.id)
            .where(*_comparable_runs(version), effective_fingerprint().in_(chunk))
        )
    ]

    samples: dict[str, dict[str, list[float]]] = {}
    raw_samples: dict[str, dict[str, list[float]]] = {}
    totals: dict[str, dict] = {}
    for fp, iterations, run_id, scored_at, subscores, *raw_values in rows:
        by_raw = raw_samples.setdefault(fp, {})
        for metric, value in zip(RAW_METRICS, raw_values):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                by_raw.setdefault(metric, []).append(float(value))
        agg = totals.setdefault(
            fp, {"iterations": 0, "runs": 0, "max_run_id": None, "scored_at": None}
        )
        agg["iterations"] += int(iterations or 1)
        agg["runs"] += 1
        if agg["max_run_id"] is None or run_id > agg["max_run_id"]:
            agg["max_run_id"] = run_id
        if scored_at is not None and (agg["scored_at"] is None or scored_at > agg["scored_at"]):
            agg["scored_at"] = scored_at
        by_metric = samples.setdefault(fp, {})
        for metric, value in (subscores or {}).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            by_metric.setdefault(metric, []).append(float(value))

    out: dict[str, dict] = {}
    for fp in fingerprints:
        by_metric = samples.get(fp) or {}
        agg = totals.get(fp) or {"iterations": 0, "runs": 0, "max_run_id": None, "scored_at": None}
        metrics = {}
        for metric, values in by_metric.items():
            if not values:
                continue
            p25, p75 = _quartiles(values)
            # The spread is stored alongside the median because it comes free in this pass
            # and is the next consumer's input (`optimistic_overall` corners over each crown
            # metric's good-side quartile). Adding it later would mean rebuilding every row.
            metrics[metric] = {"n": len(values), "median": median(values), "p25": p25, "p75": p75}
        out[fp] = {
            "metrics": metrics,
            "raw": {m: _summary(v) for m, v in (raw_samples.get(fp) or {}).items() if v},
            "iterations": agg["iterations"],
            "run_count": agg["runs"],
            # The stamp this rollup describes, derived from the very rows it aggregated —
            # never from the earlier `stamps()` read. Those are two queries with a gap
            # between them, and a run landing in that gap would store a stamp that describes
            # a different set of rows than the metrics beside it. Self-describing is the only
            # version of this that is true by construction rather than true if nothing raced.
            "stamp": (agg["max_run_id"], agg["runs"], agg["iterations"], agg["scored_at"]),
        }
    return out


def _persist(version: str, computed: dict[str, dict]) -> None:
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
                max_run_id, run_count, iterations, scored_at = agg["stamp"]
                row = existing.get(fp)
                if row is None:
                    row = ProfileAggregate(settings_fingerprint=fp, methodology_version=version)
                    session.add(row)
                row.metrics = agg["metrics"]
                row.raw = agg.get("raw") or {}
                row.iterations = iterations
                row.run_count = agg["run_count"]
                row.source_max_run_id = max_run_id
                row.source_run_count = run_count
                row.source_scored_at = scored_at
                row.computed_at = now
    except Exception:  # noqa: BLE001 — a cache write can't be why a read fails
        log.debug("Could not persist profile aggregates", exc_info=True)


#: The per-request memo (see :func:`request_memo`). ``None`` outside one, so every caller that
#: doesn't opt in — the crown follower, the duel, a background job — verifies on every call.
_MEMO: ContextVar[dict | None] = ContextVar("profile_aggregates_memo", default=None)


@contextmanager
def request_memo():
    """Verify the rollups once per read, not once per reader.

    One ``/settings/profiles`` request asks for the same field's aggregates nine times over
    (the pooled field, each crown, the leg gaps, the budget reading…), and each ask re-ran
    the grouped stamp query across every scored run. On the NAS that was most of a
    twenty-second page load. Inside this block the first ask verifies and the rest reuse
    its answer, so a read sees one consistent snapshot of the field instead of nine.

    Scope it to a single read-only request. Nothing inside the block may write runs or
    scores and then expect a fresh aggregate; outside the block nothing changes.
    """
    token = _MEMO.set({})
    try:
        yield
    finally:
        _MEMO.reset(token)


def request_cache() -> dict | None:
    """Scratch space inside the open `request_memo` for other reads that a request repeats
    (``refresh.list_profiles``), or None outside one. Same lifetime and rules as the
    aggregates memo: one request, then gone."""
    memo = _MEMO.get()
    if memo is None:
        return None
    return memo.setdefault(("__request_cache__",), {})


def _empty_aggregate() -> dict:
    return {"metrics": {}, "raw": {}, "iterations": 0, "run_count": 0}


def aggregates(
    session, version: str, fingerprints: list[str] | None = None, *, persist: bool = True
) -> dict[str, dict]:
    """``{fingerprint: {metrics, iterations, run_count}}``, verified fresh — or, inside
    :func:`request_memo`, verified once for the request."""
    memo = _MEMO.get()
    if memo is None:
        return _aggregates(session, version, fingerprints, persist=persist)
    slot = memo.setdefault(version, {"full": None, "by_fp": {}})
    by_fp: dict[str, dict] = slot["by_fp"]
    if fingerprints is None:
        if slot["full"] is None:
            full = _aggregates(session, version, None, persist=persist)
            by_fp.update(full)
            slot["full"] = list(full)
        return {fp: by_fp[fp] for fp in slot["full"]}
    wanted = [fp for fp in dict.fromkeys(fingerprints) if fp]
    missing = [fp for fp in wanted if fp not in by_fp]
    if missing:
        if slot["full"] is not None:
            # The full field was read already: a profile absent from it has no comparable
            # runs, which is exactly what an uncached read would have answered.
            by_fp.update({fp: _empty_aggregate() for fp in missing})
        else:
            by_fp.update(_aggregates(session, version, missing, persist=persist))
    return {fp: by_fp[fp] for fp in wanted}


def _aggregates(
    session, version: str, fingerprints: list[str] | None = None, *, persist: bool = True
) -> dict[str, dict]:
    """``{fingerprint: {metrics, iterations, run_count}}``, verified fresh.

    Reads the stored rollups, checks each against what it *should* have been built from,
    recomputes only the profiles that disagree, and (best-effort) writes those back. A
    caller can therefore treat the result as if it had scanned every run itself, because
    for any profile where that would have given a different answer, it did.
    """
    wanted = [fp for fp in dict.fromkeys(fingerprints) if fp] if fingerprints else None
    if wanted is None or len(wanted) >= _FIELD_MIN:
        field = _field_stamps(session, version)
        fresh = field if wanted is None else {fp: field[fp] for fp in wanted if fp in field}
    else:
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
            out[fp] = _empty_aggregate()
            continue
        # A row written before the raw medians existed (`raw` None) is rebuilt once, for
        # that profile alone — the same per-profile repair any stale stamp gets.
        if row is not None and _stored_stamp(row) == want and row.raw is not None:
            out[fp] = {
                "metrics": row.metrics or {},
                "raw": row.raw or {},
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
                "raw": agg.get("raw") or {},
                "iterations": agg["iterations"],
                "run_count": agg["run_count"],
            }
        if persist:
            _persist(version, computed)
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
    invalidate_stamps_cache()
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
