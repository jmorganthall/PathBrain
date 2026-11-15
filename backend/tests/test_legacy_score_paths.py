"""S26: the legacy whole-history score paths are deprecated (still working), and reads of the
legacy ``score_results`` table are ratcheted so a new one fails CI.

``/score/rescore`` and ``/score/rederive`` answer as before plus a ``Deprecation`` header, a
``Link`` to ``/api/score/regrade``, ``deprecated: true`` and ``replacement``, and warn once per
process. Their job bodies are stubbed here; only the envelope is under test.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from pathbrain import jobs
from pathbrain.api import routes_score


@pytest.fixture()
def no_jobs(monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(jobs, "start", lambda kind, *a, **k: started.append(kind) or f"job-{kind}")
    monkeypatch.setattr(routes_score, "_DEPRECATION_WARNED", set())
    return started


@pytest.mark.parametrize("path,kind", [("/api/score/rescore", "rescore"), ("/api/score/rederive", "rederive")])
def test_the_legacy_endpoints_still_work_and_say_they_are_deprecated(client, no_jobs, path, kind, caplog):
    with caplog.at_level(logging.WARNING):
        first = client.post(path)
        second = client.post(path)
    assert first.status_code == 202 and no_jobs == [kind, kind]
    body = first.json()
    assert body == {"job_id": f"job-{kind}", "deprecated": True, "replacement": "/api/score/regrade"}
    assert first.headers["Deprecation"] == "true"
    assert first.headers["Link"] == '</api/score/regrade>; rel="successor-version"'
    assert second.json()["deprecated"] is True
    warned = [r for r in caplog.records if "is deprecated" in r.getMessage() and kind in r.getMessage()]
    assert len(warned) == 1, "one warning per process, not per call"


def test_the_replacement_is_not_deprecated(client, monkeypatch):
    monkeypatch.setattr(jobs, "start", lambda *a, **k: "job-regrade")
    resp = client.post("/api/score/regrade")
    assert resp.status_code == 202 and resp.json() == {"job_id": "job-regrade"}
    assert "Deprecation" not in resp.headers


# ── the ratchet ──────────────────────────────────────────────────────────────

PACKAGE = Path(__file__).resolve().parents[1] / "pathbrain"
READ = re.compile(r"\bScoreResult\b|\b(?:run|r)\.score\b(?!_)")

#: file (relative to pathbrain/) → (count, why). Import lines are not counted; models.py
#: defines the table and is not scanned.
ALLOWED: dict[str, tuple[int, str]] = {
    "runner.py": (13, "the capture write, the legacy rescore/rederive bodies (they ARE the legacy "
                      "path), and the derive-cache check: derivation_version lives only on "
                      "score_results, so the re-grade's fast path reads it (no versioned twin)"),
    "api/routes_score.py": (8, "the deprecated rederive's stale-run selection (derivation_version) and "
                               "GET /score/{run_id}, the legacy-score endpoint (left in place)"),
    "instrument_drift.py": (4, "counts runs whose cache predates the current derivation "
                               "(derivation_version, only on score_results)"),
    "methodology.py": (3, "the capture-time translation of a ScoreResult into the at-measure Score"),
    "api/routes_history.py": (3, "the history row's `legacy` flag (is there only a legacy score?) and "
                                 "the data dump, which exports the legacy record as a record"),
    "api/routes_results.py": (2, "RunDetail.score, the legacy score the run page shows beside "
                                 "the versioned Overall (API shape kept)"),
    "experiment.py": (1, "a comment saying why it does NOT read run.score.sops (S18)"),
    "sweep.py": (1, "a comment saying why it does NOT read run.score.sops (S19)"),
    "portable.py": (1, "PortableRun.score: the portable kit's own column, not score_results"),
    "api/routes_portable.py": (1, "PortableRun.score: the portable kit's own column, not score_results"),
}


def _counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel == "models.py":
            continue
        n = sum(
            len(READ.findall(line)) for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith(("from ", "import "))
        )
        if n:
            out[rel] = n
    return out


def test_no_new_read_of_the_legacy_score_table():
    counts = _counts()
    grown = {f: n for f, n in counts.items() if n > ALLOWED.get(f, (0, ""))[0]}
    assert not grown, (
        f"Reads of the legacy score_results table grew: {grown}. Read the versioned scores "
        "(methodology.latest_score) instead, or add it to ALLOWED with why."
    )


def test_the_ratchet_allow_list_is_not_stale():
    counts = _counts()
    stale = {f: (counts.get(f, 0), n) for f, (n, _) in ALLOWED.items() if counts.get(f, 0) < n}
    assert not stale, f"lower these ALLOWED counts (found, allowed): {stale}"
