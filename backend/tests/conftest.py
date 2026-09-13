"""Test fixtures.

A throwaway SQLite database and the mock config provider are configured via env
*before* PathBrain is imported, so the module-level engine binds to the temp DB.
"""
from __future__ import annotations

import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="pathbrain-test-")
os.environ["PATHBRAIN_DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["PATHBRAIN_CONFIG_PROVIDER"] = "mock"

from fastapi.testclient import TestClient  # noqa: E402

from pathbrain.database import init_db  # noqa: E402
from pathbrain.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _db():
    init_db()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# There was an autouse fixture here that defused the firewall guard between every test —
# pacing to zero, the hourly budget off, the deploy gate off, and a re-arm afterwards in
# case a simulated outage had tripped hands-off. It is gone with the valve: a write is
# refused only if it names a field PathBrain never writes, which no test needs switching
# off. That the whole suite needed the guard neutralised to run is itself a fair reading of
# what it was costing the real thing.
