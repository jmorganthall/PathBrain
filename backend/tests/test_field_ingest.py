"""S32: the raw real-browser ingest endpoint and its append-only table.

Pins: off by default; records stored unchanged; the fingerprint stamp comes only from what
PathBrain already knew (the crown follower's last reading, voided by any later firewall
write) and is NULL otherwise; no provider call per upload; UPDATE/DELETE refused; and the
migration against an existing, pre-S32 database.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select, text
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError

from pathbrain import crown_follower, field_ingest
from pathbrain.config import get_settings
from pathbrain.database import Base, _migrate, session_scope
from pathbrain.models import FieldRecord, FirewallWrite
from pathbrain.providers import mock

URL = "/api/field/records"
REC = {"v": 1, "host": "www.youtube.com", "recorded_at": "2026-11-15T10:00:00.000Z", "nav_type": "navigate",
       "ttfb_ms": 81.2, "fcp_ms": 410.0, "lcp_ms": 1250.5, "cls": 0.012,
       "long_tasks": {"count": 3, "total_ms": 260.0, "blocking_ms": 110.0, "window_ms": 10000},
       "hidden_before_paint": False}


TOKEN = "s33-test-token-9f2c"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "field_ingest_token", SecretStr(TOKEN))


@pytest.fixture()
def no_provider(monkeypatch):
    """Any provider call during ingest fails the test."""
    calls = []

    def boom(name):
        def _f(*a, **k):
            calls.append(name)
            raise AssertionError(f"ingest called the provider: {name}")
        return _f

    for name in ("apply", "apply_many", "discover"):
        monkeypatch.setattr(mock.MockProvider, name, boom(name), raising=False)
    import pathbrain.providers as providers
    monkeypatch.setattr(providers, "get_provider", boom("get_provider"))
    return calls


def _reading(monkeypatch, fp, checked_at):
    result = {"live_fingerprint": fp, "checked_at": checked_at.isoformat() if checked_at else None}
    monkeypatch.setattr(crown_follower, "status", lambda: {"last_check_at": None, "last_result": result})


def _rows_after(max_id):
    with session_scope() as s:
        return s.execute(select(FieldRecord).where(FieldRecord.id > max_id).order_by(FieldRecord.id)).scalars().all()


def _max_id():
    with session_scope() as s:
        return s.execute(select(FieldRecord.id).order_by(FieldRecord.id.desc()).limit(1)).scalar() or 0


def test_ingest_is_off_by_default(client):
    assert get_settings().field_ingest_token.get_secret_value() == ""
    before = _max_id()
    r = client.post(URL, json={"records": [REC]}, headers=AUTH)
    assert r.status_code == 404
    assert _rows_after(before) == []


def test_records_are_stored_unchanged_with_a_known_stamp(client, enabled, no_provider, monkeypatch):
    seen = datetime.now(timezone.utc) + timedelta(seconds=1)  # after every ledger row so far
    _reading(monkeypatch, "fp-live-123", seen)
    odd = dict(REC, extra_field={"kept": True})  # raw: even fields we don't know are kept
    before = _max_id()
    r = client.post(URL, json={"records": [REC, odd]}, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stored"] == 2 and body["firewall_fingerprint"] == "fp-live-123"
    assert body["fingerprint_source"] == field_ingest.SOURCE_CROWN_CHECK
    rows = _rows_after(before)
    assert [row.record for row in rows] == [REC, odd]
    assert {row.firewall_fingerprint for row in rows} == {"fp-live-123"}
    assert all(row.fingerprint_seen_at is not None and row.received_at is not None for row in rows)
    assert no_provider == []


def test_the_stamp_is_null_when_nothing_is_known(client, enabled, no_provider, monkeypatch):
    for fp, at in ((None, None), ("fp-x", None), (None, datetime.now(timezone.utc))):
        _reading(monkeypatch, fp, at)
        before = _max_id()
        assert client.post(URL, json={"records": [REC]}, headers=AUTH).status_code == 200
        [row] = _rows_after(before)
        assert row.firewall_fingerprint is None and row.fingerprint_source is None
    assert no_provider == []


def test_a_firewall_write_after_the_reading_voids_the_stamp(client, enabled, no_provider, monkeypatch):
    seen = datetime.now(timezone.utc) + timedelta(seconds=1)
    _reading(monkeypatch, "fp-before-write", seen)
    with session_scope() as s:
        w = FirewallWrite(at=seen + timedelta(seconds=1), op="test-s32", changes=[], reconfigures=1,
                          outcome="failed")  # any outcome: a failed write may still have moved it
        s.add(w)
        s.flush()
        write_id = w.id
    try:
        before = _max_id()
        assert client.post(URL, json={"records": [REC]}, headers=AUTH).status_code == 200
        [row] = _rows_after(before)
        assert row.firewall_fingerprint is None
        assert no_provider == []
    finally:  # the ledger is shared with the rest of the suite; leave no future-dated row
        with session_scope() as s:
            s.execute(text("DELETE FROM firewall_writes WHERE id = :i"), {"i": write_id})


def test_the_stamp_reader_never_raises_on_bad_state(monkeypatch):
    def broken():
        raise RuntimeError("state gone")

    monkeypatch.setattr(crown_follower, "status", broken)
    with session_scope() as s:
        assert field_ingest.known_fingerprint(s) == (None, None, None)


@pytest.mark.parametrize("payload", [
    {"records": REC},
    {"records": ["not an object"]},
    {"records": [REC] * (field_ingest.MAX_BATCH + 1)},
    {"records": [dict(REC, blob="x" * field_ingest.MAX_RECORD_BYTES)]},
    {},
])
def test_bad_batches_are_refused_whole(client, enabled, payload):
    before = _max_id()
    assert client.post(URL, json=payload, headers=AUTH).status_code == 422
    assert _rows_after(before) == []


def test_the_table_is_append_only(client, enabled):
    before = _max_id()
    assert client.post(URL, json={"records": [REC]}, headers=AUTH).status_code == 200
    [row] = _rows_after(before)
    with session_scope() as s:
        for sql in ("UPDATE field_records SET firewall_fingerprint = 'x' WHERE id = :i",
                    "DELETE FROM field_records WHERE id = :i"):
            with pytest.raises(DBAPIError, match="append-only"):
                s.execute(text(sql), {"i": row.id})
            s.rollback()
    [again] = _rows_after(before)
    assert again.record == REC and again.firewall_fingerprint == row.firewall_fingerprint


def test_migration_on_an_existing_pre_s32_database(tmp_path):
    from pathbrain import models  # noqa: F401 — registers every table

    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    old = [t for t in Base.metadata.sorted_tables if t.name != "field_records"]
    Base.metadata.create_all(eng, tables=old)
    _migrate(eng)  # a pre-S32 database: the migration must not need the table
    with eng.begin() as c:
        c.execute(text("INSERT INTO firewall_writes (at, op, reconfigures, outcome) VALUES "
                       "(CURRENT_TIMESTAMP, 'old', 1, 'ok')"))
        assert not list(c.execute(text("PRAGMA table_info(field_records)")))
    # The upgrade path is init_db's: create_all (adds only the new table), then _migrate, twice.
    Base.metadata.create_all(eng)
    _migrate(eng)
    _migrate(eng)
    with eng.begin() as c:
        cols = {r[1] for r in c.execute(text("PRAGMA table_info(field_records)"))}
        assert {"id", "received_at", "record", "firewall_fingerprint", "fingerprint_source",
                "fingerprint_seen_at"} <= cols
        trig = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
        assert {"trg_field_records_no_update", "trg_field_records_no_delete"} <= trig
        assert c.execute(text("SELECT count(*) FROM firewall_writes WHERE op='old'")).scalar() == 1
        c.execute(text("INSERT INTO field_records (received_at, record) VALUES (CURRENT_TIMESTAMP, '{}')"))
    with eng.begin() as c, pytest.raises(DBAPIError, match="append-only"):
        c.execute(text("DELETE FROM field_records"))
    eng.dispose()
