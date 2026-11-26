"""Ring nominations: an engine's winner is put forward to the ring, never written to the link."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pathbrain import duel
from pathbrain.database import session_scope
from pathbrain.domain.verdict import OpenNomination
from pathbrain.models import Duel, DuelStatus, RingNomination
from pathbrain.nominations import OPEN_DAYS, nominate, open_nominations


def _clear(s):
    """Remove every nomination, and the duels these tests filed (an answered nominee from a
    previous pass would otherwise answer this pass's nomination too)."""
    for row in s.query(RingNomination).all():
        s.delete(row)
    for d in s.query(Duel).all():
        if any(isinstance(m, dict) and str(m["challenger"]).startswith("nom-")
               for m in (d.matchups or []) if "challenger" in m):
            s.delete(d)
    s.flush()


def test_a_nomination_stays_open_until_the_ring_fights_it(client):
    with session_scope() as s:
        _clear(s)
        assert nominate(s, "nom-a", "challenger", "won race #1") is not None
        assert nominate(s, "nom-a", "challenger", "won race #2") is None  # one open at a time
    with session_scope() as s:
        assert [(n.fingerprint, n.reason) for n in open_nominations(s)] == [("nom-a", "won race #1")]
        # The ring fights it after the nomination: answered, whatever the result.
        s.add(Duel(status=DuelStatus.COMPLETE,
                   finished_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                   matchups=[{"incumbent": "crown-x", "challenger": "nom-a", "verdict": "incumbent"}]))
    with session_scope() as s:
        assert "nom-a" not in {n.fingerprint for n in open_nominations(s)}
        _clear(s)


def test_an_aborted_match_does_not_answer_a_nomination(client):
    with session_scope() as s:
        _clear(s)
        nominate(s, "nom-b", "experiment", "won experiment #3")
        s.add(Duel(status=DuelStatus.COMPLETE,
                   finished_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                   matchups=[{"incumbent": "crown-x", "challenger": "nom-b", "verdict": "aborted"}]))
    with session_scope() as s:
        assert "nom-b" in {n.fingerprint for n in open_nominations(s)}
        _clear(s)


def test_a_nomination_lapses_after_the_open_window(client):
    with session_scope() as s:
        _clear(s)
        s.add(RingNomination(fingerprint="nom-c", source="challenger", reason="old",
                             created_at=datetime.now(timezone.utc) - timedelta(days=OPEN_DAYS + 1)))
    with session_scope() as s:
        assert "nom-c" not in {n.fingerprint for n in open_nominations(s)}
        _clear(s)


def test_the_ring_seats_a_nominee_as_a_live_threat(monkeypatch):
    """A profile the ring had judged outclassed, nominated by an engine that just saw it win,
    is raced ahead of an ordinary contender: the engine's win is the question to settle."""
    monkeypatch.setattr(duel, "_open_nominations", lambda: [
        OpenNomination(fingerprint="outclassed", source="challenger", reason="won challenger race #9",
                       made=datetime.now(timezone.utc))
    ])
    field = {
        "best_fingerprint": "belt",
        "profiles": [
            {"fingerprint": fp, "label": fp, "overall": ov, "confident": True, "settings": []}
            for fp, ov in (("belt", 90.0), ("hopeful", 80.0), ("outclassed", 85.0))
        ],
    }
    ratings = {
        "belt": {"rating": 1600.0, "rating_se": 20.0},
        "outclassed": {"rating": 1560.0, "rating_se": 30.0},
        "hopeful": {"rating": 1500.0, "rating_se": 200.0},
    }
    order = duel.contender_order(field, ratings, "belt")
    seat = {c["fingerprint"]: c for c in order}
    assert seat["outclassed"]["tier"] == duel.LIVE_THREAT_TIER
    assert seat["outclassed"]["nominated"] is True
    assert "nominated for the ring" in seat["outclassed"]["why"]
    fps = [c["fingerprint"] for c in order]
    assert fps.index("outclassed") < fps.index("hopeful")
