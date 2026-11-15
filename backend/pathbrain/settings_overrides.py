"""Corrections to a run's recorded settings, kept beside the run (S22).

A run row's ``settings`` and ``settings_fingerprint`` are what was captured. A correction
(today only ``/settings/backfill``, which attributes settings-less history to the current
firewall profile) is appended to ``settings_overrides`` instead of rewriting the row.

Readers see the **effective** value: the newest override for the field if one exists,
else the run's own. Two forms of the one rule:

* :func:`effective_fingerprint` / :func:`effective_settings` — SQL expressions to use in
  place of ``Run.settings_fingerprint`` / ``Run.settings`` in a query (filter, select,
  group by).
* :func:`overlay` — for code that loads ``Run`` objects and reads the attributes: sets
  each run's effective values as its *loaded* state, so they are read but never flushed
  back to the run row.
"""
from __future__ import annotations

from sqlalchemy import JSON, String, func, select
from sqlalchemy.orm import aliased
from sqlalchemy.orm.attributes import set_committed_value

from .models import Run, SettingsOverride

FIELDS = ("settings", "settings_fingerprint")


def _latest(field: str):
    so = aliased(SettingsOverride)
    return (
        select(so.value)
        .where(so.run_id == Run.id, so.field == field)
        .order_by(so.id.desc())
        .limit(1)
        .scalar_subquery()
    )


def effective_fingerprint():
    """``Run.settings_fingerprint`` with overrides applied, as a SQL expression."""
    return func.coalesce(
        func.json_extract(_latest("settings_fingerprint"), "$"), Run.settings_fingerprint,
        type_=String,
    )


def effective_settings():
    """``Run.settings`` with overrides applied, as a SQL expression."""
    return func.coalesce(_latest("settings"), Run.settings, type_=JSON)


def overrides_for(session, run_ids) -> dict[int, dict[str, object]]:
    """``{run_id: {field: newest override value}}`` for the runs that have any."""
    ids = list(dict.fromkeys(run_ids))
    out: dict[int, dict[str, object]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for run_id, field, value in session.execute(
            select(SettingsOverride.run_id, SettingsOverride.field, SettingsOverride.value)
            .where(SettingsOverride.run_id.in_(chunk), SettingsOverride.field.in_(FIELDS))
            .order_by(SettingsOverride.id)
        ):
            out.setdefault(run_id, {})[field] = value  # ascending id: the newest wins
    return out


def overlay(session, runs) -> None:
    """Give each loaded ``Run`` its effective settings and fingerprint without dirtying
    it: the values are set as the run's loaded state, so nothing is written back."""
    runs = list(runs)
    found = overrides_for(session, [r.id for r in runs])
    for run in runs:
        for field, value in found.get(run.id, {}).items():
            set_committed_value(run, field, value)


def record(session, run_id: int, field: str, value, *, source: str, reason: str | None = None):
    """Append an override row (never updates one). Caller commits."""
    if field not in FIELDS:
        raise ValueError(f"unknown settings override field {field!r}")
    row = SettingsOverride(run_id=run_id, field=field, value=value, source=source, reason=reason)
    session.add(row)
    return row
