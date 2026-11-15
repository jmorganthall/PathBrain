"""The stored-profile list: read once per request, and its cheap change stamp still sees
every change a reader would.

The stamp counts completed runs off the status index, so a run with no settings (or one
whose settings arrive later as an override) must still land in, or stay out of, the list
exactly as before.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

import pytest
from sqlalchemy import delete

from pathbrain import profile_aggregates, refresh
from pathbrain.database import session_scope
from pathbrain.models import Run, RunStatus, SettingsOverride
from pathbrain.settings_overrides import record

SETTINGS = [{"label": "wan", "enabled": True, "quantum": 1514, "note": "list-cache"}]


def _run(s, fp=None, settings=None, status=RunStatus.COMPLETE) -> int:
    run = Run(status=status, iterations=1, iterations_completed=1,
              created_at=datetime.now(timezone.utc).replace(tzinfo=None),
              settings_fingerprint=fp, settings=settings)
    s.add(run)
    s.flush()
    return run.id


@pytest.fixture()
def made():
    ids: list[int] = []
    yield ids
    with session_scope() as s:
        s.execute(delete(SettingsOverride).where(SettingsOverride.run_id.in_(ids)))
        s.execute(delete(Run).where(Run.id.in_(ids)))
    refresh.invalidate_seed_cache()


def _fps(s) -> set[str]:
    return {p["fingerprint"] for p in refresh.list_profiles(s)}


def test_a_new_run_with_settings_joins_the_list(made):
    with session_scope() as s:
        before = _fps(s)
        made.append(_run(s, "list-cache-a", SETTINGS))
    with session_scope() as s:
        assert _fps(s) == before | {"list-cache-a"}


def test_a_settings_less_run_stays_out_until_an_override_names_it(made):
    with session_scope() as s:
        before = _fps(s)
        rid = _run(s)
        made.append(rid)
    with session_scope() as s:
        assert _fps(s) == before  # counted by the stamp, but nothing to list
        record(s, rid, "settings", SETTINGS, source="test", reason="list cache test")
        record(s, rid, "settings_fingerprint", "list-cache-b", source="test", reason="list cache test")
    with session_scope() as s:
        assert "list-cache-b" in _fps(s)


def test_an_older_run_that_completes_late_is_seen(made):
    with session_scope() as s:
        old = _run(s, "list-cache-c", SETTINGS, status=RunStatus.RUNNING)
        made.append(old)
        made.append(_run(s, "list-cache-d", SETTINGS))
    with session_scope() as s:
        fps = _fps(s)
        assert "list-cache-d" in fps and "list-cache-c" not in fps
        s.get(Run, old).status = RunStatus.COMPLETE
    with session_scope() as s:
        assert "list-cache-c" in _fps(s)  # its id sits below the cached max: the count catches it


def test_inside_a_request_the_list_is_read_once(made):
    with session_scope() as s:
        made.append(_run(s, "list-cache-e", SETTINGS))
    real = refresh._latest_settings_by_fingerprint
    with session_scope() as s, profile_aggregates.request_memo():
        with mock.patch.object(refresh, "_latest_settings_by_fingerprint", side_effect=real) as spy:
            first = refresh.list_profiles(s)
            first[0]["name"] = "mutated by a caller"
            second = refresh.list_profiles(s)
    assert spy.call_count == 1
    assert second[0]["name"] != "mutated by a caller"  # each caller gets its own copies
    assert {p["fingerprint"] for p in first} == {p["fingerprint"] for p in second}


def test_outside_a_request_every_call_checks_the_table(made):
    real = refresh._latest_settings_by_fingerprint
    with session_scope() as s:
        with mock.patch.object(refresh, "_latest_settings_by_fingerprint", side_effect=real) as spy:
            refresh.list_profiles(s)
            refresh.list_profiles(s)
    assert spy.call_count == 2
