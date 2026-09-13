"""The per-field sweep: which field's write costs the household the outage?

The single probe answers "the fields or the reload?". It cannot answer "*which* field?",
and the two have different fixes — a reload that costs the same whatever moved is inherent
to reconfiguring a live shaper, while one that only hurts when a particular field moved is a
lead. These tests pin the three things that make the sweep safe to point at a live firewall:
the step is the smallest write that is still a write, the cost is known before anything is
written, and exactly one field is ever away from its original value.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pathbrain import write_probe as wp
from pathbrain.shaper_fields import WRITABLE_FIELDS


# ── the step: the smallest write that is still a write ────────────────────────


def test_an_integer_moves_by_one_not_by_a_lever_sized_step():
    """`levers._generated_values` halves and doubles because it is hunting a better value.
    This is measuring what a write costs, so the step must change the network as little as
    possible while still being a real setPipe — a halved queue limit is the failure the
    sweep investigates, not one it should cause."""
    assert wp.step_value("limit", 1000) == (1001, "+1")
    assert wp.step_value("flows", "1024") == (1025, "+1")
    assert wp.step_value("quantum", 3550)[0] == 3551


def test_a_bool_is_toggled():
    assert wp.step_value("ecn", True) == (False, "toggled")
    assert wp.step_value("ecn", "0") == (True, "toggled")


def test_an_option_keyed_field_moves_to_a_value_the_firewall_can_actually_HOLD():
    """CoDel target/interval are selects keyed by the bare number. `+1` off that list is
    accepted and silently does nothing, so the probe would time a write that never
    happened — the exact failure `field_options` exists to prevent."""
    value, how = wp.step_value("target", 5, options=[3.0, 5.0, 10.0, 20.0])
    assert value == 10 and "next option up" in how
    # At the top of the list there is nowhere up, so it steps down rather than off the end.
    value, how = wp.step_value("interval", 100, options=[20.0, 60.0, 100.0])
    assert value == 60 and "next option down" in how
    # A select with only the current value on it cannot be stepped at all.
    assert wp.step_value("target", 5, options=[5.0]) is None


def test_a_bandwidth_keeps_its_unit():
    """The legal forms of a bandwidth string are the firewall's business. Step the number,
    hand the suffix back untouched."""
    assert wp.step_value("download_bandwidth", "880Mbit") == ("881Mbit", "+1")
    assert wp.step_value("download_bandwidth", "880") == ("881", "+1")


def test_a_field_with_nothing_to_step_returns_no_step_rather_than_inventing_one():
    assert wp.step_value("limit", None) is None
    assert wp.step_value("not_a_field", 5) is None
    assert wp.step_value("scheduler", "fq_codel") is None, "a name has no +1"


# ── the plan: the cost is known before anything is written ────────────────────


def _live() -> dict:
    return {"pipe-a": {"label": "Download", "download_bandwidth": "880Mbit", "quantum": 3550,
                       "limit": 1000, "target": 5, "interval": 60, "ecn": True, "flows": 1024},
            "pipe-b": {"label": "Upload", "quantum": 500}}


def test_the_plan_covers_every_sweepable_field_and_says_why_it_skipped_the_rest():
    plan = wp.plan_sweep(_live(), "pipe-a", options={"target": [3.0, 5.0, 10.0]})
    assert [s["param"] for s in plan["steps"]] == wp.sweep_fields()
    # The upload pipe reports only a quantum, so everything else has no value to step.
    thin = wp.plan_sweep(_live(), "pipe-b")
    assert [s["param"] for s in thin["steps"]] == ["quantum"]
    assert all(s["why"] for s in thin["skipped"]), "a skip always says why"


def test_the_sweep_is_read_from_the_registry_so_a_new_writable_field_joins_it():
    """The frontend's own field list had drifted from the registry (six entries, missing the
    bandwidth). The sweep reads `WRITABLE_FIELDS` so marking a field writable puts it in the
    sweep with no second edit — the rule the Shotgun Sweep already follows."""
    assert set(wp.sweep_fields()) == set(WRITABLE_FIELDS) - wp.NEVER_STEP


def test_the_flow_table_is_never_stepped_by_a_sweep_even_when_asked_for():
    """Measured on this link (probe #5): setting `flows` was free and putting it back took
    35.3s, timed out the apply, took the box off the network for 33s and tripped hands-off.
    Every other writable field is a parameter the shaper reads; this one decides how many
    queues it allocates, so any change rebuilds the whole table. A seven-step unattended
    sweep including it would spend its budget reproducing the outage it exists to diagnose —
    and naming it explicitly must not override that, since the reason does not depend on who
    asked."""
    assert "flows" not in wp.sweep_fields()
    plan = wp.plan_sweep(_live(), "pipe-a", ["flows", "quantum"])
    assert [s["param"] for s in plan["steps"]] == ["quantum"]
    why = next(s["why"] for s in plan["skipped"] if s["param"] == "flows")
    assert "flow-table" in why


def test_a_field_costs_two_reconfigures_and_the_cheap_pass_costs_none():
    """Step it, put it back. Only a reload is a reconfigure, and a reconfigure is what
    rebuilds the queues — so the cheap `reload=False` pass writes every field without
    disturbing a single flow, which is why it is worth running first."""
    plan = wp.plan_sweep(_live(), "pipe-a")
    assert plan["reconfigures"] == 2 * len(plan["steps"])
    cheap = wp.plan_sweep(_live(), "pipe-a", reload=False)
    assert cheap["reconfigures"] == 0
    assert len(cheap["steps"]) == len(plan["steps"]), "it still visits every field"


def test_the_estimate_grows_with_the_settle_because_that_is_where_the_time_goes():
    slow = wp.plan_sweep(_live(), "pipe-a", settle_s=45.0)["seconds"]
    quick = wp.plan_sweep(_live(), "pipe-a", settle_s=10.0)["seconds"]
    assert slow > quick


def test_the_default_settle_outlasts_the_outage_the_link_watch_measured():
    """`summarize` reports the worst gap *inside* the step window, and a run still lost when
    the window closes is measured only to the last sample in it. The measured outages are
    33-35s, so the single probe's 10s settle would report 10s and truncate the finding."""
    assert wp.SWEEP_SETTLE_S >= 40.0
    assert wp.SWEEP_SETTLE_S > wp.DEFAULT_SETTLE_S


# ── no budget: a sweep is priced in time, and nothing rations it ──────────────


def test_a_sweep_is_never_refused_on_a_write_budget():
    """``budget_shortfall`` stood here: a sweep was priced against the guard's remaining
    hourly reconfigures and refused if it would not fit, because exceeding the cap tripped
    hands-off, which refused *every* write including the restore — leaving a field moved.

    Both halves of that are gone. There is no cap to exceed and no state to trip, so a
    sweep that visits every field simply runs, and what stops it holding a field is the
    thing that always actually did: it reverts each step before taking the next, and the
    ``finally`` says so in capitals when it cannot.
    """
    assert not hasattr(wp, "budget_shortfall")
    plan = wp.plan_sweep(_live(), "pipe-a")
    assert plan["steps"] and "blocked" not in plan


# ── the verdict: name the field, unless position explains it better ───────────


def _step(i: int, param: str, gap_ms: float, phase: str = "set") -> dict:
    return {"step": f"field_{phase}", "param": param, "index": i, "phase": phase,
            "label": param, "failed": None,
            "targets": {"firewall": {"worst_gap_ms": 0.0},
                        "through": {"worst_gap_ms": gap_ms}}}


def test_the_worst_field_is_named_with_its_number():
    steps = [_step(1, "quantum", 400.0), _step(2, "limit", 9000.0), _step(3, "ecn", 300.0)]
    out = wp.sweep_verdict(steps)
    assert "limit" in out and "9.0s" in out


def test_a_cost_that_tracks_POSITION_is_reported_as_position_not_as_a_field():
    """One pass gives one sample per field, so "this field is expensive" and "the fourth
    reload of a session is expensive" produce identical tables. A ranking nobody can trust is
    worse than no ranking, and the reader cannot see the confound from the rows."""
    steps = [_step(i, f"f{i}", 1000.0 * i) for i in range(1, 7)]
    out = wp.sweep_verdict(steps)
    assert "POSITION" in out and "different order" in out


def test_no_gap_anywhere_says_so_rather_than_crowning_a_winner_from_noise():
    steps = [_step(1, "quantum", 0.0), _step(2, "limit", 0.0)]
    assert "not in any one field" in wp.sweep_verdict(steps)


def test_the_box_going_quiet_leads_the_verdict():
    """"The firewall stopped answering" and "traffic through it stopped" are different
    failures with different responses, and only the first is one no config API should cause."""
    steps = [_step(1, "quantum", 500.0), _step(2, "limit", 800.0)]
    steps[1]["targets"]["firewall"] = {"worst_gap_ms": 34000.0}
    assert "firewall itself stopped answering" in wp.sweep_verdict(steps)


def test_a_sweep_with_nothing_measured_says_so():
    assert "nothing to compare" in wp.sweep_verdict([])
    assert "nothing to compare" in wp.sweep_verdict([{"step": "baseline"}])


# ── the whole sweep, against a real (mock) provider ───────────────────────────


def test_every_field_is_put_back_and_only_one_is_ever_moved(client, monkeypatch):
    """The safety property the sweep rests on: it reverts before it moves on, so a failure at
    any point can name exactly one outstanding field — and at the end the firewall reads
    exactly as it did at the start."""
    from pathbrain.providers import get_provider

    provider = get_provider()
    before = {(c.extra or {}).get("uuid"): dict(c.to_dict()) for c in provider.discover()}
    uuid = next(iter(before))

    # Drive the plan by hand through the same provider the engine uses — the sampler and the
    # settle windows are wall-clock and belong to the live-firewall path, not to this check.
    plan = wp.plan_sweep(before, uuid, options=provider.field_options())
    assert "flows" not in [s["param"] for s in plan["steps"]]
    moved: list[str] = []
    for planned in plan["steps"]:
        provider.apply_many([{"pipe_uuid": uuid, "param": planned["param"],
                              "value": planned["to"]}], reload=True)
        now = {(c.extra or {}).get("uuid"): c.to_dict() for c in provider.discover()}[uuid]
        differs = [k for k in WRITABLE_FIELDS
                   if str(now.get(k)) != str(before[uuid].get(k))]
        assert differs == [planned["param"]], f"exactly one field moved, got {differs}"
        moved.append(planned["param"])
        provider.apply_many([{"pipe_uuid": uuid, "param": planned["param"],
                              "value": planned["from"]}], reload=True)

    assert moved, "the sweep visited at least one field"
    after = {(c.extra or {}).get("uuid"): c.to_dict() for c in provider.discover()}
    for key in WRITABLE_FIELDS:
        assert str(after[uuid].get(key)) == str(before[uuid].get(key)), key


def test_the_preview_prices_the_sweep_without_writing_anything(client):
    r = client.get("/api/firewall/write-probe/sweep/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["steps"] and body["reconfigures"] == 2 * len(body["steps"])
    assert body["all_fields"] == wp.sweep_fields() and "flows" not in body["all_fields"]
    assert body["pipes"], "the caller can pick a pipe"
    cheap = client.get("/api/firewall/write-probe/sweep/preview?reload=false").json()
    assert cheap["reconfigures"] == 0
