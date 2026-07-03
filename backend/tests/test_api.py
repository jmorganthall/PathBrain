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
    # SOPS is perception-led; TTFB at "best" scores 100.
    resp = client.post("/api/score/preview", json={"http": {"ttfb_ms": 1.0}})
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


def test_config_test_apply_round_trips(client):
    """The write-path test nudges quantum +1 then restores it, verifying each step."""
    resp = client.post("/api/config/test-apply")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["changed"] is True and body["restored"] is True
    assert body["test_value"] == body["original"] + 1
    assert body["error"] is None
    # Every step recorded and successful on the mock provider.
    assert [s["step"] for s in body["steps"]] == [
        "discover", "apply +1", "verify change", "restore", "verify restore"
    ]
    assert all(s["ok"] for s in body["steps"])
    # And the firewall is genuinely back to the original value afterwards.
    after = client.post("/api/config/discover").json()
    assert any(p.get("quantum") == body["original"] for p in after["pipes"])


def test_access_check_reports_capabilities(client):
    """The access check reports read (view) + write capabilities per credential.

    On the mock provider every read succeeds and the reversible write round-trips, so all
    reported capabilities are allowed (ok=True)."""
    resp = client.post("/api/config/access-check")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "mock"
    assert body["wrote"] is True
    by_key = {c["key"]: c for c in body["checks"]}
    # Base read probes + the write round-trip are present and pass on the mock.
    assert by_key["read_shaper"]["ok"] is True
    assert by_key["read_shaper"]["category"] == "view"
    assert by_key["snapshot"]["ok"] is True
    assert by_key["write_shaper"]["ok"] is True
    assert by_key["write_shaper"]["category"] == "write"
    # And the write test genuinely restored the original value.
    after = client.post("/api/config/discover").json()
    assert all(p.get("quantum") == 1514 for p in after["pipes"] if p.get("uuid") == "mock-download")


def test_access_check_skip_write_is_non_destructive(client):
    """include_write=false reports write as a declared (not live-tested) capability and
    never touches the firewall."""
    resp = client.post("/api/config/access-check", json={"include_write": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["wrote"] is False
    write = next(c for c in body["checks"] if c["key"] == "write_shaper")
    assert "writable field" in write["detail"]


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


def test_history_pagination(client):
    count = client.get("/api/history/count")
    assert count.status_code == 200
    assert isinstance(count.json()["count"], int)

    page = client.get("/api/history?limit=2&offset=0")
    assert page.status_code == 200
    assert len(page.json()) <= 2


def test_rolling_score_shape(client):
    resp = client.get("/api/score/rolling?hours=24")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_hours"] == 24
    # Methodology-aware shape: per-axis distributions, not a single SOPS median.
    assert "count" in body and "axis_scores" in body and "axes" in body
    assert body["methodology"]


def test_adopt_rubric_and_rescore(client):
    adopted = client.post("/api/config/adopt-rubric")
    assert adopted.status_code == 200
    assert adopted.json()["rubric_version"] == "perceptual-v5"

    # Re-score now runs as a background job: returns 202 + a job id, and the job
    # shows up (and finishes) in the unified /api/jobs feed.
    resp = client.post("/api/score/rescore")
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]
    assert job_id
    job = _await_job(client, job_id)
    assert job["status"] == "succeeded"


def _await_job(client, job_id: str, timeout: float = 10.0) -> dict:
    """Poll /api/jobs until the given job finishes; return its serialized entry."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        feed = client.get("/api/jobs").json()["jobs"]
        job = next((j for j in feed if j["id"] == job_id), None)
        if job and job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in time")


def test_monitoring_status_shape(client):
    resp = client.get("/api/monitoring")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert "interval_minutes" in body and "next_run_at" in body


def test_rolling_score_axes_and_stall_attribution(client):
    """The 24h rolling figure reports per-axis (Speed/Smoothness) medians under the
    current methodology, plus the network/render attribution badge (PRD R7)."""
    from pathbrain.database import session_scope
    from pathbrain.methodology import CURRENT_METHODOLOGY
    from pathbrain.models import BenchmarkResult, Run, RunStatus, Score

    with session_scope() as s:
        run = Run(status=RunStatus.COMPLETE, methodology_version=CURRENT_METHODOLOGY)
        s.add(run)
        s.flush()
        # A Score under the current methodology with Speed/Smoothness axis scores.
        s.add(Score(
            run_id=run.id, methodology_version=CURRENT_METHODOLOGY, is_at_measure=True,
            comparability="exact", axis_scores={"speed": 88.0, "smoothness": 54.0},
            subscores={"longest_stall": 44.0}, weights_used={"longest_stall": 0.4},
            metric_values={"longest_stall": 398.0},
        ))
        # A render-dominated stall: render_stall_ms >> network_stall_ms.
        s.add(BenchmarkResult(
            run_id=run.id, plugin="browser", success=True,
            metrics={"network_stall_ms": 40.0, "render_stall_ms": 360.0, "unknown_stall_ms": 0.0},
        ))

    body = client.get("/api/score/rolling?hours=24").json()
    assert body["methodology"] == CURRENT_METHODOLOGY
    assert body["axis_scores"]["speed"]["median"] == 88.0
    assert body["axis_scores"]["smoothness"]["median"] == 54.0
    attr = body["attribution"]
    assert attr is not None
    assert attr["dominant"] == "render"  # main-thread bound — not network-tunable
    assert attr["render_ms"] == 360.0 and attr["network_ms"] == 40.0
