"""The portable (away) test: derivation, the instrument version, and the "vs home" rule
that only directly comparable data is a reference."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pathbrain import portable
from pathbrain.database import session_scope
from pathbrain.interpret.portable import coverage, derive_portable
from pathbrain.models import PortableRun

# ── raw builders ─────────────────────────────────────────────────────────────

RECIPE = [
    ("doc", "https://a.example/doc.js", 10_000, None),
    ("font", "https://b.example/font.woff2", 15_000, "doc"),
    ("lib", "https://a.example/lib.js", 90_000, "doc"),
    ("hero", "https://a.example/hero.js", 400_000, "doc"),
    ("data", "https://a.example/data.js", 250_000, "lib"),
]


def _entry(start: float, end: float, *, new_conn: bool, tao: bool = True) -> dict:
    """A Resource Timing entry. ``new_conn`` opens a connection (DNS 4ms, TCP 10ms, TLS 12ms);
    a reused one reports connectStart == connectEnd. ``tao=False`` zeroes the phases the way
    a cross-origin entry without Timing-Allow-Origin does."""
    if not tao:
        return {"startTime": start, "responseEnd": end, "fetchStart": start,
                "domainLookupStart": 0, "domainLookupEnd": 0, "connectStart": 0,
                "secureConnectionStart": 0, "connectEnd": 0, "requestStart": 0, "responseStart": 0}
    if new_conn:
        dls, dle = start + 1, start + 5
        cs, sec, ce = dle, dle + 10, dle + 22
    else:
        dls = dle = cs = ce = start + 1
        sec = 0
    rq = ce + 1
    rs = rq + 30
    return {"startTime": start, "fetchStart": start, "domainLookupStart": dls, "domainLookupEnd": dle,
            "connectStart": cs, "secureConnectionStart": sec, "connectEnd": ce, "requestStart": rq,
            "responseStart": rs, "responseEnd": end, "transferSize": 0, "encodedBodySize": 0,
            "nextHopProtocol": "h2"}


def make_raw(ends: dict[str, float] | None = None, *, fail: set[str] = frozenset(), iterations: int = 1,
             rtt: list[float] | None = None, stream_ms: float = 500.0) -> dict:
    """One raw document. ``ends`` = completion time per resource id (ms from t0=1000)."""
    ends = ends or {"doc": 80, "font": 160, "lib": 220, "hero": 700, "data": 420}
    its = []
    for _ in range(iterations):
        seen_origins: set[str] = set()
        resources = []
        for rid, url, nbytes, after in RECIPE:
            origin = url.split("/")[2]
            start = 1000.0 + (0 if after is None else ends[after])
            if rid in fail:
                resources.append({"id": rid, "url": url, "bytes": nbytes, "ok": False,
                                  "error": "TypeError: Failed to fetch", "t_start": start, "t_end": None, "entry": None})
                continue
            new_conn = origin not in seen_origins
            seen_origins.add(origin)
            resources.append({"id": rid, "url": url, "bytes": nbytes, "ok": True, "error": None,
                              "t_start": start, "t_end": 1000.0 + ends[rid],
                              "entry": _entry(start, 1000.0 + ends[rid], new_conn=new_conn)})
        its.append({
            "waterfall": {"resources": resources},
            "stream": {"url": "https://a.example/big.js", "ok": True, "partial": False, "start": 0.0,
                       "end": stream_ms, "bytes": 1_000_000,
                       "chunks": [{"t": t, "bytes": 50_000} for t in range(25, int(stream_ms) + 1, 25)]},
            "rtt": {"url": "https://a.example/doc.js", "samples_ms": rtt or [20, 22, 19, 21, 20, 23, 20, 21]},
        })
    return {"iterations": its}


# ── derivation ───────────────────────────────────────────────────────────────


def test_waterfall_milestones_and_shape_metrics():
    d = derive_portable(make_raw())
    m = d["metrics"]
    assert m["first_complete_ms"] == 80.0          # doc
    assert m["largest_complete_ms"] == 700.0       # hero (400 KB)
    assert m["last_complete_ms"] == 700.0
    # completion series 80,160,220,420,700 → gaps 80,60,200,280 → longest 280
    assert m["longest_stall_ms"] == 280.0
    assert m["stall_energy_ms"] == pytest.approx((80**2 + 60**2 + 200**2 + 280**2) ** 0.5, rel=1e-3)
    assert "cadence_cov" in m and "delivery_gini" in m and "byte_earliness_ms" in m
    assert m["rtt_ms"] == 20.5 and m["jitter_ms"] > 0
    assert m["throughput_mbps"] == pytest.approx(16.0)   # 8 Mbit in 0.5 s
    assert m["stream_ms_per_mb"] == pytest.approx(500.0)
    assert m["stream_longest_stall_ms"] == 25.0


def test_origin_phases_come_from_new_connections_only_and_need_tao():
    d = derive_portable(make_raw())
    a, b = d["per_origin"]["a.example"], d["per_origin"]["b.example"]
    assert a["dns_ms"] == 4.0 and a["tcp_ms"] == 10.0 and a["tls_ms"] == 12.0 and a["ttfb_ms"] == 30.0
    assert b["dns_ms"] == 4.0  # the font opened b.example's first connection
    # No Timing-Allow-Origin → the phases are zeroed on the wire and must NOT read as 0 ms.
    raw = make_raw()
    for r in raw["iterations"][0]["waterfall"]["resources"]:
        r["entry"] = _entry(r["t_start"], r["t_end"], new_conn=True, tao=False)
    assert derive_portable(raw)["per_origin"] == {}
    # ...but the waterfall milestones still derive off responseEnd, which is always exposed.
    assert derive_portable(raw)["metrics"]["last_complete_ms"] == 700.0


def test_failed_resource_is_coverage_not_a_zero():
    raw = make_raw(fail={"hero"})
    cov = coverage(raw)
    assert "hero" in cov["resources_failed"] and "hero" not in cov["resources_ok"]
    m = derive_portable(raw)["metrics"]
    assert m["last_complete_ms"] == 420.0            # data, the hero never landed
    assert m["largest_complete_ms"] == 420.0         # the largest that DID load
    # Restricting to a subset re-derives over it, exactly like the comparison does.
    sub = derive_portable(make_raw(), include_ids={"doc", "font", "lib"})["metrics"]
    assert sub["last_complete_ms"] == 220.0


def test_metrics_are_medians_over_iterations():
    raw = make_raw(iterations=3)
    raw["iterations"][1]["rtt"]["samples_ms"] = [90] * 8
    raw["iterations"][2]["rtt"]["samples_ms"] = [40] * 8
    assert derive_portable(raw)["metrics"]["rtt_ms"] == 40.0


# ── instrument version ───────────────────────────────────────────────────────


def test_instrument_version_tracks_the_recipe():
    base = portable.recipe({})
    assert base["instrument_version"] == portable.recipe({"portable": {"iterations": 5}})["instrument_version"], (
        "iteration count is not part of what was measured"
    )
    changed = portable.recipe({"portable": {"resources": portable.DEFAULT_RESOURCES[:-1]}})
    assert changed["instrument_version"] != base["instrument_version"]
    assert len(base["instrument_version"]) == 12


def test_score_is_a_number_on_its_own_rubric():
    score, subs = portable.score_metrics(derive_portable(make_raw())["metrics"])
    assert 0 <= score <= 100
    assert set(subs) <= set(portable.PORTABLE_RUBRIC)
    assert portable.score_metrics({}) == (None, {})


# ── the "vs home" rule ───────────────────────────────────────────────────────

VERSION = portable.recipe({})["instrument_version"]


def _store(device: str, *, home: bool, ends=None, fail=frozenset(), when: datetime | None = None,
           fp: str | None = "fp-crown", tz: int = 0, rtt=None) -> int:
    run = PortableRun(
        device_id=device, is_home=home, instrument_version=VERSION, tz_offset_minutes=tz,
        settings_fingerprint=fp if home else None, settings_summary="wan: q1514" if home and fp else None,
        raw=make_raw(ends, fail=fail, rtt=rtt),
    )
    d = derive_portable(run.raw)
    run.metrics, run.per_origin, run.coverage = d["metrics"], d["per_origin"], d["coverage"]
    run.score, run.subscores = portable.score_metrics(d["metrics"])
    if when is not None:
        run.created_at = when
    with session_scope() as s:
        s.add(run)
        s.flush()
        return run.id


@pytest.fixture()
def clean():
    with session_scope() as s:
        for r in s.scalars(__import__("sqlalchemy").select(PortableRun)).all():
            s.delete(r)
    yield


def _compare(run_id: int, **kw) -> dict:
    with session_scope() as s:
        run = s.get(PortableRun, run_id)
        return portable.compare(s, run, {"portable": {"min_home_runs": 3}}, **kw)


def test_no_reference_below_the_minimum_and_never_from_another_device(clean):
    for _ in range(3):
        _store("laptop", home=True)
    _store("phone", home=True)
    away = _store("phone", home=False)
    c = _compare(away)
    assert c["available"] is False and "1 comparable home run" in c["reason"]
    assert c["provenance"]["home_runs_on_device"] == 1  # the laptop's three never count


def test_away_run_is_read_against_home_iqr_on_the_same_device(clean):
    for _ in range(4):
        _store("phone", home=True)
    away = _store("phone", home=False, ends={"doc": 300, "font": 500, "lib": 600, "hero": 2200, "data": 1200}, rtt=[80] * 8)
    c = _compare(away)
    assert c["available"] is True
    first = c["metrics"]["first_complete_ms"]
    assert first["away"] == 300.0 and first["home_median"] == 80.0 and first["delta"] == 220.0
    assert first["verdict"] == "worse" and first["n"] == 4
    assert c["metrics"]["rtt_ms"]["verdict"] == "worse"
    assert c["score"]["away"] < c["score"]["home_median"]
    prov = c["provenance"]
    assert prov["profile"]["fingerprint"] == "fp-crown" and prov["home_runs_used"] == 4
    assert prov["time_rung"] in {"same_weekday_hour", "same_hour", "any_time"}


def test_coverage_is_pairwise_a_blocked_origin_is_dropped_from_both_sides(clean):
    for _ in range(4):
        _store("phone", home=True)
    # Same speed as home on everything that loaded; the hero was blocked at the hotel.
    away = _store("phone", home=False, fail={"hero"})
    c = _compare(away)
    assert c["available"] is True
    assert "hero" not in c["provenance"]["common_resources"]
    assert "hero" in c["provenance"]["dropped_resources"]
    # Home re-derived WITHOUT the hero: its waterfall now ends at `data` (420) like the away run.
    last = c["metrics"]["last_complete_ms"]
    assert last["away"] == 420.0 and last["home_median"] == 420.0 and last["verdict"] == "within"


def test_reference_prefers_the_crown_profile_then_the_best_populated(clean):
    for _ in range(3):
        _store("phone", home=True, fp="fp-old")
    for _ in range(3):
        _store("phone", home=True, fp="fp-crown", ends={"doc": 50, "font": 100, "lib": 150, "hero": 400, "data": 300})
    away = _store("phone", home=False)
    c = _compare(away, crown_fingerprint="fp-crown")
    assert c["provenance"]["profile"]["fingerprint"] == "fp-crown"
    assert c["metrics"]["last_complete_ms"]["home_median"] == 400.0
    # No crown known → the most-populated profile; ties keep the first seen.
    c2 = _compare(away, crown_fingerprint=None)
    assert c2["provenance"]["profile"]["fingerprint"] in {"fp-old", "fp-crown"}
    # Crown known but thin on this device → falls back rather than using 1 run.
    _store("phone", home=True, fp="fp-new")
    c3 = _compare(away, crown_fingerprint="fp-new")
    assert c3["provenance"]["profile"]["fingerprint"] != "fp-new"


def test_time_rung_walks_down_until_enough_runs(clean):
    now = datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc)  # a Tuesday, 21:00
    for i in range(3):
        _store("phone", home=True, when=now - timedelta(weeks=i + 1))   # same weekday & hour
    for i in range(3):
        _store("phone", home=True, when=now - timedelta(days=i + 1, hours=5))  # other hours
    away = _store("phone", home=False, when=now)
    assert _compare(away)["provenance"]["time_rung"] == "same_weekday_hour"
    away2 = _store("phone", home=False, when=now + timedelta(hours=3))  # Wed 00:00: no same-hour runs
    assert _compare(away2)["provenance"]["time_rung"] == "any_time"


# ── API ──────────────────────────────────────────────────────────────────────


def test_api_recipe_upload_and_compare(client, clean):
    rec = client.get("/api/portable/recipe").json()
    assert rec["instrument_version"] == VERSION and len(rec["resources"]) >= 5
    assert any(m["key"] == "first_complete_ms" for m in rec["metrics"])

    body = {"device_id": "tablet", "device_label": "iPad", "venue": None, "is_home": True,
            "instrument_version": VERSION, "tz_offset_minutes": -300, "client": {"ua": "x"}, "raw": make_raw()}
    for _ in range(6):
        r = client.post("/api/portable/runs", json=body)
        assert r.status_code == 201, r.text
    first = r.json()
    assert first["is_home"] is True and first["score"] is not None
    # Home runs are stamped with the live (mock) firewall profile.
    assert first["settings_fingerprint"]
    # A home run compares against the OTHER home runs — "is home where it was?" — once the
    # configured minimum (5) of *other* home runs exists.
    assert first["compare"]["available"] is True
    assert first["compare"]["provenance"]["home_runs_used"] == 5

    away = client.post("/api/portable/runs", json={**body, "is_home": False, "venue": "Hotel Wi-Fi"}).json()
    assert away["settings_fingerprint"] is None and away["venue"] == "Hotel Wi-Fi"
    assert away["compare"]["available"] is True and away["compare"]["provenance"]["home_runs_used"] == 6

    stale = client.post("/api/portable/runs", json={**body, "instrument_version": "deadbeef0000"})
    assert stale.status_code == 409

    runs = client.get("/api/portable/runs", params={"device_id": "tablet"}).json()
    assert len(runs) == 7 and runs[0]["id"] == away["id"]
    devs = client.get("/api/portable/devices").json()
    assert devs[0]["device_id"] == "tablet" and devs[0]["home_runs"] == 6 and devs[0]["away_runs"] == 1
    assert devs[0]["label"] == "iPad"
    assert client.put("/api/portable/devices/tablet", json={"label": "Josh's iPad"}).json()["label"] == "Josh's iPad"
    assert client.get(f"/api/portable/runs/{away['id']}").json()["device_label"] == "Josh's iPad"
    assert client.delete(f"/api/portable/runs/{away['id']}").status_code == 204
    assert client.get(f"/api/portable/runs/{away['id']}").status_code == 404


def test_portable_runs_never_touch_the_pooled_ledger(client, clean):
    """The whole point of the separate table: uploading portable runs adds nothing to
    ``runs``/``scores``, so the crown, the duel and the trends baseline can't see them."""
    before = client.get("/api/history/count").json()
    body = {"device_id": "p", "is_home": True, "instrument_version": VERSION, "raw": make_raw()}
    assert client.post("/api/portable/runs", json=body).status_code == 201
    assert client.get("/api/history/count").json() == before


# ── home is detected, not declared ───────────────────────────────────────────


def test_decide_home_by_egress_address():
    # Same public egress as the home WAN → home; a different one → away.
    assert portable.decide_home(None, "8.8.8.8", "8.8.8.8") == (True, "ip")
    assert portable.decide_home(None, "1.1.1.1", "8.8.8.8") == (False, "ip")
    # An explicit answer overrides detection either way.
    assert portable.decide_home(True, "1.1.1.1", "8.8.8.8") == (True, "manual")
    assert portable.decide_home(False, "8.8.8.8", "8.8.8.8") == (False, "manual")
    # Unknown on either side and nothing stated → refuse rather than guess the stamp.
    with pytest.raises(ValueError):
        portable.decide_home(None, None, "8.8.8.8")
    with pytest.raises(ValueError):
        portable.decide_home(None, "8.8.8.8", None)
    # A LAN / tunnel address is not an egress: it can't be compared with the WAN.
    with pytest.raises(ValueError):
        portable.decide_home(None, "192.168.1.20", "8.8.8.8")
    with pytest.raises(ValueError):
        portable.decide_home(None, "100.101.102.103", "8.8.8.8")  # Tailscale/CGNAT


def test_public_ip_classification():
    assert portable.is_public_ip("203.0.113.7") is False  # TEST-NET-3 is reserved, not routable
    assert portable.is_public_ip("8.8.8.8") is True
    assert portable.is_public_ip("2001:4860:4860::8888") is True
    for private in ("10.0.0.1", "192.168.1.1", "172.16.5.5", "127.0.0.1", "169.254.1.1", "100.64.0.1", "fe80::1", "", "nope"):
        assert portable.is_public_ip(private) is False, private


def test_home_ip_prefers_config_then_cached_lookup(monkeypatch):
    portable.reset_home_ip_cache()
    assert portable.home_ip({"portable": {"home_ip": "8.8.4.4"}})["ip"] == "8.8.4.4"
    bad = portable.home_ip({"portable": {"home_ip": "192.168.0.1"}})
    assert bad["ip"] is None and "not a public" in bad["error"]
    calls = []
    monkeypatch.setattr(portable, "_lookup_egress", lambda url, timeout=5.0: calls.append(url) or "8.8.8.8")
    a = portable.home_ip({}, now=1000.0)
    b = portable.home_ip({}, now=1000.0 + 60)
    assert a["ip"] == b["ip"] == "8.8.8.8" and a["source"] == "lookup" and len(calls) == 1
    portable.home_ip({}, now=1000.0 + portable.HOME_IP_TTL_S + 1)
    assert len(calls) == 2
    portable.reset_home_ip_cache()


def test_api_detects_home_from_addresses(client, clean, monkeypatch):
    monkeypatch.setattr(portable, "home_ip", lambda cfg, now=None: {"ip": "8.8.8.8", "source": "config", "checked_at": None, "error": None})
    body = {"device_id": "auto-phone", "instrument_version": VERSION, "raw": make_raw()}
    home = client.post("/api/portable/runs", json={**body, "egress_ip": "8.8.8.8"}).json()
    assert home["is_home"] is True and home["home_detection"] == "ip" and home["settings_fingerprint"]
    away = client.post("/api/portable/runs", json={**body, "egress_ip": "1.1.1.1", "venue": "Hotel"}).json()
    assert away["is_home"] is False and away["home_detection"] == "ip" and away["home_ip"] == "8.8.8.8"
    forced = client.post("/api/portable/runs", json={**body, "egress_ip": "1.1.1.1", "is_home": True}).json()
    assert forced["is_home"] is True and forced["home_detection"] == "manual"
    undetectable = client.post("/api/portable/runs", json={**body, "egress_ip": "192.168.1.5"})
    assert undetectable.status_code == 409 and "choose Home or Away" in undetectable.json()["detail"]
    info = client.get("/api/portable/home", headers={"x-forwarded-for": "9.9.9.9, 10.0.0.2"}).json()
    assert info["home_ip"] == "8.8.8.8" and info["request_ip"] == "9.9.9.9" and info["request_ip_public"] is True
    assert info["lookup_url"]
