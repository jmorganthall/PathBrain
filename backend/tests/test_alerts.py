"""Acknowledging an alert: cleared until the situation changes, not until the numbers do."""
from __future__ import annotations

import pytest

from pathbrain import alerts
from pathbrain import duel


def _health(share, abort_reasons=(), reasons=()):
    """A round-health payload shaped like the real one, for signature work."""
    return {
        "aborted_share": share,
        "abort_reasons": [{"reason": r, "matches": 1} for r in abort_reasons],
        "reasons": [{"reason": r, "legs": 1} for r in reasons],
    }


# ── the signature: what counts as "the same situation" ────────────────────────


def test_the_counts_moving_is_not_a_new_situation(_db):
    """The whole point. The ladder runs continuously, so 577 of 1027 becomes 578 of 1030
    within the hour — a signature over the payload would bring a dismissed banner back
    having told the reader nothing."""
    causes = ("rounds unusable — no Overall to compare",)
    legs = ("The firewall did not answer 'apply' in 3 attempts (ReadTimeout: timed out).",)
    before = _health(0.56, causes, legs)
    after = _health(0.5614, causes, legs)  # three more sessions later
    assert duel._health_signature(before) == duel._health_signature(after)


def test_the_same_failure_under_a_different_profile_is_not_a_new_situation(_db):
    """The raw causes name the profile and its fingerprint, so an alert keyed on them could
    never stay cleared on a ladder that races a different challenger every leg."""
    a = _health(0.5, reasons=(
        "could not apply Focused Falcon: Could not apply challenger profile "
        "ea5a70ab147c: quantum, target did not take.",
    ))
    b = _health(0.5, reasons=(
        "could not apply Mossy Osprey: Could not apply challenger profile "
        "52758b412bc0: quantum did not take.",
    ))
    assert duel._reason_class(a["reasons"][0]["reason"]) == duel._reason_class(b["reasons"][0]["reason"])
    assert duel._health_signature(a) == duel._health_signature(b)


def test_a_new_kind_of_failure_is_a_new_situation(_db):
    """The other half: the guard starting to refuse writes is a genuinely different problem
    from the firewall timing out, and must re-open a cleared banner."""
    before = _health(0.5, reasons=("The firewall did not answer 'apply' in 3 attempts.",))
    after = _health(0.5, reasons=(
        "The firewall did not answer 'apply' in 3 attempts.",
        "Stopped by the firewall guard — hands-off: the WAN dropped.",
    ))
    assert duel._health_signature(before) != duel._health_signature(after)


def test_missing_a_different_metric_is_a_different_instrument_problem(_db):
    """A run short of LCP and one short of a stall metric are not the same finding, so the
    metric set stays inside the class — sorted, so the order it was listed in cannot matter."""
    assert duel._reason_class("incomparable: missing fcp, lcp") == \
        duel._reason_class("incomparable: missing lcp, fcp")
    assert duel._reason_class("incomparable: missing fcp, lcp") != \
        duel._reason_class("incomparable: missing network_stall_all")


# ── the acknowledgement itself ────────────────────────────────────────────────


def test_an_acknowledged_alert_stays_cleared_and_can_be_brought_back(_db):
    sig = alerts.signature(["a"], ["b"])
    assert alerts.status("k", sig)["acknowledged"] is False
    alerts.acknowledge("k", sig, {"aborted_share": 0.5})
    assert alerts.status("k", sig)["acknowledged"] is True
    assert [a["key"] for a in alerts.acks()] == ["k"]
    assert alerts.clear("k") is True
    assert alerts.status("k", sig)["acknowledged"] is False
    assert alerts.clear("k") is False, "un-acking twice is not an error"


def test_a_changed_signature_reopens_it_and_says_why(_db):
    alerts.acknowledge("k", alerts.signature(["a"]), {})
    st = alerts.status("k", alerts.signature(["a", "b"]))
    assert st["acknowledged"] is False
    assert "new" in st["why"].lower()


def test_materially_worse_reopens_it_but_drift_and_improvement_do_not(_db):
    """An acknowledged 56% that drifts to 57% stays quiet; one that reaches 75% comes back.
    Getting better never re-fires — the concern is resolved, and that is not an alert."""
    sig = alerts.signature(["same causes"])
    alerts.acknowledge("k", sig, {"aborted_share": 0.56})
    worse_than = lambda share: alerts.status(  # noqa: E731
        "k", sig,
        supersedes=lambda st: share > float(st["aborted_share"]) + duel.HEALTH_REALERT_DELTA,
    )
    assert worse_than(0.57)["acknowledged"] is True, "drift stays quiet"
    assert worse_than(0.30)["acknowledged"] is True, "improvement is not an alert"
    reopened = worse_than(0.75)
    assert reopened["acknowledged"] is False
    assert "worse" in reopened["why"].lower()


def test_an_unreadable_acknowledgement_shows_the_alert(_db, monkeypatch):
    """Fail open: one banner too many beats a diagnostic silently lost."""
    def boom():
        raise RuntimeError("no database")
    monkeypatch.setattr(alerts, "session_scope", boom)
    assert alerts.status("k", "sig")["acknowledged"] is False


# ── end to end, through the API the page uses ─────────────────────────────────


def test_the_health_payload_carries_its_own_dismissal(client, _db):
    body = client.get("/api/duel/health").json()
    alert = body["alert"]
    assert alert["key"] == duel.HEALTH_ALERT_KEY
    assert alert["acknowledged"] is False and alert["signature"]

    acked = client.post(
        f"/api/alerts/{alert['key']}/ack",
        json={"signature": alert["signature"], "state": alert["state"]},
    )
    assert acked.status_code == 200
    assert client.get("/api/duel/health").json()["alert"]["acknowledged"] is True
    assert client.get("/api/alerts").json()["acks"][0]["key"] == duel.HEALTH_ALERT_KEY

    client.delete(f"/api/alerts/{alert['key']}/ack")
    assert client.get("/api/duel/health").json()["alert"]["acknowledged"] is False


def test_an_ack_without_a_signature_is_refused(client, _db):
    assert client.post("/api/alerts/x/ack", json={"signature": ""}).status_code == 400
