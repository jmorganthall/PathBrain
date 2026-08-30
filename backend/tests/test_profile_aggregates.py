"""The per-profile rollup: it must be fast, and it must never be wrong.

A cache under the crown is only acceptable if being stale makes it *slower*, never
*different* — the number it feeds decides which profile gets written to the firewall. Every
test here is about that property rather than about speed, because speed is the easy half.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from pathbrain import profile_aggregates
from pathbrain.crown_follower import _collect, _grade_samples, profile_overalls
from pathbrain.database import session_scope
from pathbrain.models import ProfileAggregate, Run, RunStatus, Score

VERSION = "agg-test-v1"
METRICS = ["fcp", "lcp"]
WEIGHTS = {"fcp": 1.0, "lcp": 0.5}


def _add_run(fp: str, *, fcp: float | None, lcp: float | None, iterations: int = 3,
             comparability: str = "exact", scored_at: datetime | None = None) -> int:
    with session_scope() as s:
        run = Run(status=RunStatus.COMPLETE, iterations=iterations, settings_fingerprint=fp)
        s.add(run)
        s.flush()
        sub: dict[str, float] = {}
        if fcp is not None:
            sub["fcp"] = fcp
        if lcp is not None:
            sub["lcp"] = lcp
        score = Score(run_id=run.id, methodology_version=VERSION, is_at_measure=False,
                      comparability=comparability, subscores=sub, axis_scores={},
                      weights_used={}, metric_values={})
        if scored_at is not None:
            score.computed_at = scored_at
        s.add(score)
        return run.id


def _cleanup(run_ids: list[int]) -> None:
    with session_scope() as s:
        # Scores first: there is no cascade, and SQLite reuses a deleted rowid, so an
        # orphaned Score collides with the next test's run on (run_id, methodology_version).
        s.execute(Score.__table__.delete().where(Score.run_id.in_(run_ids)))
        for rid in run_ids:
            row = s.get(Run, rid)
            if row is not None:
                s.delete(row)
        s.execute(
            ProfileAggregate.__table__.delete().where(
                ProfileAggregate.methodology_version == VERSION
            )
        )


def _rescan(session, fps: list[str]) -> dict[str, tuple[float | None, int]]:
    """The answer the old code gave: decode every run and grade it. The reference."""
    rows = session.execute(
        select(Run.settings_fingerprint, Run.iterations, Score.subscores, Score.comparability)
        .join(Score, Score.run_id == Run.id)
        .where(Run.status == RunStatus.COMPLETE, Run.settings_fingerprint.in_(fps),
               Score.methodology_version == VERSION)
    ).all()
    by_fp: dict[str, list] = {fp: [] for fp in fps}
    for fp, iters, sub, comp in rows:
        by_fp[fp].append((iters, sub, comp))
    return {
        fp: _grade_samples(*_collect(by_fp[fp], METRICS), METRICS, METRICS, WEIGHTS)
        for fp in fps
    }


def _graded(session, fps: list[str]):
    return profile_overalls(session, fps, VERSION, METRICS, METRICS, WEIGHTS)


def test_the_rollup_answers_exactly_what_a_full_rescan_would_cold_and_warm():
    """The whole bargain: the same answer, off one row per profile instead of every run."""
    ids = [
        _add_run("agg-a", fcp=80.0, lcp=60.0),
        _add_run("agg-a", fcp=90.0, lcp=70.0),
        _add_run("agg-b", fcp=55.5, lcp=44.25),     # even count → the median averages two
        _add_run("agg-b", fcp=65.5, lcp=54.25),
        _add_run("agg-c", fcp=70.0, lcp=None),      # can't supply a required metric
    ]
    fps = ["agg-a", "agg-b", "agg-c"]
    try:
        profile_aggregates.invalidate(version=VERSION)
        with session_scope() as s:
            truth = _rescan(s, fps)
            cold = _graded(s, fps)                  # nothing stored yet — computes + saves
        with session_scope() as s:
            warm = _graded(s, fps)                  # verifies the stored rows and reuses them

        assert cold == truth, (cold, truth)
        assert warm == truth, (warm, truth)
        # A profile that can't supply a required metric has no Overall — the rollup must
        # not turn "unmeasurable" into a number, the same rule the derive layer follows.
        assert truth["agg-c"][0] is None and warm["agg-c"][0] is None
        # And it really was served from storage, not silently recomputed every time.
        with session_scope() as s:
            stored = s.scalars(
                select(ProfileAggregate).where(ProfileAggregate.methodology_version == VERSION)
            ).all()
        assert {r.settings_fingerprint for r in stored} == set(fps)
    finally:
        _cleanup(ids)


def test_a_new_run_changes_the_answer_rather_than_being_missed():
    """The failure that would matter: a duel measures a profile, the rollup keeps serving
    yesterday's number, and the crown is decided on data that has moved."""
    ids = [_add_run("agg-new", fcp=50.0, lcp=50.0)]
    try:
        profile_aggregates.invalidate(version=VERSION)
        with session_scope() as s:
            before = _graded(s, ["agg-new"])
        ids.append(_add_run("agg-new", fcp=90.0, lcp=90.0))
        with session_scope() as s:
            after = _graded(s, ["agg-new"])
            assert after == _rescan(s, ["agg-new"])
        assert after != before, "the rollup served a stale answer after a new run landed"
        assert after["agg-new"][1] == 6, "both runs' iterations are counted"
    finally:
        _cleanup(ids)


def test_a_score_rewritten_in_place_is_caught_even_though_no_run_was_added():
    """A re-derive rewrites a Score **in place**: same run, same count, same newest id — only
    the values underneath move. A stamp watching ids alone would serve the old grade forever,
    which is why the newest scoring timestamp is part of it."""
    ids = [_add_run("agg-rederive", fcp=40.0, lcp=40.0)]
    try:
        profile_aggregates.invalidate(version=VERSION)
        with session_scope() as s:
            before = _graded(s, ["agg-rederive"])
        with session_scope() as s:                    # the re-derive: values move, ids don't
            score = s.scalars(
                select(Score).join(Run, Run.id == Score.run_id)
                .where(Run.settings_fingerprint == "agg-rederive")
            ).one()
            score.subscores = {"fcp": 95.0, "lcp": 95.0}
            score.computed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        with session_scope() as s:
            after = _graded(s, ["agg-rederive"])
            assert after == _rescan(s, ["agg-rederive"])
        assert after != before, "an in-place re-derive was not noticed"
    finally:
        _cleanup(ids)


def test_only_the_profile_that_got_a_run_is_recomputed():
    """The other half of the fix. The field-wide memo keys on a global stamp, so one
    completed run invalidates every profile — and during a duel a run lands every minute,
    which made the cache coldest exactly when the system was busiest. A run touches one
    profile's stamp, so its neighbours' stored rows must survive it untouched."""
    ids = [_add_run("agg-x", fcp=60.0, lcp=60.0), _add_run("agg-y", fcp=70.0, lcp=70.0)]
    try:
        profile_aggregates.invalidate(version=VERSION)
        with session_scope() as s:
            _graded(s, ["agg-x", "agg-y"])
        with session_scope() as s:
            untouched = s.scalars(
                select(ProfileAggregate).where(
                    ProfileAggregate.methodology_version == VERSION,
                    ProfileAggregate.settings_fingerprint == "agg-y",
                )
            ).one()
            stamp_before = (untouched.source_max_run_id, untouched.source_run_count,
                            untouched.computed_at)

        ids.append(_add_run("agg-x", fcp=10.0, lcp=10.0))
        with session_scope() as s:
            fresh = profile_aggregates.stamps(s, VERSION, ["agg-x", "agg-y"])
            stored = {
                r.settings_fingerprint: r for r in s.scalars(
                    select(ProfileAggregate).where(
                        ProfileAggregate.methodology_version == VERSION)
                )
            }
            # x's stored row no longer matches what it should be built from; y's still does.
            assert profile_aggregates._stored_stamp(stored["agg-x"]) != fresh["agg-x"]
            assert profile_aggregates._stored_stamp(stored["agg-y"]) == fresh["agg-y"]
            _graded(s, ["agg-x", "agg-y"])

        with session_scope() as s:
            after = s.scalars(
                select(ProfileAggregate).where(
                    ProfileAggregate.methodology_version == VERSION,
                    ProfileAggregate.settings_fingerprint == "agg-y",
                )
            ).one()
        assert (after.source_max_run_id, after.source_run_count, after.computed_at) == stamp_before
    finally:
        _cleanup(ids)


def test_an_incomparable_run_is_excluded_and_a_profile_of_only_those_has_no_rollup():
    """Comparability is the gate that keeps a run which couldn't measure a crown metric out
    of the crown. It has to hold at this layer too, or the rollup quietly re-admits what the
    methodology quarantined."""
    ids = [
        _add_run("agg-mixed", fcp=80.0, lcp=80.0),
        _add_run("agg-mixed", fcp=10.0, lcp=10.0, comparability="incomparable"),
        _add_run("agg-none", fcp=99.0, lcp=99.0, comparability="incomparable"),
    ]
    try:
        profile_aggregates.invalidate(version=VERSION)
        with session_scope() as s:
            graded = _graded(s, ["agg-mixed", "agg-none"])
            assert graded == _rescan(s, ["agg-mixed", "agg-none"])
            # Only the comparable run counted — 3 iterations, not 6.
            assert graded["agg-mixed"][1] == 3
            # A profile with nothing comparable has no grade and no iterations.
            assert graded["agg-none"] == (None, 0)
    finally:
        _cleanup(ids)


def test_a_leftover_row_is_not_served_once_its_runs_stop_being_comparable():
    """A re-grade can quarantine every run a profile has. Its stored rollup then describes
    runs that no longer qualify, and serving it would resurrect a crown contender the
    methodology just ruled out."""
    ids = [_add_run("agg-quar", fcp=80.0, lcp=80.0)]
    try:
        profile_aggregates.invalidate(version=VERSION)
        with session_scope() as s:
            assert _graded(s, ["agg-quar"])["agg-quar"][0] is not None
        with session_scope() as s:                     # the re-grade quarantines it
            score = s.scalars(
                select(Score).join(Run, Run.id == Score.run_id)
                .where(Run.settings_fingerprint == "agg-quar")
            ).one()
            score.comparability = "incomparable"
        with session_scope() as s:
            assert _graded(s, ["agg-quar"]) == {"agg-quar": (None, 0)}
    finally:
        _cleanup(ids)


def test_verifying_is_far_cheaper_than_computing_which_is_why_it_is_never_skipped():
    """The design rests on this asymmetry: the check returns a row per profile, the compute
    reads a row per run. If that ever inverted, the honest thing would be to drop the cache
    rather than skip the check."""
    ids = [_add_run(f"agg-scale-{i % 4}", fcp=float(i % 90), lcp=float(i % 70))
           for i in range(40)]
    fps = [f"agg-scale-{i}" for i in range(4)]
    try:
        profile_aggregates.invalidate(version=VERSION)
        with session_scope() as s:
            stamped = profile_aggregates.stamps(s, VERSION, fps)
            assert len(stamped) == 4, "one row per profile, whatever the run count"
            assert sum(v[1] for v in stamped.values()) == 40, "covering every run"
    finally:
        _cleanup(ids)


def test_a_real_regrade_through_upsert_score_moves_the_rollup():
    """The hole self-review found, pinned at its source.

    ``upsert_score`` refreshes an existing Score **in place** — the re-grade path for a run
    already scored under the current methodology. Its run count, its newest run id and its
    iteration total are all unchanged by that; only the values move. ``computed_at``
    defaulted on INSERT only, so it did not move either, and a rollup watching those four
    numbers would have gone on serving the old grade for as long as the methodology stood.

    So this drives the *real* writer rather than setting the timestamp by hand: if
    ``upsert_score`` ever stops stamping it, the rollup silently goes stale and this fails.
    """
    from pathbrain.methodology import upsert_score

    ids = [_add_run("agg-upsert", fcp=30.0, lcp=30.0)]
    try:
        profile_aggregates.invalidate(version=VERSION)
        with session_scope() as s:
            before = _graded(s, ["agg-upsert"])
            stamp_before = profile_aggregates.stamps(s, VERSION, ["agg-upsert"])["agg-upsert"]

        with session_scope() as s:                      # exactly what a re-grade does
            upsert_score(s, ids[0], VERSION, is_at_measure=False,
                         comparability="exact", subscores={"fcp": 95.0, "lcp": 95.0},
                         axis_scores={}, weights_used={}, metric_values={})

        with session_scope() as s:
            stamp_after = profile_aggregates.stamps(s, VERSION, ["agg-upsert"])["agg-upsert"]
            after = _graded(s, ["agg-upsert"])
            assert after == _rescan(s, ["agg-upsert"])

        # Only the timestamp can have moved — that is the point.
        assert stamp_after[:3] == stamp_before[:3], "no run was added or removed"
        assert stamp_after[3] != stamp_before[3], "upsert_score must stamp computed_at"
        assert after != before, "the rollup missed an in-place re-grade"
    finally:
        _cleanup(ids)


def test_reading_thousands_of_profiles_does_not_overrun_the_sql_parameter_limit():
    """`aggregates()` takes a fingerprint list straight into `IN (...)`. At today's ~150
    profiles that is nowhere near SQLite's limit, which is exactly why it would go unnoticed
    until a field big enough to hit it — and then fail as a confusing driver error rather
    than as anything to do with profiles. Chunked, so the size of the field is never a
    correctness question."""
    # Asserted against the chunk size rather than against a build's limit: the limit is a
    # compile-time constant this test cannot see, so the only checkable guarantee is that no
    # single statement is ever handed more than `_CHUNK` fingerprints.
    assert profile_aggregates._CHUNK <= 900, "must stay under the most conservative build's 999"
    many = [f"agg-bulk-{i}" for i in range(5000)]
    assert all(len(c) <= profile_aggregates._CHUNK for c in profile_aggregates._chunks(many))
    with session_scope() as s:
        out = profile_aggregates.aggregates(s, VERSION, many, persist=False)
        assert len(out) == 5000
        # None of them have runs, so every one is the empty answer — the point is that it
        # answered at all.
        assert all(v == {"metrics": {}, "iterations": 0, "run_count": 0} for v in out.values())


def test_a_freshly_written_rollup_is_immediately_a_hit():
    """The stored stamp is derived from the very rows the metrics were aggregated from, not
    from the earlier ``stamps()`` read. Those are two queries with a gap between them, and a
    run landing in that gap would store a stamp describing a different set of rows than the
    metrics beside it — self-correcting on the next read, but it means a row written under
    load could never be a hit. Reading a rollup back must find it valid immediately."""
    ids = [_add_run("agg-selfdesc", fcp=42.0, lcp=42.0),
           _add_run("agg-selfdesc", fcp=44.0, lcp=44.0)]
    try:
        profile_aggregates.invalidate(version=VERSION)
        with session_scope() as s:
            profile_aggregates.aggregates(s, VERSION, ["agg-selfdesc"])
        with session_scope() as s:
            fresh = profile_aggregates.stamps(s, VERSION, ["agg-selfdesc"])["agg-selfdesc"]
            row = s.scalars(
                select(ProfileAggregate).where(
                    ProfileAggregate.methodology_version == VERSION,
                    ProfileAggregate.settings_fingerprint == "agg-selfdesc",
                )
            ).one()
            assert profile_aggregates._stored_stamp(row) == fresh, (
                "a rollup must describe exactly the rows it was built from"
            )
    finally:
        _cleanup(ids)
