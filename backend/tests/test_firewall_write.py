"""The one firewall write path, and the OPNsense write it ends in.

Two kinds of guarantee are pinned here:

* **Shape** — every engine writes through ``firewall_write`` (an AST scan of the
  package) and a switch is one ``apply_many`` call, however many fields it carries.
* **Wire** — what the OPNsense provider actually sends: a pipe speed goes out as the two
  fields OPNsense keeps it in (``bandwidth`` + ``bandwidthMetric``), and a ``setPipe``
  the firewall answers with a validation failure raises instead of being recorded ``ok``.
"""
from __future__ import annotations

import ast
import json
import pathlib

import httpx
import pytest

from pathbrain import firewall_write
from pathbrain.providers.base import FirewallRejectedWrite
from pathbrain.providers.opnsense import OPNsenseProvider, split_bandwidth

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "pathbrain"
WRITE_CALLS = {"apply", "apply_many", "set_pipe_enabled", "reconfigure"}
#: The write path itself: the module every engine calls, the guarded wrapper under it, and
#: the providers that talk to the firewall.
ALLOWED = {"firewall_write.py", "session_runtime.py"}


def test_no_module_writes_the_firewall_except_through_firewall_write():
    offenders = []
    for f in sorted(PACKAGE.rglob("*.py")):
        rel = f.relative_to(PACKAGE).as_posix()
        if rel in ALLOWED or rel.startswith("providers/"):
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in WRITE_CALLS):
                offenders.append(f"{rel}:{node.lineno} {ast.unparse(node)[:70]}")
    assert not offenders, (
        "Firewall writes must go through pathbrain.firewall_write, not straight to a provider:\n"
        + "\n".join(offenders)
    )


class _Recorder:
    name = "recorder"

    def __init__(self):
        self.calls: list[tuple] = []

    def apply_many(self, changes, *, reload=True):
        self.calls.append(("apply_many", [dict(c) for c in changes], reload))
        return {"ok": True}

    def set_pipe_enabled(self, uuid, enabled):
        if uuid == "bad":
            raise RuntimeError("no such pipe")
        self.calls.append(("set_pipe_enabled", uuid, enabled))

    def reconfigure(self):
        self.calls.append(("reconfigure",))
        return {"ok": True}


def test_a_switch_is_one_call_and_empty_values_are_not_written():
    p = _Recorder()
    out = firewall_write.apply_changes(p, [
        {"pipe_uuid": "a", "param": "quantum", "value": 1500, "from": 3000},
        {"pipe_uuid": "b", "param": "target", "value": 5},
        {"pipe_uuid": "b", "param": "interval", "value": None},
    ])
    assert p.calls == [("apply_many", [
        {"pipe_uuid": "a", "param": "quantum", "value": 1500},
        {"pipe_uuid": "b", "param": "target", "value": 5},
    ], True)]
    assert [c["param"] for c in out] == ["quantum", "target"]


def test_nothing_to_write_and_dry_run_never_call_the_firewall():
    p = _Recorder()
    assert firewall_write.apply_changes(p, []) == []
    assert firewall_write.apply_changes(p, [{"param": "quantum", "value": None}]) == []
    assert firewall_write.apply_changes(p, [{"param": "quantum", "value": 1}], dry_run=True)
    assert p.calls == []


def test_restoring_pipes_is_best_effort_and_disabling_is_not():
    p = _Recorder()
    assert firewall_write.set_pipes_enabled(p, ["a", "bad", "b"], True, best_effort=True) == ["a", "b"]
    with pytest.raises(RuntimeError):
        firewall_write.set_pipes_enabled(p, ["c", "bad", "d"], False)
    assert ("set_pipe_enabled", "d", False) not in p.calls


def test_a_sweep_restore_covers_every_sweepable_field_in_one_switch():
    from pathbrain import sweep
    from pathbrain.shaper_fields import SWEEPABLE_FIELDS

    baseline = {"pipes": {"u1": {k: 7 for k in SWEEPABLE_FIELDS}, "u2": {k: 9 for k in SWEEPABLE_FIELDS}}}
    changes = sweep.baseline_restore_changes(baseline, None)
    assert {(c["pipe_uuid"], c["param"]) for c in changes} == {
        (u, k) for u in ("u1", "u2") for k in SWEEPABLE_FIELDS
    }


# ── bandwidth on the wire ────────────────────────────────────────────────────

METRIC = {"bit": {"selected": 0}, "Kbit": {"selected": 0}, "Mbit": {"selected": 1}, "Gbit": {"selected": 0}}


@pytest.mark.parametrize("value,expected", [
    ("880Mbit", ("880", "Mbit")),
    ("0.88Gbit", ("880", "Mbit")),
    ("1Gbit", ("1", "Gbit")),
    ("880", ("880", "Mbit")),        # a bare number keeps the pipe's own unit
    (880, ("880", "Mbit")),
    ("40 Mbit/s", ("40", "Mbit")),
    ("880mbps", ("880", "Mbit")),
    ("1.5Kbit", ("1500", "bit")),
])
def test_split_bandwidth(value, expected):
    assert split_bandwidth(value, {"bandwidthMetric": METRIC}) == expected


@pytest.mark.parametrize("value", ["fast", "880Furlongs", "0Mbit", ""])
def test_an_unreadable_bandwidth_fails_before_anything_is_written(value):
    with pytest.raises(ValueError):
        split_bandwidth(value, {"bandwidthMetric": METRIC})


def _pipe(**over) -> dict:
    return {"enabled": "1", "bandwidth": "880", "bandwidthMetric": dict(METRIC),
            "fqcodel_quantum": "3000", "codel_target": "5", "description": "d", **over}


class _FakeOPNsense:
    """Enough of OPNsense's traffic-shaper API to see what PathBrain sends it."""

    def __init__(self, pipes: dict, reject: set[str] | None = None, raw_reply: str | None = None):
        self.pipes = pipes
        self.reject = reject or set()
        self.raw_reply = raw_reply
        self.posts: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/settings/get"):
            return httpx.Response(200, json={"ts": {"pipes": {"pipe": self.pipes}}})
        body = json.loads(request.content or b"{}")
        self.posts.append((path, body))
        if "/setPipe/" in path:
            uuid = path.rsplit("/", 1)[-1]
            if self.raw_reply is not None:
                return httpx.Response(200, text=self.raw_reply)
            if uuid in self.reject and body["pipe"].get("bandwidth") != "880":
                return httpx.Response(200, json={"result": "failed",
                                                 "validations": {"pipe.bandwidth": "Value is not a number"}})
            return httpx.Response(200, json={"result": "saved"})
        return httpx.Response(200, json={"status": "ok"})


def _provider(fake: _FakeOPNsense, monkeypatch) -> OPNsenseProvider:
    p = OPNsenseProvider(base_url="https://fw.test", api_key="k", api_secret="s")
    monkeypatch.setattr(p, "_client", lambda: httpx.Client(
        base_url="https://fw.test", transport=httpx.MockTransport(fake.handler)))
    return p


def test_a_pipe_speed_is_written_as_number_and_unit(monkeypatch):
    fake = _FakeOPNsense({"u1": _pipe()})
    p = _provider(fake, monkeypatch)
    p.apply_many([{"pipe_uuid": "u1", "param": "download_bandwidth", "value": "0.8Gbit"}])
    (path, body), reload = fake.posts[0], fake.posts[-1]
    assert path.endswith("/setPipe/u1")
    assert body["pipe"]["bandwidth"] == "800"
    assert body["pipe"]["bandwidthMetric"] == "Mbit"
    assert reload[0].endswith("/service/reconfigure")


def test_a_rejected_setpipe_raises_and_reloads_nothing(monkeypatch):
    fake = _FakeOPNsense({"u1": _pipe(), "u2": _pipe()}, reject={"u2"})
    p = _provider(fake, monkeypatch)
    with pytest.raises(FirewallRejectedWrite) as info:
        p.apply_many([
            {"pipe_uuid": "u1", "param": "download_bandwidth", "value": "700Mbit"},
            {"pipe_uuid": "u2", "param": "download_bandwidth", "value": "700Mbit"},
        ])
    assert info.value.pipe_uuid == "u2"
    paths = [path for path, _ in fake.posts]
    assert not any(path.endswith("/reconfigure") for path in paths)
    # The pipe this call had already saved is put back, so no half-switch waits in the
    # stored config for the next reload.
    assert paths == ["/api/trafficshaper/settings/setPipe/u1",
                     "/api/trafficshaper/settings/setPipe/u2",
                     "/api/trafficshaper/settings/setPipe/u1"]
    assert fake.posts[-1][1]["pipe"]["bandwidth"] == "880"


def test_a_rejected_write_is_ledgered_failed_not_ok(monkeypatch):
    from pathbrain import firewall_guard
    from pathbrain.session_runtime import ResilientProvider

    fake = _FakeOPNsense({"u2": _pipe()}, reject={"u2"})
    outcomes: list[str] = []
    monkeypatch.setattr(firewall_guard, "record",
                        lambda op, **kw: outcomes.append(kw["outcome"]) or None)
    p = ResilientProvider(_provider(fake, monkeypatch))
    with pytest.raises(FirewallRejectedWrite):
        firewall_write.apply_changes(p, [{"pipe_uuid": "u2", "param": "download_bandwidth", "value": "700Mbit"}])
    assert outcomes == ["failed"]
    # Rejected means the request was wrong: attempted once, never retried.
    assert sum(1 for path, _ in fake.posts if "/setPipe/" in path) == 1


def test_a_reply_that_is_not_json_still_counts_as_saved(monkeypatch):
    fake = _FakeOPNsense({"u1": _pipe()}, raw_reply="OK")
    p = _provider(fake, monkeypatch)
    p.apply_many([{"pipe_uuid": "u1", "param": "quantum", "value": 1500}])
    assert fake.posts[-1][0].endswith("/reconfigure")


# ── bandwidth is only ever lowered ───────────────────────────────────────────


def test_a_sweep_never_raises_a_pipe_above_its_live_rate():
    from pathbrain import sweep

    spec = {"download_bandwidth": {"enabled": True, "min": 700, "max": 1000, "step": 100},
            "pipes": [{"uuid": "down", "label": "Download"}, {"uuid": "up", "label": "Upload"}]}
    variants = sweep.generate_variants(spec)
    assert {v["download_bandwidth"] for v in variants} == {"700Mbit", "800Mbit", "900Mbit", "1000Mbit"}
    baseline = {"pipes": {"down": {"download_bandwidth": "880Mbit"}, "up": {"download_bandwidth": "0.04Gbit"}}}
    kept, dropped = sweep.drop_above_live(variants, baseline, None)
    assert {(v["pipe_uuid"], v["download_bandwidth"]) for v in kept} == {("down", "700Mbit"), ("down", "800Mbit")}
    assert len(dropped) == 6


def test_explore_never_steps_bandwidth_past_the_fastest_rate_run():
    from pathbrain import explore

    axis = {"field": "download_bandwidth", "field_label": "Download bandwidth", "min": 700.0, "max": 880.0}
    value, why = explore._beyond(axis, at_max=True)
    assert value is None and "line rate" in why
    lower, _ = explore._beyond(axis, at_max=False)
    assert lower is not None and lower < 700.0


def test_the_sweep_page_offers_bandwidth_in_mbit_and_says_it_only_goes_down():
    from fastapi.testclient import TestClient
    from pathbrain.main import app

    fields = {f["key"]: f for f in TestClient(app).get("/api/sweep/fields").json()["fields"]}
    bw = fields["download_bandwidth"]
    assert bw["unit"] == "Mbit" and bw["never_above_live"] is True
    assert bw["default"]["enabled"] is False


class _StatefulOPNsense:
    """An OPNsense that validates like the real one: ``bandwidth`` must be a whole number,
    and a pipe whose payload fails validation is not saved AT ALL — every field in it,
    including the ones that were fine, stays as it was."""

    def __init__(self, pipes: dict):
        self.pipes = pipes

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            return httpx.Response(200, json={"ts": {"pipes": {"pipe": self.pipes}}})
        if "/setPipe/" in path:
            uuid = path.rsplit("/", 1)[-1]
            sent = json.loads(request.content)["pipe"]
            if not str(sent.get("bandwidth", "")).isdigit():
                return httpx.Response(200, json={"result": "failed",
                                                 "validations": {"pipe.bandwidth": "Please specify a value"}})
            pipe = self.pipes[uuid]
            for k, v in sent.items():
                if isinstance(pipe.get(k), dict):  # a select: move the selected flag
                    pipe[k] = {key: {**opt, "selected": int(key == v)} for key, opt in pipe[k].items()}
                else:
                    pipe[k] = v
            return httpx.Response(200, json={"result": "saved"})
        return httpx.Response(200, json={"status": "ok"})


def test_the_reported_upload_switch_now_takes(monkeypatch):
    """The reported failure: "firewall did not accept 2 field(s): Upload·download_bandwidth
    (wanted 880Mbit, is 900Mbit), Upload·target (wanted 3, is 4)". The bandwidth went out as
    the string "880Mbit", the firewall rejected the whole pipe, and the target in the same
    payload was lost with it. Split into number + unit, both fields land."""
    from pathbrain.settings_profile import normalize, plan_apply

    fake = _StatefulOPNsense({"up": _pipe(bandwidth="900", codel_target="4", description="Upload")})
    p = _provider(fake, monkeypatch)
    target = normalize(p.discover())
    target[0]["download_bandwidth"] = "880Mbit"
    target[0]["target"] = "3"
    changes, _ = plan_apply(target, p.discover())
    assert {c["param"] for c in changes} == {"download_bandwidth", "target"}

    firewall_write.apply_changes(p, changes)

    live = p.discover()[0]
    assert live.download_bandwidth == "880Mbit"
    assert str(live.target) == "3"
    remaining, _ = plan_apply(target, p.discover())
    assert remaining == []
