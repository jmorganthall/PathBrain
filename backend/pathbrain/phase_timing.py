"""Request-scoped laps: where the time inside one slow read goes, step by step.

A read that wants the breakdown opens ``recording()``; code deep inside it calls
``laps("rank")`` once and then ``lap("fit")`` after each step. Outside a recording every
lap is a no-op, so the hooks cost nothing on any other path. Read-only diagnostics: no
lap changes a result.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

_REC: ContextVar[dict | None] = ContextVar("phase_timing_recording", default=None)


@contextmanager
def recording() -> Iterator[dict]:
    """Collect laps for the duration of the block: ``{name: [total_ms, count]}`` in the
    order each name first finished."""
    rec: dict = {}
    token = _REC.set(rec)
    try:
        yield rec
    finally:
        _REC.reset(token)


def _noop(_name: str) -> None:
    return None


def laps(prefix: str) -> Callable[[str], None]:
    """A lap clock for one function: each ``lap(name)`` books the time since the previous
    lap (or since this call) under ``prefix-name``. A no-op outside a recording."""
    rec = _REC.get()
    if rec is None:
        return _noop
    last = [time.perf_counter()]

    def lap(name: str) -> None:
        now = time.perf_counter()
        slot = rec.setdefault(f"{prefix}-{name}", [0.0, 0])
        slot[0] += (now - last[0]) * 1000.0
        slot[1] += 1
        last[0] = now

    return lap
