"""The reachability audit: which measured profiles the firewall can't be driven to.

The audit exists because the consequence it reports is *silent* — an unreachable profile
simply stops appearing in the ring, the race and the heirs card — so these tests pin the
two things that make it worth reading: it counts the split correctly, and it names the one
change that brings back the most measured evidence.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from pathbrain import reachability
from pathbrain.database import session_scope
from pathbrain.models import Run, RunStatus
from pathbrain.providers.mock import MockProvider
from pathbrain.settings_profile import normalize


@pytest.fixture()
def field(monkeypatch):
    """Put a known set of profiles in front of the audit, without touching anyone else's.

    The database is session-scoped and shared by the whole suite, so an earlier draft of
    this fixture deleted every run to get a clean count — and took twelve other tests'
    seeded rows with it. The audit's input is "the stored profiles", so the honest way to
    control it is to control that list: runs are still seeded (additively, for the
    iteration counts) and ``list_profiles`` is pointed at exactly the profiles under test.
    """
    from pathbrain import refresh as refresh_mod

    entries: list[dict] = []

    def add(fp: str, settings: list[dict], runs: int = 1, iters: int = 5) -> None:
        with session_scope() as s:
            for _ in range(runs):
                s.add(Run(status=RunStatus.COMPLETE, settings_fingerprint=fp,
                          settings=settings, iterations=iters))
        entries.append({"fingerprint": fp, "settings": settings,
                        "label": "profile", "name": None})

    monkeypatch.setattr(refresh_mod, "list_profiles", lambda session: list(entries))
    return add


def _live() -> list:
    return MockProvider().discover()


def _settings(**overrides) -> list[dict]:
    """A profile matching the live firewall except for the named fields.

    Only the **download** pipe is varied: the mock's upload pipe deliberately has no uuid
    (mirroring an OPNsense pipe ``apply()`` can't target), so any difference there is
    unreachable for a second, unrelated reason — real, reported by the audit as cause
    ``pipe``, and not what these tests are about.
    """
    pipes = [dict(p) for p in normalize(_live())]
    pipes[0].update(overrides)
    return pipes


def test_a_profile_matching_the_live_environment_is_reachable(client, field):
    field("reach_same", _settings(quantum=4000))   # a writable difference only
    with session_scope() as s:
        out = reachability.audit(s, _live())
    assert out["unreachable"] == 0
    assert "Nothing is out of reach" in out["verdict"]


def test_a_profile_differing_in_the_flow_table_is_unreachable_and_named(client, field):
    """The case the audit was written for: `flows` is captured and never written, so a
    profile carrying a different one cannot be applied — and nothing else says so."""
    field("reach_flows", _settings(flows=512, quantum=4000))
    with session_scope() as s:
        out = reachability.audit(s, _live())

    assert out["unreachable"] == 1
    row = next(r for r in out["profiles"] if r["fingerprint"] == "reach_flows")
    assert row["cause"] == "field"
    diff = next(d for d in row["diffs"] if d["field"] == "flows")
    assert diff["from"] == 1024 and diff["to"] == 512
    # The writable difference is NOT what makes it unreachable and must not be listed.
    assert not any(d["field"] == "quantum" for d in row["diffs"])
    # And the field ledger attributes it.
    assert out["by_field"][0]["field"] == "flows"
    assert out["by_field"][0]["profiles"] == 1


def test_the_audit_names_the_change_that_brings_back_the_most_measurement(client, field):
    """A list of unreachable profiles is a fact; the move is the decision.

    Profiles are grouped by the exact change that would restore them, ranked by the
    iterations of evidence each group carries — because a hundred thin profiles and a few
    well-measured ones are not the same loss.
    """
    # Two profiles waiting on flows=512 (one heavily measured), one on flows=2048.
    field("many_a", _settings(flows=512, quantum=4000), runs=6)
    field("many_b", _settings(flows=512, quantum=5000), runs=1)
    field("other_c", _settings(flows=2048, quantum=6000), runs=1)
    with session_scope() as s:
        out = reachability.audit(s, _live())

    assert out["unreachable"] == 3
    assert len(out["moves"]) == 2
    best = out["moves"][0]
    assert best["profiles"] == 2                       # the flows=512 pair
    assert all(c["field"] == "flows" and c["to"] == 512 for c in best["changes"])
    assert best["iterations"] > out["moves"][1]["iterations"]
    assert "512" in out["verdict"] and "cannot be applied" in out["verdict"]


def test_the_audit_refuses_to_offer_the_change_as_an_action(client, field):
    """The move it names is a write to a field the registry forbids, and the reason it is
    forbidden is that writing it took the link down for ~30s every time. The card says what
    to change; it never offers to do it."""
    field("no_button", _settings(flows=512))
    with session_scope() as s:
        out = reachability.audit(s, _live())
    assert "PathBrain will not make that change itself" in out["verdict"]
    # Nothing in the payload is an instruction to a writer: no pipe uuid, no wire value.
    for move in out["moves"]:
        for change in move["changes"]:
            assert "pipe_uuid" not in change and "value" not in change


def test_the_audit_reads_the_firewall_as_it_stands(client, field):
    """"Reachable" is meaningless in the abstract: it is relative to the live firewall, so
    the same profile flips as the firewall moves."""
    from pathbrain.providers.mock import _OVERRIDES

    field("relative", _settings(flows=512))
    with session_scope() as s:
        assert reachability.audit(s, _live())["unreachable"] == 1
    _OVERRIDES["flows"] = 512                    # the firewall is moved to match
    try:
        with session_scope() as s:
            assert reachability.audit(s, _live())["unreachable"] == 0
    finally:
        _OVERRIDES.clear()


def test_the_endpoint_serves_it(client, field):
    field("via_api", _settings(flows=512))
    body = client.get("/api/methodologies/reachability").json()
    assert body["unreachable"] == 1 and body["moves"]
    assert body["live"]["non_writable"], "the card must say what the firewall is on"
    assert any(f["field"] == "flows" for f in body["live"]["non_writable"])


# ── The per-profile view: what a profile IS, and can it exist? ───────────────────────


def test_the_profile_view_shows_every_field_against_the_live_firewall(client, field):
    """Profile Detail showed a grade and a bout tape and never the settings themselves."""
    from pathbrain import reachability

    settings = _settings(flows=512, quantum=4000, target=3)
    field("viewfp", settings)
    with session_scope() as s:
        view = reachability.profile_view(s, "viewfp", _live())

    download = view["pipes"][0]
    by_field = {f["field"]: f for f in download["fields"]}
    # Every registry field is present, whether or not it differs — the card is "what IS
    # this profile", so a field only shown when it differs would be a diff, not a profile.
    assert {"quantum", "target", "flows", "scheduler", "queues"} <= set(by_field)
    assert by_field["quantum"]["value"] == 4000 and by_field["quantum"]["live"] == 1514
    assert by_field["quantum"]["differs"] and by_field["quantum"]["writable"]
    assert by_field["flows"]["differs"] and not by_field["flows"]["writable"]
    assert not by_field["limit"]["differs"]        # same as live
    assert view["can_exist"] is False and "Flows" in view["verdict"]


def test_the_profile_view_formats_values_once_with_the_registrys_formatter(client, field):
    """A CoDel target is stored as the bare option key the firewall echoes, so a component
    appending "ms" itself renders "5msms" for one profile and "5ms" for the next. The unit
    is applied server-side, by the one formatter."""
    from pathbrain import reachability

    field("fmtfp", _settings(target=3))
    with session_scope() as s:
        view = reachability.profile_view(s, "fmtfp", _live())
    target = next(f for f in view["pipes"][0]["fields"] if f["field"] == "target")
    assert target["display"] == "3ms"
    assert target["live_display"].count("ms") == 1


def test_a_profile_that_matches_the_environment_can_exist(client, field):
    from pathbrain import reachability

    field("okfp", _settings(quantum=4000))         # a writable difference only
    with session_scope() as s:
        view = reachability.profile_view(s, "okfp", _live())
    assert view["can_exist"] is True and not view["blocked"]
    assert "can be put on this profile" in view["verdict"]


def test_the_two_cards_can_never_disagree(client, field):
    """The field audit and the per-profile card run the same primitive, so a profile the
    audit flags is exactly one the card calls unreachable — the property that makes the
    per-profile answer worth trusting."""
    from pathbrain import reachability

    field("agree_bad", _settings(flows=512))
    field("agree_ok", _settings(quantum=4000))
    with session_scope() as s:
        audit = reachability.audit(s, _live())
        flagged = {r["fingerprint"] for r in audit["profiles"]}
        for fp in ("agree_bad", "agree_ok"):
            view = reachability.profile_view(s, fp, _live())
            assert view["can_exist"] is (fp not in flagged)


def test_the_profile_settings_endpoint_serves_it(client, field):
    field("apifp", _settings(flows=512))
    body = client.get("/api/settings/profiles/apifp/settings").json()
    assert body["can_exist"] is False and body["pipes"]
    assert client.get("/api/settings/profiles/nope-no-such/settings").status_code == 404
