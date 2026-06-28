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
        "quantum", "limit", "flows", "target", "interval", "ecn", "download_bandwidth",
    }
    assert set(sf.NON_WRITABLE_FIELDS) == {"upload_bandwidth", "queues", "scheduler"}
    assert set(sf.SWEEPABLE_FIELDS) == {"quantum", "target"}


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
