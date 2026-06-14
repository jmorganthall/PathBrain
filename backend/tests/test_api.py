"""API smoke tests that don't depend on live network benchmarks."""
from __future__ import annotations


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_plugins_listed(client):
    resp = client.get("/api/plugins")
    assert resp.status_code == 200
    names = {p["name"] for p in resp.json()}
    assert {"icmp", "dns", "tcp", "tls", "http"} <= names


def test_config_roundtrip(client):
    resp = client.get("/api/config")
    assert resp.status_code == 200
    assert "weights" in resp.json()

    upd = client.put("/api/config", json={"weights": {"render": 30}})
    assert upd.status_code == 200
    assert upd.json()["weights"]["render"] == 30

    client.post("/api/config/reset")


def test_score_preview(client):
    resp = client.post("/api/score/preview", json={"dns": {"lookup_ms": 1.0}})
    assert resp.status_code == 200
    assert resp.json()["sops"] == 100.0


def test_discover_with_mock_provider(client):
    resp = client.post("/api/config/discover")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "mock"
    assert len(body["pipes"]) >= 1
    assert body["snapshot_id"] is not None


def test_provider_health(client):
    resp = client.get("/api/config/provider")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_experiments_stub(client):
    resp = client.get("/api/experiments")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_implemented"


def test_history_empty_ok(client):
    resp = client.get("/api/history")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
