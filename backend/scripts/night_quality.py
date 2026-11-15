"""How good was each night's evidence? A read-only baseline for the night-quiet work (S4).

For each of the last N nights it prints:

* **matches** — duel matchups the ladder finished that night (aborted ones included, as
  ``duel.lineal_belt`` counts them, so the 845 / 342 figures reproduce);
* **decisive** — matches whose ``duel.outcome`` is a win for either side, and its share;
* **aborted** — matches the ladder failed to measure (window closed, unusable legs);
* **power** — ``decidability.resolving_power`` over that night's own matchups at a 30-round
  cap: the smallest margin, in Overall points, the ring could have called. Blank when the
  night had too few rounds to measure its own noise (``decidability.MIN_NOISE_MATCHUPS``);
* **host** — the share of that night's duel runs whose ``host_pressure.attribute`` reading
  is ``host`` (the NAS busy with something other than PathBrain), over the runs that carry
  a reading at all; ``with`` is how many did.

A night is the evening a duel session started in, local time: a session that starts at
22:00 and runs past midnight belongs to the night it started. A last ``ALL`` row repeats
the figures over every night shown, which is where the ledger-wide resolving power lands.

Reads only. It opens the database the app is configured for (``PATHBRAIN_DATABASE_URL``)
or the one given with ``--db``, writes nothing and touches no firewall. On the NAS:

    docker exec pathbrain python scripts/night_quality.py [--nights 30] [--tz America/Chicago]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_NIGHTS = 30
DEFAULT_ROUNDS = 30
#: A night starts at noon local: a session started before noon belongs to the night before.
NIGHT_START_HOUR = 12


def _aware(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def night_of(ts: datetime, tz: tzinfo) -> date:
    """The local evening a moment belongs to."""
    return (ts.astimezone(tz) - timedelta(hours=NIGHT_START_HOUR)).date()


def _row(night: date | str, sessions: list[dict], attributions: list[str | None], rounds: int) -> dict:
    from pathbrain import decidability, duel

    matches = decisive = aborted = 0
    for sess in sessions:
        for m in sess.get("matchups") or []:
            if not m or not m.get("incumbent") or not m.get("challenger"):
                continue
            matches += 1
            result = duel.outcome(m)
            if result == duel.ABORTED:
                aborted += 1
            elif result != "draw":
                decisive += 1
    power = decidability.resolving_power(sessions, rounds)
    measured = [a for a in attributions if a is not None]
    host = sum(1 for a in measured if a == "host")
    return {
        "night": night,
        "sessions": len(sessions),
        "matches": matches,
        "decisive": decisive,
        "aborted": aborted,
        "decisive_share": (decisive / matches) if matches else None,
        "min_margin": power.get("min_margin"),
        "sigma": power.get("sigma"),
        "runs": len(attributions),
        "runs_with_pressure": len(measured),
        "host_share": (host / len(measured)) if measured else None,
    }


def night_quality(session, *, nights: int = DEFAULT_NIGHTS, rounds: int = DEFAULT_ROUNDS,
                  tz: tzinfo = timezone.utc, now: datetime | None = None) -> list[dict]:
    """One row per night that held a duel session, oldest first, then an ``ALL`` row."""
    from sqlalchemy import select

    from pathbrain import host_pressure
    from pathbrain.models import Duel, Run

    now = _aware(now) or datetime.now(timezone.utc)
    last = night_of(now, tz)
    first = last - timedelta(days=max(1, int(nights)) - 1)

    by_night: dict[date, list[dict]] = {}
    run_ids: dict[date, list[int]] = {}
    for d in session.scalars(select(Duel).order_by(Duel.id)).all():
        start = _aware(d.started_at) or _aware(d.created_at)
        if start is None:
            continue
        n = night_of(start, tz)
        if not first <= n <= last:
            continue
        by_night.setdefault(n, []).append({"id": d.id, "matchups": list(d.matchups or [])})
        run_ids.setdefault(n, []).extend(int(r) for r in (d.run_ids or []) if r is not None)

    wanted = {rid for ids in run_ids.values() for rid in ids}
    pressure: dict[int, dict | None] = {}
    if wanted:
        for rid, hp in session.execute(select(Run.id, Run.host_pressure).where(Run.id.in_(wanted))):
            pressure[rid] = hp

    def attributions(ids: list[int]) -> list[str | None]:
        return [host_pressure.attribute(pressure[r]) if pressure.get(r) else None
                for r in ids if r in pressure]

    rows = [_row(n, by_night[n], attributions(run_ids.get(n, [])), rounds) for n in sorted(by_night)]
    all_sessions = [s for n in sorted(by_night) for s in by_night[n]]
    all_ids = [r for n in sorted(run_ids) for r in run_ids[n]]
    rows.append(_row("ALL", all_sessions, attributions(all_ids), rounds))
    return rows


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.1f}%"


def _pts(v: float | None) -> str:
    return "-" if v is None else f"{v:.2f}"


def format_table(rows: list[dict], rounds: int = DEFAULT_ROUNDS) -> str:
    head = ("night", "sess", "matches", "decisive", "share", "aborted",
            f"power@{rounds}", "sigma", "runs", "with", "host")
    body = [
        (str(r["night"]), str(r["sessions"]), str(r["matches"]), str(r["decisive"]),
         _pct(r["decisive_share"]), str(r["aborted"]), _pts(r["min_margin"]), _pts(r["sigma"]),
         str(r["runs"]), str(r["runs_with_pressure"]), _pct(r["host_share"]))
        for r in rows
    ]
    widths = [max(len(h), *(len(b[i]) for b in body)) if body else len(h) for i, h in enumerate(head)]
    line = lambda cells: "  ".join(c.rjust(w) if i else c.ljust(w) for i, (c, w) in enumerate(zip(cells, widths)))  # noqa: E731
    return "\n".join([line(head), line(tuple("-" * w for w in widths)), *(line(b) for b in body)])


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--nights", type=int, default=DEFAULT_NIGHTS)
    ap.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                    help="round cap the resolving power is computed at (default 30)")
    ap.add_argument("--tz", default=os.environ.get("TZ") or "UTC", help="IANA zone nights are cut in")
    ap.add_argument("--db", help="database URL (default: the app's PATHBRAIN_DATABASE_URL)")
    args = ap.parse_args(argv)

    if args.db:
        os.environ["PATHBRAIN_DATABASE_URL"] = args.db
    from pathbrain.database import SessionLocal
    from pathbrain.timezones import zone_or_none

    tz = zone_or_none(args.tz) or timezone.utc
    session = SessionLocal()
    try:
        rows = night_quality(session, nights=args.nights, rounds=args.rounds, tz=tz)
    finally:
        session.rollback()
        session.close()
    print(f"Night quality, last {args.nights} nights, nights cut at {NIGHT_START_HOUR}:00 {args.tz}")
    print(format_table(rows, args.rounds))


if __name__ == "__main__":
    main()
