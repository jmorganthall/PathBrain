"""The single reader of a stored ``BenchmarkResult.raw``'s nesting.

The runner persists each result as ``{"iterations": [<per-iteration raw>, ...]}`` — one entry per
suite iteration (``runner`` write site). For the browser plugin each per-iteration payload is
``{"urls": {url: {"nav", "paint", "resources", "loaf", ...}}}``. That two-level nesting is an
implicit contract every consumer used to re-implement from memory (the re-derive path, the
smoothness API, the pause diagnostic, calibration) — and a single reader getting it wrong
(reading ``raw["urls"]`` instead of ``raw["iterations"][i]["urls"]``) failed *silently*, returning
nothing instead of erroring. Defining the unwrap **once** here removes that whole class of bug: a
new consumer calls these instead of guessing the shape.
"""
from __future__ import annotations

from typing import Iterator


def stored_iterations(raw: dict | None) -> list[dict]:
    """The per-iteration raw payloads from a stored ``BenchmarkResult.raw``.

    Returns one dict per suite iteration (each a plugin's own raw payload, e.g. the browser's
    ``{"urls": ...}`` or icmp's ``{"targets": ...}``). Non-dict entries are coerced to ``{}`` so
    callers can iterate without per-item guards. Back-compat: a bare per-iteration payload (no
    ``iterations`` key) is treated as a single iteration, so an unwrapped raw still reads sanely."""
    if not isinstance(raw, dict):
        return []
    its = raw.get("iterations")
    if isinstance(its, list):
        return [it if isinstance(it, dict) else {} for it in its]
    return [raw] if raw else []  # unwrapped/bare payload → one iteration


def browser_url_observations(raw: dict | None) -> Iterator[tuple[int, str, dict]]:
    """Yield ``(iteration_index, url, observation)`` for every URL across a stored browser result.

    ``observation`` is the per-URL dict (``{"nav", "paint", "resources", "loaf", ...}``). The one
    walker of a stored browser result's per-URL observations — used by re-derive, the smoothness
    API, the pause diagnostic, and calibration — so the ``iterations`` → ``urls`` nesting lives in
    exactly one place. Non-dict / error URL entries are skipped."""
    for i, it in enumerate(stored_iterations(raw)):
        urls = it.get("urls")
        if not isinstance(urls, dict):
            continue
        for url, obs in urls.items():
            if isinstance(obs, dict):
                yield i, url, obs
