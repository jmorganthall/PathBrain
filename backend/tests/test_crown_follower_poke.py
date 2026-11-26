"""S1 (one chain): a finished duel prompts a crown re-check.

duel.py ends a session with ``crown_follower.poke("duel verdict")``. ``poke`` took no
argument, so the call raised TypeError inside a try/except that logged at debug level: the
ring's verdict never reached Auto-apply until the next run or the six-hour backstop.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from pathbrain import crown_follower

PACKAGE = Path(__file__).resolve().parents[1] / "pathbrain"


def test_poke_with_a_reason_requests_a_full_check():
    crown_follower._state["last_full_check"] = 12345.0
    crown_follower.poke("duel verdict")
    assert crown_follower._state["last_full_check"] == 0.0


def test_poke_without_a_reason_still_works():
    crown_follower._state["last_full_check"] = 12345.0
    crown_follower.poke()
    assert crown_follower._state["last_full_check"] == 0.0


def test_every_poke_call_site_fits_the_signature():
    """Every ``crown_follower.poke(...)`` in the package binds to the real signature, so no
    caller can fail silently behind a broad except again."""
    sig = inspect.signature(crown_follower.poke)
    calls = 0
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "poke"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "crown_follower"
            ):
                calls += 1
                args = [None] * len(node.args)
                kwargs = {k.arg: None for k in node.keywords if k.arg}
                sig.bind(*args, **kwargs)  # raises TypeError on a mismatch
    assert calls >= 4  # duel, runner, and two settings routes
