"""Tests for profile call signs — "Speedy Sloth" instead of "q1514 t5ms"."""
from __future__ import annotations

from pathbrain import profile_names
from pathbrain.database import session_scope
from pathbrain.models import ProfileName
from pathbrain.settings_profile import SQM_OFF_FINGERPRINT


def _clear():
    with session_scope() as s:
        s.query(ProfileName).delete()


def test_names_are_adjective_noun_and_stable():
    _clear()
    with session_scope() as s:
        first = profile_names.name_for(s, "abc123def456")
        again = profile_names.name_for(s, "abc123def456")
    assert first == again, "a profile must never be renamed behind the user's back"
    adjective, _, noun = first.partition(" ")
    assert adjective in profile_names.ADJECTIVES and noun in profile_names.NOUNS

    # Same fingerprint in a fresh database → the same name: the derivation is a pure
    # function of the fingerprint, not of insertion order.
    _clear()
    with session_scope() as s:
        assert profile_names.name_for(s, "abc123def456") == first
    _clear()


def test_the_pool_is_large_and_clean():
    """~500 of each. The size is the point: collisions become vanishingly rare, so the
    probe below is a guarantee rather than a routine code path."""
    assert len(profile_names.ADJECTIVES) >= 500
    assert len(profile_names.NOUNS) >= 500
    assert len(set(profile_names.ADJECTIVES)) == len(profile_names.ADJECTIVES)
    assert len(set(profile_names.NOUNS)) == len(profile_names.NOUNS)
    assert all(w[:1].isupper() and w.isalpha() for w in profile_names.ADJECTIVES)
    assert all(w[:1].isupper() and w.isalpha() for w in profile_names.NOUNS)
    # Every adjective initial has nouns to alliterate with.
    initials = {n[0] for n in profile_names.NOUNS}
    assert {a[0] for a in profile_names.ADJECTIVES} <= initials


def test_names_are_unique_across_a_large_field():
    """150 profiles is a realistic field — none of them may share a call sign."""
    _clear()
    with session_scope() as s:
        names = [profile_names.name_for(s, f"{i:012x}") for i in range(150)]
    assert len(set(names)) == 150
    assert sum(1 for n in names if n.split()[0][0] == n.split()[1][0]) > 100  # mostly alliterative
    _clear()


def test_a_taken_name_is_probed_past_not_reused():
    """Two fingerprints that want the same name: the second takes its next candidate."""
    _clear()
    wanted = next(iter(profile_names.candidates("aaaaaaaaaaaa")))
    with session_scope() as s:
        profile_names.rename("squatter0000", wanted)  # occupy it first
        assigned = profile_names.name_for(s, "aaaaaaaaaaaa")
    assert assigned != wanted
    assert assigned in list(profile_names.candidates("aaaaaaaaaaaa"))
    _clear()


def test_sqm_off_keeps_a_meaningful_name():
    """The unshaped control group is the one profile a whimsical name would obscure."""
    _clear()
    with session_scope() as s:
        assert profile_names.name_for(s, SQM_OFF_FINGERPRINT) == profile_names.SQM_OFF_NAME
    _clear()


def test_bulk_lookup_matches_one_by_one():
    _clear()
    fps = [f"bulk{i:08x}" for i in range(8)]
    with session_scope() as s:
        bulk = profile_names.names_for(s, fps)
        singles = {fp: profile_names.name_for(s, fp) for fp in fps}
    assert bulk == singles
    _clear()


def test_rename_is_validated(client):
    _clear()
    out = client.put(
        "/api/settings/profiles/renameme0001/name", json={"name": "  Old   Reliable "}
    ).json()
    assert out["name"] == "Old Reliable"  # whitespace normalized
    # Taken by another profile → refused, so a call sign always names exactly one profile.
    assert (
        client.put("/api/settings/profiles/other0002/name", json={"name": "Old Reliable"}).status_code
        == 422
    )
    assert client.put("/api/settings/profiles/renameme0001/name", json={"name": " "}).status_code == 422
    # Renaming the same profile again is fine.
    assert (
        client.put("/api/settings/profiles/renameme0001/name", json={"name": "Old Reliable"}).json()[
            "name"
        ]
        == "Old Reliable"
    )
    _clear()
