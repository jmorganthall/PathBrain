"""S23: instrument health lives in a derived, versioned table, read through one accessor.

``instrument_health_verdicts`` holds one row per assessment, keyed by run and
``HEALTH_VERSION``; readers take the newest row per (run, version) and fall back to the run
row's stamp only for a run with no derived verdict at all. The run column is still written.
"""
from __future__ import annotations

import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select

from pathbrain import host_adjust, instrument_health as ih, research_export
from pathbrain.database import _migrate_instrument_health, engine, session_scope
from pathbrain.models import BenchmarkResult, InstrumentHealthVerdict, Run, RunStatus

DERIVED = {"verdict": ih.DEGRADED, "ratio": 3.4, "ratios": {"context_ms": 3.4},
           "readings": {"context_ms": 3400.0, "close_ms": 2600.0, "reads_ms": 900.0, "nav_render_ms": 500.0}, "baseline": {"context_ms": 1000.0},
           "quantities": 2}
ROW = {"verdict": ih.HEALTHY, "ratio": 1.1, "readings": {"context_ms": 1100.0, "close_ms": 900.0}}


def _new_run(s, **kw) -> Run:
    run = Run(status=RunStatus.COMPLETE, iterations=1, iterations_completed=1,
              created_at=datetime.now(timezone.utc).replace(tzinfo=None), **kw)
    s.add(run)
    s.flush()
    return run


@pytest.fixture()
def runs():
    ids: list[int] = []
    yield ids
    with session_scope() as s:
        s.execute(delete(BenchmarkResult).where(BenchmarkResult.run_id.in_(ids)))
        s.execute(delete(Run).where(Run.id.in_(ids)))  # the trigger takes the verdicts with them


def test_a_derived_verdict_wins_and_the_row_is_the_fallback(runs):
    with session_scope() as s:
        derived_only = _new_run(s)
        ih.record_verdict(s, derived_only.id, DERIVED)
        row_only = _new_run(s, instrument_health=ROW)
        both = _new_run(s, instrument_health=ROW)
        ih.record_verdict(s, both.id, DERIVED)
        older_rule = _new_run(s, instrument_health=ROW)
        ih.record_verdict(s, older_rule.id, ROW, version="ih-0")
        runs += [derived_only.id, row_only.id, both.id, older_rule.id]
    with session_scope() as s:
        got = ih.health_by_run(s, runs)
        assert got[runs[0]]["verdict"] == ih.DEGRADED
        assert got[runs[1]]["verdict"] == ih.HEALTHY  # no derived row: the row stamp
        assert got[runs[2]]["verdict"] == ih.DEGRADED  # the derived row, not the row
        # Assessed under an older rule only: re-assessed on its next grade, not read off the row.
        assert runs[3] not in got
        # The SQL form agrees.
        ratios = dict(s.execute(select(Run.id, ih.effective_ratio()).where(Run.id.in_(runs))).all())
        assert ratios == {runs[0]: 3.4, runs[1]: 1.1, runs[2]: 3.4, runs[3]: None}


def test_assessing_writes_the_derived_row_and_still_stamps_the_run(runs, monkeypatch):
    monkeypatch.setattr(ih, "baseline", lambda session, limit=None: ih.Baseline(
        {"context_ms": 1000.0, "close_ms": 1000.0, "reads_ms": 300.0}, samples=50))
    with session_scope() as s:
        run = _new_run(s)
        s.add(BenchmarkResult(run_id=run.id, plugin="browser", success=True, metrics={},
                              details={"phases": {"context_ms": 3700.0, "close_ms": 5200.0}}))
        s.flush()
        s.refresh(run)
        runs.append(run.id)
        first = ih.assess_run(s, run, {})
        assert first["verdict"] == ih.DEGRADED
        again = ih.assess_run(s, run, {})  # read back, not recomputed
        assert again == first
    with session_scope() as s:
        rows = s.scalars(select(InstrumentHealthVerdict).where(InstrumentHealthVerdict.run_id == runs[0])).all()
        assert [(r.version, r.source) for r in rows] == [(ih.HEALTH_VERSION, "assess")]
        assert s.get(Run, runs[0]).instrument_health == rows[0].health  # column still written


def test_the_migration_copies_row_stamps_once(runs):
    with session_scope() as s:
        run = _new_run(s, instrument_health=ROW)
        unstamped = _new_run(s)
        runs += [run.id, unstamped.id]
    _migrate_instrument_health(engine)
    _migrate_instrument_health(engine)  # idempotent
    with session_scope() as s:
        rows = s.scalars(select(InstrumentHealthVerdict).where(InstrumentHealthVerdict.run_id.in_(runs))).all()
        assert [(r.run_id, r.version, r.source, r.health["verdict"]) for r in rows] == [
            (runs[0], ih.HEALTH_VERSION, "run_row", ih.HEALTHY)
        ]


def test_deleting_a_run_removes_its_verdicts():
    with session_scope() as s:
        run = _new_run(s)
        ih.record_verdict(s, run.id, DERIVED)
        rid = run.id
    with session_scope() as s:
        s.execute(delete(Run).where(Run.id == rid))
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(InstrumentHealthVerdict)
                        .where(InstrumentHealthVerdict.run_id == rid)) == 0


def test_readers_see_a_derived_only_verdict(runs, client, tmp_path, monkeypatch):
    with session_scope() as s:
        run = _new_run(s)
        ih.record_verdict(s, run.id, DERIVED)
        runs.append(run.id)
    # A single-run route.
    body = client.get(f"/api/results/{runs[0]}").json()
    assert body["instrument_health"]["verdict"] == ih.DEGRADED
    assert "Quarantined" in body["instrument_health_why"]
    # A batch reader.
    with session_scope() as s:
        assert runs[0] in host_adjust.run_readings(s, runs)
        assert host_adjust.leg_vector(s, runs[0]) == host_adjust.run_readings(s, runs)[runs[0]]
    # The research export: effective, raw, and the verdict rows.
    monkeypatch.setattr(research_export, "_DIR_OVERRIDE", tmp_path)
    with session_scope() as s:
        path = research_export.build(s)
    with zipfile.ZipFile(path) as zf:
        rows = [json.loads(line) for line in zf.read("runs.jsonl").decode().splitlines()]
        verdicts = [json.loads(line) for line in zf.read("instrument_health_verdicts.jsonl").decode().splitlines()]
    rec = next(r for r in rows if r["id"] == runs[0])
    assert rec["instrument_health"]["verdict"] == ih.DEGRADED and rec["instrument_health_raw"] is None
    assert [(v["run_id"], v["version"]) for v in verdicts if v["run_id"] == runs[0]] == [
        (runs[0], ih.HEALTH_VERSION)
    ]


# ── the ratchet ──────────────────────────────────────────────────────────────

PACKAGE = Path(__file__).resolve().parents[1] / "pathbrain"
DIRECT = re.compile(r"\bRun\.instrument_health\b|\brun\.instrument_health\b|getattr\([^)]*\"instrument_health\"")

#: file (relative to pathbrain/) → (count, why).
ALLOWED: dict[str, tuple[int, str]] = {
    "instrument_health.py": (4, "the accessor: the row fallback (SQL and batch), the sessionless "
                                "fallback, and the column still written at assessment"),
    "research_export.py": (1, "the raw row value exported beside the effective one"),
    "models.py": (1, "the derived model's docstring names the column it falls back to"),
}


def _counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        n = len(DIRECT.findall(path.read_text(encoding="utf-8")))
        if n:
            out[path.relative_to(PACKAGE).as_posix()] = n
    return out


def test_no_new_direct_read_of_the_run_instrument_health_column():
    counts = _counts()
    grown = {f: n for f, n in counts.items() if n > ALLOWED.get(f, (0, ""))[0]}
    assert not grown, (
        f"Direct reads of the run's instrument_health column grew: {grown}. Read it through "
        "instrument_health.health_by_run / health_of / effective_health, or add it to ALLOWED with why."
    )


def test_the_ratchet_allow_list_is_not_stale():
    counts = _counts()
    stale = {f: (counts.get(f, 0), n) for f, (n, _) in ALLOWED.items() if counts.get(f, 0) < n}
    assert not stale, f"lower these ALLOWED counts (found, allowed): {stale}"
