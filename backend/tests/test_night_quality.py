"""scripts/night_quality.py: per-night duel and host figures off a small fixture ledger."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from pathbrain import decidability
from pathbrain.database import Base
from pathbrain.models import Duel, DuelStatus, Run, RunStatus

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "night_quality.py"
_spec = importlib.util.spec_from_file_location("night_quality", SCRIPT)
nq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nq)

CHICAGO = ZoneInfo("America/Chicago")
NOW = datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc)

HOST = {"host_cpu_pct": 90.0, "container_share_of_busy": 0.1}
SELF = {"host_cpu_pct": 90.0, "container_share_of_busy": 0.9}
QUIET = {"host_cpu_pct": 10.0}


def _match(verdict: str, deltas: list[float], reason: str = "") -> dict:
    return {"incumbent": "aaa", "challenger": "bbb", "verdict": verdict, "reason": reason,
            "deltas": deltas, "design": "ring"}


def _night_one_matchups() -> list[dict]:
    return [
        _match("challenger", [1.0, 1.4, 0.6, 1.2, 0.9]),
        _match("incumbent", [-0.8, -1.1, -0.4, -0.9]),
        _match("draw", [0.1, -0.3, 0.2, 0.0], reason="window closed (undecided)"),
        _match("draw", [0.2, -0.2, 0.4, -0.1]),
        _match("draw", [-0.5, 0.3, 0.1, 0.2]),
        _match("draw", [0.0, 0.6, -0.4, 0.1]),
    ]


def _build(session: Session) -> None:
    runs = [Run(status=RunStatus.COMPLETE, host_pressure=hp) for hp in (HOST, SELF, QUIET, None, HOST)]
    session.add_all(runs)
    session.flush()
    r = [x.id for x in runs]
    # 22:30 CST on Nov 9 is 04:30 UTC on Nov 10: it belongs to the night of Nov 9.
    session.add(Duel(status=DuelStatus.COMPLETE, started_at=datetime(2026, 11, 10, 4, 30),
                     matchups=_night_one_matchups(), run_ids=r[:4]))
    session.add(Duel(status=DuelStatus.COMPLETE, started_at=datetime(2026, 11, 11, 3, 0),
                     matchups=[_match("draw", [0.1, 0.2])], run_ids=[r[4]]))
    # Outside a four-night window.
    session.add(Duel(status=DuelStatus.COMPLETE, started_at=datetime(2026, 10, 1, 3, 0),
                     matchups=[_match("challenger", [1.0, 1.0, 1.0, 1.0])], run_ids=[]))
    session.commit()


def _engine(url: str = "sqlite://"):
    eng = create_engine(url, future=True)
    Base.metadata.create_all(eng)
    return eng


def test_nights_are_counted_from_the_ledger_and_the_host_readings():
    with Session(_engine(), future=True) as s:
        _build(s)
        rows = nq.night_quality(s, nights=4, tz=CHICAGO, now=NOW)

    by = {r["night"]: r for r in rows}
    assert list(by) == [date(2026, 11, 9), date(2026, 11, 10), "ALL"]

    one = by[date(2026, 11, 9)]
    assert (one["matches"], one["decisive"], one["aborted"]) == (6, 2, 1)
    assert one["decisive_share"] == 2 / 6
    expected = decidability.resolving_power([{"matchups": _night_one_matchups()}], 30)
    assert one["min_margin"] is not None and one["min_margin"] == expected["min_margin"]
    # Four runs, three with a reading; one of those three was the NAS's own load.
    assert (one["runs"], one["runs_with_pressure"], one["host_share"]) == (4, 3, 1 / 3)

    two = by[date(2026, 11, 10)]
    assert (two["matches"], two["decisive"], two["min_margin"], two["host_share"]) == (1, 0, None, 1.0)

    total = by["ALL"]
    assert (total["sessions"], total["matches"], total["decisive"]) == (2, 7, 2)
    assert total["host_share"] == 2 / 4

    table = nq.format_table(rows)
    assert "2026-11-09" in table and "33.3%" in table and "ALL" in table


def test_the_script_runs_read_only_against_a_given_database(tmp_path):
    db = tmp_path / "nq.db"
    eng = _engine(f"sqlite:///{db}")
    with Session(eng, future=True) as s:
        _build(s)
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--nights", "3000", "--tz", "America/Chicago", "--db", f"sqlite:///{db}"],
        capture_output=True, text=True, check=True, cwd=SCRIPT.parents[1],
    ).stdout
    assert "Night quality" in out and "2026-11-09" in out and "2026-09-30" in out and "ALL" in out
    with Session(eng, future=True) as s:
        assert len(s.query(Duel).all()) == 3
