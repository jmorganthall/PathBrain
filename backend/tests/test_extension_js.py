"""S31: run the field recorder extension's Node tests (``extension/test``) with the backend
suite, when Node is available. They cover the record-shaping and privacy rules."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

EXTENSION = Path(__file__).resolve().parents[2] / "extension"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_extension_node_tests_pass():
    tests = sorted(str(p) for p in (EXTENSION / "test").glob("*.test.js"))
    assert tests, "no extension tests found"
    done = subprocess.run(["node", "--test", *tests], cwd=EXTENSION, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-2000:]
