"""S14: the ring seat reading is the backend's, and it matches what the browser computed.

The table in ``fixtures/ring_reading_cases.json`` was checked against the browser's own
logic (``frontend/src/utils/ringReading.ts``) before the frontend switched to reading
``reading``; the node check below re-runs that comparison whenever node and the frontend's
dependencies are installed, and is skipped otherwise."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pathbrain import duel, ring_reading

HERE = Path(__file__).resolve().parent
CASES = json.loads((HERE / "fixtures" / "ring_reading_cases.json").read_text())
FRONTEND = HERE.parents[1] / "frontend"


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_backend_reads_each_board_as_the_table_says(case):
    assert ring_reading.read_board(case["board"]) == {
        "standing": case["standing"], "verdict": case["verdict"],
    }


def test_status_annotates_the_live_board_and_every_seat(client, monkeypatch):
    seats = [CASES[2]["board"], CASES[7]["board"]]
    live = {**CASES[3]["board"], "seats": [dict(s) for s in seats]}
    monkeypatch.setattr(duel, "current", lambda: {"status": "complete", "live": live})
    body = client.get("/api/duel/status").json()
    assert body["live"]["reading"] == {"standing": "behind", "verdict": "unproven"}
    assert [s["reading"] for s in body["live"]["seats"]] == [
        {"standing": "ahead", "verdict": "unproven"}, {"standing": "ahead", "verdict": "called"},
    ]


def test_a_payload_without_a_live_board_is_left_alone(client, monkeypatch):
    monkeypatch.setattr(duel, "current", lambda: {"status": None})
    assert client.get("/api/duel/status").json() == {"status": None}


def _run_ts(cases: list[dict], tmp_path: Path, *, local: bool) -> list[dict]:
    path = tmp_path / ("local.json" if local else "served.json")
    path.write_text(json.dumps(cases))
    env = {**os.environ, "RING_TS": str(FRONTEND / "src" / "utils" / "ringReading.ts"),
           "RING_CASES": str(path), "RING_LOCAL": "1" if local else "0"}
    out = subprocess.run(["node", "--input-type=module", "-"], input=(HERE / "ring_reading_ts_check.mjs").read_text(),
                         capture_output=True, text=True, check=True, cwd=FRONTEND, env=env)
    return json.loads(out.stdout)


NEEDS_NODE = pytest.mark.skipif(
    not shutil.which("node") or not (FRONTEND / "node_modules" / "esbuild").exists(),
    reason="node or the frontend's dependencies are not installed",
)


@NEEDS_NODE
def test_the_browser_agrees_with_the_backend_with_and_without_the_field(tmp_path):
    served_cases = [{**c, "board": {**c["board"], "reading": ring_reading.read_board(c["board"])}} for c in CASES]
    local = _run_ts(CASES, tmp_path, local=True)  # an older backend: no `reading`
    served = _run_ts(served_cases, tmp_path, local=False)
    for c, lo, sv in zip(CASES, local, served):
        assert (lo["standing"], lo["verdict"]) == (c["standing"], c["verdict"]), c["name"]
        assert (sv["standing"], sv["verdict"]) == (c["standing"], c["verdict"]), c["name"]
        assert lo["sentence"] == sv["sentence"], c["name"]


@NEEDS_NODE
def test_the_browser_takes_the_served_reading_over_its_own(tmp_path):
    board = {**CASES[2]["board"], "reading": {"standing": "ahead", "verdict": "called"}}
    (row,) = _run_ts([{"name": "served wins", "board": board}], tmp_path, local=False)
    assert row["verdict"] == "called" and "enough to call it" in row["sentence"]
