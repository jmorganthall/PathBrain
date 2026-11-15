"""The per-request memo: one verification of the rollups per read, and the same answer.

``/settings/profiles`` asks for the same field's aggregates many times in one request. The
memo makes the first ask verify and the rest reuse it. What must hold: inside the memo the
answer is exactly what an un-memoized read gives, every shape of ask (the whole field, a
list, a profile with no runs) agrees with the uncached path, and outside the memo nothing
is memoized at all.
"""
from __future__ import annotations

from unittest import mock

from pathbrain import profile_aggregates
from pathbrain.database import session_scope
from pathbrain.models import ProfileAggregate, Run, RunStatus, Score

VERSION = "agg-memo-test-v1"


def _add_run(fp: str, fcp: float, lcp: float) -> int:
    with session_scope() as s:
        run = Run(status=RunStatus.COMPLETE, iterations=2, settings_fingerprint=fp)
        s.add(run)
        s.flush()
        s.add(Score(run_id=run.id, methodology_version=VERSION, is_at_measure=False,
                    comparability="exact", subscores={"fcp": fcp, "lcp": lcp},
                    axis_scores={}, weights_used={}, metric_values={}))
        return run.id


def _cleanup(run_ids: list[int]) -> None:
    with session_scope() as s:
        s.execute(Score.__table__.delete().where(Score.run_id.in_(run_ids)))
        for rid in run_ids:
            row = s.get(Run, rid)
            if row is not None:
                s.delete(row)
        s.execute(ProfileAggregate.__table__.delete().where(
            ProfileAggregate.methodology_version == VERSION))


def _seed() -> list[int]:
    ids = []
    for i in range(3):
        ids.append(_add_run("memo-aaaaaaa", 80 + i, 70 + i))
        ids.append(_add_run("memo-bbbbbbb", 60 + i, 90 - i))
    return ids


def test_inside_the_memo_the_stamp_query_runs_once_for_many_asks():
    ids = _seed()
    try:
        fps = ["memo-aaaaaaa", "memo-bbbbbbb"]
        with session_scope() as s:
            plain_list = profile_aggregates.aggregates(s, VERSION, fps)
            plain_one = profile_aggregates.aggregates(s, VERSION, ["memo-bbbbbbb"])
        real = profile_aggregates.stamps
        with mock.patch.object(profile_aggregates, "stamps", side_effect=real) as spy, \
                session_scope() as s, profile_aggregates.request_memo():
            first = profile_aggregates.aggregates(s, VERSION, fps)
            again = profile_aggregates.aggregates(s, VERSION, fps)
            subset = profile_aggregates.aggregates(s, VERSION, ["memo-bbbbbbb"])
            assert spy.call_count == 1
        assert first == plain_list
        assert again == plain_list
        assert subset == plain_one
    finally:
        _cleanup(ids)


def test_the_whole_field_and_a_runless_profile_match_the_uncached_answers():
    ids = _seed()
    try:
        with session_scope() as s:
            plain_full = profile_aggregates.aggregates(s, VERSION)
            plain_ghost = profile_aggregates.aggregates(s, VERSION, ["memo-ghost00", "memo-aaaaaaa"])
        with session_scope() as s, profile_aggregates.request_memo():
            full = profile_aggregates.aggregates(s, VERSION)
            # Asked after the whole field was read: answered from it, including the empty
            # entry an uncached read gives a profile with no comparable runs.
            ghost = profile_aggregates.aggregates(s, VERSION, ["memo-ghost00", "memo-aaaaaaa"])
            full_again = profile_aggregates.aggregates(s, VERSION)
        assert full == plain_full
        assert full_again == plain_full
        assert ghost == plain_ghost
        assert ghost["memo-ghost00"] == {"metrics": {}, "raw": {}, "iterations": 0, "run_count": 0}
    finally:
        _cleanup(ids)


def test_outside_the_memo_every_ask_verifies_and_sees_new_runs():
    ids = _seed()
    try:
        with session_scope() as s:
            before = profile_aggregates.aggregates(s, VERSION, ["memo-aaaaaaa"])
        ids.append(_add_run("memo-aaaaaaa", 99, 99))
        real = profile_aggregates.stamps
        with mock.patch.object(profile_aggregates, "stamps", side_effect=real) as spy, \
                session_scope() as s:
            after = profile_aggregates.aggregates(s, VERSION, ["memo-aaaaaaa"])
            profile_aggregates.aggregates(s, VERSION, ["memo-aaaaaaa"])
            assert spy.call_count == 2
        assert after["memo-aaaaaaa"]["run_count"] == before["memo-aaaaaaa"]["run_count"] + 1
    finally:
        _cleanup(ids)


def test_the_memo_ends_with_its_block():
    assert profile_aggregates._MEMO.get() is None
    with profile_aggregates.request_memo():
        assert profile_aggregates._MEMO.get() == {}
    assert profile_aggregates._MEMO.get() is None


def test_the_profiles_endpoint_reads_inside_one_memo(client):
    seen = []
    real = profile_aggregates.aggregates

    def spy(*a, **k):
        seen.append(profile_aggregates._MEMO.get() is not None)
        return real(*a, **k)

    with mock.patch.object(profile_aggregates, "aggregates", side_effect=spy):
        r = client.get("/api/settings/profiles")
    assert r.status_code == 200
    assert all(seen)
