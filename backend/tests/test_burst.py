"""Burst fairness: the round-robin mechanism, measured on the portable waterfall
(`interpret.portable._burst_metrics`, `why.burst_block`, `portable.backfill_burst`).

Pinned: a small object downloaded beside a large one at the large flow's pace reads an
interleave of 1 and a bulk share of ½; one that waited behind the bulk reads far below 1
with the bulk share toward 1; the metrics are read only off TAO'd download windows; they
ride `derive_portable` additively (the instrument version does not move); a row derived
before they existed is backfilled from raw on the standings read; and the "why it wins"
reading carries them per device with a noise bar, in the verdict when clear.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete

from pathbrain import why
from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.interpret.portable import BURST_METRICS, PORTABLE_METRICS, _burst_metrics, derive_portable
from pathbrain.models import PortableRun
from pathbrain.portable import backfill_burst, instrument_version, profile_standings, recipe


def _res(rid: str, nbytes: int, rs: float, re_: float, *, tao: bool = True) -> dict:
    entry = {"startTime": rs - 40, "fetchStart": rs - 40, "domainLookupStart": 0, "domainLookupEnd": 0,
             "connectStart": rs - 30, "secureConnectionStart": 0, "connectEnd": rs - 30,
             "requestStart": rs - 20 if tao else 0, "responseStart": rs if tao else 0, "responseEnd": re_,
             "transferSize": 0, "encodedBodySize": 0, "nextHopProtocol": "h2"}
    return {"id": rid, "url": f"https://a.example/{rid}", "bytes": nbytes, "ok": True, "error": None,
            "t_start": rs - 40, "t_end": re_, "entry": entry}


def _waterfall(*resources) -> dict:
    return {"resources": list(resources)}


def test_a_fair_interleave_reads_one_and_a_starved_small_object_reads_far_below():
    hero = _res("hero", 400_000, 100.0, 500.0)  # 1000 B/ms over its whole window
    # Fair: the font moves at the large flow's pace beside it (15 KB in 15 ms).
    fair = _burst_metrics(_waterfall(hero, _res("font", 15_000, 200.0, 215.0)), None)
    assert fair["interleave_index"] == 1.0
    assert fair["bulk_share"] == 0.5
    assert fair["small_under_large_ms"] == 15.0
    # Starved: the same font took 150 ms behind the bulk — a tenth of the pace, and the
    # bulk took nine tenths of the bytes while both were in flight.
    starved = _burst_metrics(_waterfall(hero, _res("font", 15_000, 200.0, 350.0)), None)
    assert starved["interleave_index"] == 0.1
    assert starved["bulk_share"] == 0.909
    assert starved["small_under_large_ms"] == 150.0


def test_only_tao_windows_and_real_overlaps_are_read():
    hero = _res("hero", 400_000, 100.0, 500.0)
    # No Timing-Allow-Origin: no download window, nothing to read.
    assert _burst_metrics(_waterfall(hero, _res("font", 15_000, 200.0, 215.0, tao=False)), None) == {}
    # A small object fetched AFTER the large one finished is not "under" it.
    after = _burst_metrics(_waterfall(hero, _res("font", 15_000, 600.0, 615.0)), None)
    assert "interleave_index" not in after and "bulk_share" not in after
    # Two mid-sized objects are neither small nor large: no pair, but the bulk share still reads.
    mids = _burst_metrics(_waterfall(_res("a", 60_000, 100.0, 200.0), _res("b", 60_000, 100.0, 200.0)), None)
    assert "interleave_index" not in mids and mids["bulk_share"] == 0.5
    # A lone download has no burst at all.
    assert _burst_metrics(_waterfall(hero), None) == {}
    # The subset rule applies here too: exclude the large flow and the pair is gone.
    assert "interleave_index" not in _burst_metrics(_waterfall(hero, _res("font", 15_000, 200.0, 215.0)), {"font"})


def test_the_metrics_ride_the_derivation_additively():
    raw = {"iterations": [
        {"waterfall": _waterfall(_res("hero", 400_000, 100.0, 500.0), _res("font", 15_000, 200.0, 230.0)),
         "stream": {"url": "s", "ok": False, "partial": False, "start": 0, "end": None, "bytes": 0, "chunks": []},
         "rtt": {"url": "r", "samples_ms": [20, 21]}},
        {"waterfall": _waterfall(_res("hero", 400_000, 100.0, 500.0), _res("font", 15_000, 200.0, 250.0)),
         "stream": {"url": "s", "ok": False, "partial": False, "start": 0, "end": None, "bytes": 0, "chunks": []},
         "rtt": {"url": "r", "samples_ms": [20, 21]}},
    ]}
    out = derive_portable(raw)
    assert out["metrics"]["interleave_index"] == 0.4  # median of 0.5 and 0.3
    assert out["metrics"]["small_under_large_ms"] == 40.0
    assert out["coverage"]["burst"] is True
    for key in BURST_METRICS:
        assert key in PORTABLE_METRICS
    # The catalog says which way is better: interleave up, the other two down.
    assert PORTABLE_METRICS["interleave_index"][2] is False
    assert PORTABLE_METRICS["bulk_share"][2] is True and PORTABLE_METRICS["small_under_large_ms"][2] is True
    # Additive: the instrument version — which admits old home runs as references — did not move.
    with session_scope() as s:
        body = recipe(get_config(s))
    assert instrument_version({k: body[k] for k in ("resources", "stream", "rtt")}) == body["instrument_version"]


def _raw_for(interleave: float) -> dict:
    """A one-iteration raw whose font moves at ``interleave`` × the hero's pace."""
    dur = 15.0 / interleave
    return {"iterations": [{
        "waterfall": _waterfall(_res("hero", 400_000, 100.0, 500.0), _res("font", 15_000, 200.0, 200.0 + dur)),
        "stream": {"url": "s", "ok": False, "partial": False, "start": 0, "end": None, "bytes": 0, "chunks": []},
        "rtt": {"url": "r", "samples_ms": [20, 21, 22]},
    }]}


def _seed_portable(device: str, fp: str, interleave: float, version: str, *, flagged: bool) -> int:
    raw = _raw_for(interleave)
    derived = derive_portable(raw)
    metrics = dict(derived["metrics"])
    cov = dict(derived["coverage"])
    if not flagged:  # a row written before the burst metrics existed
        for k in BURST_METRICS:
            metrics.pop(k, None)
        cov.pop("burst", None)
    with session_scope() as s:
        r = PortableRun(device_id=device, device_label=device, is_home=True, instrument_version=version,
                        settings_fingerprint=fp, raw=raw, metrics=metrics, coverage=cov, per_origin={},
                        score=50.0, subscores={}, created_at=datetime.now(timezone.utc))
        s.add(r)
        s.flush()
        return r.id


def test_the_standings_backfill_rows_derived_before_the_metrics_existed():
    with session_scope() as s:
        version = recipe(get_config(s))["instrument_version"]
    ids = [_seed_portable("burstphone", "burstfpA0001", 0.9, version, flagged=False) for _ in range(2)]
    ids.append(_seed_portable("burstphone", "burstfpA0001", 0.9, version, flagged=True))
    try:
        with session_scope() as s:
            rows = s.scalars(__import__("sqlalchemy").select(PortableRun).where(PortableRun.id.in_(ids))).all()
            assert sum(1 for r in rows if "interleave_index" in (r.metrics or {})) == 1
            assert backfill_burst(s, rows) == 2
            # The read that triggered it sees the numbers at once…
            assert all("interleave_index" in (r.metrics or {}) for r in rows)
        # …and they were persisted, so the next read has nothing to do.
        with session_scope() as s:
            rows = s.scalars(__import__("sqlalchemy").select(PortableRun).where(PortableRun.id.in_(ids))).all()
            assert all((r.coverage or {}).get("burst") for r in rows)
            assert backfill_burst(s, rows) == 0
            out = profile_standings(s, get_config(s), device_id="burstphone")
        prof = out["devices"][0]["profiles"][0]
        assert prof["metrics"]["interleave_index"] == 0.9
        assert out["burst_backfilled"] == 0
    finally:
        with session_scope() as s:
            s.execute(delete(PortableRun).where(PortableRun.id.in_(ids)))


def test_why_reads_the_mechanism_per_device_and_names_it_when_clear():
    with session_scope() as s:
        cfg = get_config(s)
        version = recipe(cfg)["instrument_version"]
    ids = []
    for i in range(4):
        ids.append(_seed_portable("burstphone", "burstfpA0002", 0.9 + 0.01 * (i % 2), version, flagged=True))
        ids.append(_seed_portable("burstphone", "burstfpB0002", 0.4 + 0.01 * (i % 2), version, flagged=True))
    ids.append(_seed_portable("burstlaptop", "burstfpA0002", 0.8, version, flagged=True))  # one run only: no row
    try:
        with session_scope() as s:
            block = why.burst_block(s, cfg, "burstfpA0002", "burstfpB0002", 2.0)
        assert block["min_runs"] == why.BURST_MIN_RUNS
        assert [d["device_id"] for d in block["devices"]] == ["burstphone"]
        dev = block["devices"][0]
        assert dev["runs_a"] == 4 and dev["runs_b"] == 4 and dev["is_server"] is False
        rows = {m["key"]: m for m in dev["metrics"]}
        assert rows["interleave_index"]["a"] == 0.905 and rows["interleave_index"]["b"] == 0.405
        assert rows["interleave_index"]["delta"] == 0.5 and rows["interleave_index"]["clear"] is True
        assert rows["interleave_index"]["higher_is_better"] is True
        assert rows["bulk_share"]["higher_is_better"] is False and rows["bulk_share"]["delta"] < 0
        # The verdict names it, from the winner's side, whichever side asked.
        gap = {"points": 2.0, "se": 0.2, "clear": True}
        text = why.verdict(("Alpha", "Beta"), gap, [], [], [], block)
        assert "Under a burst on burstphone, small objects ran at 0.91× the large flow's pace under Alpha against 0.41× under Beta — Alpha interleaves better." in text
        flipped = why.verdict(("Alpha", "Beta"), {"points": -2.0, "se": 0.2, "clear": True}, [], [], [], block)
        assert "under Beta against 0.91× under Alpha — Beta interleaves worse." in flipped
        # Not clear → not claimed.
        quiet = dict(block, devices=[dict(dev, metrics=[dict(rows["interleave_index"], clear=False)])])
        assert "Under a burst" not in why.verdict(("Alpha", "Beta"), gap, [], [], [], quiet)
    finally:
        with session_scope() as s:
            s.execute(delete(PortableRun).where(PortableRun.id.in_(ids)))
