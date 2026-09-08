"""Lever duels and the lever ledger (`levers.py`; duel `contenders = "levers"`;
`GET /api/explore/levers`).

A profile is a bundle of levers, and a bout between two bundles is several questions with
one answer. Pinned here: a single-lever difference is recognised numerically (never a
phantom from notation); the ring's lever mode seats the defender's own single-lever
variants — measured siblings first, then steps the firewall can hold — and records which
lever each match measured, the per-round margins and where (on which crown leg) the
margin lived; the ledger pools every single-lever match as the effect of moving UP and
reads it against the mechanism prediction; and an ordinary ring match now carries its
per-crown-leg margins too.
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
from pathbrain.models import Duel, DuelStatus, Run, RunStatus
from pathbrain.settings_profile import fingerprint

WAN = {"label": "wan", "download_bandwidth": "880Mbit", "quantum": 1514, "target": 5, "interval": 100,
       "ecn": True, "flows": 1024, "limit": 10240, "scheduler": "fq_codel", "queues": 1}


def _settings(**over) -> list[dict]:
    return [{**WAN, **over}]


def _fp(settings) -> str:
    return fingerprint(settings)


# ── Recognising a single-lever difference ────────────────────────────────────────


def test_single_lever_diff_is_numeric_and_exactly_one():
    assert levers.single_lever_diff(_settings(), _settings())is None
    d = levers.single_lever_diff(_settings(quantum=1514), _settings(quantum=300))
    assert d and (d["pipe"], d["field"], d["from"], d["to"]) == ("wan", "quantum", 1514, 300)
    assert d["field_label"] == "Quantum"
    # Two levers apart is not a lever reading.
    assert levers.single_lever_diff(_settings(), _settings(quantum=300, target=6)) is None
    # Notation is not a difference: "5ms" and 5 are the same target.
    assert levers.single_lever_diff(_settings(target="5ms"), _settings(target=5)) is None
    # A non-writable difference alone is not a lever either (nothing the ring could move).
    assert levers.single_lever_diff(_settings(), _settings(scheduler="fq_pie")) is None


# ── Generating the defender's variants ────────────────────────────────────────────


def _profile(settings, fp=None, **extra) -> dict:
    fp = fp or _fp(settings)
    return {"fingerprint": fp, "label": fp, "name": fp, "settings": settings, "overall": 60.0,
            "iterations": 20, **extra}


def test_variants_seat_measured_siblings_first_then_steps_the_firewall_can_hold():
    defender = _profile(_settings())
    sibling = _profile(_settings(quantum=300))
    two_apart = _profile(_settings(quantum=300, ecn=False))
    out = levers.lever_variants(defender, [defender, sibling, two_apart], _settings(),
                                allowed={"target": [3, 4, 5, 6, 7, 8], "interval": [20, 50, 100, 200]})
    by = {(v["lever"]["field"], v["lever"]["to"]): v for v in out}
    # The measured sibling is offered, first among quantum's variants.
    assert by[("quantum", 300)]["source"] == "field" and by[("quantum", 300)]["fingerprint"] == sibling["fingerprint"]
    quantum = [v for v in out if v["lever"]["field"] == "quantum"]
    assert quantum[0]["source"] == "field"
    # Generated quantum steps halve and double; a select steps to its adjacent options.
    assert {v["lever"]["to"] for v in quantum if v["source"] == "generated"} == {757, 3028}
    assert {v["lever"]["to"] for v in out if v["lever"]["field"] == "target"} == {4, 6}
    assert {v["lever"]["to"] for v in out if v["lever"]["field"] == "interval"} == {50, 200}
    # A boolean flips; an unbounded integer halves and doubles.
    assert {v["lever"]["to"] for v in out if v["lever"]["field"] == "ecn"} == {False}
    assert {v["lever"]["to"] for v in out if v["lever"]["field"] == "flows"} == {512, 2048}
    # Bandwidth is never generated (the provider owns its legal forms).
    assert not any(v["lever"]["field"] == "download_bandwidth" for v in out)
    # The two-lever profile is not a variant of anything.
    assert two_apart["fingerprint"] not in {v["fingerprint"] for v in out}
    # A generated variant is the defender's whole settings with one field moved, so it
    # hashes as the firewall will echo it and stays reachable by construction.
    gen = by[("quantum", 757)]
    assert gen["profile"]["generated"] is True
    assert gen["profile"]["settings"][0]["scheduler"] == "fq_codel"
    assert gen["fingerprint"] == _fp(_settings(quantum=757))
    assert "757" in gen["why"] and "nobody has measured it" in gen["why"]


def test_variants_round_robin_levers_by_evidence_and_refuse_an_unreachable_defender():
    defender = _profile(_settings())
    # Quantum already has 40 paired rounds on the ledger; ECN has none → ECN first.
    out = levers.lever_variants(defender, [defender], _settings(), history={("wan", "quantum"): 40})
    assert out[0]["lever"]["field"] != "quantum"
    assert out[-1]["lever"]["field"] == "quantum"
    # A defender the live environment can't be set to yields nothing to seat.
    assert levers.lever_variants(defender, [defender], _settings(scheduler="fq_pie")) == []
    assert levers.lever_variants({"fingerprint": "x", "settings": []}, [], None) == []


def test_next_variant_skips_this_sessions_pairs_and_only_orders_by_cooldown():
    defender = _profile(_settings())
    out = levers.lever_variants(defender, [defender], _settings())
    first, second = out[0], out[1]
    fp, why, v = levers.next_variant(out, defender["fingerprint"])
    assert fp == first["fingerprint"] and v is first
    fp, _, v = levers.next_variant(out, defender["fingerprint"],
                                   fought={frozenset((defender["fingerprint"], first["fingerprint"]))})
    assert fp == second["fingerprint"]
    # On cooldown: still offered, last among equals, and the reason says so.
    fp, why, _ = levers.next_variant(out, defender["fingerprint"], recently_decided=lambda a, b: True)
    assert fp == first["fingerprint"] and "re-raced" in why
    assert levers.next_variant([], defender["fingerprint"]) == (None, "", None)


# ── The ledger ────────────────────────────────────────────────────────────────────


def _seed_run(fp: str, settings: list[dict]) -> int:
    with session_scope() as s:
        r = Run(status=RunStatus.COMPLETE, created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                settings_fingerprint=fp, settings=settings, iterations=1)
        s.add(r)
        s.flush()
        return r.id


def _match(inc, cha, *, wins_i, wins_c, delta, verdict="challenger", lever=None, deltas=None, crown=None):
    return {
        "incumbent": inc, "challenger": cha, "incumbent_label": inc, "challenger_label": cha,
        "pairs": wins_i + wins_c, "wins_incumbent": wins_i, "wins_challenger": wins_c,
        "median_delta": delta, "verdict": verdict, "reason": "test",
        "lever": lever, "deltas": deltas, "median_crown_delta": crown,
    }


def test_the_ledger_pools_single_lever_matches_as_the_effect_of_moving_up(client):
    q1514, q300 = _settings(quantum=1514), _settings(quantum=300)
    t5, t7 = _settings(target=5), _settings(target=7)
    fps = {k: _fp(v) for k, v in {"q1514": q1514, "q300": q300, "t5": t5, "t7": t7}.items()}
    two_apart = _settings(quantum=300, target=7)
    fps["two"] = _fp(two_apart)
    run_ids = [_seed_run(fps["q1514"], q1514), _seed_run(fps["q300"], q300),
               _seed_run(fps["t5"], t5), _seed_run(fps["t7"], t7), _seed_run(fps["two"], two_apart)]
    # A lever match seated by the ring (carries `lever` + rounds + crown split): the LOWER
    # quantum (challenger) won 10 of 12 rounds by 1.5 points, mostly on network stall.
    seated = _match(fps["q1514"], fps["q300"], wins_i=2, wins_c=10, delta=1.5,
                    lever={"pipe": "wan", "field": "quantum", "field_label": "Quantum", "unit": None,
                           "from": 1514, "to": 300},
                    deltas=[1.2, 1.6, 1.4, -0.3, 1.9, 1.5, 1.1, 1.7, -0.2, 1.3, 1.6, 1.4],
                    crown={"fcp": 0.4, "lcp": 0.2, "network_stall_all": 3.1})
    # Ordinary matches whose profiles happen to be one lever apart: the HIGHER target won
    # every round of two matches (the surprise — the mechanism predicts no effect)…
    plain_a = _match(fps["t5"], fps["t7"], wins_i=0, wins_c=10, delta=2.0)
    plain_b = _match(fps["t7"], fps["t5"], wins_i=10, wins_c=0, delta=-2.0, verdict="incumbent")
    # …one two levers apart (skipped), and one aborted (never evidence).
    unrelated = _match(fps["q1514"], fps["two"], wins_i=3, wins_c=5, delta=0.5)
    aborted = _match(fps["q1514"], fps["q300"], wins_i=0, wins_c=0, delta=None, verdict="draw")
    aborted["reason"] = "aborted: repeated unusable rounds"
    with session_scope() as s:
        s.query(Duel).delete()
        s.add(Duel(status=DuelStatus.COMPLETE, matchups=[seated, plain_a, unrelated, aborted],
                   finished_at=datetime.now(timezone.utc), duration_s=60))
        s.add(Duel(status=DuelStatus.COMPLETE, matchups=[plain_b],
                   finished_at=datetime.now(timezone.utc), duration_s=60))
        s.commit()
    try:
        body = client.get("/api/explore/levers").json()
        assert body["matches_used"] == 3 and body["matches_skipped"] == 1 and body["matches_aborted"] == 1
        rows = {(l["pipe"], l["field"]): l for l in body["levers"]}
        q = rows[("wan", "quantum")]
        # Oriented as moving UP: the lower value won, so moving up loses.
        assert q["rounds"] == 12 and q["wins_higher"] == 2 and q["wins_lower"] == 10
        assert q["median_margin_up"] == -1.5
        assert q["crown_margin_up"] == {"fcp": -0.4, "lcp": -0.2, "network_stall_all": -3.1}
        assert q["paired_rounds"] == 12 and q["paired_p"] is not None and q["paired_p"] < 0.05
        assert q["direction"] == "lower"
        assert q["prediction"] == "interior" and q["agreement"] == "consistent"
        assert q["seated_as_lever"] == 1
        t = q["transitions"][0]
        assert (t["from"], t["to"]) == (300.0, 1514.0) and t["from_shown"] == "300" and t["rounds"] == 12
        tg = rows[("wan", "target")]
        assert tg["matches"] == 2 and tg["rounds"] == 20 and tg["wins_higher"] == 20
        assert tg["median_margin_up"] == 2.0 and tg["direction"] == "higher"
        assert tg["prediction"] == "null" and tg["agreement"] == "surprise"
        assert tg["transitions"][0]["from_shown"] == "5ms" and tg["transitions"][0]["to_shown"] == "7ms"
        # Levers never fought still get a row with their prediction and no verdict.
        assert rows[("wan", "ecn")]["agreement"] == "untested" and rows[("wan", "ecn")]["rounds"] == 0
        assert ("wan", "download_bandwidth") in rows
        assert body["min_rounds"] == levers.MIN_ROUNDS
    finally:
        with session_scope() as s:
            s.query(Duel).delete()
            s.execute(delete(Run).where(Run.id.in_(run_ids)).execution_options(synchronize_session=False))
            s.commit()


def test_thin_evidence_reads_thin_not_as_a_direction():
    from pathbrain.levers import _direction, _summary

    assert _direction(3, 0, 0.25, None, [1.0]) == "thin"
    assert _direction(9, 1, 0.02, None, [1.0]) == "higher"
    assert _direction(1, 9, 0.02, None, [-1.0]) == "lower"
    assert _direction(5, 5, 1.0, None, [0.1]) == "none"
    r = levers._orient(_match("a", "b", wins_i=4, wins_c=4, delta=0.0),
                       {"field": "quantum", "from": 300, "to": 1514})
    assert r["wins_hi"] == 4 and r["margin"] == 0.0
    assert _summary([r])["direction"] == "none"


# ── The ring in lever mode, and per-crown-leg margins on every match ─────────────


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


def _fake_engine(monkeypatch, applied: list[str], scores: dict[str, float], crowns: dict[str, dict]):
    """Fake the runs, scoring each leg — Overall and crown subscores — by the profile
    applied for it."""
    by_run: dict[int, str] = {}
    seq = {"n": 0}

    def fake_chunk(label, notes, iterations, teardown=True, job_group=None, job_group_total=None, **_):
        seq["n"] += 1
        run_id = 9500 + seq["n"]
        by_run[run_id] = applied[-1] if applied else ""
        return (run_id, True, iterations)

    monkeypatch.setattr(duel_mod, "run_chunk", fake_chunk)
    monkeypatch.setattr(duel_mod, "_run_overall", lambda run_id, ver: scores.get(by_run.get(run_id, ""), 0.0))
    monkeypatch.setattr(duel_mod, "_run_crown", lambda run_id, ver: crowns.get(by_run.get(run_id, "")))
    monkeypatch.setattr(duel_mod, "_weather_stamper", lambda meth_version: None)
    monkeypatch.setattr(challenger_mod, "_apply_profile", lambda p, s, fp: applied.append(fp))
    return by_run


@pytest.fixture()
def lever_mode():
    with session_scope() as s:
        prior = dict(get_config(s).get("duel", {}) or {})
        save_config(s, {"duel": {"settle_seconds": 0, "seats": 1, "belt_every": 2, "contenders": "levers"}})
        s.query(Duel).delete()
        s.commit()
    try:
        yield
    finally:
        with session_scope() as s:
            save_config(s, {"duel": {"contenders": prior.get("contenders", "ring")}})
            s.query(Duel).delete()
            s.commit()


def test_lever_mode_seats_the_defenders_own_variants_and_records_the_lever(monkeypatch, lever_mode):
    import pathbrain.api.routes_settings as rs

    inc_settings, sib_settings = _settings(quantum=1514), _settings(quantum=300)
    inc_fp, sib_fp = _fp(inc_settings), _fp(sib_settings)
    fake_field = {
        "best_fingerprint": inc_fp,
        "profiles": [
            {"fingerprint": inc_fp, "label": "incumbent", "settings": inc_settings, "overall": 66.0, "iterations": 40, "confident": True},
            {"fingerprint": sib_fp, "label": "sibling", "settings": sib_settings, "overall": 60.0, "iterations": 20, "confident": True},
        ],
    }
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: fake_field)
    monkeypatch.setattr(rs, "_compute_heirs", lambda result, session, live=None: {"items": []})
    # The mocked provider "discovers" the incumbent's own settings, so every variant is reachable.
    from pathbrain import providers

    class _Prov:
        def discover(self):
            from pathbrain.providers.mock import MockProvider  # noqa: F401 — real class untouched
            return []

        def field_options(self):
            return {"target": [3, 4, 5, 6, 7, 8]}

    applied: list[str] = []
    crowns = {
        inc_fp: {"fcp": 70.0, "lcp": 60.0, "network_stall_all": 50.0},
        sib_fp: {"fcp": 64.0, "lcp": 60.0, "network_stall_all": 44.0},
    }
    _fake_engine(monkeypatch, applied, {inc_fp: 66.0, sib_fp: 60.0}, crowns)
    real_provider = providers.get_provider()
    monkeypatch.setattr(duel_mod, "normalize", lambda cfgs: inc_settings)
    monkeypatch.setattr(duel_mod, "_field_options", lambda provider: {"target": [3, 4, 5, 6, 7, 8]})
    monkeypatch.setattr(duel_mod, "plan_apply", lambda target, live: ([], []))
    assert real_provider is not None

    d = _wait_finish(duel_mod.start(duration_minutes=10))
    assert d.status == DuelStatus.COMPLETE, d.error
    assert d.matchups, "the lever mode seated nothing"
    # Every match measured exactly one lever, and says which.
    assert all(m["lever"] for m in d.matchups)
    first = d.matchups[0]
    assert (first["lever"]["field"], first["lever"]["from"], first["lever"]["to"]) == ("quantum", 1514, 300)
    assert first["challenger"] == sib_fp and "a measured profile" in first["challenger_why"]
    assert first["verdict"] == "incumbent"
    # The per-round margins ride the record, aligned with the crown split, and the split
    # says where the incumbent's win lived: FCP and network stall, not LCP.
    assert len(first["deltas"]) == first["pairs"]
    assert first["median_crown_delta"] == {"fcp": -6.0, "lcp": 0.0, "network_stall_all": -6.0}
    assert all(len(v) == first["pairs"] for v in first["crown_deltas"].values())
    # Once the measured sibling is decided the ring steps the defender's OWN levers — every
    # later challenger is a generated variant of the incumbent, nearest step first.
    later = d.matchups[1:]
    assert later, "no generated variants were seated"
    assert all("nobody has measured it" in m["challenger_why"] for m in later)
    levers_seen = [(m["lever"]["field"], m["lever"]["to"]) for m in later]
    assert ("quantum", 757) in levers_seen and ("quantum", 3028) in levers_seen
    assert levers_seen.index(("quantum", 757)) < levers_seen.index(("quantum", 3028))
    # A generated variant was applied as its own settings and recorded under its fingerprint.
    gen_fp = _fp(_settings(quantum=757))
    assert gen_fp in applied
    assert any(m["challenger"] == gen_fp for m in later)
    # The ledger reads the session straight back.
    with session_scope() as s:
        book = levers.lever_ledger(s)
    q = next(l for l in book["levers"] if (l["pipe"], l["field"]) == ("wan", "quantum"))
    assert q["seated_as_lever"] >= 3 and q["rounds"] == sum(m["pairs"] for m in d.matchups if m["lever"]["field"] == "quantum")


def test_an_ordinary_ring_match_now_carries_its_crown_split(monkeypatch):
    import pathbrain.api.routes_settings as rs

    with session_scope() as s:
        save_config(s, {"duel": {"settle_seconds": 0, "seats": 1, "belt_every": 2, "contenders": "ring"}})
        s.query(Duel).delete()
        s.commit()
    applied: list[str] = []
    fake_field = {
        "best_fingerprint": "inc0000000x",
        "profiles": [
            {"fingerprint": "inc0000000x", "label": "incumbent", "settings": [{"label": "wan", "quantum": 1514}]},
            {"fingerprint": "cha0000000x", "label": "challenger", "settings": [{"label": "wan", "quantum": 300}]},
        ],
    }
    monkeypatch.setattr(rs, "compute_profiles", lambda session, **_: fake_field)
    monkeypatch.setattr(rs, "_compute_heirs", lambda result, session, live=None: {"items": [{"fingerprint": "cha0000000x"}]})
    crowns = {
        "inc0000000x": {"fcp": 60.0, "lcp": 60.0, "network_stall_all": 60.0},
        "cha0000000x": {"fcp": 62.0, "lcp": 70.0, "network_stall_all": 60.0},
    }
    _fake_engine(monkeypatch, applied, {"inc0000000x": 60.0, "cha0000000x": 66.0}, crowns)
    try:
        d = _wait_finish(duel_mod.start(duration_minutes=10))
        assert d.status == DuelStatus.COMPLETE, d.error
        m = d.matchups[0]
        assert m["verdict"] == "challenger" and m["lever"] is None
        assert m["median_crown_delta"] == {"fcp": 2.0, "lcp": 10.0, "network_stall_all": 0.0}
        assert m["deltas"] and all(abs(x - 6.0) < 1e-9 for x in m["deltas"])
        # These profiles have no stored settings (a fake field, no runs), so the ledger
        # cannot tell whether they are one lever apart — it skips the match rather than
        # guessing, and says so in the counts.
        with session_scope() as s:
            book = levers.lever_ledger(s)
        assert book["matches_used"] == 0 and book["matches_skipped"] >= 1
    finally:
        with session_scope() as s:
            s.query(Duel).delete()
            s.commit()
