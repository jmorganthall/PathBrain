"""Tests for the crown follower: churn ledger, follow-the-crown apply, guards, stats."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from pathbrain import coordinator, crown_follower
from pathbrain.config_store import save_config
from pathbrain.database import session_scope
from pathbrain.models import CrownEvent
from pathbrain.providers import get_provider
from pathbrain.providers.mock import _OVERRIDES
from pathbrain.settings_profile import SQM_OFF_FINGERPRINT, fingerprint, normalize


def _reset_state():
    crown_follower._state.update(
        {"last_full_check": 0.0, "backstop_s": None, "retry_at": None, "last_result": None, "cache": None}
    )
    with crown_follower._pending_lock:
        crown_follower._pending.clear()


@pytest.fixture(autouse=True)
def _clean():
    """Each test starts with an empty ledger, default config, and a pristine mock firewall."""
    _OVERRIDES.clear()
    _reset_state()
    with session_scope() as session:
        session.query(CrownEvent).delete()
        save_config(session, {"crown_follow": {"enabled": False, "interval_minutes": 360}})
    yield
    _OVERRIDES.clear()
    _reset_state()
    with session_scope() as session:
        session.query(CrownEvent).delete()
        save_config(session, {"crown_follow": {"enabled": False, "interval_minutes": 360}})


def _live_norm() -> list[dict]:
    return normalize(get_provider().discover())


def _profile(settings: list[dict], overall: float = 90.0) -> dict:
    return {
        "fingerprint": fingerprint(settings),
        "label": "test-profile",
        "confident": True,
        "overall": overall,
        "settings": settings,
    }


def _field_for(*profiles: dict, best: str | None = None) -> dict:
    return {
        "profiles": list(profiles),
        "best_fingerprint": best if best is not None else (profiles[0]["fingerprint"] if profiles else None),
        # The quick-filter cache stamps the confidence bar it was computed under; mirror
        # the config default so a synthetic field doesn't read as "config changed".
        "min_iterations": 15,
    }


def _off_crown_target(quantum: int = 2000) -> list[dict]:
    """The live mock profile with a different (writable) quantum — reachable, off-crown."""
    target = copy.deepcopy(_live_norm())
    target[0]["quantum"] = quantum
    return target


def _events(kind: str | None = None) -> list[CrownEvent]:
    with session_scope() as session:
        rows = session.query(CrownEvent).order_by(CrownEvent.id.asc()).all()
        session.expunge_all()
    return [r for r in rows if kind is None or r.kind == kind]


def _enable():
    with session_scope() as session:
        save_config(session, {"crown_follow": {"enabled": True}})


# ── Tracking (churn ledger) ──────────────────────────────────────────────────────────


def test_first_observation_marks_tracking_start_not_a_change(monkeypatch):
    live = _live_norm()
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(live)))
    result = crown_follower.check()
    assert result["crown_fingerprint"] == fingerprint(live)
    assert result["crown_changed"] is False  # first observation, not a change
    assert result["on_crown"] is True        # firewall already on the crown
    events = _events("change")
    assert len(events) == 1 and events[0].previous_fingerprint is None
    with session_scope() as session:
        stats = crown_follower.stats(session)
    assert stats["total_changes"] == 0
    assert stats["tracked_since"] is not None
    assert stats["current_crown_fingerprint"] == fingerprint(live)


def test_crown_change_is_recorded_and_counted(monkeypatch):
    live = _live_norm()
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(live)))
    crown_follower.check()

    target = _off_crown_target()
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(target)))
    result = crown_follower.check()
    assert result["crown_changed"] is True
    assert result["applied"] is False  # disabled: track only
    events = _events("change")
    assert len(events) == 2
    assert events[1].previous_fingerprint == fingerprint(live)
    assert events[1].fingerprint == fingerprint(target)
    with session_scope() as session:
        stats = crown_follower.stats(session)
    assert stats["total_changes"] == 1
    assert stats["changes_24h"] == 1
    assert stats["current_crown_fingerprint"] == fingerprint(target)


def test_no_crown_records_nothing(monkeypatch):
    monkeypatch.setattr(
        crown_follower, "_compute_field", lambda s: {"profiles": [], "best_fingerprint": None}
    )
    result = crown_follower.check()
    assert result["crown_fingerprint"] is None
    assert _events() == []


def test_unchanged_crown_records_no_new_event(monkeypatch):
    live = _live_norm()
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(live)))
    crown_follower.check()
    crown_follower.check()
    assert len(_events("change")) == 1


# ── Following (the firewall write) ───────────────────────────────────────────────────


def test_apply_when_enabled_and_off_crown(monkeypatch):
    _enable()
    target = _off_crown_target(quantum=2000)
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(target)))
    result = crown_follower.check()
    assert result["applied"] is True
    assert result["on_crown"] is True
    assert int(_OVERRIDES["quantum"]) == 2000  # the write actually landed on the firewall
    events = _events("change")
    assert len(events) == 1 and events[0].applied is True
    assert "change(s) written" in (events[0].detail or "")


def test_disabled_tracks_but_never_writes(monkeypatch):
    target = _off_crown_target(quantum=3000)
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(target)))
    result = crown_follower.check()
    assert result["applied"] is False
    assert result["on_crown"] is False
    assert "disabled" in (result["apply_skipped"] or "")
    assert "quantum" not in _OVERRIDES  # firewall untouched


def test_apply_without_crown_change_records_apply_event(monkeypatch):
    # Crown already recorded, then the follower is enabled while the firewall sits
    # elsewhere: the write happens with no crown change → a standalone "apply" row.
    target = _off_crown_target(quantum=2500)
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(target)))
    crown_follower.check()  # disabled: records the crown, no write
    _enable()
    result = crown_follower.check()
    assert result["applied"] is True and result["crown_changed"] is False
    assert len(_events("change")) == 1
    applies = _events("apply")
    assert len(applies) == 1 and applies[0].applied is True


def test_sqm_off_crown_is_never_applied(monkeypatch):
    _enable()
    target = copy.deepcopy(_live_norm())
    target[0]["enabled"] = False  # the collapsed "SQM off" profile
    profile = _profile(target)
    assert profile["fingerprint"] == SQM_OFF_FINGERPRINT
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(profile))
    result = crown_follower.check()
    assert result["applied"] is False
    assert "SQM off" in (result["apply_skipped"] or "")
    assert "quantum" not in _OVERRIDES


def test_unreachable_crown_is_skipped(monkeypatch):
    _enable()
    target = copy.deepcopy(_live_norm())
    target[0]["scheduler"] = "fq_pie"  # non-writable field → unreachable environment
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(target)))
    result = crown_follower.check()
    assert result["applied"] is False
    assert "unreachable" in (result["apply_skipped"] or "")


def test_busy_coordinator_defers_the_apply(monkeypatch):
    _enable()
    target = _off_crown_target(quantum=4000)
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(target)))
    with coordinator.hold("test-session"):
        result = crown_follower.check()
    assert result["applied"] is False
    assert (result["apply_skipped"] or "").startswith("deferred")
    assert "quantum" not in _OVERRIDES
    # The change is still recorded — tracking never defers.
    assert len(_events("change")) == 1


# ── step(): event-driven gating ──────────────────────────────────────────────────────


def test_step_first_tick_runs_backstop_then_goes_quiet(monkeypatch):
    import time

    calls = []
    monkeypatch.setattr(crown_follower, "check", lambda: calls.append(1) or {"applied": False})
    # First tick: no cached backstop yet → the startup full check runs (seeds the cache).
    assert crown_follower.step() is False
    assert len(calls) == 1
    assert crown_follower._state["backstop_s"] == 360 * 60.0
    assert crown_follower._state["last_full_check"] <= time.time()
    # Quiet tick: no completed runs, backstop hours away → no check.
    assert crown_follower.step() is False
    assert len(calls) == 1


def test_step_quiet_tick_does_no_io(monkeypatch):
    # After the backstop is cached, a tick with nothing pending must not even open a
    # DB session — the "hyper-efficient" contract for the every-15s scheduler tick.
    import time

    crown_follower._state["backstop_s"] = 3600.0
    crown_follower._state["last_full_check"] = time.time()

    def _boom(*a, **k):
        raise AssertionError("quiet tick touched the DB / ran a check")

    monkeypatch.setattr(crown_follower, "session_scope", _boom)
    monkeypatch.setattr(crown_follower, "check", _boom)
    monkeypatch.setattr(crown_follower, "_needs_full_check", _boom)
    assert crown_follower.step() is False


def test_run_completion_below_crown_skips_full_check(monkeypatch):
    # A completed run on a profile that stays below the crown must NOT trigger the full
    # standings recompute — only the cheap single-profile filter runs.
    live = _live_norm()
    calls = []
    real_field = _field_for(_profile(live, overall=90.0))

    def _counting_field(session):
        calls.append(1)
        return real_field

    monkeypatch.setattr(crown_follower, "_compute_field", _counting_field)
    crown_follower.step()  # startup backstop check → seeds the cache
    assert len(calls) == 1

    # A run completed on an unknown profile with no comparable data → below the crown.
    crown_follower.notify_run_complete("feedbeef0000")
    assert crown_follower.step() is False
    assert len(calls) == 1  # no second full recompute


def test_run_completion_that_could_take_crown_triggers_full_check(monkeypatch):
    from pathbrain.methodology import ensure_current_methodology, overall_metrics
    from pathbrain.config_store import get_config
    from pathbrain.models import Run, RunStatus, Score

    live = _live_norm()
    calls = []
    field = _field_for(_profile(live, overall=50.0))
    monkeypatch.setattr(
        crown_follower, "_compute_field", lambda s: calls.append(1) or field
    )
    crown_follower.step()  # seed cache: crown overall 50
    assert len(calls) == 1

    # Materialize a confident challenger in the DB whose weighted crown subscores beat 50.
    challenger_fp = "beefbeef0001"
    with session_scope() as session:
        methodology = ensure_current_methodology(session, get_config(session))
        crown_metrics, _ = overall_metrics(methodology.definition or {})
        run = Run(
            status=RunStatus.COMPLETE,
            settings_fingerprint=challenger_fp,
            settings=live,
            iterations=50,
        )
        session.add(run)
        session.flush()
        # The suite-wide SQLite reuses row ids after other tests delete runs; an orphaned
        # Score for the recycled id would violate the (run_id, methodology) uniqueness.
        session.query(Score).filter(Score.run_id == run.id).delete()
        session.add(
            Score(
                run_id=run.id,
                methodology_version=methodology.version,
                comparability="exact",
                subscores={m: 95.0 for m in crown_metrics},
                axis_scores={"overall": 95.0},
                metric_values={},
            )
        )
        session.commit()
        run_id = run.id

    try:
        crown_follower.notify_run_complete(challenger_fp)
        crown_follower.step()
        assert len(calls) == 2  # the challenger justified a full recompute
    finally:
        with session_scope() as session:
            session.query(Score).filter(Score.run_id == run_id).delete()
            session.query(Run).filter(Run.id == run_id).delete()
            session.commit()


def test_run_completion_off_crown_while_enabled_triggers_full_check(monkeypatch):
    # Following armed + a run measurably happened on another profile → the firewall
    # drifted (or an engine raced others); the full check must run so it can re-apply.
    _enable()
    live = _live_norm()
    calls = []
    field = _field_for(_profile(live, overall=90.0))
    monkeypatch.setattr(
        crown_follower, "_compute_field", lambda s: calls.append(1) or field
    )
    crown_follower.step()  # seed cache; firewall on crown
    assert len(calls) == 1
    crown_follower.notify_run_complete("0ffcafe00001")
    crown_follower.step()
    assert len(calls) == 2


# ── Stats math ───────────────────────────────────────────────────────────────────────


def test_stats_reign_and_window_math():
    now = datetime.now(timezone.utc)
    rows = [
        ("A", None, now - timedelta(days=10)),   # tracking starts
        ("B", "A", now - timedelta(days=8)),     # reign A: 48h
        ("A", "B", now - timedelta(days=2)),     # reign B: 144h
        ("C", "A", now - timedelta(hours=1)),    # reign A: 47h; C reigns now
    ]
    with session_scope() as session:
        for fp, prev, at in rows:
            session.add(
                CrownEvent(kind="change", fingerprint=fp, previous_fingerprint=prev, created_at=at)
            )
            session.flush()
    with session_scope() as session:
        stats = crown_follower.stats(session, now=now)
    assert stats["total_changes"] == 3
    assert stats["changes_24h"] == 1
    assert stats["changes_7d"] == 2
    assert stats["changes_30d"] == 3
    assert stats["distinct_crowns_30d"] == 3  # B, A, C
    assert stats["current_crown_fingerprint"] == "C"
    assert stats["current_reign_hours"] == 1.0
    assert stats["median_reign_hours"] == 48.0
    assert stats["mean_reign_hours"] == pytest.approx(79.7, abs=0.1)
    assert stats["changes_per_day"] == pytest.approx(0.3, abs=0.01)


# ── API ──────────────────────────────────────────────────────────────────────────────


def test_api_status_config_and_toggle(client):
    res = client.get("/api/settings/crown-follow")
    assert res.status_code == 200
    body = res.json()
    assert body["config"] == {"enabled": False, "interval_minutes": 360.0, "policy": "pooled"}
    assert "stats" in body and "events" in body and "status" in body

    res = client.post("/api/settings/crown-follow", json={"enabled": True, "interval_minutes": 10})
    assert res.status_code == 200
    assert res.json()["config"] == {"enabled": True, "interval_minutes": 10.0, "policy": "pooled"}
    assert client.get("/api/settings/crown-follow").json()["config"]["enabled"] is True

    assert client.post("/api/settings/crown-follow", json={}).status_code == 400
    assert (
        client.post("/api/settings/crown-follow", json={"interval_minutes": 1}).status_code == 400
    )


def test_api_sync_runs_a_check(client, monkeypatch):
    live = _live_norm()
    monkeypatch.setattr(crown_follower, "_compute_field", lambda s: _field_for(_profile(live)))
    res = client.post("/api/settings/crown-follow/sync")
    assert res.status_code == 200
    assert res.json()["result"]["crown_fingerprint"] == fingerprint(live)


def test_the_follow_best_popover_reads_in_call_signs_not_settings_summaries(client):
    """A crown ledger printed as stored labels is two settings summaries and an arrow —
    *"Download: 880Mbit q7313 t7 i45 ecn | Upload: … → Download: 880Mbit q450 …"* — in a
    340px popover. It says what changed and never says **who**, which is the entire reason
    call signs exist. Resolved by fingerprint at read time, like the duel tape and the
    standings, so a rename lands here too.
    """
    summary = "Download: 880Mbit q7313 t7 i45 ecn | Upload: 880Mbit q450 t3 i60 ecn"
    with session_scope() as session:
        session.add(
            CrownEvent(
                kind="change",
                fingerprint="crownaaaaaaa",
                previous_fingerprint="crownbbbbbbb",
                label=summary,
                previous_label=summary,
            )
        )

    body = client.get("/api/settings/crown-follow").json()
    event = body["events"][0]
    assert event["name"] and event["name"] != event["label"]
    assert event["previous_name"] and event["previous_name"] != event["previous_label"]
    # The technical summary is kept beside the name, not replaced by it.
    assert event["label"] == summary


# ── The crowning policy: which verdict the follower actually writes ──────────────────


def _duel_champion_policy(monkeypatch, champion_fp: str | None, *, decisive: bool = True):
    """Arm `crown_follow.policy = "duel"` with a stubbed ladder champion."""
    from pathbrain import duel as duel_mod

    with session_scope() as session:
        save_config(session, {"crown_follow": {"policy": "duel"}})
    monkeypatch.setattr(
        duel_mod,
        "latest_champion",
        lambda session, max_age_days: (
            None
            if champion_fp is None
            else {
                "fingerprint": champion_fp,
                "label": "champ",
                "duel_id": 41,
                "finished_at": "2026-08-27T05:01:00",
                "decisive": decisive,
                "consecutive_sessions": 1,
                "provisional": False,
            }
        ),
    )


def test_under_the_duel_policy_the_follower_writes_the_CHAMPION_not_the_pooled_crown(
    monkeypatch,
):
    """The policy selects, the follower applies — so "duel" must write the champion.

    This is the contract the whole crowning module exists for and nothing pinned it. The
    pooled crown stays the tracked statistic (it is what the churn ledger counts), which
    is exactly why it is easy to write the wrong one.
    """
    live = _live_norm()
    champion = _off_crown_target(2000)          # reachable, and NOT where the firewall is
    pooled = _off_crown_target(3000)            # a different profile entirely
    champion_fp, pooled_fp = fingerprint(champion), fingerprint(pooled)
    monkeypatch.setattr(
        crown_follower,
        "_compute_field",
        lambda s: _field_for(
            _profile(pooled, overall=95.0), _profile(champion, overall=80.0), best=pooled_fp
        ),
    )
    _duel_champion_policy(monkeypatch, champion_fp)
    _enable()

    result = crown_follower.check()

    assert result["policy"] == "duel"
    assert result["governing_source"] == "duel"
    assert result["governing_fingerprint"] == champion_fp
    # The pooled crown is still tracked and reported — it is just not what was written.
    assert result["crown_fingerprint"] == pooled_fp
    assert result["applied"] is True
    assert result["live_fingerprint"] == champion_fp, "the CHAMPION is on the firewall"
    assert fingerprint(_live_norm()) != pooled_fp


def test_the_duel_policy_falls_back_to_pooled_and_says_so(monkeypatch):
    """No fresh decisive champion → the pooled crown governs, and `governing_source`
    reports "pooled" rather than claiming a duel verdict it doesn't have."""
    pooled = _off_crown_target(3000)
    pooled_fp = fingerprint(pooled)
    monkeypatch.setattr(
        crown_follower, "_compute_field", lambda s: _field_for(_profile(pooled), best=pooled_fp)
    )
    _duel_champion_policy(monkeypatch, None)
    _enable()

    result = crown_follower.check()
    assert result["policy"] == "duel"
    assert result["governing_source"] == "pooled"
    assert result["governing_fingerprint"] == pooled_fp
    assert result["applied"] is True


def test_a_champion_missing_from_the_field_falls_back_HONESTLY(monkeypatch):
    """The champion has no row in the field (thin, or quarantined by a methodology change),
    so the follower falls back to the pooled crown — and must SAY pooled.

    It used to keep `governing_source: "duel"` while writing the pooled profile, which is
    the worst of both: the firewall goes somewhere the policy didn't choose and the status
    reports the choice it didn't make.
    """
    pooled = _off_crown_target(3000)
    pooled_fp = fingerprint(pooled)
    monkeypatch.setattr(
        crown_follower, "_compute_field", lambda s: _field_for(_profile(pooled), best=pooled_fp)
    )
    _duel_champion_policy(monkeypatch, "achampionnotinthefield")
    _enable()

    result = crown_follower.check()
    assert result["governing_fingerprint"] == pooled_fp
    assert result["governing_source"] == "pooled"
    assert "not in the measured field" in (result["governing_detail"] or "")


def test_a_crown_change_row_is_only_marked_applied_when_THAT_crown_was_written(
    monkeypatch,
):
    """The `applied` flag on a crown-change row must mean "this profile was written".

    Under the duel policy the change row records the POOLED crown while the follower
    writes the champion, so copying `applied` onto it made the popover read
    "Voyaging Echo → Eternal Emu · applied" for a profile that never touched the firewall.
    """
    live = _live_norm()
    champion = _off_crown_target(2000)
    pooled_a, pooled_b = _off_crown_target(3000), _off_crown_target(4000)
    champion_fp = fingerprint(champion)
    monkeypatch.setattr(
        crown_follower,
        "_compute_field",
        lambda s: _field_for(
            _profile(pooled_a), _profile(champion), best=fingerprint(pooled_a)
        ),
    )
    _duel_champion_policy(monkeypatch, champion_fp)
    _enable()
    crown_follower.check()  # first observation: marks tracking start

    # The pooled crown changes. The follower is already on the champion, so it writes
    # nothing — and the change row must not claim otherwise.
    monkeypatch.setattr(
        crown_follower,
        "_compute_field",
        lambda s: _field_for(
            _profile(pooled_b), _profile(champion), best=fingerprint(pooled_b)
        ),
    )
    result = crown_follower.check()
    assert result["crown_changed"] is True
    assert result["applied"] is False, "already on the champion — nothing to write"
    change = _events("change")[-1]
    assert change.fingerprint == fingerprint(pooled_b)
    assert change.applied is False


def test_a_pooled_crown_change_is_not_marked_applied_when_the_champion_was_written(
    monkeypatch,
):
    """The reported symptom, end to end.

    Under `policy="duel"` the popover's crown-change list read *"Voyaging Echo → Eternal
    Emu · applied"* while the profile actually on the firewall was the duel champion. The
    pooled crown changed (tracking is always on) and the follower wrote the champion in the
    same check, so the `applied` flag landed on a row naming a profile it never wrote.
    """
    _enable()
    champion = _off_crown_target(2000)
    pooled_a, pooled_b = _off_crown_target(3000), _off_crown_target(4000)
    champion_fp = fingerprint(champion)
    _duel_champion_policy(monkeypatch, champion_fp)

    monkeypatch.setattr(
        crown_follower,
        "_compute_field",
        lambda s: _field_for(_profile(pooled_a), _profile(champion), best=fingerprint(pooled_a)),
    )
    crown_follower.check()  # tracking starts; the champion is written

    # The firewall drifts off the champion (a duel session restoring its own baseline, a
    # profile test, a sweep) AND the pooled crown moves — the two together are what put the
    # flag on the wrong row.
    _OVERRIDES["quantum"] = "9999"
    monkeypatch.setattr(
        crown_follower,
        "_compute_field",
        lambda s: _field_for(_profile(pooled_b), _profile(champion), best=fingerprint(pooled_b)),
    )
    result = crown_follower.check()

    assert result["crown_changed"] is True
    assert result["applied"] is True
    assert result["governing_fingerprint"] == champion_fp
    assert int(_OVERRIDES["quantum"]) == 2000, "the CHAMPION is what reached the firewall"

    change = _events("change")[-1]
    assert change.fingerprint == fingerprint(pooled_b)
    assert change.applied is False, "the pooled crown was not what got written"
    assert "followed the duel crown instead" in (change.detail or "")
    # …and the write is recorded on its own row, naming what actually landed.
    apply_row = _events("apply")[-1]
    assert apply_row.fingerprint == champion_fp and apply_row.applied is True
