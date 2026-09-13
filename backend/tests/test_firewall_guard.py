"""The firewall write path: every write ledgered, one kind refused, and a write that times
out never reissued.

There used to be twice as many tests here, and their absence is the subject. The guard
also paced writes, budgeted them by the hour, cooled down after an outage, held a new build
read-only until somebody armed it, and refused whole sessions at the door. That was a rate
limit built on the theory that how *often* PathBrain wrote was the hazard — and the ledger,
the one part of it that was an instrument rather than a valve, disproved it: the cost was
one field (``flows``, now non-writable), never the rate. So the valve is gone and the tests
for it went with it, replaced by two that pin the rollback: nothing throttles, and no
session is turned away.
"""
from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from pathbrain import firewall_guard as fg
from pathbrain import session_runtime
from pathbrain.database import session_scope
from pathbrain.models import FirewallWrite
from pathbrain.providers import get_provider
from pathbrain.providers.mock import _OVERRIDES, _PIPE_ENABLED
from pathbrain.session_runtime import FirewallUnavailable, ResilientProvider

from .faults import FaultyProvider


@pytest.fixture()
def guard(monkeypatch):
    """A clean ledger. There is no longer any guard *state* to reset — which is the point."""
    monkeypatch.setattr(fg, "_build_sha", lambda: "")
    monkeypatch.setattr(session_runtime, "FIREWALL_BACKOFF_S", (0.0, 0.0))   # reads retry, without the wait

    def reset():
        with session_scope() as s:
            for r in s.scalars(select(FirewallWrite)).all():
                s.delete(r)

    # The mock provider's state is shared by the whole suite: save it and put it back, so a
    # value written here never changes what a later test discovers.
    saved = dict(_OVERRIDES)
    saved_enabled = dict(_PIPE_ENABLED)
    reset()
    _OVERRIDES.clear()
    yield
    reset()
    _OVERRIDES.clear()
    _OVERRIDES.update(saved)
    _PIPE_ENABLED.clear()
    _PIPE_ENABLED.update(saved_enabled)


def _ledger() -> list[dict]:
    return list(reversed(fg.recent_writes(1000)))


def _provider() -> tuple[ResilientProvider, FaultyProvider]:
    inner = FaultyProvider()
    return ResilientProvider(inner), inner


CH = {"pipe_uuid": None, "param": "quantum", "value": 1514}


# ── 1. the ledger — the instrument that found the real cause ───────────────────


def test_every_write_lands_on_the_ledger_with_its_cost(guard):
    p, inner = _provider()
    p.apply(CH)
    p.apply_many([CH, {"pipe_uuid": None, "param": "limit", "value": 1000}])
    p.set_pipe_enabled(None, False)
    rows = _ledger()
    assert [r["op"] for r in rows] == ["apply", "apply_many", "set_pipe_enabled"]
    assert all(r["outcome"] == "ok" for r in rows)
    assert [r["reconfigures"] for r in rows] == [1, 1, 1]      # a switch costs ONE reload
    assert rows[1]["field"] == "limit,quantum" and rows[0]["value"] == "1514"
    assert all(r["latency_ms"] is not None for r in rows)
    assert inner.reconfigures == 3


def test_a_wrong_request_is_recorded_as_failed(guard):
    p, inner = _provider()
    inner.fail_next("400")
    with pytest.raises(httpx.HTTPStatusError):
        p.apply(CH)
    assert _ledger()[-1]["outcome"] == "failed"


def test_the_rate_is_reported_and_never_enforced(guard):
    """``summary`` counts reconfigures per hour. That number is a *reading*.

    It was a cap, and the cap is what this rollback removes: a burst of writes now simply
    happens, is counted, and is visible. Fifty in a row here against the old default of
    sixty an hour — the old guard would have tripped hands-off part way through and left
    every engine unable to write, including to restore.
    """
    p, inner = _provider()
    for _ in range(50):
        p.apply(CH)
    assert inner.reconfigures == 50, "every write went through"
    assert [r["outcome"] for r in _ledger()] == ["ok"] * 50
    assert fg.summary()["reconfigures_last_hour"] == 50
    assert "hands_off" not in fg.summary(), "there is no state left to report"


def test_nothing_paces_a_write(guard, monkeypatch):
    """``before_write`` used to sleep out a minimum gap between reconfigures. It cannot
    sleep now — it has no clock to consult and takes no ``sleep`` argument at all."""
    import time as time_mod

    slept: list[float] = []
    monkeypatch.setattr(time_mod, "sleep", lambda s: slept.append(s))
    p, _ = _provider()
    p.apply(CH)
    p.apply(CH)
    assert slept == [], "a write waited for something"


# ── 2. a field the registry does not mark writable is never written ────────────


def test_a_write_to_a_field_the_registry_never_writes_is_refused(guard):
    """The flow table is captured on every run and never changed — the one rule the
    evidence actually supports, and the reason the rate valve was never needed.

    ``shaper_fields`` is where the decision lives and ``plan_apply`` honours it, so no
    engine can *plan* such a change — but a hand-built change list, a route, or a job spec
    queued before the registry changed still reaches the provider, and this field costs a
    30-second outage rather than a wasted call. So the last thing between a change list and
    the firewall checks it too, and nothing is written.
    """
    p, inner = _provider()
    flows = {"pipe_uuid": None, "param": "flows", "value": 2048}
    for call in (lambda: p.apply(flows), lambda: p.apply_many([flows]),
                 lambda: p.apply_many([CH, flows])):   # one bad change condemns the batch
        with pytest.raises(fg.FirewallWriteRefused) as ei:
            call()
        assert ei.value.kind == "protected_field"
        assert "flows" in ei.value.reason and "never written" in ei.value.reason
    assert inner.reconfigures == 0 and not [c for c in inner.calls if c != "discover"]
    assert [r["outcome"] for r in _ledger()] == ["refused"] * 3
    # ...and an ordinary writable field still writes, immediately.
    p.apply(CH)
    assert inner.reconfigures == 1


def test_the_pipe_toggle_is_not_a_shaper_field_and_is_untouched(guard):
    """``set_pipe_enabled`` writes ``param: "enabled"``, which the registry has never heard
    of — a separate, documented write path (the baseline test's SQM-off). Refusing every
    unrecognised param would have taken it out with the flow table."""
    p, inner = _provider()
    assert fg.protected_params([{"param": "enabled", "value": False}]) == []
    p.set_pipe_enabled(None, False)
    assert inner.reconfigures == 1


def test_the_refusal_points_at_no_remedy_it_does_not_have(guard):
    """There is no Arm button any more, and there was never one that helped here: this
    refusal is about what the field *is*, so the card must not send anyone to press
    something."""
    text = session_runtime.describe_failure(
        fg.FirewallWriteRefused("Flows is captured but never written"))
    assert "does not lift" in text and "Arm" not in text and "Nothing was written" in text


# ── 3. a timed-out write is verified by re-reading, never reissued ─────────────


def test_a_timed_out_write_that_took_is_verified_not_reissued(guard):
    p, inner = _provider()
    inner.fail_next("timeout", land=True)   # the reconfigure ran; only the answer was late
    out = p.apply({"pipe_uuid": None, "param": "quantum", "value": 300})
    assert out["ok"] and out.get("verified_after_timeout") is True
    assert inner.calls.count("apply") == 1 and inner.calls.count("discover") >= 1
    assert inner.reconfigures == 1                            # exactly one reload reached the box
    assert _ledger()[-1]["outcome"] == "verified"


def test_a_timed_out_write_that_did_not_take_fails_that_session_only(guard):
    """The write is attempted once and reported gone. It used to also trip hands-off, which
    stopped every *other* engine too and refused the restores they were holding — one
    session's bad minute becoming the whole platform's."""
    p, inner = _provider()
    inner.fail_next("timeout", land=False)
    with pytest.raises(FirewallUnavailable):
        p.apply({"pipe_uuid": None, "param": "quantum", "value": 300})
    assert inner.calls.count("apply") == 1, "ONE attempt, never a retry"
    assert _ledger()[-1]["outcome"] == "failed"
    # The very next write is allowed: nothing latched.
    p.apply(CH)
    assert _ledger()[-1]["outcome"] == "ok"


def test_a_502_on_a_write_is_never_retried_either(guard):
    p, inner = _provider()
    inner.fail_next("502", land=False)
    with pytest.raises(FirewallUnavailable):
        p.set_pipe_enabled(None, False)
    assert inner.calls.count("set_pipe_enabled") == 1


def test_an_outage_on_a_read_is_retried_and_then_raised(guard):
    """Reads may be retried — only writes may not — and a failed read is now just a failed
    read: there is no state for it to set."""
    p, inner = _provider()
    inner.dark(10)
    with pytest.raises(FirewallUnavailable):
        p.discover()
    assert inner.calls.count("discover") == session_runtime.FIREWALL_ATTEMPTS


# ── 4. the rollback: no session is refused at the door ─────────────────────────


def test_no_session_asks_the_write_path_for_permission_to_start():
    """Six engines, the job queue, the ticket dispatcher and both nightly scheduler gates
    each asked the guard before starting. That check existed only because hands-off
    existed, and it is what turned one tripped valve into a night with nothing measured.

    Asserted at the source, like the raw-provider scan above, because the alternative is
    actually starting seven sessions to watch them not be refused. Both halves are checked:
    nobody calls the door check, and the door check does not exist to be called.
    """
    root = Path(__file__).resolve().parents[1] / "pathbrain"
    callers = [p.relative_to(root).as_posix() for p in root.rglob("*.py")
               if "blocked_reason" in p.read_text()]
    assert not callers, f"these still ask the write path for permission: {callers}"
    for gone in ("blocked_reason", "WRITING_KINDS", "arm", "hands_off", "trip",
                 "startup_check", "state", "note_contact", "take_wait_ms", "DEFAULTS"):
        assert not hasattr(fg, gone), f"firewall_guard still exposes {gone}"


def test_the_queue_takes_every_job(guard):
    """``job_queue.submit`` carried the one refusal the queueing contract allowed. With no
    guard state there is no bad request left to refuse, so the contract is unconditional
    again: a job starts now or it queues."""
    from pathbrain import job_queue

    assert job_queue.submit("duel", "Duel ladder", lambda: 1).result == 1
    assert job_queue.submit("current_test", "Test current", lambda: 42).result == 42


# ── 5. the chokepoint: every provider is the guarded one ───────────────────────


def test_get_provider_is_always_the_guarded_wrapper():
    assert isinstance(get_provider(), ResilientProvider)


def test_no_module_builds_a_raw_provider_or_bypasses_apply_many():
    """A regression on the write path has to go through ``session_runtime``: nothing outside
    the providers package constructs a provider directly, and no engine writes a profile
    switch as a per-field loop."""
    root = Path(__file__).resolve().parents[1] / "pathbrain"
    offenders = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        text = path.read_text()
        if not rel.startswith("providers/") and re.search(r"\b(OPNsenseProvider|MockProvider)\(", text):
            offenders.append(f"{rel}: constructs a provider directly")
        if rel not in ("profile_test.py",) and re.search(r"for \w+ in \w+:\s*\n\s*provider\.apply\(", text):
            offenders.append(f"{rel}: applies a change list one field at a time")
    assert not offenders, offenders


# ── 6. the API ─────────────────────────────────────────────────────────────────


def test_the_guard_endpoint_reports_the_rate_and_nothing_to_arm(client, guard):
    body = client.get("/api/firewall/guard").json()
    assert "reconfigures_last_hour" in body and body["writes"] == []
    assert "hands_off" not in body and "config" not in body
    assert "reconfigures_last_hour" in client.get("/api/health/pipeline").json()["firewall"]
    # The POSTs are gone with the state they set. Asserted against the route table rather
    # than by POSTing and reading the status: an unrouted path is answered by whatever
    # catch-all is mounted, and this app mounts the built frontend as one — so the same
    # request is 405 on a machine that has run `npm run build` and 404 on one that has not.
    # That is a test of whether the frontend was built, which is not the question.
    from pathbrain.main import app

    gone = {"/api/firewall/guard/arm", "/api/firewall/guard/hands-off"}
    assert not (gone & {getattr(r, "path", "") for r in app.routes})


def test_the_config_write_test_is_ledgered_like_any_other_write(client, guard):
    """`POST /config/test-apply` proves the write path works by nudging quantum +1 and
    setting it back. It holds no special status: both writes are on the ledger at one
    reconfigure each."""
    body = client.post("/api/config/test-apply").json()
    assert body["ok"] is True
    rows = client.get("/api/firewall/guard").json()["writes"]
    assert [(r["field"], r["outcome"], r["reconfigures"]) for r in rows] == [
        ("quantum", "ok", 1),
        ("quantum", "ok", 1),
    ], "the nudge and the restore are each one ledgered reconfigure"


def test_a_no_op_apply_writes_nothing(guard):
    """A leg whose profile is the one the firewall is already on plans no changes, so
    ``_apply_all`` returns without calling the provider at all."""
    from pathbrain.profile_test import _apply_all

    prov = get_provider()
    _apply_all(prov, [])
    assert fg.recent_writes() == [], "a no-op switch touches the firewall not at all"


# ── 7. OPNsense: one reconfigure per switch, however many fields ───────────────


def test_opnsense_apply_many_reconfigures_once(monkeypatch):
    from pathbrain.providers.opnsense import OPNsenseProvider

    posts: list[str] = []
    settings = {"ts": {"pipes": {"pipe": {
        "u-down": {"fqcodel_quantum": "1514", "fqcodel_limit": "10240", "codel_target": {"5": {"value": "5ms", "selected": 1}}},
        "u-up": {"fqcodel_quantum": "300", "fqcodel_limit": "10240", "codel_target": {"5": {"value": "5ms", "selected": 1}}},
    }}}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=settings)
        posts.append(request.url.path)
        return httpx.Response(200, json={"result": "saved"})

    prov = OPNsenseProvider(base_url="https://fw.test", api_key="k", api_secret="s")
    monkeypatch.setattr(prov, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fw.test"))
    out = prov.apply_many([
        {"pipe_uuid": "u-down", "param": "quantum", "value": 1000},
        {"pipe_uuid": "u-down", "param": "limit", "value": 2000},
        {"pipe_uuid": "u-up", "param": "quantum", "value": 500},
    ])
    assert out["reconfigures"] == 1 and len(out["applied"]) == 3
    assert posts.count("/api/trafficshaper/service/reconfigure") == 1
    assert sorted(p for p in posts if "setPipe" in p) == ["/api/trafficshaper/settings/setPipe/u-down", "/api/trafficshaper/settings/setPipe/u-up"]
    # The single-field apply is the same path with one change: still one reload.
    posts.clear()
    prov.apply({"pipe_uuid": "u-up", "param": "quantum", "value": 600})
    assert posts.count("/api/trafficshaper/service/reconfigure") == 1
