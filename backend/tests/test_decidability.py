"""What can the ladder actually settle? (`decidability.py`)

The duel had elaborate machinery for *answering* a question and none for deciding whether
one was **answerable**, so it spent its nights on pairs it had no power to separate and
recorded the result as a draw — which on screen reads identically to "these two are equal".

Pinned here: the ring's own noise is MEASURED from margins already on the ledger and the
estimator recovers a known σ (and barely moves under drift within a match, which is why it
uses successive differences rather than deviations about a median); a pair that cannot differ —
identical, differing only in fields PathBrain never writes, or one immaterial lever step —
is refused before a round is run and the reason is a sentence; a gap below the ladder's
measured resolution is NAMED rather than silently drawn; and an unmeasured pair is never
refused, because "nobody has looked" is a reason to race it.
"""
from __future__ import annotations

import random
import statistics

from pathbrain import decidability as dec

WAN = {"label": "wan-download", "download_bandwidth": "880Mbit", "quantum": 5800, "target": 3,
       "interval": 60, "ecn": True, "flows": 1024, "limit": 10240,
       "scheduler": "fq_codel", "queues": 1}


def _settings(**over) -> list[dict]:
    return [{**WAN, **over}]


def _ledger(matches: list[list[float]]) -> list[dict]:
    """A ledger of matchups carrying just their per-round margins."""
    return [{"matchups": [{"deltas": list(d)} for d in matches]}]


def _synthetic(true_sigma: float, *, n_per=6, n_match=8, drift=0.0, seed=0) -> list[dict]:
    """Matches whose true edges differ but whose round noise is exactly ``true_sigma``.

    The edge is drawn ONCE per match — it is a property of the pair, not of the round, and
    re-drawing it per round would fold a second noise source into the very quantity these
    tests are checking the estimator recovers.
    """
    rng = random.Random(seed)
    out: list[list[float]] = []
    for _ in range(n_match):
        edge = rng.uniform(-2, 2)
        out.append([edge + drift * i + rng.gauss(0, true_sigma) for i in range(n_per)])
    return _ledger(out)


# ── The ring measures its own noise ───────────────────────────────────────────────────


def test_the_noise_estimator_recovers_a_known_sigma():
    """The whole module rests on this number, so it is checked against ground truth rather
    than asserted. Tolerance is wide because a thin ledger genuinely is imprecise — what
    must not happen is a systematic read far below the truth, which would tell the ladder
    it can resolve differences it cannot."""
    for true_sigma in (0.5, 1.0, 2.0):
        got = [
            dec.round_noise(_synthetic(true_sigma, seed=s))["sigma"]
            for s in range(40)
        ]
        measured = statistics.median(got)
        assert 0.85 * true_sigma <= measured <= 1.15 * true_sigma, (
            f"σ={true_sigma} read back as {measured}"
        )


def test_drift_within_a_match_barely_moves_the_estimate():
    """The reason it takes successive differences instead of deviations about a median.

    A match running for hours across changing conditions has a wandering centre; deviations
    about one median book the whole wander as measurement noise, where a successive
    difference sees only one round of it. Less sensitive, not immune — and the residue is
    conservative, which is the right direction for a number that decides what to refuse.
    """
    flat = statistics.median(
        dec.round_noise(_synthetic(1.0, seed=s))["sigma"] for s in range(40)
    )
    drifting = statistics.median(
        dec.round_noise(_synthetic(1.0, drift=0.5, seed=s))["sigma"] for s in range(40)
    )
    assert drifting >= flat, "drift must never make the ladder look sharper than it is"
    assert drifting < flat * 1.15, f"drift moved the estimate {flat:.3f} → {drifting:.3f}"

    # And the alternative really would have been fooled: deviations about each match's own
    # median, on the same drifting data, read far higher.
    sessions = _synthetic(1.0, drift=0.5, seed=1)
    devs: list[float] = []
    for m in sessions[0]["matchups"]:
        centre = statistics.median(m["deltas"])
        devs.extend(abs(d - centre) for d in m["deltas"])
    centred = statistics.median(devs) * dec.MAD_TO_SIGMA
    assert centred > drifting * 1.25


def test_a_ledger_too_thin_to_measure_refuses_nothing():
    """`None` means *we cannot tell*, never *no*. A ladder that refused bouts because it
    had not yet measured itself would refuse everything on its first night."""
    power = dec.resolving_power(_ledger([[0.1, 0.2]]), 30)
    assert power["min_margin"] is None and power["sigma"] is None
    assert "nothing is being refused" in power["verdict"]

    verdict = dec.decidable(
        "a", "b", power=power,
        settings_by_fp={"a": _settings(), "b": _settings(quantum=7000)},
        overalls={"a": 60.0, "b": 59.9},
    )
    assert verdict["verdict"] == dec.UNKNOWN
    assert verdict["verdict"] not in dec.REFUSED


def test_resolving_power_falls_as_the_round_cap_rises():
    """More rounds per match resolve smaller margins — the relationship the whole
    allocation argument rests on."""
    sessions = _synthetic(1.0, n_per=10, n_match=10, seed=3)
    tight = dec.resolving_power(sessions, 120)["min_margin"]
    loose = dec.resolving_power(sessions, 10)["min_margin"]
    assert tight < loose
    assert dec.rounds_to_resolve(0.25, 1.0) > dec.rounds_to_resolve(1.0, 1.0)
    # A margin small enough is not reachable by measuring, and says so rather than
    # printing a number nobody would run.
    assert dec.rounds_to_resolve(0.001, 1.0) is None


# ── Pairs that cannot differ at all ───────────────────────────────────────────────────


def test_identical_profiles_cannot_differ():
    why = dec.cannot_differ(_settings(), _settings())
    assert why and "same profile" in why


def test_a_pair_differing_only_in_a_field_pathbrain_never_writes_cannot_differ():
    """`flows` is captured but never written, so the firewall cannot be driven from one of
    these to the other: whichever was asked for, the other is what gets measured. Observed
    on the real ladder as two standings rows printing an identical settings summary."""
    why = dec.cannot_differ(_settings(), _settings(flows=512))
    assert why is not None
    assert "never writes" in why and "Flows" in why


def test_a_writable_difference_beside_an_unwritable_one_is_still_refused():
    """The bout would be applied for its writable half and land on neither profile."""
    why = dec.cannot_differ(_settings(), _settings(quantum=7000, flows=512))
    assert why and "never writes" in why


def test_one_quantum_apart_is_immaterial_against_the_measured_range():
    """`q5799` vs `q5800` on an 880 Mbit link — one part in ten thousand of the range the
    field has actually run quantum over. A variant-generation artifact, not a contender."""
    field = [
        {"settings": _settings(quantum=800)},
        {"settings": _settings(quantum=10814)},
    ]
    why = dec.cannot_differ(_settings(quantum=5800), _settings(quantum=5799), profiles=field)
    assert why and "immaterial" in why
    assert "%" in why  # it states how small, against what range


def test_a_real_lever_move_is_never_called_immaterial():
    """The guard must not swallow the moves the ladder exists to adjudicate."""
    field = [
        {"settings": _settings(quantum=800)},
        {"settings": _settings(quantum=10814)},
    ]
    assert dec.cannot_differ(
        _settings(quantum=5800), _settings(quantum=7313), profiles=field
    ) is None
    # A boolean has no range, so a flip is always material.
    assert dec.cannot_differ(_settings(), _settings(ecn=False), profiles=field) is None


def test_immateriality_needs_a_measured_range_to_judge_against():
    """With fewer than two distinct values on record there is no span, and an unmeasured
    lever is never called immaterial — the claim would have no basis."""
    assert dec.cannot_differ(
        _settings(quantum=5800), _settings(quantum=5799), profiles=[]
    ) is None


# ── The verdict ───────────────────────────────────────────────────────────────────────


def _power(sigma_sessions=None, max_pairs=30):
    return dec.resolving_power(sigma_sessions or _synthetic(1.0, n_per=10, n_match=10, seed=5),
                               max_pairs)


def test_a_gap_below_the_resolution_is_named_not_silently_drawn():
    """The failure this module exists to end: 30 rounds spent, a draw recorded, and nothing
    anywhere saying the question was never answerable."""
    power = _power()
    verdict = dec.decidable(
        "a", "b", power=power,
        settings_by_fp={"a": _settings(), "b": _settings(quantum=7313)},
        overalls={"a": 60.10, "b": 60.05},
    )
    assert verdict["verdict"] == dec.BELOW_RESOLUTION
    assert f"{power['min_margin']:.2f}" in verdict["why"]
    assert verdict["rounds_needed"] and verdict["rounds_needed"] > power["max_pairs"]


def test_a_gap_above_the_resolution_is_worth_racing():
    power = _power()
    verdict = dec.decidable(
        "a", "b", power=power,
        settings_by_fp={"a": _settings(), "b": _settings(quantum=7313)},
        overalls={"a": 64.0, "b": 60.0},
    )
    assert verdict["verdict"] == dec.YES


def test_direct_rounds_outrank_the_pooled_gap():
    """Paired, interleaved, same-weather rounds on exactly this pair beat a difference of
    two pooled medians taken at different times — so when they exist, nothing else is
    consulted."""
    sessions = _synthetic(1.0, n_per=10, n_match=10, seed=5)
    sessions[0]["matchups"].append(
        {"incumbent": "b", "challenger": "a", "deltas": [4.0, 4.2, 3.8, 4.1]}
    )
    prior = dec.expected_margin("a", "b", sessions_data=sessions, overalls={"a": 60.0, "b": 60.0})
    assert prior["source"] == "direct"
    assert prior["margin"] > 3.5          # the pooled gap of 0.0 was not used
    assert prior["signed"] > 0            # and it is signed from a's side

    flipped = dec.expected_margin("b", "a", sessions_data=sessions, overalls={})
    assert flipped["signed"] < 0


def test_an_unmeasured_pair_is_raced_not_refused():
    """Refusing on an absence of evidence is how a ladder stops racing exactly the pairs
    nobody has looked at yet."""
    verdict = dec.decidable(
        "a", "b", power=_power(),
        settings_by_fp={"a": _settings(), "b": _settings(quantum=7313)},
        overalls={},  # neither has a pooled score
    )
    assert verdict["verdict"] == dec.UNKNOWN
    assert verdict["verdict"] not in dec.REFUSED


def test_only_cannot_differ_is_actually_refused():
    """`below_resolution` is a statement about the instrument and `unknown` about the
    evidence; neither is a structural fact, so neither bars a bout. Pinned because widening
    REFUSED is exactly the change that would quietly stop the ladder racing."""
    assert dec.REFUSED == (dec.CANNOT_DIFFER,)


# ── The night's card ──────────────────────────────────────────────────────────────────


def test_plan_says_what_it_can_and_cannot_settle():
    power = _power()
    field = [{"settings": _settings(quantum=800)}, {"settings": _settings(quantum=10814)}]
    settings_by_fp = {
        "belt": _settings(),
        "big": _settings(quantum=7313),        # a real gap, above resolution
        "hair": _settings(quantum=6300),       # a real move, but a gap below resolution
        "flows": _settings(flows=512),         # cannot be applied
        "twin": _settings(quantum=5799),       # immaterial step
        "fresh": _settings(quantum=9000),      # nobody has measured it
    }
    out = dec.plan(
        "belt", ["big", "hair", "flows", "twin", "fresh"],
        power=power, settings_by_fp=settings_by_fp, profiles=field,
        overalls={"belt": 60.0, "big": 64.0, "hair": 60.02},
    )
    assert out["worth_racing"] == ["big"]
    assert out["below"] == ["hair"]
    assert sorted(out["refused"]) == ["flows", "twin"]
    assert out["unknown"] == ["fresh"]
    assert "can settle a margin of" in out["verdict"]
    # Every entry carries its own reason, so the page never has to invent one.
    assert all(e["why"] for e in out["entries"])


def test_plan_never_seats_the_defender_against_itself():
    out = dec.plan(
        "belt", ["belt", "other"], power=_power(),
        settings_by_fp={"belt": _settings(), "other": _settings(quantum=7313)},
        overalls={"belt": 60.0, "other": 64.0},
    )
    assert [e["fingerprint"] for e in out["entries"]] == ["other"]


# ── Wired into matchmaking ────────────────────────────────────────────────────────────


def _field(entries: dict[str, list[dict]], best: str | None = None) -> dict:
    return {
        "profiles": [
            {"fingerprint": fp, "settings": st, "overall": 60.0, "confident": True}
            for fp, st in entries.items()
        ],
        "best_fingerprint": best,
    }


def test_build_queue_drops_a_bout_that_cannot_be_applied():
    """The end-to-end point of the module: a profile differing from the defender only in a
    field PathBrain never writes never reaches the ring, because the firewall cannot be put
    on it and the leg would be measured under the wrong profile's name."""
    from pathbrain import duel as duel_mod

    field = _field({
        "belt": _settings(),
        "real": _settings(quantum=7313),
        "unappliable": _settings(flows=512),
    })
    heirs = {"items": [{"fingerprint": "real"}, {"fingerprint": "unappliable"}]}
    queue = duel_mod.build_queue(field, heirs, "belt", contenders="heirs")
    assert "real" in queue
    assert "unappliable" not in queue


def test_a_profile_with_no_settings_on_the_field_is_never_refused():
    """A lever session seats GENERATED variants, which by construction are not on the
    field — so the filter sees no settings for them. Refusing on that absence would empty
    a lever session's card entirely. (Checked on `undecidable_bouts` rather than through
    `build_queue`, because the heirs path drops unknown fingerprints for its own reasons
    and would pass this test without the filter behaving at all.)"""
    from pathbrain import duel as duel_mod

    field = _field({"belt": _settings(), "known": _settings(quantum=7313)})
    blocked = duel_mod.undecidable_bouts(field, "belt", ["known", "generated"])
    assert "generated" not in blocked


def test_nothing_is_refused_when_the_defender_itself_has_no_settings():
    from pathbrain import duel as duel_mod

    field = {"profiles": [{"fingerprint": "other", "settings": _settings()}]}
    assert duel_mod.undecidable_bouts(field, "belt", ["other"]) == {}


def test_undecidable_bouts_names_a_reason_for_every_one_it_drops():
    from pathbrain import duel as duel_mod

    field = _field({
        "belt": _settings(),
        "twin": _settings(),
        "unappliable": _settings(flows=512),
        "real": _settings(quantum=7313),
    })
    blocked = duel_mod.undecidable_bouts(field, "belt", ["twin", "unappliable", "real"])
    assert set(blocked) == {"twin", "unappliable"}
    assert all(why for why in blocked.values())
    assert "real" not in blocked


# ── The endpoint ──────────────────────────────────────────────────────────────────────


def test_the_decidability_route_is_real():
    """Asserted against the router, never by requesting the path and reading a status: the
    app mounts the built frontend as a catch-all, so an unrouted path answers 200 with
    index.html where `dist` exists and 404 where it does not. A test that reads the status
    is testing whether the frontend was built."""
    from pathbrain.main import app

    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/duel/decidability" in paths


def test_the_power_is_reported_from_the_stored_margins(client):
    """End to end: margins written by past sessions become the ladder's own resolving
    power, with no re-measurement and nothing to configure."""
    from datetime import datetime, timezone

    from pathbrain.database import session_scope
    from pathbrain.models import Duel, DuelStatus

    sessions = _synthetic(1.0, n_per=10, n_match=10, seed=11)
    with session_scope() as s:
        s.add(Duel(
            status=DuelStatus.COMPLETE,
            matchups=sessions[0]["matchups"],
            started_at=datetime.now(timezone.utc).replace(tzinfo=None),
            finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))

    body = client.get("/api/duel/decidability").json()
    assert body["power"]["sigma"] is not None
    assert 0.8 <= body["power"]["sigma"] <= 1.2, body["power"]
    assert body["power"]["min_margin"] > 0
    assert "resolves a margin of" in body["power"]["verdict"]
    # The cheap read never runs a field pass.
    assert body["card"] is None


# ── The contract with Explore ─────────────────────────────────────────────────────────


def _bet(predicted: float, uncertainty: float = 0.5) -> dict:
    return {"predicted": predicted, "uncertainty": uncertainty, "evidence": "matched_pair"}


def test_a_bet_is_told_whether_the_ring_could_ever_confirm_it():
    """The loop's missing wire: a proposal predicting a gain the referee cannot read will be
    raced, drawn, and recorded as no result — the proposal was never wrong, the instrument
    simply could not see it."""
    from pathbrain.explore import rank_bets

    bets = rank_bets(
        [_bet(64.0), _bet(60.1)], {}, best_overall=60.0, ring_resolution=0.7,
    )
    by_gain = {b["predicted_gain"]: b for b in bets}
    assert by_gain[4.0]["ring_can_confirm"] is True
    assert by_gain[0.1]["ring_can_confirm"] is False
    # And it prices what it would take, rather than only refusing.
    assert by_gain[0.1]["rounds_to_confirm"] > 30


def test_the_ring_floor_labels_bets_and_never_reorders_them():
    """A gain of 0.000001 is still a gain — the pooled crown crowns it by argmax with no
    floor. What the ring cannot confirm is a fact about the instrument, so it is a label
    beside `clears_bar`, never a filter and never a change to the order."""
    from pathbrain.explore import rank_bets

    candidates = [_bet(60.1), _bet(64.0), _bet(61.0)]
    unpriced = rank_bets(candidates, {}, best_overall=60.0)
    priced = rank_bets(candidates, {}, best_overall=60.0, ring_resolution=0.7)
    assert len(priced) == len(unpriced) == 3
    assert [b["confidence_score"] for b in priced] == [b["confidence_score"] for b in unpriced]
    assert all(b["ring_can_confirm"] is None for b in unpriced)


def test_an_unmeasured_ring_leaves_every_bet_unpriced_rather_than_refused():
    from pathbrain.explore import rank_bets

    bets = rank_bets([_bet(64.0)], {}, best_overall=60.0, ring_resolution=None)
    assert bets[0]["ring_can_confirm"] is None
    assert bets[0]["rounds_to_confirm"] is None
    assert bets[0]["predicted_gain"] == 4.0
