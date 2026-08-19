"""IANA timezone resolution — the one place a stored zone name becomes a ``tzinfo``.

A schedule's ``hour``/``minute`` mean *the user's* wall clock, so each schedule stores the
IANA zone the browser was in when it was saved. Resolving that name needs an IANA tz
database, and a slim container may not ship one: ``zoneinfo`` then raises
``ZoneInfoNotFoundError`` for **every** name, including perfectly valid ones. That used to
surface as "unknown timezone 'America/Chicago' (use an IANA name)" on save — a 422 that
blocked every edit on the page, blaming the user's input for a missing OS package.

Two rules follow, and both live here so the routes and the scheduler can't drift:

* ``validate_timezone`` rejects a name only when the system *can* resolve names and this
  one isn't among them. With no tz database installed it accepts a well-formed name and
  warns — a saveable schedule that falls back to container-local beats an unsaveable one.
* ``schedule_zone`` never raises: an unresolvable zone degrades to the container's local
  zone rather than killing a scheduler tick.

The backend also depends on the ``tzdata`` package, so a normal install *does* have the
database and the fallback stays a safety net rather than the usual path.
"""
from __future__ import annotations

import re
from datetime import datetime, tzinfo
from functools import lru_cache
from zoneinfo import ZoneInfo

from .logging_config import get_logger

log = get_logger("timezones")

# "Region/City", "Region/Sub/City", or a bare "UTC"/"GMT"-style name.
_IANA_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9+_-]*(/[A-Za-z0-9+_.-]+){0,2}$")


@lru_cache(maxsize=1)
def tzdata_available() -> bool:
    """Can this system resolve IANA zone names at all?

    Probed with a zone that exists in every database — so a False here means "no tz
    database", never "that name is wrong".
    """
    try:
        ZoneInfo("America/New_York")
        return True
    except Exception:  # noqa: BLE001 — no database (or an unusable one)
        return False


def zone_or_none(name: str | None) -> tzinfo | None:
    """Resolve an IANA name, or None if it's empty/unresolvable."""
    name = (name or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return None


def validate_timezone(name: str | None) -> str:
    """Normalize a zone name for storage, raising ``ValueError`` if it's really invalid.

    Empty is valid (means container-local). A name that can't be resolved is rejected
    only when the system has a tz database to check against.
    """
    name = (name or "").strip()
    if not name:
        return ""
    if zone_or_none(name) is not None:
        return name
    if tzdata_available():
        raise ValueError(f"unknown timezone {name!r} (use an IANA name like 'America/Chicago')")
    if not _IANA_NAME.match(name):
        raise ValueError(f"{name!r} is not a valid IANA timezone name")
    log.warning(
        "No IANA tz database on this system; storing timezone %r unverified "
        "(schedules fall back to container-local until tzdata is installed)",
        name,
    )
    return name


def schedule_zone(section: dict) -> tzinfo | None:
    """The tzinfo a schedule's hour/minute are expressed in — never raises.

    ``section`` is a config section carrying a ``timezone`` key (``baseline_test``,
    ``duel``, …). Empty/unresolvable → the container's local zone, which is the legacy
    behavior and correct whenever TZ is wired through to the container.
    """
    name = (section.get("timezone") or "").strip()
    if name:
        zone = zone_or_none(name)
        if zone is not None:
            return zone
        log.warning("Invalid or unresolvable schedule timezone %r; using container-local", name)
    return datetime.now().astimezone().tzinfo


__all__ = ["schedule_zone", "tzdata_available", "validate_timezone", "zone_or_none"]
