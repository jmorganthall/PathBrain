"""Write-and-ping: the two halves of a write, timed separately, with ping running."""
from __future__ import annotations

import pytest

from pathbrain import firewall_guard as fg
from pathbrain import write_probe
from pathbrain.providers.mock import MockProvider


# ── the reading: gaps, not loss percentages ───────────────────────────────────


def _series(pattern: str, start: float = 0.0, step: float = 0.1):
    """'..xx..' → samples at `step` apart, x = lost."""
    return [{"t": round(start + i * step, 3), "rtt_ms": None if c == "x" else 1.0}
            for i, c in enumerate(pattern)]


def test_the_headline_is_the_worst_continuous_gap_not_the_loss_rate():
    """Twenty scattered drops and two seconds of nothing are the same loss percentage and
    completely different events. Only the second is an outage."""
    scattered = _series("x.x.x.x.x.x.x.x.x.x.")   # 50% loss, never two in a row
    solid = _series(".....xxxxxxxxxx.....")        # 50% loss, one unbroken second

    a = write_probe.summarize(scattered, 0.0, 10.0)
    b = write_probe.summarize(solid, 0.0, 10.0)
    assert a["loss_pct"] == b["loss_pct"] == 50.0
    assert a["worst_gap_ms"] == 0.0, "isolated drops are not a gap"
    assert b["worst_gap_ms"] >= 900.0


def test_a_short_flicker_is_not_reported_as_an_event():
    """Two consecutive misses on a 100ms sampler is ordinary internet, under MIN_GAP_MS."""
    assert write_probe.summarize(_series("...xx..."), 0.0, 10.0)["worst_gap_ms"] == 0.0


def test_a_window_with_no_samples_says_nothing_rather_than_zero():
    """A sampler that never ran must not read as a clean result."""
    out = write_probe.summarize([], 0.0, 10.0)
    assert out["loss_pct"] is None and out["worst_gap_ms"] is None


# ── the verdict: which half cost what, and which target ───────────────────────


def _step(name, through=0.0, firewall=0.0):
    return {"step": name, "targets": {"through": {"worst_gap_ms": through},
                                      "firewall": {"worst_gap_ms": firewall}}}


def test_the_reload_being_the_expensive_half_is_named_as_such():
    v = write_probe.verdict([_step("set_fields"), _step("reload", through=2400)])
    assert "reload cost 2.4s" in v and "kept answering" in v
    assert "inherent" in v


def test_the_box_going_away_outranks_traffic_dropping():
    """A queue rebuild dropping flows is expected; the firewall itself vanishing is not,
    and the verdict must lead with the second whenever both happened."""
    v = write_probe.verdict([_step("set_fields"), _step("reload", through=3000, firewall=2500)])
    assert "firewall itself stopped answering" in v
    assert "crash dump" in v


def test_a_field_write_costing_traffic_is_called_out_as_abnormal():
    v = write_probe.verdict([_step("set_fields", through=800), _step("reload", through=2000)])
    assert "Both halves" in v and "should be free" in v


def test_a_clean_write_says_so_without_inventing_a_culprit():
    v = write_probe.verdict([_step("set_fields"), _step("reload")])
    assert "cheap" in v


# ── the probe itself ──────────────────────────────────────────────────────────


def test_the_two_halves_are_issued_separately_and_only_the_reload_is_charged(_db):
    """The whole instrument rests on this: writing fields and reloading the shaper are two
    calls, and the ledger charges a reconfigure only to the half that performs one."""
    prov = MockProvider()
    assert prov.apply_many([{"param": "quantum", "value": 3000}], reload=False)["reconfigures"] == 0
    assert prov.reconfigure()["reconfigures"] == 1


def test_a_probe_is_refused_while_writes_are_hands_off(_db):
    """A supervised diagnostic is the case for arming writes deliberately, never a way
    around the guard — the one write path that studied the guard's own subject must not be
    the one that bypasses it."""
    fg.hands_off("the WAN dropped", by="test")
    with pytest.raises(ValueError, match="hands-off"):
        write_probe.start([{"param": "quantum", "value": 3000}], firewall_target="10.0.0.1")
    assert "write_probe" in fg.WRITING_KINDS


def test_a_probe_needs_something_to_write_and_somewhere_to_ping(_db):
    with pytest.raises(ValueError, match="Nothing to write"):
        write_probe.start([], firewall_target="10.0.0.1")
    with pytest.raises(ValueError, match="address to ping"):
        write_probe.start([{"param": "quantum", "value": 3000}], firewall_target="")


def test_the_api_reports_a_bad_request_as_400_not_500(client, _db):
    r = client.post("/api/firewall/write-probe",
                    json={"changes": [], "firewall_target": "10.0.0.1"})
    assert r.status_code == 400 and "Nothing to write" in r.json()["detail"]


# ── the address is known, not asked for ───────────────────────────────────────


def test_the_firewall_address_comes_from_the_provider_already_configured(monkeypatch):
    """Asking a person to retype the address of the box PathBrain talks to all day is
    asking for something the application already has."""
    from pathbrain.config import get_settings

    for url, want in [
        ("https://192.168.2.1:8443", "192.168.2.1"),
        ("http://fw.lan", "fw.lan"),
        ("192.168.2.1", "192.168.2.1"),       # no scheme, still an address
        ("", None),                            # nothing configured → no guess
    ]:
        monkeypatch.setenv("PATHBRAIN_OPNSENSE_URL", url)
        get_settings.cache_clear()
        assert write_probe.firewall_address() == want, url
    get_settings.cache_clear()


def test_a_probe_without_an_address_uses_the_configured_one(_db, monkeypatch):
    """`firewall_target` is optional: supplying one is an override, not a requirement.
    The driver is stubbed, so this tests the address resolution and nothing else."""
    from pathbrain.config import get_settings

    monkeypatch.setenv("PATHBRAIN_OPNSENSE_URL", "https://192.168.2.1")
    get_settings.cache_clear()
    seen: dict = {}
    monkeypatch.setattr(write_probe, "_drive", lambda *a, **k: seen.update(target=a[2]))

    probe_id = write_probe.start([{"param": "quantum", "value": 3000}])
    assert isinstance(probe_id, int)
    write_probe._state.update({"active": False, "id": None, "thread": None})
    get_settings.cache_clear()

    stored = write_probe.get(probe_id)
    assert stored is not None and stored["firewall_target"] == "192.168.2.1"


def test_with_nothing_configured_it_says_so_instead_of_guessing(_db, monkeypatch):
    from pathbrain.config import get_settings

    monkeypatch.setenv("PATHBRAIN_OPNSENSE_URL", "")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="No firewall address"):
        write_probe.start([{"param": "quantum", "value": 3000}])
    get_settings.cache_clear()


def test_the_status_endpoint_offers_the_address_so_the_page_can_fill_it_in(client, _db):
    body = client.get("/api/firewall/write-probe").json()
    assert "defaults" in body and "firewall_target" in body["defaults"]
    assert body["defaults"]["through_target"] == "1.1.1.1"
