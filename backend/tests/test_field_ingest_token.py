"""S33: the field ingest endpoint accepts only the LAN token from config, and never logs it."""
from __future__ import annotations

import logging

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from pathbrain.config import Settings, get_settings
from pathbrain.database import session_scope
from pathbrain.models import FieldRecord

URL = "/api/field/records"
TOKEN = "s33-lan-token-7d41e0"
BODY = {"records": [{"v": 1, "host": "en.wikipedia.org"}]}


def _count() -> int:
    with session_scope() as s:
        return len(s.execute(select(FieldRecord.id)).scalars().all())


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setattr(get_settings(), "field_ingest_token", SecretStr(TOKEN))


def test_the_right_token_is_accepted(client, token):
    n = _count()
    r = client.post(URL, json=BODY, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200, r.text
    assert r.json()["stored"] == 1 and _count() == n + 1


@pytest.mark.parametrize("header", [f"Bearer {TOKEN}x", "Bearer wrong", f"Bearer {TOKEN[:-1]}"])
def test_a_wrong_token_is_refused(client, token, header):
    n = _count()
    r = client.post(URL, json=BODY, headers={"Authorization": header})
    assert r.status_code == 403 and _count() == n
    assert TOKEN not in r.text


@pytest.mark.parametrize("headers", [{}, {"Authorization": ""}, {"Authorization": "Bearer "},
                                     {"Authorization": TOKEN}, {"Authorization": f"Basic {TOKEN}"}])
def test_a_missing_token_is_refused(client, token, headers):
    n = _count()
    r = client.post(URL, json=BODY, headers=headers)
    assert r.status_code == 401 and _count() == n
    assert TOKEN not in r.text


def test_an_empty_token_disables_ingest(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "field_ingest_token", SecretStr(""))
    n = _count()
    for headers in ({}, {"Authorization": "Bearer "}, {"Authorization": "Bearer anything"}):
        assert client.post(URL, json=BODY, headers=headers).status_code == 404
    assert _count() == n


def test_the_token_reads_from_the_environment_and_is_off_by_default(monkeypatch):
    monkeypatch.delenv("PATHBRAIN_FIELD_INGEST_TOKEN", raising=False)
    assert Settings(_env_file=None).field_ingest_token.get_secret_value() == ""
    monkeypatch.setenv("PATHBRAIN_FIELD_INGEST_TOKEN", TOKEN)
    assert Settings(_env_file=None).field_ingest_token.get_secret_value() == TOKEN


def test_the_token_is_never_logged_or_shown(client, token, caplog):
    caplog.set_level(logging.DEBUG)
    for headers in ({"Authorization": f"Bearer {TOKEN}"}, {"Authorization": "Bearer wrong"}, {}):
        client.post(URL, json=BODY, headers=headers)
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(get_settings()) and TOKEN not in str(get_settings().field_ingest_token)
