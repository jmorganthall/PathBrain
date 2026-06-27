"""Tests for the challenger race: optimistic-band ranking + the driver lifecycle."""
from __future__ import annotations

from pathbrain import challenger
from pathbrain.api.routes_settings import optimistic_overall
from pathbrain.database import session_scope
from pathbrain.models import ChallengerRace, ChallengerRaceStatus


def _spread(median: float, p75: float, n: int) -> dict:
    return {"median": median, "p25": median, "p75": p75, "min": median, "max": p75, "n": n}


def _axes(median: float, p75: float, n: int) -> dict:
    return {a: _spread(median, p75, n) for a in ("responsiveness", "smoothness", "speed")}


# ── optimistic_overall ───────────────────────────────────────────────────────


def test_optimistic_overall_uses_p75_band():
    # A wide, thin sample is optimistic (p75 ≫ median); a tight one is not.
    tight = optimistic_overall(_axes(80, 80, 10))
    wide = optimistic_overall(_axes(80, 95, 3))
    assert wide > tight
    # The tight band ≈ the plain corner Overall of the medians.
    assert abs(tight - 80.0) < 0.5


def test_optimistic_overall_margin_for_thin_samples():
    # With <2 samples there's no usable spread, so it gets median + margin benefit.
    thin = optimistic_overall(_axes(80, 80, 1))
    plain = optimistic_overall(_axes(80, 80, 10))
    assert thin > plain  # the 5-point optimism margin lifts a 1-shot challenger


def test_optimistic_overall_none_when_missing_a_corner_axis():
    partial = {"responsiveness": _spread(80, 90, 3)}  # no smoothness/speed
    assert optimistic_overall(partial) is None


# ── rank_challengers ─────────────────────────────────────────────────────────


def _field(profiles: list[dict], best_fingerprint: str | None) -> dict:
    return {"profiles": profiles, "best_fingerprint": best_fingerprint}


def test_rank_challengers_selects_leader_and_eliminates():
    field = _field(
        [
            {"fingerprint": "B", "label": "B", "confident": True, "overall": 85.0, "axis_spreads": {}},
            # contender: optimistic ~95 ≥ bar 85
            {"fingerprint": "C", "label": "C", "confident": False, "overall": None,
             "axis_spreads": _axes(80, 95, 3)},
            # laggard: optimistic ~55 < bar → eliminated
            {"fingerprint": "D", "label": "D", "confident": False, "overall": None,
             "axis_spreads": _axes(50, 55, 8)},
            # incomplete corner coverage → eliminated
            {"fingerprint": "E", "label": "E", "confident": False, "overall": None,
             "axis_spreads": {"responsiveness": _spread(90, 95, 4)}},
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
            {"fingerprint": "B", "label": "B", "confident": True, "overall": 85.0, "axis_spreads": {}},
            {"fingerprint": "C", "label": "C", "confident": False, "overall": None,
             "axis_spreads": _axes(80, 95, 3)},
        ],
        best_fingerprint="B",
    )
    _, _, leader, contenders, _ = challenger.rank_challengers(field, {"C": {}})
    assert leader is None and contenders == []


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
             "axis_spreads": {}, "settings": [{"label": "B"}]},
            {"fingerprint": "C", "label": "C", "confident": False, "overall": None,
             "axis_spreads": _axes(80, 96, 3), "settings": [{"label": "C"}]},
        ],
        best_fingerprint="B",
    )
    f2 = _field(
        [
            {"fingerprint": "B", "label": "B", "confident": True, "overall": 85.0,
             "axis_spreads": {}, "settings": [{"label": "B"}]},
            {"fingerprint": "C", "label": "C", "confident": True, "overall": 90.0,
             "axis_spreads": {}, "settings": [{"label": "C"}]},
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
