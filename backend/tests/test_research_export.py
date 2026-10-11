"""The research export (docs/plan-learning-engine.md, phase 1): every record a model needs,
streamed to a zip, with nothing secret in it."""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone

import pytest
from sqlalchemy import delete, select

from pathbrain import research_export
from pathbrain.database import session_scope
from pathbrain.methodology import ensure_current_methodology
from pathbrain.models import BenchmarkResult, Duel, DuelStatus, ProfileName, Run, RunStatus, Score

FP = "researchexp001"
SITE1, SITE2 = "https://one.example/", "https://two.example/"
SECRET_IP = "203.0.113.77"


def _nav(fcp: float) -> dict:
    return {
        "navigationStart": 0.0, "domainLookupStart": 5.0, "domainLookupEnd": 8.0,
        "connectStart": 8.0, "secureConnectionStart": 20.0, "connectEnd": 40.0,
        "requestStart": 41.0, "responseStart": 80.0, "responseEnd": 120.0,
        "loadEventEnd": fcp + 300.0,
    }


def _obs(fcp: float, lcp: float) -> dict:
    return {
        "nav": _nav(fcp),
        "paint": {"fcp": fcp, "lcp": lcp},
        "resources": [{"responseEnd": t} for t in (150.0, 200.0, 320.0)],
        "loaf": {"source": "longtask", "entries": []},
    }


@pytest.fixture()
def export_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(research_export, "_DIR_OVERRIDE", tmp_path)
    return tmp_path


@pytest.fixture()
def seeded(monkeypatch) -> dict:
    """One run, its scores, a profile name and a duel — removed again afterwards, because the
    suite shares one database and a stray COMPLETE duel or name changes other tests' ledgers.
    The secret-bearing config is patched in, never saved."""
    from pathbrain import config_store

    real_get_config = config_store.get_config

    def with_secret(session):
        cfg = dict(real_get_config(session))
        cfg["portable"] = {**(cfg.get("portable") or {}), "home_ip": SECRET_IP}
        return cfg

    monkeypatch.setattr(config_store, "get_config", with_secret)
    with session_scope() as s:
        version = ensure_current_methodology(s, real_get_config(s)).version
        run = Run(
            status=RunStatus.COMPLETE, iterations=2, iterations_completed=2,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            settings_fingerprint=FP,
            settings=[{"label": "wan", "quantum": 1514, "enabled": True}],
            methodology_version=version,
            config_used={"browser": {"urls": [SITE1, SITE2]}, "api_key": "sk-should-not-leave"},
        )
        s.add(run)
        s.flush()
        s.add(BenchmarkResult(
            run_id=run.id, plugin="browser", success=True, metrics={"fcp_ms": 310.0},
            details={
                "iteration_metrics": [{"fcp_ms": 300.0, "lcp_ms": 400.0, "unregistered_x": 1.0},
                                      {"fcp_ms": 320.0, "lcp_ms": 420.0}],
                "phases": {"context_setup_ms": 400.0},
            },
            raw={"iterations": [
                {"urls": {SITE1: _obs(300.0, 400.0), SITE2: _obs(310.0, 450.0)}},
                {"urls": {SITE1: _obs(320.0, 420.0), SITE2: _obs(330.0, 460.0)}},
            ]},
        ))
        s.add(Score(
            run_id=run.id, methodology_version=version, is_at_measure=True,
            comparability="exact", axis_scores={"overall": 81.5},
            subscores={"fcp": 90.0}, metric_values={"fcp": 310.0},
        ))
        s.add(Score(run_id=run.id, methodology_version="old-v1", axis_scores={"overall": 70.0},
                    subscores={"fcp": 1.0}, metric_values={"fcp": 1.0}))
        s.add(ProfileName(fingerprint=FP, name="Research Ruffle"))
        duel = Duel(
            status=DuelStatus.COMPLETE, run_ids=[run.id],
            matchups=[{"incumbent": FP, "challenger": "other", "deltas": [0.4, -0.1, 0.7],
                       "verdict": "draw", "note": f"seen from {SECRET_IP}"}],
        )
        s.add(duel)
        s.flush()
        ids = {"run_id": run.id, "duel_id": duel.id, "version": version}
    yield ids
    with session_scope() as s:
        s.execute(delete(Score).where(Score.run_id == ids["run_id"]))
        s.execute(delete(BenchmarkResult).where(BenchmarkResult.run_id == ids["run_id"]))
        s.execute(delete(Run).where(Run.id == ids["run_id"]))
        s.execute(delete(Duel).where(Duel.id == ids["duel_id"]))
        s.execute(delete(ProfileName).where(ProfileName.fingerprint == FP))


def _read(path) -> dict[str, object]:
    out: dict[str, object] = {}
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            data = zf.read(name).decode()
            out[name] = json.loads(data) if name.endswith(".json") else [json.loads(l) for l in data.splitlines()]
    return out


def test_the_export_carries_what_a_model_needs(seeded, export_dir):
    with session_scope() as s:
        path = research_export.build(s)
    files = _read(path)
    manifest = files["manifest.json"]
    # Every count in the manifest is the number of lines actually written.
    for name, n in manifest["counts"].items():
        assert len(files[name]) == n, name

    run = next(r for r in files["runs.jsonl"] if r["id"] == seeded["run_id"])
    browser = run["plugins"]["browser"]
    # Per-iteration readings, in the registry's vocabulary, registry metrics only.
    assert browser["iterations"][0]["fcp"] == 300.0
    assert browser["iterations"][1]["lcp"] == 420.0
    assert "unregistered_x" not in json.dumps(browser["iterations"])
    assert browser["phases"] == {"context_setup_ms": 400.0}
    # Full score detail under the current methodology, a summary under any other.
    assert run["scores"][seeded["version"]]["subscores"] == {"fcp": 90.0}
    assert run["scores"]["old-v1"]["overall"] == 70.0 and "subscores" not in run["scores"]["old-v1"]
    assert run["site_set"]

    prof = next(p for p in files["profiles.jsonl"] if p["fingerprint"] == FP)
    assert prof["name"] == "Research Ruffle" and prof["settings"][0]["quantum"] == 1514
    duel = next(d for d in files["duels.jsonl"] if seeded["run_id"] in (d["run_ids"] or []))
    assert duel["matchups"][0]["deltas"] == [0.4, -0.1, 0.7]
    assert any(m["is_current"] for m in files["methodologies.jsonl"])
    assert "site_metrics.jsonl" not in files  # off by default


def test_nothing_secret_leaves_the_box(seeded, export_dir):
    with session_scope() as s:
        path = research_export.build(s)
    with zipfile.ZipFile(path) as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist()).decode()
    assert SECRET_IP not in blob
    assert "sk-should-not-leave" not in blob
    assert '"home_ip"' not in blob
    assert "[redacted-ip]" in blob  # the address in free text was redacted, not dropped silently


def test_per_site_metrics_are_derived_per_page(seeded, export_dir):
    with session_scope() as s:
        path = research_export.build(s, per_site=True)
    rows = [r for r in _read(path)["site_metrics.jsonl"] if r["run_id"] == seeded["run_id"]]
    assert {(r["iteration"], r["url"]) for r in rows} == {(0, SITE1), (0, SITE2), (1, SITE1), (1, SITE2)}
    by = {(r["iteration"], r["url"]): r["metrics"] for r in rows}
    # Each page's own number, not the run's mean over pages.
    assert by[(0, SITE1)]["fcp"] == 300.0 and by[(0, SITE2)]["fcp"] == 310.0
    assert by[(0, SITE2)]["lcp"] == 450.0


def test_date_filter_bounds_the_runs(seeded, export_dir):
    with session_scope() as s:
        path = research_export.build(s, since=datetime(2100, 1, 1))
    assert _read(path)["runs.jsonl"] == []


def test_api_estimates_lists_downloads_and_refuses_paths(seeded, export_dir, client):
    est = client.get("/api/export/research/estimate").json()
    assert est["runs"] >= 1 and est["browser_results"] >= 1
    assert est["per_site_seconds"] is not None

    with session_scope() as s:
        path = research_export.build(s)
    listing = client.get("/api/export/research").json()["exports"]
    assert listing[0]["name"] == path.name
    resp = client.get(f"/api/export/research/{path.name}")
    assert resp.status_code == 200
    assert "manifest.json" in zipfile.ZipFile(io.BytesIO(resp.content)).namelist()
    # A name that is not a listed export never reaches the filesystem.
    assert client.get("/api/export/research/..pathbrain.db").status_code == 404
    traversal = client.get("/api/export/research/..%2F..%2Ftest.db")
    assert "zip" not in traversal.headers.get("content-type", "")
    assert b"SQLite format" not in traversal.content
    assert client.get("/api/export/research/nope.zip").status_code == 404


def test_only_the_newest_exports_are_kept(seeded, export_dir):
    for i in range(research_export.KEEP + 2):
        (export_dir / f"pathbrain-research-2000010{i}T000000Z.zip").write_bytes(b"x")
    with session_scope() as s:
        research_export.build(s)
    assert len(list(export_dir.glob("pathbrain-research-*.zip"))) == research_export.KEEP


def test_the_start_endpoint_runs_as_a_tracked_job(seeded, export_dir, client):
    import time

    from pathbrain import jobs

    job_id = client.post("/api/export/research").json()["job_id"]
    job = jobs.get(int(job_id))
    for _ in range(200):
        if job.status != "running":
            break
        time.sleep(0.05)
    assert job.status == "succeeded", job.error
    assert job.message.startswith("wrote pathbrain-research-")
    assert any(e["name"] in job.message for e in research_export.list_exports())


def test_a_malformed_page_is_skipped_and_counted_not_fatal(seeded, export_dir):
    with session_scope() as s:
        res = s.scalars(
            select(BenchmarkResult).where(BenchmarkResult.run_id == seeded["run_id"])
        ).one()
        raw = json.loads(json.dumps(res.raw))
        raw["iterations"][1]["urls"][SITE2]["loaf"] = [1, 2, 3]  # not the shape derive reads
        res.raw = raw
    with session_scope() as s:
        path = research_export.build(s, per_site=True)
    files = _read(path)
    rows = [r for r in files["site_metrics.jsonl"] if r["run_id"] == seeded["run_id"]]
    assert len(rows) == 3
    assert files["manifest.json"]["site_metrics_skipped"] >= 1
    assert {"run_id": seeded["run_id"], "iteration": 1, "url": SITE2} in files["manifest.json"]["site_metrics_skipped_sample"]
