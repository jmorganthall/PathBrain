"""Tests for the challenger race: optimistic-band ranking + the driver lifecycle."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain import challenger
from pathbrain.database import session_scope
from pathbrain.models import ChallengerRace, ChallengerRaceStatus

# The race ranks under-minimum profiles by their ``optimistic`` ceiling — a field-normalized
# raw crown corner precomputed per profile by ``compute_profiles``. In these unit tests we set
# ``optimistic`` directly on the synthetic profiles; ``None`` means a required crown metric
# wasn't captured (incomplete corner coverage → eliminated).


# ── rank_challengers ─────────────────────────────────────────────────────────


def _field(profiles: list[dict], best_fingerprint: str | None) -> dict:
    return {"profiles": profiles, "best_fingerprint": best_fingerprint}


def test_rank_challengers_selects_leader_and_eliminates():
    field = _field(
        [
            {"fingerprint": "B", "label": "B", "confident": True, "overall": 85.0},
            # contender: optimistic ~95 ≥ bar 85
            {"fingerprint": "C", "label": "C", "confident": False, "overall": None,
             "optimistic": 95.0},
            # laggard: optimistic ~55 < bar → eliminated
            {"fingerprint": "D", "label": "D", "confident": False, "overall": None,
             "optimistic": 55.0},
            # incomplete corner coverage (missing required perceived_time) → eliminated
            {"fingerprint": "E", "label": "E", "confident": False, "overall": None,
             "optimistic": None},
        ],
        best_fingerprint="B",
    )
    best_fp, bar, leader, contenders, newly = challenger.rank_challengers(field, {})
    assert best_fp == "B" and bar == 85.0
    assert leader["fingerprint"] == "C"
    assert [p["fingerprint"] for p, _ in contenders] == ["C"]
    assert set(newly) == {"D", "E"}
    assert "best-case" in newly["D"]["reason"]
    assert "coverage" in newly["E"]["reason"]


def test_rank_challengers_respects_already_eliminated():
    field = _field(
        [
            {"fingerprint": "B", "label": "B", "confident": True, "overall": 85.0},
            {"fingerprint": "C", "label": "C", "confident": False, "overall": None,
             "optimistic": 95.0},
        ],
        best_fingerprint="B",
    )
    _, _, leader, contenders, _ = challenger.rank_challengers(field, {"C": {}})
    assert leader is None and contenders == []


def _nodata(fp: str) -> dict:
    return {"fingerprint": fp, "label": fp, "confident": False, "overall": None,
            "last_seen": None, "no_data": True}


def test_rank_prioritizes_threat_then_no_data():
    field = _field(
        [
            {"fingerprint": "B", "label": "B", "confident": True, "overall": 85.0},
            {"fingerprint": "C", "label": "C", "confident": False, "overall": None,
             "optimistic": 95.0},  # under-min threat to the crown
            _nodata("N"),  # no current-methodology data — raced, but sampled last
        ],
        best_fingerprint="B",
    )
    _, _, leader, contenders, newly = challenger.rank_challengers(field, {})
    # The biggest known threat is confirmed/refuted before gambling on the unknown.
    assert leader["fingerprint"] == "C"
    assert [p["fingerprint"] for p, _ in contenders] == ["C", "N"]
    assert "N" not in newly  # no-data is never eliminated, just deprioritized


def test_rank_excludes_unreachable_profiles():
    # apply() can't change scheduler/queues/upload bandwidth, so a contender on a different
    # scheduler is unreachable from the live environment — it must be eliminated (with a
    # reason), not raced (racing it would abort the whole race when we can't reach it).
    from pathbrain.settings_profile import environment_signature

    reachable = environment_signature([{"scheduler": "fq_codel", "queues": 1}])
    field = _field(
        [
            {"fingerprint": "B", "label": "B", "confident": True, "overall": 85.0,
             "settings": [{"scheduler": "fq_codel", "queues": 1}]},
            {"fingerprint": "C", "label": "C", "confident": False, "overall": None,
             "optimistic": 95.0, "settings": [{"scheduler": "fq_codel", "queues": 1}]},
            {"fingerprint": "X", "label": "X", "confident": False, "overall": None,
             "optimistic": 99.0, "settings": [{"scheduler": "fq_pie", "queues": 1}]},
        ],
        "B",
    )
    _, _, leader, contenders, newly = challenger.rank_challengers(
        field, {}, reachable_env=reachable
    )
    fps = {p["fingerprint"] for p, _ in contenders}
    assert "C" in fps          # same environment → reachable
    assert "X" not in fps      # different scheduler → unreachable, excluded
    assert "unreachable" in newly["X"]["reason"]
    assert leader["fingerprint"] == "C"


def test_eliminations_tag_structural_vs_provisional():
    # The driver relies on this tag: only *structural* eliminations (unreachable — the live
    # environment can't change mid-race) may be frozen. *Provisional* ones (optimistic < bar /
    # incomplete corner coverage) are field-relative — the percentile scale re-normalizes as
    # iterations accrue — so the driver must re-evaluate them each loop rather than persist them.
    from pathbrain.settings_profile import environment_signature

    reachable = environment_signature([{"scheduler": "fq_codel", "queues": 1}])
    field = _field(
        [
            {"fingerprint": "B", "label": "B", "confident": True, "overall": 85.0,
             "settings": [{"scheduler": "fq_codel", "queues": 1}]},
            # provisional: best-case ceiling below the bar
            {"fingerprint": "D", "label": "D", "confident": False, "overall": None,
             "optimistic": 55.0, "settings": [{"scheduler": "fq_codel", "queues": 1}]},
            # provisional: missing a required crown metric (no ceiling)
            {"fingerprint": "E", "label": "E", "confident": False, "overall": None,
             "optimistic": None, "settings": [{"scheduler": "fq_codel", "queues": 1}]},
            # structural: unreachable environment
            {"fingerprint": "X", "label": "X", "confident": False, "overall": None,
             "optimistic": 99.0, "settings": [{"scheduler": "fq_pie", "queues": 1}]},
        ],
        best_fingerprint="B",
    )
    _, _, _, _, newly = challenger.rank_challengers(field, {}, reachable_env=reachable)
    assert newly["X"]["structural"] is True     # unreachable → persist
    assert newly["D"]["structural"] is False    # optimistic < bar → re-evaluate
    assert newly["E"]["structural"] is False    # incomplete coverage → re-evaluate


def test_apply_profile_tolerates_nonwritable_mismatch(monkeypatch):
    # Writable params took (no planned change remains) but the read-back fingerprint
    # differs — a non-writable field. Must NOT abort the race; just log and proceed.
    monkeypatch.setattr(challenger, "plan_apply", lambda target, live: ([], []))
    monkeypatch.setattr(challenger, "_apply_all", lambda provider, changes: None)
    monkeypatch.setattr(challenger, "normalize", lambda x: [])
    monkeypatch.setattr(challenger, "fingerprint", lambda n: "actual-fp")

    class _P:
        def discover(self):
            return []

    challenger._apply_profile(_P(), [{"label": "x"}], "wanted-fp")  # does not raise


def test_apply_profile_raises_when_writable_did_not_take(monkeypatch):
    import pytest

    # A writable param is still pending after the apply → a genuine apply failure.
    monkeypatch.setattr(challenger, "plan_apply", lambda target, live: ([{"field": "quantum"}], []))
    monkeypatch.setattr(challenger, "_apply_all", lambda provider, changes: None)
    monkeypatch.setattr(challenger, "normalize", lambda x: [])

    class _P:
        def discover(self):
            return []

    with pytest.raises(RuntimeError, match="did not take"):
        challenger._apply_profile(_P(), [{"label": "x"}], "wanted-fp")


def test_rank_bootstrap_with_no_confident_best():
    # No confident best (e.g. right after a methodology change) → still yields contenders.
    field = _field(
        [
            _nodata("N"),
            {"fingerprint": "C", "label": "C", "confident": False, "overall": None,
             "optimistic": 95.0},
        ],
        best_fingerprint=None,
    )
    best, bar, leader, contenders, _ = challenger.rank_challengers(field, {})
    assert best is None and bar is None
    assert {p["fingerprint"] for p, _ in contenders} == {"N", "C"}
    # Even in bootstrap, the profile that already has data (C) is sampled before the unknown.
    assert leader["fingerprint"] == "C"


def test_rank_stale_confident_ordered_by_closeness():
    now = datetime(2026, 1, 2, 12, 0, 0)
    stale = (now - timedelta(minutes=300)).isoformat()  # 5h > 180
    fresh = now.isoformat()
    field = _field(
        [
            {"fingerprint": "B", "label": "B", "confident": True, "overall": 90.0,
             "last_seen": fresh},  # the winner
            {"fingerprint": "S1", "label": "S1", "confident": True, "overall": 88.0,
             "last_seen": stale},  # close to winner, stale
            {"fingerprint": "S2", "label": "S2", "confident": True, "overall": 70.0,
             "last_seen": stale},  # far from winner, stale
            {"fingerprint": "F", "label": "F", "confident": True, "overall": 89.0,
             "last_seen": fresh},  # close but fresh → not a contender
        ],
        best_fingerprint="B",
    )
    _, _, leader, contenders, newly = challenger.rank_challengers(
        field, {}, now=now, stale_minutes=180
    )
    # Both stale confident profiles race, closest-to-winner first; fresh F and winner B excluded.
    assert [p["fingerprint"] for p, _ in contenders] == ["S1", "S2"]
    assert leader["fingerprint"] == "S1"
    assert not newly  # stale-confident are never eliminated


def test_rank_full_priority_threat_then_stale_then_no_data():
    # The full ordering across all three tiers: confront the biggest known threat first,
    # then refresh nearby stale incumbents, then fill in unknowns last.
    now = datetime(2026, 1, 2, 12, 0, 0)
    stale = (now - timedelta(minutes=300)).isoformat()  # 5h > 180
    field = _field(
        [
            {"fingerprint": "B", "label": "B", "confident": True, "overall": 85.0,
             "last_seen": now.isoformat()},  # the crown
            {"fingerprint": "T1", "label": "T1", "confident": False, "overall": None,
             "optimistic": 99.0},  # under-min, highest ceiling → biggest threat
            {"fingerprint": "T2", "label": "T2", "confident": False, "overall": None,
             "optimistic": 90.0},  # under-min, lower ceiling
            {"fingerprint": "S", "label": "S", "confident": True, "overall": 84.0,
             "last_seen": stale},  # stale-but-nearby incumbent
            _nodata("N"),  # no data → filled in last
        ],
        best_fingerprint="B",
    )
    _, _, leader, contenders, _ = challenger.rank_challengers(
        field, {}, now=now, stale_minutes=180
    )
    assert [p["fingerprint"] for p, _ in contenders] == ["T1", "T2", "S", "N"]
    assert leader["fingerprint"] == "T1"  # biggest threat to the crown sampled first


# ── driver lifecycle (restore vs auto-promote) ───────────────────────────────


class _FakeProvider:
    name = "fake"

    def discover(self):
        return []


def _drive_with(monkeypatch, *, auto_promote: bool) -> tuple[ChallengerRace, dict]:
    """Run _drive synchronously over a scripted 2-step field, with the firewall/run
    primitives stubbed. Returns (final row, call-spy counts)."""
    spy = {"apply_profile": [], "restore": 0, "runs": 0}

    # Scripted field: step 1 has a strong under-min challenger C; step 2 C is confident
    # and crowned best → winner, no contenders left → loop breaks.
    f1 = _field(
        [
            {"fingerprint": "B", "label": "B", "confident": True, "overall": 85.0,
             "settings": [{"label": "B"}]},
            {"fingerprint": "C", "label": "C", "confident": False, "overall": None,
             "optimistic": 96.0, "settings": [{"label": "C"}]},
        ],
        best_fingerprint="B",
    )
    f2 = _field(
        [
            {"fingerprint": "B", "label": "B", "confident": True, "overall": 85.0,
             "settings": [{"label": "B"}]},
            {"fingerprint": "C", "label": "C", "confident": True, "overall": 90.0,
             "settings": [{"label": "C"}]},
        ],
        best_fingerprint="C",
    )
    scripted = iter([f1, f2])
    last = {"f": f2}

    def fake_field(_session):
        try:
            last["f"] = next(scripted)
        except StopIteration:
            pass
        return last["f"]

    def fake_apply_profile(provider, settings, fp):
        spy["apply_profile"].append(fp)

    def fake_apply_all(provider, changes):
        spy["restore"] += 1

    def fake_create_run(**kwargs):
        spy["runs"] += 1
        return spy["runs"]

    monkeypatch.setattr(challenger, "get_provider", lambda: _FakeProvider())
    monkeypatch.setattr(challenger, "normalize", lambda x: [])
    # These tests exercise the drive loop, not reachability — make every profile reachable.
    monkeypatch.setattr(challenger, "environment_signature", lambda x: "env")
    monkeypatch.setattr(challenger, "plan_apply", lambda target, live: ([], []))
    monkeypatch.setattr(challenger, "_apply_all", fake_apply_all)
    monkeypatch.setattr(challenger, "_apply_profile", fake_apply_profile)
    monkeypatch.setattr(challenger, "create_run", fake_create_run)
    monkeypatch.setattr(challenger, "execute_run", lambda rid: None)
    monkeypatch.setattr(challenger, "_field", fake_field)

    with session_scope() as s:
        race = ChallengerRace(
            status=ChallengerRaceStatus.PENDING, time_budget_s=300,
            auto_promote=auto_promote, eliminated=[],
        )
        s.add(race)
        s.flush()
        rid = race.id

    challenger._state.update({"active": True, "id": rid, "cancel": False})
    challenger._drive(rid)

    with session_scope() as s:
        return s.get(ChallengerRace, rid), spy


def test_drive_auto_promotes_winner(monkeypatch):
    race, spy = _drive_with(monkeypatch, auto_promote=True)
    assert race.status == ChallengerRaceStatus.COMPLETE
    assert race.winner_fingerprint == "C"
    assert race.promoted is True
    assert race.iterations_run == 1  # sampled C once, then C confirmed → break
    # Applied C during the race AND promoted C at the end; never ran the restore path.
    assert spy["apply_profile"] == ["C", "C"]
    assert spy["restore"] == 0


def test_drive_restores_baseline_when_not_promoting(monkeypatch):
    race, spy = _drive_with(monkeypatch, auto_promote=False)
    assert race.status == ChallengerRaceStatus.COMPLETE
    assert race.winner_fingerprint == "C"
    assert race.promoted is False
    # Applied C once during the race; finalized via the baseline-restore path.
    assert spy["apply_profile"] == ["C"]
    assert spy["restore"] == 1


# ── incumbent refresh (contemporaneous bar) ──────────────────────────────────


def test_incumbent_stale_logic():
    now = datetime(2026, 1, 2, 12, 0, 0)
    old = (now - timedelta(minutes=90)).isoformat()
    fresh = (now - timedelta(minutes=10)).isoformat()
    assert challenger._incumbent_stale(old, 60, now) is True
    assert challenger._incumbent_stale(fresh, 60, now) is False
    # 0 disables the refresh entirely.
    assert challenger._incumbent_stale(old, 0, now) is False
    # Unknown/unparseable age → never churn the firewall.
    assert challenger._incumbent_stale(None, 60, now) is False
    assert challenger._incumbent_stale("not-a-date", 60, now) is False
    # A tz-aware last_seen is normalized to naive UTC before comparing.
    aware = (now - timedelta(minutes=90)).replace(tzinfo=timezone.utc).isoformat()
    assert challenger._incumbent_stale(aware, 60, now) is True


def _run_drive(monkeypatch, fields, *, auto_promote=False):
    """Run _drive synchronously over a scripted list of fields, primitives stubbed."""
    spy = {"apply_profile": [], "restore": 0, "runs": 0}
    scripted = iter(fields)
    last = {"f": fields[-1]}

    def fake_field(_session):
        try:
            last["f"] = next(scripted)
        except StopIteration:
            pass
        return last["f"]

    def fake_create_run(**kwargs):
        spy["runs"] += 1
        return spy["runs"]

    monkeypatch.setattr(challenger, "get_provider", lambda: _FakeProvider())
    monkeypatch.setattr(challenger, "normalize", lambda x: [])
    # Drive-loop tests aren't about reachability — make every profile reachable.
    monkeypatch.setattr(challenger, "environment_signature", lambda x: "env")
    monkeypatch.setattr(challenger, "plan_apply", lambda target, live: ([], []))
    monkeypatch.setattr(challenger, "_apply_all", lambda provider, changes: spy.__setitem__("restore", spy["restore"] + 1))
    monkeypatch.setattr(challenger, "_apply_profile", lambda provider, settings, fp: spy["apply_profile"].append(fp))
    monkeypatch.setattr(challenger, "create_run", fake_create_run)
    monkeypatch.setattr(challenger, "execute_run", lambda rid: None)
    monkeypatch.setattr(challenger, "_field", fake_field)

    with session_scope() as s:
        race = ChallengerRace(
            status=ChallengerRaceStatus.PENDING, time_budget_s=300,
            auto_promote=auto_promote, eliminated=[],
        )
        s.add(race)
        s.flush()
        rid = race.id

    challenger._state.update({"active": True, "id": rid, "cancel": False})
    challenger._drive(rid)
    with session_scope() as s:
        return s.get(ChallengerRace, rid), spy


def test_drive_refreshes_stale_incumbent(monkeypatch):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    stale = (now - timedelta(minutes=120)).isoformat()
    fresh = now.isoformat()
    b_stale = {"fingerprint": "B", "label": "B", "confident": True, "overall": 85.0,
               "settings": [{"label": "B"}], "last_seen": stale}
    b_fresh = {**b_stale, "last_seen": fresh}
    c_under = {"fingerprint": "C", "label": "C", "confident": False, "overall": None,
               "optimistic": 96.0, "settings": [{"label": "C"}], "last_seen": fresh}
    c_conf = {"fingerprint": "C", "label": "C", "confident": True, "overall": 90.0,
              "settings": [{"label": "C"}], "last_seen": fresh}
    # Step 1: incumbent B is 2h stale → re-measure B first (don't touch a challenger yet).
    # Step 2: B fresh → sample challenger C. Step 3: C confirmed best → winner, race ends.
    f1 = _field([b_stale, c_under], best_fingerprint="B")
    f2 = _field([b_fresh, c_under], best_fingerprint="B")
    f3 = _field([b_fresh, c_conf], best_fingerprint="C")

    race, spy = _run_drive(monkeypatch, [f1, f2, f3], auto_promote=False)
    assert race.incumbent_refreshes == 1
    assert race.iterations_run == 2  # one incumbent refresh + one challenger iteration
    # Re-measured B (the stale incumbent), then sampled challenger C.
    assert spy["apply_profile"] == ["B", "C"]
    assert spy["restore"] == 1
    assert race.winner_fingerprint == "C"


def test_drive_skips_refresh_when_incumbent_fresh(monkeypatch):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fresh = now.isoformat()
    b_fresh = {"fingerprint": "B", "label": "B", "confident": True, "overall": 85.0,
               "settings": [{"label": "B"}], "last_seen": fresh}
    c_under = {"fingerprint": "C", "label": "C", "confident": False, "overall": None,
               "optimistic": 96.0, "settings": [{"label": "C"}], "last_seen": fresh}
    c_conf = {"fingerprint": "C", "label": "C", "confident": True, "overall": 90.0,
              "settings": [{"label": "C"}], "last_seen": fresh}
    f1 = _field([b_fresh, c_under], best_fingerprint="B")
    f2 = _field([b_fresh, c_conf], best_fingerprint="C")

    race, spy = _run_drive(monkeypatch, [f1, f2], auto_promote=False)
    assert race.incumbent_refreshes == 0
    assert spy["apply_profile"] == ["C"]  # straight to the challenger, no refresh


def test_drive_bootstraps_no_data_with_no_confident_best(monkeypatch):
    # No confident best at all (e.g. post-methodology-change): the race still runs and
    # samples the no-data profile, which then becomes the confident winner.
    c_nodata = {"fingerprint": "C", "label": "C", "confident": False, "overall": None,
                "no_data": True, "settings": [{"label": "C"}],
                "last_seen": None}
    c_conf = {"fingerprint": "C", "label": "C", "confident": True, "overall": 90.0,
              "settings": [{"label": "C"}], "last_seen": None}
    f1 = _field([c_nodata], best_fingerprint=None)
    f2 = _field([c_conf], best_fingerprint="C")

    race, spy = _run_drive(monkeypatch, [f1, f2], auto_promote=False)
    assert spy["apply_profile"] == ["C"]  # raced the no-data profile despite no confident best
    assert race.winner_fingerprint == "C"
    assert spy["restore"] == 1  # baseline restored at the end
