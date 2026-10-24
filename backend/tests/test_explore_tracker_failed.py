"""A claim whose profile test never ran is `failed`, not `pending` for ever — and it stops
blocking its own coordinates once it is old enough to try again."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pathbrain import explore_tracker
from pathbrain.database import session_scope
from pathbrain.models import ExploreRecommendation, ProfileTest, ProfileTestStatus

_FP = "recfail-"
_MATCHED = ["measured directly on a matched pair (1 pair; claim shrunk accordingly)"]


def _clear():
    with session_scope() as s:
        for row in s.query(ExploreRecommendation).filter(
            ExploreRecommendation.fingerprint.like(f"{_FP}%")
        ).all():
            s.delete(row)
        for row in s.query(ProfileTest).filter(ProfileTest.fingerprint.like(f"{_FP}%")).all():
            s.delete(row)


@pytest.fixture(autouse=True)
def _isolate():
    _clear()
    yield
    _clear()


def _version() -> str:
    from pathbrain.config_store import get_config
    from pathbrain.methodology import ensure_current_methodology

    with session_scope() as s:
        return ensure_current_methodology(s, get_config(s)).version


def _test(fp: str, status: ProfileTestStatus, error: str | None = None) -> int:
    with session_scope() as s:
        row = ProfileTest(status=status, error=error, fingerprint=fp, iterations=5)
        s.add(row)
        s.flush()
        return row.id


def _claim(fp: str, test_id: int | None, *, parent: str = "recfail-parent", key: str = "Download::quantum",
           to: float = 2500.0, age_days: float = 0.0) -> int:
    rid = explore_tracker.record(
        fingerprint=fp, parent_fingerprint=parent, predicted=70.0, uncertainty=2.0,
        evidence=_MATCHED, methodology_version=_version(), profile_test_id=test_id,
        changes=[{"key": key, "to": to, "from": 1514.0}],
    )
    if age_days:
        with session_scope() as s:
            row = s.get(ExploreRecommendation, rid)
            row.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=age_days)
    return rid


def test_a_claim_whose_test_failed_reads_failed_with_the_tests_own_error(client):
    tid = _test(f"{_FP}a", ProfileTestStatus.FAILED, "Firewall did not accept quantum 2500 on wan-download")
    _claim(f"{_FP}a", tid)
    _claim(f"{_FP}b", None)  # no test on record, no runs → still pending
    body = client.get("/api/explore/recommendations").json()
    rows = {r["fingerprint"]: r for r in body["recommendations"]}
    failed, pending = rows[f"{_FP}a"], rows[f"{_FP}b"]
    assert failed["verdict"] == "failed" and failed["test_status"] == "failed"
    assert "did not accept quantum" in failed["why"] and "did not accept quantum" in failed["test_error"]
    assert failed["provisional"] is False and failed["beat_best"] is None
    assert pending["verdict"] == "pending" and pending["test_status"] is None
    assert body["summary"]["failed"] >= 1 and body["summary"]["graded"] == 0


def test_a_cancelled_test_is_failed_too_and_a_finished_one_is_not():
    cancelled = _test(f"{_FP}c", ProfileTestStatus.CANCELLED, None)
    complete = _test(f"{_FP}d", ProfileTestStatus.COMPLETE, None)

    class _Rec:
        uncertainty, predicted, methodology_version, unreachable = 2.0, 70.0, "v", False

    v = explore_tracker._verdict(_Rec(), None, 0, 15, "v", test_state=("cancelled", None))
    assert v["verdict"] == "failed" and v["test_status"] == "cancelled"
    # A test that completed but whose runs were quarantined is honestly still pending.
    v = explore_tracker._verdict(_Rec(), None, 0, 15, "v", test_state=("complete", None))
    assert v["verdict"] == "pending"
    # A measured claim is graded whatever happened to the test row afterwards.
    v = explore_tracker._verdict(_Rec(), 70.5, 10, 15, "v", test_state=("failed", "late"))
    assert v["verdict"] == "on_target" and v["test_error"] is None
    assert cancelled and complete


def test_a_failed_claim_blocks_its_coordinates_for_the_cooldown_and_then_lets_go():
    fresh_fail = _test(f"{_FP}e", ProfileTestStatus.FAILED, "refused")
    old_fail = _test(f"{_FP}f", ProfileTestStatus.FAILED, "refused")
    done = _test(f"{_FP}g", ProfileTestStatus.COMPLETE, None)
    _claim(f"{_FP}e", fresh_fail, to=2500.0)
    _claim(f"{_FP}f", old_fail, to=2600.0, age_days=explore_tracker.FAILED_CLAIM_COOLDOWN_DAYS + 1)
    _claim(f"{_FP}g", done, to=2700.0, age_days=30)
    _claim(f"{_FP}h", None, to=2800.0, age_days=30)  # no test row at all: paid for, kept
    with session_scope() as s:
        moves = {m["moves"]["Download::quantum"] for m in explore_tracker.claimed_moves(s)
                 if m["parent_fingerprint"] == "recfail-parent"}
    assert 2500.0 in moves, "a fresh failure still blocks: it may have been the firewall's night"
    assert 2600.0 not in moves, "an old failure lets the proposal be made again"
    assert 2700.0 in moves and 2800.0 in moves, "measured claims block for good, however old"
