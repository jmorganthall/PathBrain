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


def test_discover_provider_failure_returns_502(client, monkeypatch):
    """A failing provider should yield a 502 with a useful message, not a 500."""

    class BoomProvider:
        name = "opnsense"

        def discover(self):
            raise RuntimeError("connect timeout to 192.168.1.1")

        def snapshot(self):
            return {}

    monkeypatch.setattr(
        "pathbrain.api.routes_config.get_provider", lambda: BoomProvider()
    )
    resp = client.post("/api/config/discover")
    assert resp.status_code == 502
    assert "connect timeout" in resp.json()["detail"]


def test_experiments_status(client):
    resp = client.get("/api/experiments")
    assert resp.status_code == 200
    body = resp.json()
    assert "status" in body and "experiments" in body
    assert body["status"]["enabled"] is False  # disarmed by default


def test_history_empty_ok(client):
    resp = client.get("/api/history")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_rolling_score_shape(client):
    resp = client.get("/api/score/rolling?hours=24")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_hours"] == 24
    assert "median" in body and "count" in body and "p25" in body and "p75" in body


def test_adopt_rubric_and_rescore(client):
    adopted = client.post("/api/config/adopt-rubric")
    assert adopted.status_code == 200
    assert adopted.json()["rubric_version"] == "perceptual-v1"

    resp = client.post("/api/score/rescore")
    assert resp.status_code == 200
    body = resp.json()
    assert "rescored" in body and body["rubric_version"] == "perceptual-v1"


def test_monitoring_status_shape(client):
    resp = client.get("/api/monitoring")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert "interval_minutes" in body and "next_run_at" in body
