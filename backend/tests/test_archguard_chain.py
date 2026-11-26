"""The chain's contract: the crown is decided in one place, and it stays that way.

`verdict.current()` is the one door to the crown (one chain, S4). An AST scan pins that no
module but `verdict` resolves the crown, and archguard (``scripts/archguard.mjs``, run in CI)
ratchets everything the chain has not absorbed yet: readers that still ask a single judge,
promoters other than Auto-apply, views that read a crown field other than the Verdict.
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
PKG = BACKEND / "pathbrain"


def _calls(tree: ast.AST, owner: str, name: str) -> list[int]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == name and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == owner:
            out.append(node.lineno)
    return out


def test_only_the_verdict_resolves_the_crown():
    offenders = []
    for path in PKG.rglob("*.py"):
        rel = path.relative_to(BACKEND).as_posix()
        if rel in ("pathbrain/verdict.py", "pathbrain/crowning.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line in _calls(tree, "crowning", "resolve"):
            offenders.append(f"{rel}:{line}")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.endswith("crowning"):
                if any(a.name in ("resolve", "_resolve_policy") for a in node.names):
                    offenders.append(f"{rel}:{node.lineno} imports {node.module}.resolve")
    assert not offenders, "the crown is resolved outside verdict.py: " + ", ".join(offenders)


def test_auto_apply_reads_the_verdict():
    tree = ast.parse((PKG / "crown_follower.py").read_text(encoding="utf-8"))
    assert _calls(tree, "verdict", "current"), "Auto-apply no longer reads verdict.current()"


def test_the_chain_rules_have_a_baseline():
    cfg = json.loads((REPO / "archguard.config.json").read_text())
    baseline = json.loads((REPO / ".archguard-baseline.json").read_text())
    chain = [r["id"] for r in cfg["rules"] if r["id"].startswith(("crown-", "frontend-crown-"))]
    assert {"crown-resolved-outside-verdict", "crown-judged-by-a-reader",
            "crown-promoted-outside-auto-apply", "frontend-crown-outside-verdict"} <= set(chain)
    assert all(rid in baseline for rid in chain)
    assert baseline["crown-resolved-outside-verdict"] == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed here; CI runs archguard")
def test_archguard_holds_the_baseline():
    proc = subprocess.run(["node", "scripts/archguard.mjs"], cwd=REPO, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
