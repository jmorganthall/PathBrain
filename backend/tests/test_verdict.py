"""The Verdict: one typed answer to "which profile is best", behind one door.

This sprint wraps `crowning.resolve` exactly, so the pins here are that the wrapping is
lossless (same crown, same reason, same evidence) and that a resolver that stops saying
who decided fails loudly instead of reaching a screen as a blank.
"""
from __future__ import annotations

import dataclasses

import pytest

from pathbrain import crowning, verdict
from pathbrain.database import session_scope


def _resolution(**over):
    base = {
        "policy": "fused", "fingerprint": "fpcrown00001", "source": "fused",
        "detail": "the fused ranking's crown", "duel_champion": {"fingerprint": "fpduel000001"},
        "fused": {"fingerprint": "fpcrown00001", "overall": 71.2},
        "speed_budget": {"enabled": True, "checked": True, "within": True},
    }
    return {**base, **over}


def test_a_resolution_round_trips_through_the_verdict():
    r = _resolution(lcp_first={"fingerprint": "fplcp0000001"})
    v = verdict.Verdict.from_resolution(r)
    assert (v.fingerprint, v.source, v.policy, v.detail) == (
        "fpcrown00001", "fused", "fused", "the fused ranking's crown")
    assert v.evidence["fused"]["overall"] == 71.2 and v.evidence["lcp_first"]["fingerprint"] == "fplcp0000001"
    assert v.as_dict() == r


def test_the_verdict_is_frozen_and_a_fallback_is_a_new_verdict():
    v = verdict.Verdict.from_resolution(_resolution())
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.fingerprint = "other"  # type: ignore[misc]
    fb = v.fallen_back("fppooled0001", "pooled", "not in the measured field")
    assert (fb.fingerprint, fb.source, fb.policy) == ("fppooled0001", "pooled", "fused")
    assert v.fingerprint == "fpcrown00001"
    assert fb.evidence == v.evidence


@pytest.mark.parametrize("missing", ["source", "policy", "detail"])
def test_a_resolution_that_does_not_say_who_decided_is_refused(missing):
    r = _resolution()
    del r[missing]
    with pytest.raises(KeyError):
        verdict.Verdict.from_resolution(r)


def test_current_is_exactly_what_crowning_resolves():
    with session_scope() as s:
        raw = crowning.resolve(s, pooled_best_fp="fpverdict001")
        v = verdict.current(s, "fpverdict001")
    assert v.as_dict() == {**raw, "duel_champion": raw.get("duel_champion") or None,
                           "speed_budget": raw.get("speed_budget") or None}


def test_the_crowns_card_still_serves_the_governing_verdict():
    from fastapi.testclient import TestClient

    from pathbrain.main import app

    with TestClient(app) as c:
        r = c.get("/api/settings/crowns")
    assert r.status_code == 200, r.text[:500]
    governing = r.json()["governing"]
    assert {"source", "fingerprint", "detail", "speed_budget"} <= set(governing)


# ── the verdict history (S5) ──────────────────────────────────────────────────────────


def _clear_history():
    from pathbrain.models import VerdictRecord

    with session_scope() as s:
        s.query(VerdictRecord).delete()


def test_the_history_records_each_new_answer_once():
    _clear_history()
    a = verdict.Verdict.from_resolution(_resolution())
    a_again = verdict.Verdict.from_resolution(_resolution(detail="same crown, new margin"))
    b = a.fallen_back("fppooled0001", "pooled", "not in the measured field")
    try:
        with session_scope() as s:
            assert verdict.record(s, a, "speed-smoothness-v19") is not None
            s.commit()
            assert verdict.record(s, a_again) is None  # a new detail alone is not a new answer
            row = verdict.record(s, b, "speed-smoothness-v19")
            s.commit()
            assert row.previous_fingerprint == "fpcrown00001"
            hist = verdict.history(s)
        assert [h["fingerprint"] for h in hist] == ["fppooled0001", "fpcrown00001"]
        assert hist[1]["evidence"] == {"fused": "fpcrown00001"}
        assert hist[0]["source"] == "pooled" and hist[0]["methodology_version"] == "speed-smoothness-v19"
    finally:
        _clear_history()


def test_the_verdict_endpoint_serves_the_answer_and_its_history():
    from fastapi.testclient import TestClient

    from pathbrain.main import app

    _clear_history()
    try:
        with session_scope() as s:
            verdict.record(s, verdict.Verdict.from_resolution(_resolution()))
            s.commit()
        with TestClient(app) as c:
            r = c.get("/api/verdict?history=5")
        assert r.status_code == 200, r.text[:500]
        body = r.json()
        assert {"policy", "fingerprint", "source", "detail"} <= set(body["verdict"])
        assert body["history"][0]["fingerprint"] == "fpcrown00001"
    finally:
        _clear_history()


def test_a_crown_check_writes_the_verdict_history(monkeypatch):
    """Auto-apply's check is where the governing answer is resolved every tick, so it is
    what records it: the first check after a change leaves one row."""
    from pathbrain import crown_follower

    _clear_history()
    field = {"best_fingerprint": "fpfollow0001", "profiles": [
        {"fingerprint": "fpfollow0001", "label": "a", "overall": 70.0, "settings": []}]}
    monkeypatch.setattr(crown_follower, "_compute_field", lambda session: field)
    monkeypatch.setattr(verdict, "current", lambda session, fp: verdict.Verdict.from_resolution(
        _resolution(fingerprint="fpfollow0001", source="pooled", policy="pooled", detail="pooled crown")))
    try:
        try:
            crown_follower.check()
        except Exception:  # noqa: BLE001 — the follow half may stop on the mock firewall
            pass
        with session_scope() as s:
            hist = verdict.history(s)
        assert hist and hist[0]["fingerprint"] == "fpfollow0001" and hist[0]["source"] == "pooled"
    finally:
        _clear_history()
