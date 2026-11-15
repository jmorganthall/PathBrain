"""S34: CORS lets the field recorder extension call its one endpoint, and nothing else."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from pathbrain.config import get_settings

URL = "/api/field/records"
EXT = "chrome-extension://ocfdagedpmcnfecmanjblkkjejeoakkf"
EDGE = "extension://ocfdagedpmcnfecmanjblkkjejeoakkf"
OTHER_EXT = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
TOKEN = "s34-cors-token-51aa"
PREFLIGHT = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization, content-type"}


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setattr(get_settings(), "field_ingest_token", SecretStr(TOKEN))


@pytest.mark.parametrize("origin", [EXT, EDGE])
def test_the_extension_preflight_is_answered(client, origin):
    r = client.options(URL, headers={"Origin": origin, **PREFLIGHT})
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == origin
    assert "POST" in r.headers["access-control-allow-methods"]
    assert "authorization" in r.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in r.headers


def test_the_extension_post_carries_the_allow_origin(client, token):
    r = client.post(URL, json={"records": [{"v": 1, "host": "www.reddit.com"}]},
                    headers={"Origin": EXT, "Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == EXT
    refused = client.post(URL, json={"records": []}, headers={"Origin": EXT, "Authorization": "Bearer nope"})
    assert refused.status_code == 403 and refused.headers["access-control-allow-origin"] == EXT


@pytest.mark.parametrize("origin", [OTHER_EXT, "http://evil.example", "null"])
def test_other_origins_get_nothing_on_the_ingest_endpoint(client, token, origin):
    pre = client.options(URL, headers={"Origin": origin, **PREFLIGHT})
    assert pre.status_code == 403 and "access-control-allow-origin" not in pre.headers
    r = client.post(URL, json={"records": []}, headers={"Origin": origin, "Authorization": f"Bearer {TOKEN}"})
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.parametrize("path", ["/api/health", "/api/config", "/api/firewall/writes"])
def test_the_extension_origin_is_not_allowed_anywhere_else(client, path):
    r = client.get(path, headers={"Origin": EXT})
    assert "access-control-allow-origin" not in r.headers
    pre = client.options(path, headers={"Origin": EXT, **PREFLIGHT})
    assert pre.headers.get("access-control-allow-origin") != EXT


def test_the_default_origin_is_the_pinned_extension_id():
    import base64
    import hashlib

    manifest = json.loads((Path(__file__).resolve().parents[2] / "extension" / "manifest.json").read_text())
    digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
    ext_id = "".join(chr(ord("a") + int(c, 16)) for c in digest)
    from pathbrain import field_ingest

    assert ext_id == "ocfdagedpmcnfecmanjblkkjejeoakkf"
    assert field_ingest.allowed_origins() == {f"chrome-extension://{ext_id}", f"extension://{ext_id}"}
