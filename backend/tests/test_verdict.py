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
