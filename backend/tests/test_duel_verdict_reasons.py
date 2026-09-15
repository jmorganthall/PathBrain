"""**The ledger must never claim evidence it does not have.**

Reported from a phone, off the match tape of a real overnight session:

    margins consistently one-sided (p=1.0000 ≤ 0.0214, median Δ +2.05)

1.0000 is not ≤ 0.0214. The match was real and the verdict was correct — it was decided by
the *streak* rule — but `_adjudicate` wrote the *signed-rank* sentence over both rules, so
the tape credited a test that had never run. Worse, that particular 1.0000 is
`wilcoxon_p`'s "fewer than four rounds, I have no opinion" sentinel, printed as though it
were a measured result: the reader is handed the strongest possible claim by a test making
the weakest possible one.

Chasing that turned up three more of the same kind, all in what the ladder *says* rather
than what it decides. Everything here is about the reporting; no verdict changes.
"""
from __future__ import annotations

import re

import pytest

from pathbrain import decidability, duel as duel_mod
from pathbrain.duel import PairedEvidence, WILCOXON_MIN_PAIRS, wilcoxon_p

ENV = {"scheduler": "fq_codel", "queues": 1, "upload_bandwidth": "40Mbit", "flows": 1024}


def _settings(quantum: int, *, flows: int = 1024) -> list[dict]:
    return [{"label": "wan-download", **ENV, "flows": flows, "quantum": quantum},
            {"label": "wan-upload", **ENV, "flows": flows, "quantum": quantum}]


def _seat(paired: PairedEvidence):
    class _S:
        def __init__(self) -> None:
            self.paired, self.deltas, self.sprt = paired, paired.deltas, None
    return _S()


def _decide(deltas, *, streak_wins=3, min_pairs=4, max_pairs=12, alpha=0.10, min_margin=0.0):
    ev = PairedEvidence(alpha, min_margin, min_pairs, max_pairs, streak_wins=streak_wins)
    for d in deltas:
        ev.add(d)
        if ev.decision() is not None:
            break
    return ev, duel_mod._adjudicate(
        _seat(ev), method="margins", min_pairs=min_pairs, max_pairs=max_pairs,
        min_margin=min_margin,
    )


#: Every "p ≤ alpha" claim the tape can print, so a test can check the arithmetic.
_CLAIM = re.compile(r"p=([0-9.]+)\s*≤\s*([0-9.]+)")


# ── 1. The sentence names the rule that actually ended the match ──────────────


def test_a_streak_verdict_says_streak_and_never_borrows_the_p_value():
    """The reported case: three straight rounds, 2.05 points apart. The streak rule ends
    it — and the sentence must say so, because "three in a row" and "the margins are
    significant" are different claims that earn different amounts of trust."""
    ev, (verdict, reason) = _decide([2.0, 2.05, 2.1])
    assert verdict == "challenger"
    assert ev.pairs == 3, "the streak rule should end it at exactly three"
    assert "3 rounds in a row" in reason
    assert "median Δ +2.05" in reason
    # And it states where the statistical test stands rather than impersonating it.
    assert f"needs {WILCOXON_MIN_PAIRS} rounds" in reason and "no opinion" in reason
    assert "consistently one-sided" not in reason


def test_a_streak_verdict_past_the_minimum_reports_the_p_it_did_not_clear():
    """Enough rounds for the test to speak, but it hasn't cleared — the honest reading is
    "the streak ended it, and the signed-rank test does not agree yet", not silence."""
    _, (verdict, reason) = _decide([-0.6, -0.2, 0.3, 0.4, 0.9, 1.2])
    assert verdict == "challenger"
    assert "rounds in a row" in reason
    assert "has not cleared its threshold" in reason


def test_a_signed_rank_verdict_still_reads_as_one():
    """The other branch is untouched: where the test genuinely decides, it says so."""
    _, (verdict, reason) = _decide([1.4] * 8, streak_wins=0)
    assert verdict == "challenger"
    assert "margins consistently one-sided" in reason


# ── 2. The arithmetic in any claim the tape prints is true ────────────────────


@pytest.mark.parametrize("deltas,streak", [
    ([2.0, 2.05, 2.1], 3),                            # the reported match
    ([-0.6, -0.2, 0.3, 0.4, 0.9, 1.2], 3),            # the other one on that tape
    ([1.4] * 8, 0),                                   # a real signed-rank verdict
    ([-1.1] * 6, 3),                                  # the holder's side
    ([0.9, -0.4, 1.1, 1.3, 1.0, 1.2, 0.8], 0),
])
def test_the_tape_never_asserts_an_inequality_that_is_false(deltas, streak):
    """The guarantee, stated over the sentence itself: wherever the reason claims
    ``p ≤ alpha``, that has to be arithmetically true. This is the test that would have
    caught `p=1.0000 ≤ 0.0214` the day it shipped."""
    _, (verdict, reason) = _decide(deltas, streak_wins=streak)
    if verdict is None:
        return
    for p_text, alpha_text in _CLAIM.findall(reason):
        assert float(p_text) <= float(alpha_text), reason


def test_wilcoxon_is_silent_below_its_minimum_rather_than_confident():
    """`1.0` under four rounds means *the test cannot speak*, not *there is no effect* —
    the distinction the old sentence destroyed by printing it as a result."""
    assert wilcoxon_p([2.0, 2.05, 2.1], 1) == 1.0
    assert len([2.0, 2.05, 2.1]) < WILCOXON_MIN_PAIRS
    # With one more round in the same direction it is free to speak, and does.
    assert wilcoxon_p([2.0, 2.05, 2.1, 2.2], 1) < 1.0


def test_decide_reports_the_basis_and_decision_still_returns_the_verdict():
    """`decision()` keeps its contract for every existing caller; `decide()` adds the half
    the caller needed."""
    ev = PairedEvidence(0.10, 0.0, 4, 12, streak_wins=3)
    for d in (1.0, 1.1, 1.2):
        ev.add(d)
    assert ev.decide() == ("challenger", "streak")
    assert ev.decision() == "challenger"


# ── 3. The filter runs on the mode the ladder actually uses ──────────────────


def test_the_default_ring_mode_drops_undecidable_candidates():
    """The regression this PR exists for. The filter was wired into `build_queue` and
    described as covering "every mode at once" — but `"ring"`, the default and the only
    mode a nightly session runs, returns `contender_order` directly and never reaches it.
    So the guard skipped the one path that matters."""
    twin = _settings(3000)                       # identical to the defender in every field
    real = _settings(6000)                       # a genuine, raceable difference
    field = {"profiles": [
        {"fingerprint": "belt", "settings": _settings(3000), "overall": 70.0, "iterations": 40},
        {"fingerprint": "twin", "settings": twin, "overall": 71.0, "iterations": 40},
        {"fingerprint": "real", "settings": real, "overall": 69.0, "iterations": 40},
    ]}
    order = duel_mod._challenger_order(
        field, {}, "belt", mode="ring", heirs={}, baseline=_settings(3000), top_n=8,
    )
    seated = [c["fingerprint"] for c in order]
    assert "twin" not in seated, "a profile identical in every writable field cannot be raced"
    assert "real" in seated, "a real difference must still be seated"


def test_only_a_bout_that_cannot_differ_is_dropped_from_the_ring():
    """The filter's discipline holds on this path too: `below_resolution` orders, it never
    excludes, so a small-but-real difference is still raced."""
    field = {"profiles": [
        {"fingerprint": "belt", "settings": _settings(3000), "overall": 70.0, "iterations": 40},
        {"fingerprint": "near", "settings": _settings(3001), "overall": 70.1, "iterations": 40},
    ]}
    # No measured span over this lever in a two-profile field, so nothing is immaterial.
    assert decidability.cannot_differ(_settings(3001), _settings(3000),
                                      profiles=field["profiles"]) is None
    order = duel_mod._challenger_order(
        field, {}, "belt", mode="ring", heirs={}, baseline=_settings(3000), top_n=8,
    )
    assert "near" in [c["fingerprint"] for c in order]


# ── 4. An empty queue names the cause that emptied it ────────────────────────


def test_an_empty_queue_blames_the_filter_when_the_filter_emptied_it():
    """Every other reason in `_no_contenders_reason` was *true of the field* and none of
    them was why nothing could be raced — so it reported the rematch cooldown, sending the
    reader to wait out something that was never the problem."""
    field = {"profiles": [
        {"fingerprint": "belt", "name": "Hazy Alloy", "settings": _settings(3000),
         "overall": 70.0, "iterations": 40},
        {"fingerprint": "twin", "name": "Twin Peak", "settings": _settings(3000),
         "overall": 71.0, "iterations": 40},
    ]}
    dropped = duel_mod.undecidable_bouts(field, "belt", ["twin"])
    assert dropped, "the twin must be refused for the premise to hold"

    reason = duel_mod._no_contenders_reason(field, {}, "belt", _settings(3000), dropped)
    assert "undecidable" in reason
    assert "Twin Peak" in reason, "name the profile, not its fingerprint"
    assert "cooldown" not in reason


def test_without_drops_the_reason_is_unchanged():
    """The new branch is additive: with nothing refused, the existing explanations stand."""
    field = {"profiles": [{"fingerprint": "belt", "settings": _settings(3000),
                           "overall": 70.0, "iterations": 40}]}
    reason = duel_mod._no_contenders_reason(field, {}, "belt", _settings(3000), {})
    assert "only one profile has been measured" in reason


def test_queue_with_reasons_returns_what_build_queue_returns():
    """One implementation, two shapes — so the queue the engine walks and the drops the
    explanation reads can never come from different passes."""
    field = {"profiles": [
        {"fingerprint": "belt", "settings": _settings(3000), "overall": 70.0, "iterations": 40},
        {"fingerprint": "twin", "settings": _settings(3000), "overall": 71.0, "iterations": 40},
    ]}
    order, dropped = duel_mod.queue_with_reasons(
        field, {}, "belt", contenders="leaders", baseline=_settings(3000))
    assert order == duel_mod.build_queue(
        field, {}, "belt", contenders="leaders", baseline=_settings(3000))
    assert "twin" in dropped


# ── 5. The report that explains an empty ladder does not crash on one ────────


def test_the_decidability_report_answers_when_there_is_no_defender(monkeypatch):
    """`decidability_report` called `_no_contenders_reason(field, live)` — two positional
    arguments against a four-argument signature. So `GET /api/duel/decidability` raised
    TypeError on precisely the case it exists to explain: a ladder with nobody to defend."""
    from pathbrain.api import routes_settings
    from pathbrain.database import session_scope

    # `compute_profiles` / `_compute_heirs` are imported function-locally, so the patch has
    # to land on the module they come from rather than on `duel`.
    monkeypatch.setattr(routes_settings, "compute_profiles", lambda *a, **k: {"profiles": []})
    monkeypatch.setattr(routes_settings, "_compute_heirs", lambda *a, **k: {})
    monkeypatch.setattr(duel_mod, "_seeded_field", lambda s, f: f)
    monkeypatch.setattr(duel_mod, "_engine_heirs", lambda h: {})
    monkeypatch.setattr(duel_mod, "select_incumbent", lambda *a, **k: (None, "nobody"))

    with session_scope() as session:
        out = duel_mod.decidability_report(session, card=True)
    assert out["card"]["incumbent"] is None
    assert isinstance(out["card"]["verdict"], str) and out["card"]["verdict"]
