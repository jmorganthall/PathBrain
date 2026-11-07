"""The one way PathBrain changes the firewall.

Every engine that writes the firewall calls one of three functions here, and nothing
else. The functions are:

* :func:`apply_changes` — write a list of shaper-field changes as ONE switch: every field
  on every pipe, then a single shaper reload (``provider.apply_many``). Profile switches,
  restores, sweep variants, the experiment's steps, "apply best", config test-apply and
  the write probe all go through it.
* :func:`set_pipes_enabled` — the pipe on/off toggle (the baseline test's SQM off/on).
  ``enabled`` is not a shaper field, so it is a separate firewall operation, but it is
  still made here.
* :func:`reload_shaper` — reload the shaper without writing a field (only the write
  probe uses it, to time the two halves of a switch separately).

Underneath, ``get_provider()`` always returns the guarded ``ResilientProvider``, so every
call here is ledgered, refused if it names a field PathBrain never writes, and attempted
exactly once. This module is about the *shape* of a write, not its policy. It used to be
written seven ways: two private ``apply_many`` wrappers, a sweep that restored one field
at a time, an apply-best loop that paid one shaper reload per field, and so on. Each of
those was a place where a fix to how PathBrain writes (the bandwidth unit split, say)
could be applied to some callers and missed by others.
``test_firewall_write.test_no_module_writes_the_firewall_except_through_firewall_write``
scans the source tree to keep it that way.
"""
from __future__ import annotations

from typing import Iterable

from .logging_config import get_logger

log = get_logger("firewall_write")


def normalize_changes(changes: Iterable[dict]) -> list[dict]:
    """``changes`` as the provider's wire dicts ``{pipe_uuid, param, value}``.

    Extra keys a planner carries (``from``, labels, …) are dropped. A change with no
    value is dropped too, because there is nothing to write: a baseline the firewall
    never reported does not become ``"None"`` on the wire."""
    out: list[dict] = []
    for ch in changes or []:
        if not ch or ch.get("value") is None:
            continue
        if not ch.get("param"):
            raise ValueError(f"A firewall change needs a param: {ch!r}")
        out.append({"pipe_uuid": ch.get("pipe_uuid") or None, "param": ch["param"], "value": ch["value"]})
    return out


def apply_changes(provider, changes: Iterable[dict], *, reload: bool = True,
                  dry_run: bool = False) -> list[dict]:
    """Write ``changes`` to the firewall as one switch, with one shaper reload.

    Returns the normalized change list that was written (empty means nothing was written,
    and the firewall was not called at all). ``dry_run`` logs what would be written and
    writes nothing. ``reload=False`` writes the fields and leaves the running shaper alone;
    only the write probe should ask for that, since a switch without a reload has not
    taken effect. Errors propagate unchanged: the guard's refusal, the provider's
    rejection and an unreachable firewall each mean something different to the caller."""
    batch = normalize_changes(changes)
    if not batch:
        return []
    if dry_run:
        log.info("[dry-run] would write %d field(s): %s", len(batch),
                 [(c["pipe_uuid"], c["param"], c["value"]) for c in batch])
        return batch
    provider.apply_many(batch, reload=reload)
    return batch


def set_pipes_enabled(provider, pipe_uuids: Iterable[str | None], enabled: bool, *,
                      best_effort: bool = False) -> list[str | None]:
    """Turn each pipe's shaping on or off. Returns the pipes that were switched.

    With ``best_effort`` a pipe that fails is logged and the rest are still switched:
    that is what a restore wants, since leaving one pipe off is better than leaving all
    of them off. Without it the first failure raises, which is what turning shaping
    off wants: stop before half the link is unshaped."""
    done: list[str | None] = []
    for uuid in pipe_uuids:
        try:
            provider.set_pipe_enabled(uuid, enabled)
            done.append(uuid)
        except Exception:  # noqa: BLE001 — re-raised unless best-effort
            if not best_effort:
                raise
            log.exception("Could not turn shaping %s on pipe %s", "on" if enabled else "off", uuid)
    return done


def reload_shaper(provider) -> dict:
    """Reload the shaper without writing any field."""
    return provider.reconfigure()
