"""The duel ladder across a methodology change.

A publish — a new crown metric, a changed site list, a changed browser client — makes
every run on record incomparable under the new version. That must not reset the ladder's
intelligence: a fresh session seeds its field from the prior version's standings (the old
crown defends, the old runner-up challenges first), and a session ALREADY RUNNING when the
publish lands re-seeds instead of stranding itself on rounds it can no longer read.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete

from pathbrain import challenger as challenger_mod
from pathbrain import duel as duel_mod
from pathbrain.config_store import get_config, save_config
from pathbrain.database import session_scope
from pathbrain.methodology import ensure_current_methodology
from pathbrain.models import Duel, DuelStatus, Methodology, Run, RunStatus, Score

# The mock provider's environment: two pipes, and reachability is a per-pipe signature over
# the non-writable fields, so a runnable profile carries both.
ENV = {"scheduler": "fq_codel", "queues": 1, "upload_bandwidth": "40Mbit"}


def _settings(quantum: int) -> list[dict]:
    return [{"label": "wan-download", **ENV, "quantum": quantum},
            {"label": "wan-upload", **ENV, "quantum": quantum}]


# ── the mocked-engine harness (same shape as test_duel's) ────────────────────────────


def _no_settle():
    """The shipped duel rules (other suites leave their own contender mode / preset in the
    shared config), with the settle off and one seat so match order is sequential."""
    from pathbrain.config_store import DEFAULT_CONFIG

    with session_scope() as s:
        save_config(s, {"duel": {**DEFAULT_CONFIG["duel"], "settle_seconds": 0, "seats": 1, "belt_every": 2}})


def _score_by_profile(monkeypatch, applied: list[str], scores: dict[str, float]):
    """Fake each leg's run, scoring it by the profile that was applied for it."""
    by_run: dict[int, str] = {}
    seq = {"n": 0}

    def fake_chunk(label, notes, iterations, teardown=True, job_group=None, job_group_total=None, **_):
        seq["n"] += 1
        run_id = 9000 + seq["n"]
        by_run[run_id] = applied[-1] if applied else ""
        return (run_id, True, iterations)

    monkeypatch.setattr(duel_mod, "run_chunk", fake_chunk)
    monkeypatch.setattr(
        duel_mod, "_run_overall", lambda run_id, ver: scores.get(by_run.get(run_id, ""), 0.0)
    )
    return by_run


def _wait_finish(duel_id: int, timeout: float = 15.0) -> Duel:
    start = time.time()
    terminal = (DuelStatus.COMPLETE, DuelStatus.FAILED, DuelStatus.CANCELLED)
    while time.time() - start < timeout:
        with session_scope() as s:
            d = s.get(Duel, duel_id)
            if d and d.status in terminal:
                s.expunge(d)
                return d
        time.sleep(0.02)
    raise AssertionError("duel did not finish in time")


def _seed_prior(session, version: str, specs, when):
    """A non-current methodology (newest by created_at) with one scored run per spec, each
    run carrying settings so the profile is runnable by the ladder."""
    session.add(Methodology(version=version, rubric_version=version, derivation_version="derive-v14",
                            definition={"axes": [], "metrics": []}, is_current=False, created_at=when))
    ids = []
    for fp, ov, iters, quantum in specs:
        run = Run(status=RunStatus.COMPLETE, created_at=when, settings_fingerprint=fp,
                  settings=_settings(quantum), iterations=iters, per_iteration_ms=1000.0)
        session.add(run)
        session.flush()
        session.add(Score(run_id=run.id, methodology_version=version, comparability="exact",
                          axis_scores={"overall": ov}))
        ids.append(run.id)
    return ids


def _clear_prior(session, version, run_ids):
    session.execute(delete(Score).where(Score.run_id.in_(run_ids)))
    session.execute(delete(Run).where(Run.id.in_(run_ids)))
    session.execute(delete(Methodology).where(Methodology.version == version))


def _current_version() -> str:
    with session_scope() as s:
        return ensure_current_methodology(s, get_config(s)).version


def _profile(fp: str, quantum: int) -> dict:
    return {"fingerprint": fp, "label": fp, "settings": _settings(quantum)}


def test_after_a_publish_the_prior_crown_defends_against_the_prior_runner_up(monkeypatch):
    """Right after a publish the current version has scored nothing: no crown, no profiles.
    The ladder must still open with the most informative match there is — the old crown
    defending against the old runner-up — rather than idle, or race randoms."""
    import pathbrain.api.routes_settings as rs

    version = "test-duel-seed-prior-v0"
    when = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=365)  # newest prior
    crown, runner, low = "dseedcrown00", "dseedrunner0", "dseedlow0000"
    with session_scope() as s:
        s.query(Duel).delete()
        ids = _seed_prior(
            s, version, [(crown, 90.0, 20, 1514), (runner, 70.0, 20, 300), (low, 30.0, 20, 600)], when
        )
    applied: list[str] = []
    # Nothing comparable under the current version: the field is empty and crownless.
    empty = {"best_fingerprint": None, "profiles": [], "min_iterations": 15}
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: dict(empty))
    monkeypatch.setattr(rs, "_discover_live_normalized", lambda: None)  # no reachability filter
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: applied.append(fp))
    _no_settle()
    _score_by_profile(monkeypatch, applied, {crown: 60.0, runner: 66.0, low: 40.0})
    try:
        d = _wait_finish(duel_mod.start(duration_minutes=10))
        assert d.status == DuelStatus.COMPLETE, d.error
        # The prior crown walked in with the belt (pooled fallback = the seeded crown) and
        # the prior runner-up was the first challenger — ordered by the previous verdict.
        assert applied[:2] == [crown, runner], applied[:4]
        first = d.matchups[0]
        assert first["incumbent"] == crown and first["challenger"] == runner
        assert "seeded" in first["challenger_why"]
        assert first["methodology"] == _current_version()
        # The seeded order is an ordering, not a verdict: the ring still decided on its runs.
        assert first["verdict"] == "challenger"
    finally:
        with session_scope() as s:
            _clear_prior(s, version, ids)
            s.query(Duel).delete()


def test_the_seed_still_applies_once_the_firewalls_own_profile_is_crowned(monkeypatch):
    """Hours after a publish the one profile the firewall sits on reaches confidence from
    monitoring alone and becomes the current crown. The seed used to switch off at that
    moment, leaving a field of one crowned profile and nobody to challenge with. Now the
    current crown defends (a current measurement is never displaced) and the prior
    version's best unmeasured profile is the first challenger."""
    import pathbrain.api.routes_settings as rs

    version = "test-duel-seed-crowned-v0"
    when = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=365)
    cur, prior_best, prior_low = "dcrowncur000", "dcrownbest00", "dcrownlow000"
    with session_scope() as s:
        s.query(Duel).delete()
        ids = _seed_prior(s, version, [(prior_best, 88.0, 20, 300), (prior_low, 40.0, 20, 600)], when)
    applied: list[str] = []
    # The current version has scored exactly one profile — the firewall's — and crowned it.
    crowned = {"best_fingerprint": cur, "min_iterations": 15, "profiles": [
        {**_profile(cur, 1514), "confident": True, "overall": 61.0, "optimistic": 63.0,
         "iterations": 20, "last_seen": None, "crown_spreads": {}},
    ]}
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: dict(crowned))
    monkeypatch.setattr(rs, "_discover_live_normalized", lambda: None)
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: applied.append(fp))
    _no_settle()
    _score_by_profile(monkeypatch, applied, {cur: 60.0, prior_best: 66.0, prior_low: 40.0})
    try:
        d = _wait_finish(duel_mod.start(duration_minutes=10))
        assert d.status == DuelStatus.COMPLETE, d.error
        assert applied[:2] == [cur, prior_best], applied[:4]
        first = d.matchups[0]
        assert first["incumbent"] == cur and first["challenger"] == prior_best
        assert "seeded" in first["challenger_why"]
    finally:
        with session_scope() as s:
            _clear_prior(s, version, ids)
            s.query(Duel).delete()


def test_a_publish_mid_session_reseeds_the_ring_instead_of_stranding_it(monkeypatch):
    """A leg is scored under whatever version is current when it lands; the ring was reading
    every leg under the version it opened with. After a publish every later round used to
    come back "not scored under <old>" and the ladder spent the rest of the window on
    unusable rounds. Now: the seated matches close with the reason, the field is rebuilt
    (seeded), the defender is re-chosen, and the ring goes on under the new version."""
    import pathbrain.api.routes_settings as rs

    with session_scope() as s:
        s.query(Duel).delete()
    applied: list[str] = []
    real = _current_version()
    new = "test-mid-session-v2"
    field_old = {"best_fingerprint": "midinc00000x",
                 "profiles": [_profile("midinc00000x", 1514), _profile("midcha00000x", 300)]}
    field_new = {"best_fingerprint": "midinc2xxxxx",
                 "profiles": [_profile("midinc2xxxxx", 900), _profile("midcha2xxxxx", 450)]}
    state = {"field": field_old, "checks": 0}

    def fake_version(session):
        # The ring asks once per cycle. Two cycles under the old version, then the publish.
        state["checks"] += 1
        if state["checks"] > 2:
            state["field"] = field_new
            return new
        return real

    monkeypatch.setattr(duel_mod, "_current_methodology_version", fake_version)
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: dict(state["field"]))
    monkeypatch.setattr(rs, "_compute_heirs", lambda result, session, live=None: {
        "items": [{"fingerprint": p["fingerprint"]} for p in result["profiles"]
                  if p["fingerprint"] != result["best_fingerprint"]]
    })
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: applied.append(fp))
    _no_settle()
    _score_by_profile(monkeypatch, applied, {
        "midinc00000x": 60.0, "midcha00000x": 66.0, "midinc2xxxxx": 60.0, "midcha2xxxxx": 66.0,
    })
    try:
        d = _wait_finish(duel_mod.start(duration_minutes=10))
        assert d.status == DuelStatus.COMPLETE, d.error
        by_pair = {(m["incumbent"], m["challenger"]): m for m in d.matchups}
        old = by_pair[("midinc00000x", "midcha00000x")]
        assert old["verdict"] == "draw" and "methodology changed mid-session" in old["reason"]
        assert f"{real} → {new}" in old["reason"]
        assert old["methodology"] == real and 0 < old["pairs"] < 8  # closed open, not decided
        fresh = by_pair[("midinc2xxxxx", "midcha2xxxxx")]
        assert fresh["verdict"] == "challenger" and fresh["methodology"] == new
        # Nothing from the old field was applied again once the ring re-seeded.
        flip = applied.index("midinc2xxxxx")
        assert all(fp in ("midinc2xxxxx", "midcha2xxxxx") for fp in applied[flip:])
        assert d.champion_fingerprint == "midcha2xxxxx"
    finally:
        with session_scope() as s:
            s.query(Duel).delete()


def test_a_carried_match_from_another_methodology_is_closed_not_resumed(monkeypatch):
    """An open match's margins are Overalls on ONE rubric's scale. One carried over from a
    session under another version cannot take a round under this one: it is closed with
    the reason, and the pair is then raced fresh."""
    import pathbrain.api.routes_settings as rs

    with session_scope() as s:
        s.query(Duel).delete()
        s.add(Duel(status=DuelStatus.COMPLETE, duration_s=60, matchups=[], open_matches=[{
            "challenger": "carcha00000x", "incumbent": "carinc00000x", "methodology": "test-old-rubric-v0",
            "challenger_label": "carcha00000x", "why": "contender", "deltas": [1.0, 2.0, 1.5],
            "weather_shifts": [None, None, None], "leg_distances": [1, 1, 1], "legs": 3,
            "unusable": 0, "unusable_why": {}, "bad_streak": 0, "sessions": [1],
        }]))
    applied: list[str] = []
    field = {"best_fingerprint": "carinc00000x",
             "profiles": [_profile("carinc00000x", 1514), _profile("carcha00000x", 300)]}
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: dict(field))
    monkeypatch.setattr(rs, "_compute_heirs", lambda result, session, live=None: {"items": [{"fingerprint": "carcha00000x"}]})
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: applied.append(fp))
    _no_settle()
    _score_by_profile(monkeypatch, applied, {"carinc00000x": 60.0, "carcha00000x": 66.0})
    try:
        d = _wait_finish(duel_mod.start(duration_minutes=10))
        assert d.status == DuelStatus.COMPLETE, d.error
        closed = [m for m in d.matchups if "could not resume" in m["reason"]]
        assert len(closed) == 1 and "fought under methodology test-old-rubric-v0" in closed[0]["reason"]
        assert closed[0]["pairs"] == 3  # the carried evidence is on record, not silently dropped
        fresh = [m for m in d.matchups if m["verdict"] == "challenger"]
        assert fresh and fresh[0]["challenger"] == "carcha00000x" and not fresh[0]["carried"]
    finally:
        with session_scope() as s:
            s.query(Duel).delete()


def test_open_match_snapshots_carry_their_methodology():
    seat = duel_mod._Seat("cha", {"label": "cha"}, "why", "inc", p1=0.7, alpha=0.05,
                          min_margin=0.0, min_pairs=3, max_pairs=10, streak_wins=0)
    snap = duel_mod._seat_snapshot(seat, 7, "some-version")
    assert snap["methodology"] == "some-version" and snap["sessions"] == [7]
