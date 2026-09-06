"""Outlier flagging on the Settings-Impact field + the refresh engine's explicit scope.

Two different questions hide in "what do we do about outliers?": hiding is a view decision,
re-running is an evidence decision. The flag is computed once (robust z over profile
medians) and both actions read it; these tests pin the statistic and the scope.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain.api.routes_settings import OUTLIER_MIN_PROFILES, _outlier_report
from pathbrain.database import session_scope
from pathbrain.refresh import _select, preview
from tests.test_settings import _seed_run

_DEF = {
    "metrics": [
        {"key": "fcp", "label": "First Contentful Paint", "higher_is_better": False},
        {"key": "lcp", "label": "Largest Contentful Paint", "higher_is_better": False},
    ]
}


def _profile(fp: str, fcp: float, overall: float, iterations: int = 20) -> dict:
    return {"fingerprint": fp, "metrics": {"fcp": fcp}, "overall": overall, "iterations": iterations}


def test_a_far_out_profile_is_flagged_on_the_bad_side_and_the_pack_is_not():
    # Nine profiles in a tight pack, one at four times the field: the classic "one dot and a
    # smudge" scatter. The pack must NOT be flagged — an ordinary z would be pulled toward
    # the outlier, which is why the statistic is MAD-based.
    profiles = [_profile(f"pack{i}", 600 + i * 5, 80 + i * 0.3) for i in range(9)]
    profiles.append(_profile("faraway00000", 2300, 38, iterations=2))
    summary = _outlier_report(profiles, ["fcp"], _DEF, min_iterations=15)

    assert summary["fingerprints"] == ["faraway00000"]
    assert summary["count"] == 1 and summary["thin"] == 1 and summary["confident"] == 0
    flagged = next(p for p in profiles if p["fingerprint"] == "faraway00000")["outlier"]
    assert flagged["thin"] is True
    by_key = {m["key"]: m for m in flagged["metrics"]}
    # Slow FCP is the bad side for a lower-is-better metric; a low Overall is bad too.
    assert by_key["fcp"]["side"] == "worse" and by_key["fcp"]["z"] > 3.5
    assert by_key["overall"]["side"] == "worse" and by_key["overall"]["z"] < -3.5
    assert all(p["outlier"] is None for p in profiles if p["fingerprint"] != "faraway00000")


def test_a_confident_outlier_is_counted_as_confident_not_thin():
    profiles = [_profile(f"pack{i}", 600 + i * 5, 80) for i in range(9)]
    profiles.append(_profile("slowbutsure", 2300, 80, iterations=40))
    summary = _outlier_report(profiles, ["fcp"], _DEF, min_iterations=15)
    assert summary["confident"] == 1 and summary["thin"] == 0


def test_an_implausibly_fast_thin_reading_is_flagged_on_the_better_side():
    profiles = [_profile(f"pack{i}", 600 + i * 5, 80) for i in range(9)]
    profiles.append(_profile("luckyread000", 120, 80, iterations=3))
    summary = _outlier_report(profiles, ["fcp"], _DEF, min_iterations=15)
    assert summary["fingerprints"] == ["luckyread000"]
    m = next(p for p in profiles if p["fingerprint"] == "luckyread000")["outlier"]["metrics"][0]
    assert m["key"] == "fcp" and m["side"] == "better"


def test_too_few_profiles_or_a_degenerate_field_flags_nothing():
    few = [_profile(f"p{i}", 600, 80) for i in range(OUTLIER_MIN_PROFILES - 1)]
    few.append(_profile("far", 5000, 10))
    assert _outlier_report(few, ["fcp"], _DEF, 15)["count"] == 0
    # More than half the field at one value → MAD is 0 → no meaningful z, so nothing is
    # flagged rather than everything else being flagged.
    same = [_profile(f"p{i}", 600, 80) for i in range(8)] + [_profile("odd", 610, 81)]
    assert _outlier_report(same, ["fcp"], _DEF, 15)["count"] == 0


def test_refresh_select_with_an_explicit_fingerprint_list_is_exact_and_ordered():
    from sqlalchemy import select as sa_select

    from pathbrain.models import Run, Score

    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    _seed_run("outlierfp0a", 40.0, t0 - timedelta(minutes=5), settings=[{"label": "wan", "quantum": 111}])
    _seed_run("outlierfp0b", 41.0, t0 - timedelta(minutes=4), settings=[{"label": "wan", "quantum": 222}])
    try:
        with session_scope() as s:
            # Order follows the caller's list; unknown fingerprints are dropped, never guessed.
            profiles, ranked_by = _select(
                s, top=5, rank_by=None, fingerprints=["outlierfp0b", "nope", "outlierfp0a"]
            )
            assert [p["fingerprint"] for p in profiles] == ["outlierfp0b", "outlierfp0a"]
            assert ranked_by is None  # the list IS the ranking; top/rank_by are ignored
            pv = preview(s, 5, top=3, fingerprints=["outlierfp0a"])
            assert pv["profiles"] == 1 and pv["fingerprints"] == 1 and pv["top"] is None
    finally:
        # The test DB is shared: two thin, low-scoring profiles left behind would change what
        # other suites' field-level assertions see (every profile confident, etc.).
        with session_scope() as s:
            runs = s.scalars(
                sa_select(Run).where(Run.settings_fingerprint.in_(["outlierfp0a", "outlierfp0b"]))
            ).all()
            # Methodology Score rows don't cascade from the run, and SQLite re-issues a
            # deleted run's id to the next insert — an orphaned Score would then collide with
            # the next test's score on (run_id, methodology_version).
            for sc in s.scalars(sa_select(Score).where(Score.run_id.in_([r.id for r in runs]))).all():
                s.delete(sc)
            for run in runs:
                s.delete(run)


def test_settings_profiles_carries_the_outlier_summary(client):
    body = client.get("/api/settings/profiles").json()
    assert "outliers" in body
    o = body["outliers"]
    assert set(o) >= {"count", "thin", "confident", "fingerprints", "threshold_z", "method"}
    assert o["count"] == len(o["fingerprints"])
    for p in body["profiles"]:
        assert "outlier" in p
