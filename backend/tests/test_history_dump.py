"""Tests for the consolidated /history/dump export (last X runs + raw)."""
from __future__ import annotations

from pathbrain.database import session_scope
from pathbrain.models import BenchmarkResult, Run, RunStatus, ScoreResult


def _seed_run_with_raw(label: str) -> int:
    with session_scope() as session:
        run = Run(status=RunStatus.COMPLETE, label=label, iterations=2)
        session.add(run)
        session.flush()
        session.add(
            ScoreResult(run_id=run.id, sops=77.0, subscores={}, weights_used={}, metric_values={})
        )
        session.add(
            BenchmarkResult(
                run_id=run.id,
                plugin="icmp",
                success=True,
                metrics={"latency_ms": 12.0},
                details={"samples": 2},
                raw={"iterations": [{"rtts_ms": [10, 14]}, {"rtts_ms": [11, 13]}]},
            )
        )
        return run.id


def test_history_dump_includes_raw(client):
    rid = _seed_run_with_raw("dump-me")
    body = client.get("/api/history/dump?limit=5").json()
    assert body["count"] >= 1
    assert "generated_at" in body
    run = next(r for r in body["runs"] if r["id"] == rid)
    assert run["label"] == "dump-me"
    assert run["score"]["sops"] == 77.0
    # The whole point: per-plugin raw observations are present and consolidated.
    icmp = next(res for res in run["results"] if res["plugin"] == "icmp")
    assert icmp["raw"]["iterations"][0]["rtts_ms"] == [10, 14]


def test_history_dump_respects_limit(client):
    for i in range(3):
        _seed_run_with_raw(f"limit-{i}")
    body = client.get("/api/history/dump?limit=2").json()
    assert body["count"] == 2
    assert body["limit"] == 2
