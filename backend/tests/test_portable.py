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
             rtt: list[float] | None = None, stream_ms: float = 500.0, warm: bool = False) -> dict:
    """One raw document. ``ends`` = completion time per resource id (ms from t0=1000). ``warm``
    reuses a connection for every resource (a long-lived tab); the default opens one per origin."""
    ends = ends or {"doc": 80, "font": 160, "lib": 220, "hero": 700, "data": 420}
    its = []
    for _ in range(iterations):
        seen_origins: set[str] = set(o.split("/")[2] for _r, o, _b, _a in RECIPE) if warm else set()
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
    # Same public IPv4 egress as the home WAN → home; a different one → away.
    assert portable.decide_home(None, "8.8.8.8", "8.8.8.8") == (True, "ip4")
    assert portable.decide_home(None, "1.1.1.1", "8.8.8.8") == (False, "ip4")
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


def test_decide_home_across_address_families():
    home = {"v4": "8.8.8.8", "v6": "2001:4860:4860::8888"}
    # IPv6 hosts never share an address, they share the prefix: another host in the home /64 is home.
    assert portable.decide_home(None, {"v6": "2001:4860:4860::beef"}, home) == (True, "ip6")
    # A different /64 is away.
    assert portable.decide_home(None, {"v6": "2001:4860:4860:1::1"}, home) == (False, "ip6")
    # The reported case: the device answers IPv6, the server IPv4 → nothing comparable → refuse.
    with pytest.raises(ValueError):
        portable.decide_home(None, {"v6": "2001:4860:4860::beef"}, {"v4": "8.8.8.8"})
    # ...and the fix: ask both families on both sides, so one of them can decide.
    assert portable.decide_home(None, {"v4": "8.8.8.8", "v6": "2001:4860:4860::beef"}, {"v4": "8.8.8.8"}) == (True, "ip4")
    assert portable.decide_home(None, {"v6": "2001:4860:4860::beef"}, home) == (True, "ip6")
    # Either family matching means home: a CGNAT can hand flows different public v4s while
    # the v6 prefix stays the home's.
    assert portable.decide_home(None, {"v4": "1.1.1.1", "v6": "2001:4860:4860::beef"}, home) == (True, "ip6")
    # Both known, neither matching → away (reported on the v4 comparison).
    assert portable.decide_home(None, {"v4": "1.1.1.1", "v6": "2001:4860:4860:1::1"}, home) == (False, "ip4")
    # The prefix length is a setting: at /48 the "other" /64 above is inside the home delegation.
    assert portable.decide_home(None, {"v6": "2001:4860:4860:1::1"}, home, v6_prefix=48) == (True, "ip6")


def test_split_families_files_addresses_by_what_they_are():
    fams = portable.split_families("8.8.8.8", "2001:4860:4860::8888")
    assert fams == {"v4": "8.8.8.8", "v6": "2001:4860:4860::8888"}
    # A v6 lookup that answered v4 (dual-stack service on a v4-only network) lands under v4.
    assert portable.split_families(None, "1.1.1.1") == {"v4": "1.1.1.1", "v6": None}
    # Config may state both in one string; non-public tokens are ignored.
    assert portable.split_families("8.8.8.8, 2001:4860:4860::8888 192.168.0.1") == {"v4": "8.8.8.8", "v6": "2001:4860:4860::8888"}
    assert portable.describe_addresses(fams) == "8.8.8.8 / 2001:4860:4860::8888"


def test_public_ip_classification():
    assert portable.is_public_ip("203.0.113.7") is False  # TEST-NET-3 is reserved, not routable
    assert portable.is_public_ip("8.8.8.8") is True
    assert portable.is_public_ip("2001:4860:4860::8888") is True
    for private in ("10.0.0.1", "192.168.1.1", "172.16.5.5", "127.0.0.1", "169.254.1.1", "100.64.0.1", "fe80::1", "fd00::1", "", "nope"):
        assert portable.is_public_ip(private) is False, private
    assert portable.address_family("8.8.8.8") == "v4" and portable.address_family("2001:4860:4860::8888") == "v6"
    assert portable.address_family("10.0.0.1") is None


def test_home_addresses_prefer_config_then_cached_lookup(monkeypatch):
    portable.reset_home_ip_cache()
    cfg = portable.home_addresses({"portable": {"home_ip": "8.8.4.4 2001:4860:4860::8844"}})
    assert cfg["v4"] == "8.8.4.4" and cfg["v6"] == "2001:4860:4860::8844" and cfg["source"] == "config"
    bad = portable.home_addresses({"portable": {"home_ip": "192.168.0.1"}})
    assert bad["v4"] is None and bad["v6"] is None and "no public address" in bad["errors"]["config"]
    calls = []

    def fake(url, timeout=5.0):
        calls.append(url)
        return "2001:4860:4860::8888" if "v6" in url else "8.8.8.8"

    monkeypatch.setattr(portable, "_lookup_egress", fake)
    a = portable.home_addresses({"portable": {"ip_lookup_url": "https://v4.example", "ip_lookup_url_v6": "https://v6.example"}}, now=1000.0)
    b = portable.home_addresses({"portable": {"ip_lookup_url": "https://v4.example", "ip_lookup_url_v6": "https://v6.example"}}, now=1060.0)
    assert a["v4"] == b["v4"] == "8.8.8.8" and a["v6"] == "2001:4860:4860::8888" and a["source"] == "lookup"
    assert len(calls) == 2  # one per family, then cached
    portable.home_addresses({"portable": {"ip_lookup_url": "https://v4.example", "ip_lookup_url_v6": "https://v6.example"}}, now=1000.0 + portable.HOME_IP_TTL_S + 1)
    assert len(calls) == 4
    portable.reset_home_ip_cache()
    # A v4-only home: the v6 lookup fails and is reported per family, v4 still decides.
    def v4_only(url, timeout=5.0):
        if "v6" in url:
            raise OSError("network unreachable")
        return "8.8.8.8"

    monkeypatch.setattr(portable, "_lookup_egress", v4_only)
    h = portable.home_addresses({"portable": {"ip_lookup_url": "https://v4.example", "ip_lookup_url_v6": "https://v6.example"}}, now=5000.0)
    assert h["v4"] == "8.8.8.8" and h["v6"] is None and "OSError" in h["errors"]["v6"]
    portable.reset_home_ip_cache()


def test_api_detects_home_from_addresses(client, clean, monkeypatch):
    monkeypatch.setattr(
        portable, "home_addresses",
        lambda cfg, now=None: {"v4": "8.8.8.8", "v6": "2001:4860:4860::8888", "source": "config", "checked_at": None, "errors": {}},
    )
    body = {"device_id": "auto-phone", "instrument_version": VERSION, "raw": make_raw()}
    home = client.post("/api/portable/runs", json={**body, "egress_ip": "8.8.8.8"}).json()
    assert home["is_home"] is True and home["home_detection"] == "ip4" and home["settings_fingerprint"]
    away = client.post("/api/portable/runs", json={**body, "egress_ip": "1.1.1.1", "venue": "Hotel"}).json()
    assert away["is_home"] is False and away["home_detection"] == "ip4"
    assert away["home_ip"] == "8.8.8.8 / 2001:4860:4860::8888" and away["egress_ip"] == "1.1.1.1"
    # The IPv4/IPv6 case: the device only knows its v6 egress; the home /64 decides.
    v6 = client.post("/api/portable/runs", json={**body, "egress_ip_v6": "2001:4860:4860::c0fe"}).json()
    assert v6["is_home"] is True and v6["home_detection"] == "ip6"
    forced = client.post("/api/portable/runs", json={**body, "egress_ip": "1.1.1.1", "is_home": True}).json()
    assert forced["is_home"] is True and forced["home_detection"] == "manual"
    undetectable = client.post("/api/portable/runs", json={**body, "egress_ip": "192.168.1.5"})
    assert undetectable.status_code == 409 and "choose Home or Away" in undetectable.json()["detail"]

    info = client.get("/api/portable/home", headers={"x-forwarded-for": "9.9.9.9, 10.0.0.2"}).json()
    assert info["home_ip"] == "8.8.8.8" and info["home_ip_v6"] == "2001:4860:4860::8888"
    assert info["request_ip"] == "9.9.9.9" and info["request_ip_public"] is True
    assert info["lookup_url"] and info["lookup_url_v6"] and info["v6_prefix"] == 64 and info["detected"] is None
    # The page asks the server for the verdict, so the preview and the upload use one rule.
    v = client.get("/api/portable/home", params={"egress_ip": "1.1.1.1", "egress_ip_v6": "2001:4860:4860::1"}).json()
    assert v["detected"] is True and v["detected_by"] == "ip6" and "IPv6" in v["reason"]
    v = client.get("/api/portable/home", params={"egress_ip": "1.1.1.1"}).json()
    assert v["detected"] is False and v["detected_by"] == "ip4"
    v = client.get("/api/portable/home", params={"egress_ip": "10.0.0.5"}).json()
    assert v["detected"] is None and "choose Home or Away" in v["reason"]


# ── PathBrain's own reading: the server reference ────────────────────────────

from pathbrain.interpret.derive import derive as _derive
from pathbrain.models import BenchmarkResult, Run, RunStatus
from pathbrain.plugins import get_plugin
from pathbrain.plugins.base import PluginResult


def test_portable_plugin_registered_and_derives_one_iteration():
    p = get_plugin("portable")
    assert p is not None and p.name == "portable"
    it = make_raw()["iterations"][0]
    # The plugin's raw now carries the phone's whole sequence (a list); the older single
    # ``iteration`` shape still derives, so history filed before the change keeps working.
    m = _derive("portable", {"iterations": [it, it], "instrument_version": VERSION, "client": {}})
    assert m["first_complete_ms"] == 80.0 and m["rtt_ms"] == 20.5
    legacy = _derive("portable", {"iteration": it, "instrument_version": VERSION, "client": {}})
    assert legacy["first_complete_ms"] == 80.0
    assert _derive("portable", {"nope": 1}) == {}


def test_portable_plugin_fails_fast_without_its_server(monkeypatch):
    """No server at self_url → a failed measurement in milliseconds, and no Chromium launched."""
    p = get_plugin("portable")
    launched = []
    browser = get_plugin("browser")
    monkeypatch.setattr(browser, "borrow_browser", lambda cfg=None: launched.append(1))
    r = p.run({"self_url": "http://127.0.0.1:9", "page_timeout_s": 1})
    assert r.success is False and r.error and launched == []
    assert p.run({"enabled": False}).success is False


def test_portable_plugin_runs_one_iteration_through_the_page(monkeypatch):
    from pathbrain.plugins import benchmark_portable as bp

    it = make_raw()["iterations"][0]
    calls: list[str] = []

    class FakePage:
        def goto(self, url, **kw):
            calls.append(f"goto {url}")

        def wait_for_function(self, expr, **kw):
            calls.append("wait")

        def evaluate(self, expr, arg=None):
            if "runOne" in expr:
                assert arg and arg["resources"], "the recipe body is handed to the page"
                # The page's `run` — the phone's full sequence — is preferred; `runOne` is the
                # fallback for an older bundle. The plugin asks for either in one expression.
                assert "__pathbrainPortable.run " in expr or "__pathbrainPortable.run|" in expr.replace(" ", "")
                return {"iterations": [it, it]}
            return {"user_agent": "HeadlessChrome"}

    class FakeContext:
        closed = False

        def new_page(self):
            return FakePage()

        def close(self):
            FakeContext.closed = True

    class FakeBrowser:
        def new_context(self):
            return FakeContext()

    monkeypatch.setattr(bp, "fetch_recipe", lambda url, timeout=5.0: {**portable.recipe({}), "instrument_version": VERSION})
    monkeypatch.setattr(get_plugin("browser"), "borrow_browser", lambda cfg=None: FakeBrowser())
    r = get_plugin("portable").run({"self_url": "http://pathbrain.test"})
    assert r.success, r.error
    assert r.raw["instrument_version"] == VERSION and r.raw["iterations"] == [it, it]
    assert r.raw["client"]["device"] == portable.SERVER_DEVICE_ID
    assert r.details["resources"] == 5 and r.details["rtt_samples"] == 8
    assert r.details["iterations"] == 2 and r.details["warm_up"] == "run"
    assert calls[0] == "goto http://pathbrain.test/away?embedded=1" and FakeContext.closed


def _server_run(*, ends=None, fail=frozenset(), when: datetime, fp="fp-crown", iterations=2) -> int:
    """A completed benchmark run carrying `portable` plugin results, filed the way the
    runner does it (result row + `record_server_run`)."""
    raw = make_raw(ends, fail=fail, iterations=iterations)
    with session_scope() as s:
        run = Run(status=RunStatus.COMPLETE, iterations=iterations, iterations_completed=iterations,
                  settings_fingerprint=fp, settings=[{"label": "wan", "quantum": 1514}], created_at=when)
        s.add(run)
        s.flush()
        results = [
            PluginResult("portable", success=True, raw={"iteration": it, "instrument_version": VERSION, "client": {"device": "pathbrain-server"}})
            for it in raw["iterations"]
        ]
        s.add(BenchmarkResult(run_id=run.id, plugin="portable", success=True, metrics={}, raw={"iterations": [r.raw for r in results]}))
        row = portable.record_server_run(s, run, results)
        assert row is not None and row.source_run_id == run.id and row.raw is None
        return run.id


def test_server_reference_is_filed_from_the_run_and_kept_apart_from_the_phone(clean):
    now = datetime(2026, 9, 8, 21, 0, tzinfo=timezone.utc)
    for i in range(5):
        _server_run(when=now - timedelta(hours=i + 1))
    away = _store("phone", home=False, ends={"doc": 300, "font": 500, "lib": 600, "hero": 2200, "data": 1200}, when=now)
    c = _compare(away)
    # No phone home runs → the headline falls back to PathBrain's own readings, labelled.
    assert c["headline"] == "server" and c["available"] is True
    assert c["references"]["device"]["available"] is False
    srv = c["references"]["server"]
    assert srv["provenance"]["reference"] == "server" and srv["provenance"]["device_id"] == portable.SERVER_DEVICE_ID
    assert "wired" in srv["provenance"]["note"]
    assert srv["metrics"]["first_complete_ms"]["verdict"] == "worse" and srv["metrics"]["first_complete_ms"]["n"] == 5
    assert srv["provenance"]["profile"]["fingerprint"] == "fp-crown"
    # The server samples are their own device: they never enter the phone's home pool.
    with session_scope() as s:
        run = s.get(PortableRun, away)
        assert all(r.device_id == portable.SERVER_DEVICE_ID for r in portable.server_candidates(s, run))
        assert portable.home_candidates(s, run) == []
    # Once the phone has its own home runs, they take the headline; the server stays beside.
    for _ in range(3):
        _store("phone", home=True, when=now - timedelta(days=1))
    c2 = _compare(away)
    assert c2["headline"] == "device" and c2["references"]["server"]["available"] is True
    # A server run compares only to other server runs.
    with session_scope() as s:
        srow = s.scalars(__import__("sqlalchemy").select(PortableRun).where(PortableRun.device_id == portable.SERVER_DEVICE_ID)).first()
        cs = portable.compare(s, srow, {"portable": {"min_home_runs": 3}})
        assert cs["references"]["server"] is None and cs["references"]["device"]["available"] is True


def test_record_server_run_refuses_mixed_versions_and_empty(clean):
    with session_scope() as s:
        run = Run(status=RunStatus.COMPLETE, iterations=2, iterations_completed=2, settings_fingerprint="fp")
        s.add(run)
        s.flush()
        it = make_raw()["iterations"][0]
        mixed = [
            PluginResult("portable", success=True, raw={"iteration": it, "instrument_version": VERSION}),
            PluginResult("portable", success=True, raw={"iteration": it, "instrument_version": "other000000"}),
        ]
        assert portable.record_server_run(s, run, mixed) is None
        assert portable.record_server_run(s, run, [PluginResult("portable", success=False, error="x")]) is None
        assert portable.record_server_run(s, run, []) is None


def test_runner_skips_a_plugin_disabled_in_config(client, clean):
    """`portable.enabled: false` leaves the plugin out of a run exactly like `skip` — no
    result row, nothing fabricated."""
    from pathbrain.runner import create_run, execute_run

    run_id = create_run(iterations=1, config_overrides={"portable": {"enabled": False}})
    execute_run(run_id)
    with session_scope() as s:
        plugins = set(s.scalars(__import__("sqlalchemy").select(BenchmarkResult.plugin).where(BenchmarkResult.run_id == run_id)).all())
        assert "portable" not in plugins and "browser" in plugins


# ── Venue recall: a place you have tested before is recognised, not re-asked ────────────
#
# Being somewhere you have tested before is the common case — the same hotel, the same
# office, the same café. Asking for the label from scratch every time is busywork, and it is
# how one place ends up recorded under three spellings, which silently splits its history.
# The page used to pre-fill whatever this device typed *last*, wherever that was, which is
# wrong in the one way that matters: it has nothing to do with where you are now.


def _away(device: str, venue: str | None, egress: str | None, when: datetime | None = None) -> int:
    run = PortableRun(
        device_id=device, is_home=False, instrument_version=VERSION, tz_offset_minutes=0,
        venue=venue, egress_ip=egress, raw=make_raw(),
    )
    if when is not None:
        run.created_at = when
    with session_scope() as s:
        s.add(run)
        s.flush()
        return run.id


def test_a_previously_labelled_address_suggests_its_venue(clean):
    _away("phone", "Hotel Denver", "93.184.216.34")
    with session_scope() as s:
        out = portable.recall_venue(s, {"v4": "93.184.216.34", "v6": None})
    assert out is not None
    assert out["venue"] == "Hotel Denver"
    assert out["matched_on"] == "ip4"


def test_a_different_address_suggests_nothing(clean):
    """The recall must be about *this* network. Suggesting the last label typed anywhere is
    the behaviour being replaced."""
    _away("phone", "Hotel Denver", "93.184.216.34")
    with session_scope() as s:
        assert portable.recall_venue(s, {"v4": "104.16.132.229", "v6": None}) is None


def test_ipv6_is_recalled_by_prefix_not_by_address(clean):
    """Hosts on one network share a prefix and never an address, so an exact v6 comparison
    would recognise a place exactly never — the same rule `decide_home` follows."""
    _away("phone", "Airport lounge", "2606:2800:220:1::5")
    with session_scope() as s:
        out = portable.recall_venue(s, {"v4": None, "v6": "2606:2800:220:1::99"}, v6_prefix=64)
        assert out is not None and out["venue"] == "Airport lounge"
        assert out["matched_on"] == "ip6"
        # A different /64 is a different network.
        assert portable.recall_venue(s, {"v4": None, "v6": "2606:2800:220:2::5"}, v6_prefix=64) is None


def test_a_laptop_inherits_the_name_a_phone_gave_the_place(clean):
    """Deliberately not device-scoped: the point is that the network has a name."""
    _away("phone", "Hotel Denver", "93.184.216.34")
    with session_scope() as s:
        out = portable.recall_venue(s, {"v4": "93.184.216.34", "v6": None}, device_id="laptop")
    assert out["venue"] == "Hotel Denver"
    assert out["from_this_device"] is False


def test_this_devices_own_label_wins_over_someone_elses(clean):
    """When two devices spell a place differently, suggest the reader their own spelling."""
    _away("phone", "Denver hotel wifi", "93.184.216.34", when=datetime(2026, 1, 1, tzinfo=timezone.utc))
    _away("laptop", "Hotel Denver", "93.184.216.34", when=datetime(2026, 1, 2, tzinfo=timezone.utc))
    with session_scope() as s:
        out = portable.recall_venue(s, {"v4": "93.184.216.34", "v6": None}, device_id="phone")
    assert out["venue"] == "Denver hotel wifi"
    assert out["from_this_device"] is True


def test_home_runs_are_never_a_venue_source(clean):
    """A home run carries no venue by construction, and home is not a place you label."""
    with session_scope() as s:
        s.add(PortableRun(
            device_id="phone", is_home=True, instrument_version=VERSION,
            venue="should not be suggested", egress_ip="93.184.216.34", raw=make_raw(),
        ))
    with session_scope() as s:
        assert portable.recall_venue(s, {"v4": "93.184.216.34", "v6": None}) is None


def test_the_home_endpoint_carries_the_suggestion(client, clean):
    """One round trip: the page already asks this endpoint where it is, so it learns what
    this network is called in the same answer."""
    _away("phone", "Hotel Denver", "93.184.216.34")
    body = client.get(
        "/api/portable/home?egress_ip=93.184.216.34&device_id=phone"
    ).json()
    assert body["venue"] is not None
    assert body["venue"]["venue"] == "Hotel Denver"
    assert body["venue"]["from_this_device"] is True

    # An address nobody has labelled leaves the field empty rather than guessing.
    other = client.get("/api/portable/home?egress_ip=104.16.132.229").json()
    assert other["venue"] is None


# ── warmth: the same instrument at the same warmth, or say so ────────────────


def test_warmth_reads_reused_share_and_transport_off_the_entries():
    from pathbrain.interpret.portable import warmth

    cold = warmth(make_raw())
    origins = len({o.split("/")[2] for _r, o, _b, _a in RECIPE})
    # One new connection per origin, the rest reused; every entry carries Timing-Allow-Origin.
    assert cold["tao_resources"] == len(RECIPE) and cold["reused"] == len(RECIPE) - origins
    assert cold["reused_share"] == round((len(RECIPE) - origins) / len(RECIPE), 3) and cold["protocol"] == "h2"
    warm = warmth(make_raw(warm=True))
    assert warm["reused_share"] == 1.0
    # Entries without TAO are unreadable and never counted either way.
    raw = make_raw()
    for r in raw["iterations"][0]["waterfall"]["resources"]:
        r["entry"] = _entry(r["t_start"], r["t_end"], new_conn=True, tao=False)
    assert warmth(raw)["tao_resources"] == 0 and warmth(raw)["reused_share"] is None
    # Stored with the coverage, so it rides on the row without a migration.
    assert derive_portable(make_raw())["coverage"]["warmth"]["protocol"] == "h2"


def test_client_family_tells_engines_apart():
    fam = portable.client_family
    assert fam({"device": portable.SERVER_DEVICE_ID, "user_agent": "Mozilla/5.0 Chrome/128"}) == {"family": "PathBrain Chromium", "engine": "Blink"}
    assert fam({"user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit/605 Version/17.0 Mobile/15E148 Safari/604.1"}) == {"family": "Safari", "engine": "WebKit"}
    assert fam({"user_agent": "Mozilla/5.0 (iPhone) AppleWebKit/605 CriOS/128 Mobile Safari/604.1"}) == {"family": "Chrome", "engine": "WebKit"}
    assert fam({"user_agent": "Mozilla/5.0 (Linux; Android 14) Chrome/128 Mobile Safari/537.36"}) == {"family": "Chrome", "engine": "Blink"}
    assert fam({"user_agent": "Mozilla/5.0 HeadlessChrome/120"})["family"] == "Chromium (headless)"
    assert fam({})["engine"] == "unknown"


def test_setup_bound_rows_are_not_compared_across_a_warmth_gap(clean):
    """A phone's warm tab against a cold reference reads as the phone "beating" the wire on
    every setup-bound row by exactly the handshake cost. The rows stay; the verdict does not."""
    when = datetime(2026, 3, 3, 10, 0, tzinfo=timezone.utc)
    for i in range(3):
        _store("phone", home=True, when=when - timedelta(days=i))  # cold: one new connection per origin
    warm_run = _store("phone", home=True, when=when, **{})
    with session_scope() as s:
        run = s.get(PortableRun, warm_run)
        run.raw = make_raw(warm=True)
        d = derive_portable(run.raw)
        run.metrics, run.per_origin, run.coverage = d["metrics"], d["per_origin"], d["coverage"]
    out = _compare(warm_run)["references"]["device"]
    assert out["available"]
    w = out["provenance"]["warmth"]
    assert w["away"]["reused_share"] == 1.0 and w["home"]["reused_share"] < 0.7 and w["comparable"] is False
    assert "paying handshakes" in w["note"]
    for key in portable.SETUP_BOUND:
        if key in out["metrics"]:
            assert out["metrics"][key]["setup_bound"] is True and out["metrics"][key]["comparable"] is False
            assert out["metrics"][key]["verdict"] == "incomparable"
    assert out["metrics"]["rtt_ms"].get("setup_bound") is None and out["metrics"]["rtt_ms"]["verdict"] in {"within", "better", "worse"}
    assert out["score"]["comparable"] is False
    # Same warmth on both sides: everything compares, and the note says so by being absent.
    cold_run = _store("phone", home=True, when=when + timedelta(hours=1))
    out = _compare(cold_run)["references"]["device"]
    assert out["provenance"]["warmth"]["comparable"] is True and out["provenance"]["warmth"]["note"] is None
    assert out["metrics"]["first_complete_ms"]["comparable"] is True and out["score"]["comparable"] is True


def test_stream_time_per_mb_is_shown_but_no_longer_scored():
    assert "stream_ms_per_mb" not in portable.PORTABLE_RUBRIC
    cat = {m["key"]: m for m in portable.metric_catalog()}
    assert cat["stream_ms_per_mb"]["scored"] is False and cat["first_complete_ms"]["setup_bound"] is True
    assert cat["rtt_ms"]["setup_bound"] is False


# ── the per-profile phone standing ───────────────────────────────────────────


def test_profile_standings_rank_each_devices_profiles_and_check_the_crown(clean, monkeypatch):
    from pathbrain import crown_follower

    when = datetime(2026, 3, 3, 10, 0, tzinfo=timezone.utc)
    fast = {"doc": 60, "font": 120, "lib": 170, "hero": 500, "data": 300}
    slow = {"doc": 160, "font": 300, "lib": 420, "hero": 1400, "data": 900}
    for i in range(4):
        _store("phone", home=True, when=when - timedelta(hours=i), fp="fp-a", ends=fast)
        _store("phone", home=True, when=when - timedelta(hours=i), fp="fp-b", ends=slow)
    _store("phone", home=True, when=when, fp="fp-c", ends=fast)  # thin
    for i in range(3):
        _store(portable.SERVER_DEVICE_ID, home=True, when=when - timedelta(hours=i), fp="fp-b", ends=fast)
    cfg = {"portable": {"min_home_runs": 3}}

    monkeypatch.setattr(crown_follower, "current_crown", lambda session: None)
    with session_scope() as s:
        out = portable.profile_standings(s, cfg)
    assert out["min_home_runs"] == 3 and out["crown"] is None
    phone = next(d for d in out["devices"] if d["device_id"] == "phone")
    assert out["devices"][0]["device_id"] == "phone" and out["devices"][-1]["is_server"] is True
    ranks = {p["fingerprint"]: p["rank"] for p in phone["profiles"]}
    assert ranks["fp-a"] == 1 and ranks["fp-b"] == 2 and ranks["fp-c"] is None  # thin: shown, unranked
    a = next(p for p in phone["profiles"] if p["fingerprint"] == "fp-a")
    assert a["runs"] == 4 and a["confident"] and a["score"] > next(p for p in phone["profiles"] if p["fingerprint"] == "fp-b")["score"]
    assert a["name"]  # call signs resolve
    assert phone["best"]["fingerprint"] == "fp-a" and "no pooled crown" in phone["agreement"]["verdict"]

    # The crown is the device's #2: disagreement, with the crown's rank on the device named.
    monkeypatch.setattr(crown_follower, "current_crown", lambda session: {"fingerprint": "fp-b", "label": "B"})
    with session_scope() as s:
        out = portable.profile_standings(s, cfg, device_id="phone")
    phone = out["devices"][0]
    assert out["crown"]["fingerprint"] == "fp-b" and phone["agreement"]["agree"] is False
    assert phone["agreement"]["crown_rank_on_device"] == 2 and "disagrees" in phone["agreement"]["verdict"]
    assert next(p for p in phone["profiles"] if p["is_crown"])["fingerprint"] == "fp-b"

    # The crown is the device's #1: agreement.
    monkeypatch.setattr(crown_follower, "current_crown", lambda session: {"fingerprint": "fp-a", "label": "A"})
    with session_scope() as s:
        out = portable.profile_standings(s, cfg, device_id="phone")
    assert out["devices"][0]["agreement"]["agree"] is True and "agrees" in out["devices"][0]["agreement"]["verdict"]

    # A crown the device has never measured: said outright, never guessed.
    monkeypatch.setattr(crown_follower, "current_crown", lambda session: {"fingerprint": "fp-zzz", "label": "Z"})
    with session_scope() as s:
        out = portable.profile_standings(s, cfg, device_id="phone")
    assert "has not measured the crown" in out["devices"][0]["agreement"]["verdict"]


def test_standings_endpoint(client, clean):
    body = client.get("/api/portable/standings").json()
    assert body["devices"] == [] and body["min_home_runs"] == 5 and "instrument_version" in body


# ── the location map ─────────────────────────────────────────────────────────


def _place(device: str, venue: str | None, *, ends=None, rtt=None, when=None) -> int:
    run_id = _store(device, home=False, ends=ends, rtt=rtt, when=when)
    with session_scope() as s:
        s.get(PortableRun, run_id).venue = venue
    return run_id


def test_location_map_pools_venues_across_devices_and_keeps_home_apart(clean):
    slow = {"doc": 300, "font": 500, "lib": 600, "hero": 2200, "data": 1200}
    for _ in range(5):
        _store("phone", home=True, fp="fp-live")
    _store("phone", home=True, fp="fp-other")           # another profile: not the home dot
    _store(portable.SERVER_DEVICE_ID, home=True, fp="fp-live")
    _place("phone", "Hotel Wifi", ends=slow, rtt=[80] * 8)
    _place("laptop", " hotel wifi ", ends=slow, rtt=[90] * 8)   # same place, another spelling
    _place("phone", "Office")
    _place("phone", None)
    with session_scope() as s:
        m = portable.location_map(s, {"portable": {"min_home_runs": 3}}, home_fingerprint="fp-live")
    by_key = {loc["key"]: loc for loc in m["locations"]}
    assert m["home_profile"]["fingerprint"] == "fp-live" and m["home_profile"]["source"] == "live"
    home = by_key["home"]
    assert home["runs"] == 5 and home["confident"] and home["kind"] == "home"
    assert m["excluded"]["home_other_profiles"] == 1
    server = by_key["home-server"]
    assert server["kind"] == "home_server" and server["runs"] == 1
    hotel = by_key["venue:hotel wifi"]
    assert hotel["runs"] == 2 and hotel["label"] == "Hotel Wifi" and not hotel["confident"]
    assert {d["device_id"] for d in hotel["devices"]} == {"phone", "laptop"}
    assert hotel["metrics"]["rtt_ms"] > home["metrics"]["rtt_ms"]
    assert by_key["venue:office"]["runs"] == 1
    assert by_key[f"venue:{portable.UNNAMED_VENUE.casefold()}"]["label"] == portable.UNNAMED_VENUE
    # Home first, then the places, the unnamed bucket last whatever its score.
    assert [loc["kind"] for loc in m["locations"]][:2] == ["home", "home_server"]
    assert m["locations"][-1]["label"] == portable.UNNAMED_VENUE
    assert m["metrics"][0]["key"] == "score" and any(x["key"] == "rtt_ms" for x in m["metrics"])


def test_location_map_gates_on_the_instrument_version_and_says_so(clean):
    _store("phone", home=True, fp="fp-live")
    run_id = _place("phone", "Cafe")
    with session_scope() as s:
        s.get(PortableRun, run_id).instrument_version = "older"
    with session_scope() as s:
        m = portable.location_map(s, {}, home_fingerprint="fp-live")
    assert [loc["key"] for loc in m["locations"]] == ["home"]
    assert m["excluded"]["older_version"] == 1


def test_location_map_without_a_known_profile_uses_every_home_run(clean):
    _store("phone", home=True, fp="fp-a")
    _store("phone", home=True, fp="fp-b")
    with session_scope() as s:
        m = portable.location_map(s, {}, home_fingerprint=None)
    assert m["home_profile"]["source"] == "any"
    assert m["locations"][0]["runs"] == 2


def test_api_location_map(client, clean, monkeypatch):
    monkeypatch.setattr(portable, "home_stamp", lambda: ("fp-live", "wan: q1514"))
    for _ in range(2):
        _store("phone", home=True, fp="fp-live")
    _place("phone", "Hotel")
    body = client.get("/api/portable/locations").json()
    assert body["home_profile"]["fingerprint"] == "fp-live"
    assert body["home_profile"]["summary"] == "wan: q1514" and body["home_profile"]["source"] == "live"
    assert [loc["kind"] for loc in body["locations"]] == ["home", "away"]


def test_the_map_is_wired_to_the_methodology_crown(clean):
    from pathbrain.methodology import CURRENT_METHODOLOGY

    for _ in range(3):
        _store("phone", home=True, fp="fp-live")
    _place("phone", "Hotel", ends={"doc": 300, "font": 500, "lib": 600, "hero": 2200, "data": 1200})
    with session_scope() as s:
        crown = portable.crown_stand_ins(s, {})
        m = portable.location_map(s, {}, home_fingerprint="fp-live")
    assert crown["methodology"] == CURRENT_METHODOLOGY and crown["legs"]
    # Every current crown leg has a stand-in on this instrument, and it is a scored reading,
    # so the crown stand-in score exists and reads off the same rubric arithmetic.
    for leg in crown["legs"]:
        assert leg["portable_metric"] == portable.CROWN_STAND_INS[leg["crown_metric"]]
        assert leg["scored"], leg
    assert crown["complete"] and m["crown"] == crown
    home, hotel = m["locations"][0], m["locations"][1]
    assert home["crown_score"] is not None and hotel["crown_score"] is not None
    assert hotel["crown_score"] < home["crown_score"]
    assert any(x["key"] == "crown_score" for x in m["metrics"])


def test_crown_stand_in_score_is_the_weighted_mean_of_the_legs_subscores():
    legs = [
        {"portable_metric": "first_complete_ms", "weight": 1.0, "scored": True},
        {"portable_metric": "largest_complete_ms", "weight": 1.0, "scored": True},
        {"portable_metric": "stall_energy_ms", "weight": 0.5, "scored": True},
    ]
    metrics = {"first_complete_ms": 100.0, "largest_complete_ms": 500.0, "stall_energy_ms": 200.0, "rtt_ms": 20.0}
    _, subs = portable.score_metrics(metrics)
    want = (subs["first_complete_ms"] + subs["largest_complete_ms"] + 0.5 * subs["stall_energy_ms"]) / 2.5
    assert portable.crown_stand_in_score(metrics, legs) == round(want, 1)
    # A leg without a stand-in reading, or an unscored leg, means no score — never a default.
    assert portable.crown_stand_in_score({"first_complete_ms": 100.0}, legs) is None
    assert portable.crown_stand_in_score(metrics, legs + [{"portable_metric": "throughput_mbps", "weight": 1, "scored": False}]) is None
