"""How one live ring seat stands against the belt, and whether that is settled yet.

The Duels page used to work this out in the browser (``frontend/src/utils/ringReading.ts``)
from the live board's numbers: round count, median margin, p-value against alpha, the
streak, the practical floor and the minimum rounds. The rule is the duel's, so it is
computed here, beside the board, and the page reads it (``reading`` on each board). The
browser keeps its own copy only as a fallback for a backend that does not send it.

Pure: a board in, two words out. Nothing here changes a verdict; ``duel._adjudicate`` is
what ends a match, and this only reads the same numbers for a person.
"""
from __future__ import annotations

STANDINGS = ("ahead", "behind", "level", "unmeasured")
VERDICTS = ("called", "too_early", "unproven", "inside_floor", "nothing")


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def read_board(board: dict | None) -> dict:
    """``{"standing", "verdict"}`` for one seat's live board.

    * standing: ``unmeasured`` with no rounds or no median margin, else ``ahead`` /
      ``behind`` / ``level`` by the sign of the median margin (challenger − belt).
    * verdict, only once measured: ``inside_floor`` when the margin is under a positive
      ``min_margin``; else ``called`` when the p-value is at or under alpha or the streak
      has reached what it needs; else ``too_early`` under ``min_pairs`` rounds; else
      ``unproven``. ``nothing`` while unmeasured.
    """
    b = board or {}
    rounds = int(_num(b.get("pairs")) or 0)
    margin = _num(b.get("median_margin"))
    if rounds <= 0 or margin is None:
        return {"standing": "unmeasured", "verdict": "nothing"}
    standing = "ahead" if margin > 0 else "behind" if margin < 0 else "level"
    p_value, alpha = _num(b.get("p_value")), _num(b.get("alpha"))
    streak = b.get("streak") or {}
    length, needed = _num(streak.get("length")) or 0.0, _num(streak.get("needed")) or 0.0
    min_margin = _num(b.get("min_margin")) or 0.0
    min_pairs = _num(b.get("min_pairs")) or 0.0
    proven = p_value is not None and alpha is not None and p_value <= alpha
    streak_ends = length >= needed and length > 0
    if abs(margin) < min_margin and min_margin > 0:
        verdict = "inside_floor"
    elif proven or streak_ends:
        verdict = "called"
    elif rounds < min_pairs:
        verdict = "too_early"
    else:
        verdict = "unproven"
    return {"standing": standing, "verdict": verdict}


def annotate_live(live: dict | None) -> None:
    """Attach ``reading`` to a live payload and to each of its seats, in place."""
    if not isinstance(live, dict):
        return
    if "pairs" in live:
        live["reading"] = read_board(live)
    for seat in live.get("seats") or []:
        if isinstance(seat, dict):
            seat["reading"] = read_board(seat)
