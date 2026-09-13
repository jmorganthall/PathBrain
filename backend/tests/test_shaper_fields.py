"""The shaper-field registry is the single source of truth for the SQM field model.

These tests freeze the invariants that used to live only in comments — the kind whose
silent drift produced the "valid but unappliable profile" bug that aborted the challenger
race. A bad edit to the registry, the read model, or the provider mapping fails here.
"""
from __future__ import annotations

from dataclasses import fields as dataclass_fields

from pathbrain import shaper_fields as sf
from pathbrain.providers.base import FqCodelConfig
from pathbrain.providers.mock import MockProvider
from pathbrain.providers.opnsense import _PARAM_FIELD


def test_derived_views_match_the_known_model():
    # Regression lock on the de-duplication: the derived constants equal the values that
    # were previously hand-listed across settings_profile (order-independent).
    assert set(sf.CANON_FIELDS) == {
        "download_bandwidth", "upload_bandwidth", "quantum", "limit", "target",
        "interval", "ecn", "flows", "queues", "scheduler",
    }
    assert set(sf.WRITABLE_FIELDS) == {
        "quantum", "limit", "target", "interval", "ecn", "download_bandwidth",
    }
    assert set(sf.NON_WRITABLE_FIELDS) == {"upload_bandwidth", "flows", "queues", "scheduler"}
    assert set(sf.SWEEPABLE_FIELDS) == {"quantum", "target", "interval"}


def test_writable_fields_are_identity_fields():
    # If apply() can change a field, it must define the profile — else applying it wouldn't
    # move the profile. (The converse — identity fields we can't write — is allowed and is
    # exactly what the challenger reachability check handles.)
    assert set(sf.WRITABLE_FIELDS) <= set(sf.CANON_FIELDS)


def test_sweepable_fields_are_writable():
    assert set(sf.SWEEPABLE_FIELDS) <= set(sf.WRITABLE_FIELDS)


def test_read_model_matches_the_registry():
    # FqCodelConfig (the discover() read model) must carry exactly the identity fields —
    # a sixth copy of the field list would otherwise drift from the registry.
    model = {f.name for f in dataclass_fields(FqCodelConfig)} - {"extra"}
    assert model == set(sf.CANON_FIELDS)


def test_opnsense_can_map_every_writable_field():
    # The OPNsense apply() mapping must cover every writable field, or applying it silently
    # no-ops. This is the "1:1 by comment" relationship, now executable.
    assert set(sf.WRITABLE_FIELDS) <= set(_PARAM_FIELD)


def test_provider_writable_fields_accessor():
    # The single capability accessor returns the registry set by default.
    assert set(MockProvider().writable_fields()) == set(sf.WRITABLE_FIELDS)


def test_coerce_value_canonicalizes_to_firewall_format():
    # Unit fields → BARE int (the firewall keys duration selects by the bare number, e.g. "5",
    # not "5ms"), regardless of how the value arrives.
    assert sf.coerce_value("target", "5ms") == 5
    assert sf.coerce_value("target", 5) == 5
    assert sf.coerce_value("target", "5") == 5
    assert sf.coerce_value("target", 5.0) == 5
    assert sf.coerce_value("interval", 100) == 100
    assert isinstance(sf.coerce_value("target", "5ms"), int)
    # Int fields → int (never a string), so the fingerprint matches discover()'s int.
    assert sf.coerce_value("quantum", "3000") == 3000
    assert sf.coerce_value("quantum", 3000.0) == 3000
    assert isinstance(sf.coerce_value("quantum", "3000"), int)
    # Bool field → bool from common truthy strings.
    assert sf.coerce_value("ecn", "true") is True
    assert sf.coerce_value("ecn", "false") is False
    assert sf.coerce_value("ecn", False) is False
    # Bandwidth / unknown-format strings pass through untouched.
    assert sf.coerce_value("download_bandwidth", "100Mbit") == "100Mbit"
    # Unparseable numeric input degrades to passthrough (no raise).
    assert sf.coerce_value("target", "fast") == "fast"
    assert sf.coerce_value("quantum", None) is None


def test_flows_is_captured_but_never_written():
    """The flow table is recorded on every run and never changed.

    Writing it forces dummynet to rebuild its queues rather than re-read a value, and every
    write on this link that carried it took the box off the network for 30-35s while every
    write that did not was sub-second. So it keeps its identity facet — it is discovered,
    stored, fingerprinted, and tells two profiles apart — and loses its writable one.
    """
    f = sf.field("flows")
    assert f is not None and f.identity and not f.writable and not f.sweepable
    assert "flows" in sf.CANON_FIELDS          # still captured, still part of the profile
    assert "flows" not in sf.WRITABLE_FIELDS   # never written
    assert "flows" in sf.NON_WRITABLE_FIELDS   # ...and so a differing profile is unreachable


def test_no_plan_can_write_the_flow_table():
    """The registry decision reaches the planner: a target whose flows differs from live
    plans every other difference and simply omits that one.

    This is the property that matters, because the write that hurt was never a deliberate
    "set flows" — it was a profile switch whose diff happened to contain it.
    """
    from pathbrain.settings_profile import normalize, plan_apply

    live = MockProvider().discover()
    target = normalize(live)
    for pipe in target:
        pipe["flows"] = int(pipe.get("flows") or 1024) * 2
        pipe["quantum"] = int(pipe.get("quantum") or 1514) + 1
    changes, _ = plan_apply(target, live)
    assert changes, "the quantum difference should still be planned"
    assert "flows" not in {c["param"] for c in changes}
