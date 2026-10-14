"""How loaded the NAS was while a run measured (`host_pressure`): read from the kernel at the
start and end of the run, kept as differences, attributed, and never able to fail a run."""
from __future__ import annotations

from pathlib import Path

from pathbrain import host_pressure as H
from pathbrain import runner, settings_profile
from pathbrain.database import session_scope
from pathbrain.models import Run, RunStatus
from pathbrain.runner import create_run, execute_run

from tests.test_integrity import _CountingPlugin


def _fake_host(root: Path, *, busy: int, total: int, io_some_us: int, cg_usec: int,
               v2: bool = True) -> dict:
    proc = root / "proc"
    (proc / "pressure").mkdir(parents=True, exist_ok=True)
    idle = total - busy
    (proc / "stat").write_text(f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\ncpu0 1 0 0 1 0 0 0 0 0 0\n")
    (proc / "pressure" / "cpu").write_text("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
    (proc / "pressure" / "io").write_text(
        f"some avg10=1.00 avg60=1.00 avg300=1.00 total={io_some_us}\n"
        f"full avg10=0.50 avg60=0.50 avg300=0.50 total={io_some_us // 2}\n"
    )
    (proc / "pressure" / "memory").write_text(
        "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )
    (proc / "meminfo").write_text("MemTotal: 32000000 kB\nMemAvailable: 8000000 kB\n"
                                  "SwapTotal: 2048000 kB\nSwapFree: 1024000 kB\n")
    (proc / "loadavg").write_text("6.50 5.10 4.00 3/900 12345\n")
    cg2 = root / "cg2"
    cg1 = root / "cg1"
    cg2.mkdir(exist_ok=True)
    cg1.mkdir(exist_ok=True)
    if v2:
        (cg2 / "cpu.stat").write_text(f"usage_usec {cg_usec}\nuser_usec 1\nsystem_usec 1\n")
    else:
        (cg1 / "cpuacct.usage").write_text(f"{cg_usec * 1000}\n")
    return {"proc_root": str(proc), "cgroup_v2_root": str(cg2), "cgroup_v1_cpuacct": str(cg1)}


def test_a_window_is_kept_as_differences_and_attributed(tmp_path, monkeypatch):
    """The NAS's own job: the host is 80% busy and stalled on IO a fifth of the time while
    PathBrain used a small share of the busy CPU — the slowdown is the NAS's, not ours."""
    roots = _fake_host(tmp_path, busy=1000, total=2000, io_some_us=0, cg_usec=0)
    clock = iter([100.0, 110.0])
    monkeypatch.setattr(H.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(H.os, "cpu_count", lambda: 4)
    start = H.snapshot(**roots)
    # Ten seconds later: 800 of 1000 ticks busy, 2 s of IO stall, 1 CPU-second for PathBrain.
    roots = _fake_host(tmp_path, busy=1800, total=3000, io_some_us=2_000_000, cg_usec=1_000_000)
    end = H.snapshot(**roots)

    s = H.summarize(start, end)
    assert s["wall_s"] == 10.0
    assert s["host_cpu_pct"] == 80.0
    assert s["host_psi_pct"]["io_some"] == 20.0 and s["host_psi_pct"]["io_full"] == 10.0
    assert s["container_cpu_cores"] == 0.1
    tck = end["clk_tck"]
    assert s["container_share_of_busy"] == round(min(1.0, 1.0 / (800 / tck)), 2)
    assert s["attribution"] == "host"
    assert s["load1"] == 6.5 and s["mem_available_mb"] == round(8000000 / 1024, 1)
    why = H.explain(s)
    assert why.startswith("The NAS was busy with something other than PathBrain")
    assert "host CPU 80% busy" in why and "io (some) 20%" in why


def test_cgroup_v1_cpu_accounting_is_read_too(tmp_path):
    roots = _fake_host(tmp_path, busy=10, total=20, io_some_us=0, cg_usec=5_000_000, v2=False)
    assert H.snapshot(**roots)["cgroup_cpu_usec"] == 5_000_000


def test_pathbrain_busy_and_a_quiet_host_are_named_apart():
    busy_ours = {"host_cpu_pct": 90.0, "host_psi_pct": {}, "container_share_of_busy": 0.85}
    quiet = {"host_cpu_pct": 12.0, "host_psi_pct": {"io_some": 0.5}, "container_share_of_busy": 0.9}
    assert H.attribute(busy_ours) == "pathbrain"
    assert H.attribute(quiet) == "quiet"
    # Busy, but who used it is unknown: no claim.
    assert H.attribute({"host_cpu_pct": 90.0}) is None


def test_missing_sources_degrade_to_nothing_never_an_invented_number(tmp_path):
    empty = {"proc_root": str(tmp_path / "nope"), "cgroup_v2_root": str(tmp_path / "x"),
             "cgroup_v1_cpuacct": str(tmp_path / "y")}
    a, b = H.snapshot(**empty), H.snapshot(**empty)
    s = H.summarize(a, b) if b["t"] > a["t"] else H.summarize(a, {**b, "t": a["t"] + 1})
    assert s["host_psi_pct"] is None and "host_cpu_pct" not in s and s["attribution"] is None
    assert H.summarize(None, b) is None
    assert H.explain(None) is None


def test_every_run_carries_its_window(monkeypatch):
    plugin = _CountingPlugin("light")
    monkeypatch.setattr(runner, "iter_plugins", lambda: [plugin])
    monkeypatch.setattr(settings_profile, "fingerprint", lambda _norm: "stable-fp")
    snaps = iter([
        {"t": 0.0, "host_cpu": [100, 200], "clk_tck": 100, "cpus": 2, "cgroup_cpu_usec": 0},
        {"t": 10.0, "host_cpu": [300, 400], "clk_tck": 100, "cpus": 2, "cgroup_cpu_usec": 1_900_000},
    ])
    monkeypatch.setattr(H, "snapshot", lambda **_: next(snaps))
    rid = create_run(label="host-pressure", iterations=1)
    execute_run(rid)
    with session_scope() as s:
        run = s.get(Run, rid)
        assert run.status == RunStatus.COMPLETE, run.error
        assert run.host_pressure["host_cpu_pct"] == 100.0
        assert run.host_pressure["attribution"] == "pathbrain"


def test_a_failed_reading_never_fails_the_run(monkeypatch):
    plugin = _CountingPlugin("light")
    monkeypatch.setattr(runner, "iter_plugins", lambda: [plugin])
    monkeypatch.setattr(settings_profile, "fingerprint", lambda _norm: "stable-fp")

    def boom(*_a, **_k):
        raise RuntimeError("no /proc here")

    monkeypatch.setattr(H, "summarize", boom)
    rid = create_run(label="host-pressure-fail", iterations=1)
    execute_run(rid)
    with session_scope() as s:
        run = s.get(Run, rid)
        assert run.status == RunStatus.COMPLETE, run.error
        assert run.host_pressure is None
