"""Lever campaigns: one base, measured until its levers are settled, across sessions.

A lever session used to defend whoever the ring said was #1, re-decided every cycle: a
variant that won took the belt and became the next base, and a carried match was closed the
moment the belt moved. Pinned here: the campaign's base defends every match even when a
variant beats it; open matches live on the campaign row, survive an ordinary ladder session
in between, and resume with their rounds intact; evidence is read at the base and settled
transitions are raced last; the campaign API opens, lists, reports and closes.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from pathbrain import challenger as challenger_mod
from pathbrain import duel as duel_mod
from pathbrain import levers
from pathbrain.config_store import get_config, save_config
from pathbrain.database import session_scope
from pathbrain.methodology import ensure_current_methodology
from pathbrain.models import Duel, DuelStatus, LeverCampaign, Run, RunStatus
from pathbrain.settings_profile import fingerprint

WAN = {"label": "wan", "download_bandwidth": "880Mbit", "quantum": 1514, "target": 5, "interval": 100,
       "ecn": True, "flows": 1024, "limit": 10240, "scheduler": "fq_codel", "queues": 1}


def _settings(**over) -> list[dict]:
    return [{**WAN, **over}]


def _fp(settings) -> str:
    return fingerprint(settings)


def _wait_finish(duel_id: int, timeout: float = 20.0) -> Duel:
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


def _fake_engine(monkeypatch, applied: list[str], scores: dict[str, float], live_settings: list[dict]):
    by_run: dict[int, str] = {}
    seq = {"n": 0}

    def fake_chunk(label, notes, iterations, teardown=True, job_group=None, job_group_total=None, **_):
        seq["n"] += 1
        run_id = 9700 + seq["n"]
        by_run[run_id] = applied[-1] if applied else ""
        return (run_id, True, iterations)

    monkeypatch.setattr(duel_mod, "run_chunk", fake_chunk)
    monkeypatch.setattr(duel_mod, "_run_overall", lambda run_id, ver: scores.get(by_run.get(run_id, ""), 0.0))
    monkeypatch.setattr(duel_mod, "_run_crown", lambda run_id, ver: None)
    monkeypatch.setattr(duel_mod, "_weather_stamper", lambda meth_version: None)
    monkeypatch.setattr(duel_mod, "normalize", lambda cfgs: live_settings)
    monkeypatch.setattr(duel_mod, "_field_options", lambda provider: {})
    monkeypatch.setattr(duel_mod, "plan_apply", lambda target, live: ([], []))
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: applied.append(fp))
    return by_run


def _field(base_fp, base_settings, sib_fp, sib_settings, *, base_overall=60.0, sib_overall=70.0):
    return {
        "best_fingerprint": base_fp,
        "profiles": [
            {"fingerprint": base_fp, "label": "base", "name": "Base", "settings": base_settings,
             "overall": base_overall, "iterations": 40, "confident": True},
            {"fingerprint": sib_fp, "label": "sibling", "name": "Sibling", "settings": sib_settings,
             "overall": sib_overall, "iterations": 20, "confident": True},
        ],
    }


@pytest.fixture()
def clean_ring():
    with session_scope() as s:
        prior = dict(get_config(s).get("duel", {}) or {})
        save_config(s, {"duel": {"settle_seconds": 0, "seats": 1, "belt_every": 2, "contenders": "ring"}})
        s.query(Duel).delete()
        s.query(LeverCampaign).delete()
        s.commit()
    try:
        yield
    finally:
        with session_scope() as s:
            save_config(s, {"duel": {"contenders": prior.get("contenders", "ring")}})
            s.query(Duel).delete()
            s.query(LeverCampaign).delete()
            s.commit()


def _seed_base_run(settings: list[dict]) -> int:
    """A completed run carrying the base's settings — what makes a profile a possible base
    (a campaign takes its settings from the newest run under that fingerprint)."""
    with session_scope() as s:
        r = Run(status=RunStatus.COMPLETE, created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                settings_fingerprint=_fp(settings), settings=settings, iterations=1)
        s.add(r)
        s.flush()
        return r.id


def test_the_campaign_base_defends_every_match_even_when_a_variant_beats_it(monkeypatch, clean_ring):
    import pathbrain.api.routes_settings as rs

    base_s, sib_s = _settings(quantum=1514), _settings(quantum=300)
    base_fp, sib_fp = _fp(base_s), _fp(sib_s)
    run_id = _seed_base_run(base_s)
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: _field(base_fp, base_s, sib_fp, sib_s))
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: _field(base_fp, base_s, sib_fp, sib_s))
    monkeypatch.setattr(rs, "_compute_heirs", lambda result, session, live=None: {"items": []})
    applied: list[str] = []
    # The sibling BEATS the base — under the lineal rule it would take the belt.
    _fake_engine(monkeypatch, applied, {base_fp: 60.0, sib_fp: 70.0}, base_s)
    try:
        duel_id = duel_mod.start(duration_minutes=10, contenders="levers", base_fingerprint=base_fp)
        d = _wait_finish(duel_id)
        assert d.status == DuelStatus.COMPLETE, d.error
        assert d.mode == "levers" and d.campaign_id is not None
        assert len(d.matchups) >= 2
        assert d.matchups[0]["verdict"] == "challenger" and d.matchups[0]["challenger"] == sib_fp
        # …but the base kept defending: every later match is still "the base with one lever moved".
        assert all(m["incumbent"] == base_fp for m in d.matchups), [m["incumbent"] for m in d.matchups]
        assert all(m["lever"] for m in d.matchups)
        with session_scope() as s:
            camp = s.get(LeverCampaign, d.campaign_id)
            assert camp.base_fingerprint == base_fp and camp.status == "open"
            assert duel_id in camp.sessions
            assert not camp.open_matches  # nothing left open — every seated match decided
    finally:
        with session_scope() as s:
            s.execute(delete(Run).where(Run.id == run_id).execution_options(synchronize_session=False))
            s.commit()


def test_a_campaigns_open_match_survives_a_ladder_session_and_resumes(monkeypatch, clean_ring):
    import pathbrain.api.routes_settings as rs

    base_s, sib_s = _settings(quantum=1514), _settings(quantum=300)
    base_fp, sib_fp = _fp(base_s), _fp(sib_s)
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: _field(base_fp, base_s, sib_fp, sib_s, sib_overall=50.0))
    monkeypatch.setattr(rs, "_compute_heirs", lambda result, session, live=None: {"items": [{"fingerprint": sib_fp}]})
    applied: list[str] = []
    _fake_engine(monkeypatch, applied, {base_fp: 66.0, sib_fp: 60.0}, base_s)
    with session_scope() as s:
        version = ensure_current_methodology(s, get_config(s)).version
        camp = levers.create_campaign(s, base_fp, base_settings=base_s)
        camp.open_matches = [{
            "challenger": sib_fp, "incumbent": base_fp, "methodology": version,
            "challenger_label": "sibling", "challenger_name": "Sibling",
            "why": "lever: wan Quantum 1514 → 300 (a measured profile, 20 iterations)",
            "deltas": [1.0, 1.2, 0.9], "crown_deltas": {}, "weather_shifts": [None, None, None],
            "leg_distances": [1, 1, 1], "legs": 3, "unusable": 0, "unusable_why": {}, "bad_streak": 0,
            "sessions": [1],
            "lever": {"pipe": "wan", "field": "quantum", "field_label": "Quantum", "unit": None, "from": 1514, "to": 300},
        }]
        s.commit()
        camp_id = camp.id

    # An ordinary ladder session in between must not consume the campaign's open match.
    ladder = _wait_finish(duel_mod.start(duration_minutes=10))
    assert ladder.status == DuelStatus.COMPLETE, ladder.error
    assert ladder.mode is None and ladder.campaign_id is None
    assert not any("resumed" in (m.get("challenger_why") or "") for m in ladder.matchups)
    with session_scope() as s:
        assert len(s.get(LeverCampaign, camp_id).open_matches or []) == 1

    # The next lever session resumes it, rounds intact, against the same base.
    d = _wait_finish(duel_mod.start(duration_minutes=10, contenders="levers", campaign_id=camp_id))
    assert d.status == DuelStatus.COMPLETE, d.error
    first = d.matchups[0]
    assert first["challenger"] == sib_fp and first["incumbent"] == base_fp
    assert first["carried"] is True and "resumed with 3 round(s)" in first["challenger_why"]
    assert first["pairs"] > 3 and first["deltas"][:3] == [1.0, 1.2, 0.9]
    assert sorted(first["sessions"]) == sorted({1, d.id})
    with session_scope() as s:
        camp = s.get(LeverCampaign, camp_id)
        assert not camp.open_matches and d.id in camp.sessions


def _match(inc, cha, *, wins_i, wins_c, delta, lever, verdict="challenger"):
    return {"incumbent": inc, "challenger": cha, "incumbent_label": inc, "challenger_label": cha,
            "pairs": wins_i + wins_c, "wins_incumbent": wins_i, "wins_challenger": wins_c,
            "median_delta": delta, "verdict": verdict, "reason": "test", "lever": lever}


def _lever(field, label, frm, to):
    return {"pipe": "wan", "field": field, "field_label": label, "unit": None, "from": frm, "to": to}


def test_evidence_is_read_at_the_base_and_settled_steps_are_raced_last(clean_ring):
    base_s = _settings()
    base_fp = _fp(base_s)
    q300, t6, i200 = _fp(_settings(quantum=300)), _fp(_settings(target=6)), _fp(_settings(interval=200))
    with session_scope() as s:
        s.add(Duel(status=DuelStatus.COMPLETE, duration_s=60, finished_at=datetime.now(timezone.utc), matchups=[
            # Base defends; the lower quantum loses 2–10 by 1.5 → settled "worse".
            _match(base_fp, q300, wins_i=10, wins_c=2, delta=-1.5, lever=_lever("quantum", "Quantum", 1514, 300), verdict="incumbent"),
            # Base CHALLENGES a target-6 profile: 8–8 over 16 rounds, margin 0.1 → "null" at the base.
            _match(t6, base_fp, wins_i=8, wins_c=8, delta=0.1, lever=_lever("target", "CoDel target", 6, 5), verdict="draw"),
            # Four rounds on interval: still open.
            _match(base_fp, i200, wins_i=3, wins_c=1, delta=-0.4, lever=_lever("interval", "CoDel interval", 100, 200), verdict="draw"),
        ]))
        camp = levers.create_campaign(s, base_fp, base_settings=base_s)
        s.commit()
        status = levers.campaign_status(s, camp)
    by = {(l["pipe"], l["field"]): l for l in status["levers"]}
    q = by[("wan", "quantum")]
    assert q["state"] == "no_gain" and q["transitions"][0]["state"] == "worse"
    assert q["transitions"][0]["margin"] == -1.5 and q["transitions"][0]["to_shown"] == "300"
    t = by[("wan", "target")]
    # Read from the base's side: the base was the challenger, so the variant's margin flips.
    assert t["transitions"][0]["margin"] == -0.1 and t["transitions"][0]["to_shown"] == "6ms"
    assert t["transitions"][0]["rounds"] == 16 and t["transitions"][0]["state"] == "null"
    assert t["state"] == "no_gain"
    assert by[("wan", "interval")]["state"] == "open"
    assert status["open"] == 1 and status["no_gain"] == 2 and status["improves"] == 0
    assert {(u["pipe"], u["field"]) for u in status["untested"]} >= {("wan", "ecn"), ("wan", "flows"), ("wan", "limit")}
    assert status["rounds"] == 12 + 16 + 4
    # Settled steps go last when the ring picks the next variant.
    with session_scope() as s:
        from pathbrain.duel import _ledger_sessions
        evidence = levers.campaign_evidence(_ledger_sessions(s), base_fp, lambda fp: None)
    assert evidence["settled_transitions"] == {("wan", "quantum", 300.0), ("wan", "target", 6.0)}
    base_profile = {"fingerprint": base_fp, "label": "base", "settings": base_s, "overall": 60.0, "iterations": 40}
    siblings = [
        {"fingerprint": q300, "label": "q300", "settings": _settings(quantum=300), "overall": 58.0, "iterations": 20},
        {"fingerprint": t6, "label": "t6", "settings": _settings(target=6), "overall": 60.0, "iterations": 20},
    ]
    out = levers.lever_variants(base_profile, [base_profile, *siblings], base_s, settled=evidence["settled_transitions"])
    flags = [v["settled"] for v in out]
    assert flags == sorted(flags), "settled variants must all come after the unsettled ones"
    assert out[0]["settled"] is False and out[-1]["settled"] is True
    assert {v["fingerprint"] for v in out if v["settled"]} == {q300, t6}


def test_the_campaign_api_opens_lists_reports_and_closes(client, clean_ring):
    base_s = _settings(quantum=2000)
    base_fp = _fp(base_s)
    with session_scope() as s:
        r = Run(status=RunStatus.COMPLETE, created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                settings_fingerprint=base_fp, settings=base_s, iterations=1)
        s.add(r)
        s.flush()
        run_id = r.id
    try:
        created = client.post("/api/levers/campaigns", json={"base_fingerprint": base_fp})
        assert created.status_code == 201, created.text
        cid = created.json()["id"]
        assert created.json()["base_label"] and created.json()["status"] == "open"
        # Opening again on the same base returns the open campaign, never a second one.
        assert client.post("/api/levers/campaigns", json={"base_fingerprint": base_fp}).json()["id"] == cid
        listing = client.get("/api/levers/campaigns").json()
        assert listing["open_ids"] == [cid]
        status = client.get(f"/api/levers/campaigns/{cid}").json()
        assert status["campaign"]["base_fingerprint"] == base_fp and status["rounds"] == 0
        assert status["levers"] == [] and len(status["untested"]) >= 5
        # A campaign only applies to a lever session.
        r = client.post("/api/duel/start", json={"duration_minutes": 5, "campaign_id": cid})
        assert r.status_code == 422
        # A base with nothing on record cannot be a campaign.
        assert client.post("/api/levers/campaigns", json={"base_fingerprint": "nosuchbase000"}).status_code == 422
        closed = client.post(f"/api/levers/campaigns/{cid}/close").json()
        assert closed["status"] == "closed"
        assert client.get("/api/levers/campaigns").json()["open_ids"] == []
        assert client.get("/api/levers/campaigns/999999").status_code == 404
    finally:
        with session_scope() as s:
            s.execute(delete(Run).where(Run.id == run_id).execution_options(synchronize_session=False))
            s.commit()


def test_the_base_picker_reads_the_cached_profile_list_not_the_field(client, clean_ring):
    """The Levers page filled its base picker from `GET /settings/profiles` — a full
    `compute_profiles` pass with the weather cohort pass, on every page load. It needs a
    name, a label and a number to sort on; `GET /levers/bases` answers off the cached
    stored-profile list and the rollup, and never calls the field."""
    from pathbrain.api import routes_settings as rs

    base_s = _settings(quantum=2000)
    base_fp = _fp(base_s)
    with session_scope() as s:
        r = Run(status=RunStatus.COMPLETE, created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                settings_fingerprint=base_fp, settings=base_s, iterations=1)
        s.add(r)
        s.flush()
        run_id = r.id
    calls: list[str] = []
    real = rs.compute_profiles

    def spy(*args, **kwargs):
        calls.append("compute_profiles")
        return real(*args, **kwargs)

    try:
        rs.compute_profiles = spy  # type: ignore[assignment]
        body = client.get("/api/levers/bases").json()
    finally:
        rs.compute_profiles = real  # type: ignore[assignment]
        with session_scope() as s:
            s.execute(delete(Run).where(Run.id == run_id).execution_options(synchronize_session=False))
            s.commit()
    assert calls == []
    mine = [b for b in body["bases"] if b["fingerprint"] == base_fp]
    assert len(mine) == 1 and mine[0]["label"] and "overall" in mine[0] and "iterations" in mine[0]


def test_settings_lookup_reads_one_row_per_fingerprint():
    """The ledger's settings lookup selected every completed run of the profiles it named
    and kept the first — for the crown, every run it ever took, decoded to keep one. One
    grouped max-id subquery reads exactly the newest row per fingerprint."""
    from sqlalchemy import event

    from pathbrain import levers

    older = _settings(quantum=1400)
    newer = _settings(quantum=1500)
    fp = "lookup-test-" + _fp(older)[:12]
    ids: list[int] = []
    with session_scope() as s:
        for settings in (older, newer):
            r = Run(status=RunStatus.COMPLETE, created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    settings_fingerprint=fp, settings=settings, iterations=1)
            s.add(r)
            s.flush()
            ids.append(r.id)
        s.commit()
    statements: list[str] = []

    def _capture(conn, cursor, statement, parameters, context, executemany):
        if ".settings" in statement.lower():
            statements.append(statement.lower())

    try:
        with session_scope() as s:
            engine = s.get_bind()
            event.listen(engine, "before_cursor_execute", _capture)
            try:
                out = levers._settings_lookup(s, {fp})
            finally:
                event.remove(engine, "before_cursor_execute", _capture)
    finally:
        with session_scope() as s:
            s.execute(delete(Run).where(Run.id.in_(ids)).execution_options(synchronize_session=False))
            s.commit()
    assert out[fp] == newer
    assert len(statements) == 1 and "max(" in statements[0], statements
