"""No surface offers a field PathBrain never writes as something to change.

`flows` is captured and never written because writing it took the household off the network
for about half a minute every time (the write ledger: every write carrying it timed out the
30s call and went quiet for 30-35s; every write without it was sub-second). The registry
enforces that for the *write path* — `plan_apply` emits only writable fields, the guard
refuses the rest, the leg apply refuses to measure a profile the firewall is not on.

None of that stops a **reading** surface from offering it. A page that lists the fields you
may tune, an export that hands a model a range to explore, a preview that proposes a step, a
card that computes the most valuable flow-table change and ranks it — each of those is a
"change this" put on screen by PathBrain, and the last one is not hypothetical: the
reachability card shipped with a *"What would bring them back / Change the firewall to:
Flows 1024 → 512"* table, ranked by the measurement each change restored. No button, and
still the hazard re-created as a recommendation.

So this walks the real API surfaces — the ones whose whole job is to say what can be
changed — and fails if a non-writable field appears as an offer. It is keyed on the registry
rather than on `flows`, so a field made unwritable tomorrow is covered the day it changes.
"""
from __future__ import annotations

import json


import pytest

from pathbrain.database import session_scope
from pathbrain.models import Run, RunStatus
from pathbrain.providers.mock import MockProvider
from pathbrain.settings_profile import fingerprint, normalize
from pathbrain.shaper_fields import NON_WRITABLE_FIELDS, WRITABLE_FIELDS

#: Keys whose value is "what the caller may act on". A non-writable field appearing under
#: one of these is an offer, whatever the surrounding prose says.
OFFER_KEYS = {"writable_fields", "sweepable_fields", "all_fields", "proposals", "steps"}

#: Phrases that turn a reading into an instruction. Checked against every free-text field
#: in a payload that also names a non-writable field.
PRESCRIPTIONS = (
    "change the firewall",
    "change it to",
    "set the firewall",
    "would bring back",
    "biggest single fix",
    "make it at the firewall",
)


@pytest.fixture()
def unreachable_profile():
    """A stored profile that differs from live in a non-writable field, so every surface
    that *would* offer the field has a live reason to."""
    pipes = [dict(p) for p in normalize(MockProvider().discover())]
    pipes[0].update({"flows": 512, "quantum": 4000})
    fp = fingerprint(pipes)
    with session_scope() as s:
        for _ in range(3):
            s.add(Run(status=RunStatus.COMPLETE, settings_fingerprint=fp,
                      settings=pipes, iterations=5))
    return fp


def _offers(node, path: str = "") -> list[str]:
    """Every place a non-writable field is presented as actionable."""
    banned = set(NON_WRITABLE_FIELDS)
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in OFFER_KEYS:
                if isinstance(value, dict) and banned & set(value):
                    found.append(f"{path}.{key} keys {sorted(banned & set(value))}")
                if isinstance(value, list):
                    for entry in value:
                        name = entry if isinstance(entry, str) else (
                            (entry.get("param") or entry.get("field") or entry.get("key"))
                            if isinstance(entry, dict) else None
                        )
                        if name in banned:
                            found.append(f"{path}.{key} offers {name!r}")
            found.extend(_offers(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            found.extend(_offers(value, f"{path}[{i}]"))
    return found


def _prescriptions(payload) -> list[str]:
    """Free text that tells someone to change one of these fields."""
    text = json.dumps(payload).lower()
    if not any(f in text for f in NON_WRITABLE_FIELDS):
        return []
    return [p for p in PRESCRIPTIONS if p in text]


def _surfaces(fp: str) -> list[tuple[str, str]]:
    return [
        # What the AI is told it may tune.
        ("optimizer export", "/api/settings/export/optimizer?profile_limit=5"),
        # What the Shotgun Sweep offers as a grid.
        ("sweep fields", "/api/sweep/fields"),
        # What the write probe proposes stepping, per field.
        ("write-probe fields", "/api/firewall/write-probe/fields"),
        # The two reachability readings — the ones that know which fields are unwritable and
        # could therefore most easily be tempted into naming a change.
        ("reachability audit", "/api/methodologies/reachability"),
        ("per-profile settings", f"/api/settings/profiles/{fp}/settings"),
    ]


def test_this_guard_is_actually_watching_the_surfaces_it_names(client, unreachable_profile):
    """A guard that silently stops watching is worse than no guard.

    Every surface above is addressed by URL, and this app mounts the built frontend as a
    catch-all — so an endpoint that is renamed or removed does not 404 here, it answers
    **200 with index.html**. A checker that skips non-200 responses would then police a
    surface that no longer exists and report itself green. (That is not hypothetical: the
    write probe's sweep preview was removed and this file kept asking for it.)

    So the paths are checked against the route table, which cannot be satisfied by HTML.
    """
    from pathbrain.main import app

    routed = {getattr(r, "path", "") for r in app.routes}
    for label, url in _surfaces(unreachable_profile):
        path = url.split("?")[0]
        # A path-parameter route registers its template, not the filled-in path.
        ok = path in routed or any(
            "{" in r and len(r.split("/")) == len(path.split("/")) and
            all(a == b or "{" in a for a, b in zip(r.split("/"), path.split("/")))
            for r in routed
        )
        assert ok, f"{label}: {path} is not a route — this guard is watching nothing"


def test_no_surface_offers_a_field_pathbrain_never_writes(client, unreachable_profile):
    problems: list[str] = []
    for label, url in _surfaces(unreachable_profile):
        resp = client.get(url)
        if resp.status_code != 200:
            continue
        for offer in _offers(resp.json()):
            problems.append(f"{label}: {offer}")
    assert not problems, "a non-writable field is offered as changeable:\n  " + "\n  ".join(problems)


def test_no_surface_tells_anyone_to_change_one(client, unreachable_profile):
    """Naming the field is fine — that is "capture". Telling someone to change it is not."""
    problems: list[str] = []
    for label, url in _surfaces(unreachable_profile):
        resp = client.get(url)
        if resp.status_code != 200:
            continue
        for phrase in _prescriptions(resp.json()):
            problems.append(f"{label}: {phrase!r}")
    assert not problems, "a surface recommends changing an unwritable field:\n  " + "\n  ".join(problems)


def test_the_guard_would_actually_catch_a_regression(client, unreachable_profile):
    """The two tests above pass trivially if `_offers` is broken, so prove it fires.

    A guard nobody has seen fail is a guard nobody should trust — the same reason the write
    probe times a real write rather than asserting one happened.
    """
    assert _offers({"shaper_model": {"writable_fields": ["quantum", "flows"]}})
    assert _offers({"plan": {"proposals": {"flows": {"to": 1025}}}})
    assert _offers({"plan": {"steps": [{"param": "flows", "to": 1025}]}})
    assert not _offers({"shaper_model": {"writable_fields": list(WRITABLE_FIELDS)}})
    # And the prose check fires on the phrasing that actually shipped.
    assert _prescriptions({"verdict": "The biggest single fix is Flows 1024 → 512"})
    assert not _prescriptions({"verdict": "Flows 512 (the firewall is on 1024)"})


def test_the_registry_is_the_only_place_this_is_decided():
    """The guard keys on the registry, so a field made unwritable tomorrow is covered the
    day it changes — no second list to remember."""
    assert "flows" in NON_WRITABLE_FIELDS and "flows" not in WRITABLE_FIELDS
    assert not (set(NON_WRITABLE_FIELDS) & set(WRITABLE_FIELDS))


def test_the_experiment_engine_refuses_a_non_writable_param(client):
    """The one engine that takes a field *name* from config rather than from a diff — so
    the only place a person could still type "flows" and have something act on it.

    It refuses before touching the provider (returns None and logs why), rather than
    starting and silently no-op'ing every trial.
    """
    from pathbrain import experiment

    with session_scope() as s:
        for field in NON_WRITABLE_FIELDS:
            started = experiment._start(s, {"param": field, "values": [1, 2], "pipe_uuid": None})
            assert started is None, f"an experiment started on the non-writable {field!r}"
