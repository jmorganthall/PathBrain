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
    # The firewall guard paces and budgets every write (``firewall_guard``); the suite's mock
    # provider would otherwise wait the reconfigure gap on every apply and trip the hourly
    # budget mid-run. Pacing off here; ``test_firewall_guard`` sets its own values.
    from pathbrain.config_store import save_config
    from pathbrain.database import session_scope

    with session_scope() as s:
        save_config(s, {"firewall": {"min_reconfigure_gap_s": 0, "max_reconfigures_per_hour": 0,
                                     "cooldown_after_outage_s": 0, "arm_required_after_deploy": False}})


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)
