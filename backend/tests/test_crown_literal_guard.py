"""S15: a ratchet on crown metric key literals outside the methodology.

A crown rule lives only in ``methodology.py`` (CLAUDE.md). Code that names a crown leg by
literal (``"fcp_ms"``, ``"lcp"``, ``"cadence_cov"`` …) instead of reading it from the
methodology keeps ranking by the old legs after a publish. Many literal uses are
legitimate (the derive step produces ``fcp_ms``, the browser's paint entries are named
``fcp``/``lcp``, catalogs list every metric), so this is a ratchet, not a ban: ``ALLOWED``
records today's count per file and why. A new file using a literal, or a file whose count
grows, fails. When a "to move" use is moved, lower its count (or drop the entry).

Counted: string constants equal to one of ``LITERALS`` in executable code (docstrings are
not code and are skipped; comments are invisible to the AST). ``methodology.py`` and
``metrics.py`` are where these keys are defined and are not scanned.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "pathbrain"
LITERALS = frozenset({"fcp_ms", "lcp_ms", "cadence_cov", "fcp", "lcp"})
EXEMPT = frozenset({"methodology.py", "metrics.py"})

#: file (relative to pathbrain/) → (count, literals in that count still to move, why).
ALLOWED: dict[str, tuple[int, int, str]] = {
    "api/routes_smoothness.py": (2, 0, "the Smoothness page's catalog of every smoothness and "
                                       "speed reading, crown or not"),
    "calibration/smoothness_calibration.py": (2, 0, "the browser's paint-timing entry name"),
    "crowning.py": (4, 4, "to move: the speed budget's legs and the math crown's detail "
                          "sentence name the crown's millisecond legs"),
    "evidence.py": (1, 0, "EPOCH_EXCLUDED_KEYS: the named filter for a leg too coarse to "
                          "place an epoch boundary (S10)"),
    "idle_audit.py": (1, 0, "the browser's paint-timing entry name"),
    "instrument_drift.py": (6, 0, "the audit's catalog of quantities and the raw reads of each, "
                                  "former crown legs included"),
    "interpret/portable.py": (2, 0, "derive: produces cadence_cov and labels it"),
    "interpret/smoothness.py": (16, 0, "derive: reads paint entries and produces fcp_ms, lcp_ms, "
                                       "cadence_cov"),
    "interpret/waterfall.py": (2, 0, "the browser's paint-timing entry names"),
    "plugins/benchmark_browser.py": (4, 0, "the plugin emits fcp_ms / lcp_ms from paint entries"),
    "pooled_field.py": (40, 38, "to move: _raw_rows, the math crown, fastest_legs and leg_gaps "
                                "read fcp/lcp by name (38); the two leg-first policy leads in "
                                "LEG_FIRST_POLICIES are policy (2, S13)"),
    "ranking.py": (2, 0, "FCP and LCP are milestone sums that bake in connection setup: a "
                         "fact about the metrics, not a crown choice (moved from "
                         "api/routes_settings.py in S17)"),
    "portable.py": (6, 0, "the portable kit's own frozen rubric and its stand-in map keyed by "
                          "every crown metric any version used"),
}


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def literal_counts() -> dict[str, Counter]:
    found: dict[str, Counter] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel in EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_ids(tree)
        hits = Counter(
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value in LITERALS and id(n) not in docs
        )
        if hits:
            found[rel] = hits
    return found


def test_no_new_crown_literals_outside_the_methodology():
    problems = []
    for rel, hits in literal_counts().items():
        count = sum(hits.values())
        allowed = ALLOWED.get(rel, (0, 0, ""))[0]
        if count > allowed:
            problems.append(f"{rel}: {count} crown key literal(s) {dict(hits)}, allowed {allowed}")
    assert not problems, (
        "Crown metric keys named by literal outside methodology.py / metrics.py:\n  "
        + "\n  ".join(problems)
        + "\nRead the crown legs from the methodology instead (methodology.crown_keys, "
        "crown_metric_keys, ranking_crown, overall_metrics). If the use is genuinely not a "
        "crown choice (a derive step, a paint entry name, a catalog), add it to ALLOWED in "
        "tests/test_crown_literal_guard.py with the reason."
    )


def test_the_allow_list_is_tight():
    """A count that fell (a literal was moved) must be lowered, so the ratchet keeps the gain."""
    counts = {rel: sum(h.values()) for rel, h in literal_counts().items()}
    loose = {rel: (allowed, counts.get(rel, 0)) for rel, (allowed, _m, _w) in ALLOWED.items()
             if counts.get(rel, 0) < allowed}
    assert not loose, f"lower these ALLOWED counts to what the code now holds (allowed, found): {loose}"


def test_every_entry_says_why_and_to_move_never_exceeds_its_count():
    for rel, (count, to_move, why) in ALLOWED.items():
        assert why.strip(), rel
        assert 0 <= to_move <= count, rel
        assert (to_move > 0) == why.startswith("to move"), rel


def test_the_scanner_sees_a_new_literal(tmp_path, monkeypatch):
    pkg = tmp_path / "pathbrain"
    pkg.mkdir()
    (pkg / "fresh.py").write_text('"""Docstring mentioning "fcp" is fine."""\nLEGS = ("fcp", "lcp_ms")\n')
    (pkg / "methodology.py").write_text('CROWN = ("fcp", "lcp")\n')
    import sys

    monkeypatch.setattr(sys.modules[__name__], "PACKAGE", pkg)
    assert literal_counts() == {"fresh.py": Counter({"fcp": 1, "lcp_ms": 1})}
