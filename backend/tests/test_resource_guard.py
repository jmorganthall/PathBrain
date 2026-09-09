"""The app reads its own footprint from the kernel and backs off in code."""
from __future__ import annotations

import os

from pathbrain import resource_guard as rg


def _fs(tmp_path, *, meminfo: str, loadavg: str, v2: dict | None = None, v1: dict | None = None):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text(meminfo)
    (proc / "loadavg").write_text(loadavg)
    cg2 = tmp_path / "cg2"
    cg2.mkdir()
    for name, value in (v2 or {}).items():
        (cg2 / name).write_text(value)
    cg1 = tmp_path / "cg1"
    cg1.mkdir()
    for name, value in (v1 or {}).items():
        (cg1 / name).write_text(value)
    return dict(proc_root=str(proc), cgroup_v2_root=str(cg2), cgroup_v1_root=str(cg1))


MEMINFO = "MemTotal:       32000000 kB\nMemFree:         1000000 kB\nMemAvailable:   16000000 kB\n"


def test_the_container_limit_is_the_yardstick_when_it_has_one(tmp_path):
    roots = _fs(tmp_path, meminfo=MEMINFO, loadavg="1.0 0.9 0.8 1/200 1234",
                v2={"memory.current": str(3_500 * 1048576), "memory.max": str(4_096 * 1048576)})
    r = rg.pressure(cpus=4, **roots)
    assert r["memory_basis"] == "cgroup_limit"
    assert r["memory_limit_mb"] == 4096.0 and r["memory_used_mb"] == 3500.0
    assert r["memory_pct"] == 85.4 and r["level"] == "high"
    assert r["load_per_cpu"] == 0.25 and r["host_available_mb"] == 15625.0
    assert r["reasons"] == ["memory 85% of cgroup limit"]


def test_an_unlimited_container_is_measured_against_the_host(tmp_path):
    roots = _fs(tmp_path, meminfo=MEMINFO, loadavg="0.5 0.4 0.3 1/200 1234",
                v2={"memory.current": str(3_500 * 1048576), "memory.max": "max"})
    r = rg.pressure(cpus=4, **roots)
    assert r["memory_basis"] == "host_total" and r["memory_limit_mb"] is None
    assert r["memory_pct"] == 50.0 and r["level"] == "ok" and r["reasons"] == []


def test_cgroup_v1_is_read_when_v2_is_absent_and_huge_means_unlimited(tmp_path):
    roots = _fs(tmp_path, meminfo=MEMINFO, loadavg="0.5 0.4 0.3 1/200 1234",
                v1={"memory.usage_in_bytes": str(2_000 * 1048576), "memory.limit_in_bytes": str(1 << 62)})
    r = rg.pressure(cpus=4, **roots)
    assert r["memory_used_mb"] == 2000.0 and r["memory_limit_mb"] is None
    assert r["memory_basis"] == "host_total"


def test_load_alone_can_raise_the_level_and_critical_holds_scheduled_work(tmp_path):
    roots = _fs(tmp_path, meminfo=MEMINFO, loadavg="17.0 12.0 8.0 9/400 99",
                v2={"memory.current": str(1_000 * 1048576), "memory.max": str(4_096 * 1048576)})
    r = rg.pressure(cpus=4, **roots)
    assert r["level"] == "critical" and r["reasons"] == ["load 17.0 on 4 CPU(s)"]
    calls: list[str] = []
    clock = {"t": 1000.0}
    out = rg.relieve(r, reap=lambda: calls.append("reap") or {}, recycle=lambda: calls.append("recycle"),
                     drop_caches=lambda: calls.append("drop"), now=lambda: clock["t"])
    assert out == {"level": "critical", "acted": True, "hold_scheduled": True}
    assert calls == ["reap", "recycle", "drop"]
    # Relief is rate-limited; the hold is re-evaluated every tick from the reading.
    calls.clear()
    clock["t"] += 10
    out = rg.relieve(r, reap=lambda: calls.append("reap") or {}, now=lambda: clock["t"])
    assert out["acted"] is False and out["hold_scheduled"] is True and calls == []
    clock["t"] += 60
    out = rg.relieve(r, reap=lambda: calls.append("reap") or {}, now=lambda: clock["t"])
    assert out["acted"] is True and calls == ["reap"]


def test_ok_does_nothing_and_missing_files_degrade_to_none(tmp_path):
    roots = _fs(tmp_path, meminfo=MEMINFO, loadavg="0.2 0.2 0.1 1/100 5")
    r = rg.pressure(cpus=2, **roots)
    assert r["level"] == "ok" and r["memory_used_mb"] is None and r["memory_basis"] == "host_total"
    calls: list[str] = []
    out = rg.relieve(r, reap=lambda: calls.append("reap") or {}, now=lambda: 1e9)
    assert out == {"level": "ok", "acted": False, "hold_scheduled": False} and calls == []
    empty = rg.pressure(proc_root=str(tmp_path / "nowhere"), cgroup_v2_root=str(tmp_path / "no"),
                        cgroup_v1_root=str(tmp_path / "no1"), cpus=1)
    assert empty["level"] == "ok" and empty["memory_pct"] is None and empty["load1"] is None


def test_the_live_reading_is_on_the_health_endpoint(client):
    body = client.get("/api/health/pipeline").json()
    assert "pressure" in body and body["pressure"]["level"] in ("ok", "high", "critical")
    assert body["pressure"]["cpus"] == (os.cpu_count() or 1)
