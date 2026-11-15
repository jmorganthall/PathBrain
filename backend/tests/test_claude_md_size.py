"""CLAUDE.md stays a rulebook (sprint S3 of docs/plan-strategic-review.md).

It once grew to 415 KB of design diary, which every session had to load before it reached
the rules. History belongs in ``docs/history/``; this guard keeps it from creeping back.
"""

from pathlib import Path

CLAUDE_MD = Path(__file__).resolve().parents[2] / "CLAUDE.md"
MAX_BYTES = 25_000


def test_claude_md_stays_a_rulebook():
    size = CLAUDE_MD.stat().st_size
    assert size <= MAX_BYTES, (
        f"CLAUDE.md is {size:,} bytes (limit {MAX_BYTES:,}). Keep it to rules that name the "
        "file or test enforcing them; move narrative and rationale to docs/history/."
    )
