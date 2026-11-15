"""S24: the duel's verdict rules live in the methodology record, read through one accessor.

``methodology.verdict_rules(version)`` returns a frozen :class:`VerdictRules`. Under v17 every
rule equals the literal the duel module used to hard-code (pinned below), a version that states
none falls back to :data:`DEFAULT_VERDICT_RULES`, and a fixed bout fixture gets the same verdict
and reason at every round as it did before the move (recorded from the pre-S24 code).
"""
from __future__ import annotations

import dataclasses

import pytest

from pathbrain import duel as d
from pathbrain import methodology as m

#: The literals as they stood in duel.py before S24.
PINNED = {
    "round_weather_shift": 25.0,     # ROUND_WEATHER_SHIFT
    "weather_scale_sample": 5000,    # WEATHER_SCALE_SAMPLE
    "wilcoxon_min_pairs": 4,         # WILCOXON_MIN_PAIRS
    "wilcoxon_exact_max_pairs": 25,  # wilcoxon_p: exact when n <= 25
    "peek_penalty_slope": 3.32,      # peek_penalty: max(2.0, 3.32·ln(peeks) − 2.97)
    "peek_penalty_offset": 2.97,
    "peek_penalty_floor": 2.0,
    "sprt_null_p": 0.5,              # SprtState: ln(p1/0.5), ln((1−p1)/0.5)
    "sprt_default_p1": 0.70,         # sprt_requirements: p1 or 0.70
    "sprt_p1_min": 0.501,            # SprtState: clamp p1 to [0.501, 0.999]
    "sprt_p1_max": 0.999,
    "alpha_min": 0.001,              # SprtState / PairedEvidence: clamp alpha to [0.001, 0.2]
    "alpha_max": 0.2,
    "paired_default_alpha": 0.05,    # PairedEvidence: alpha or 0.05
}


def test_v17_states_exactly_the_old_literals():
    rules = m.verdict_rules("speed-smoothness-v17")
    assert dataclasses.asdict(rules) == PINNED
    assert m.verdict_rules() == rules  # the code's current version
    assert m.METHODOLOGY_REGISTRY["speed-smoothness-v17"]["verdict_rules"] == PINNED


@pytest.mark.parametrize("version", [
    "speed-smoothness-v1", "speed-smoothness-v6", "speed-smoothness-v16",
    "speed-smoothness-v17+sites-abc123", "a-gui-published-version-not-in-the-registry",
])
def test_every_other_version_falls_back_to_the_same_rules(version):
    assert dataclasses.asdict(m.verdict_rules(version)) == PINNED
    assert m.DEFAULT_VERDICT_RULES == m.verdict_rules("speed-smoothness-v17")


def test_the_rules_are_frozen_and_a_partial_statement_keeps_the_rest(monkeypatch):
    rules = m.verdict_rules()
    with pytest.raises(dataclasses.FrozenInstanceError):
        rules.round_weather_shift = 1.0  # type: ignore[misc]
    monkeypatch.setitem(m.METHODOLOGY_REGISTRY, "s24-partial", {"verdict_rules": {"wilcoxon_min_pairs": 6}})
    m._verdict_rules.cache_clear()
    try:
        partial = m.verdict_rules("s24-partial")
        assert partial.wilcoxon_min_pairs == 6
        assert partial.round_weather_shift == PINNED["round_weather_shift"]
    finally:
        m._verdict_rules.cache_clear()


def test_the_old_module_names_still_read_the_rules():
    rules = m.verdict_rules()
    assert d.ROUND_WEATHER_SHIFT == rules.round_weather_shift == 25.0
    assert d.WEATHER_SCALE_SAMPLE == rules.weather_scale_sample == 5000
    assert d.WILCOXON_MIN_PAIRS == rules.wilcoxon_min_pairs == 4


#: A fixed set of bouts: (method, seat dials, margins per round).
CASES = [
    ("signed_rank", dict(p1=0.7, alpha=0.05, min_margin=0.0, min_pairs=6, max_pairs=20, streak_wins=0),
     [0.8, 1.2, -0.3, 0.9, 1.5, 0.4, 1.1, 0.7, -0.2, 1.3]),
    ("signed_rank", dict(p1=0.7, alpha=0.10, min_margin=0.0, min_pairs=3, max_pairs=12, streak_wins=3),
     [0.2, 0.3, 0.1]),
    ("signed_rank", dict(p1=0.7, alpha=0.05, min_margin=1.0, min_pairs=5, max_pairs=8, streak_wins=0),
     [0.3, -0.2, 0.4, 0.1, -0.1, 0.2, 0.3, 0.05]),
    ("pair_wins", dict(p1=0.7, alpha=0.05, min_margin=0.0, min_pairs=5, max_pairs=30, streak_wins=0),
     [1.0, 0.5, 0.7, 0.2, 0.9, 1.1, 0.4, 0.6, 0.3, 0.8]),
    ("pair_wins", dict(p1=0.7, alpha=0.05, min_margin=0.0, min_pairs=5, max_pairs=10, streak_wins=0),
     [0.5, -0.5, 0.4, -0.6, 0.3, -0.2, 0.1, -0.1, 0.2, -0.3]),
]

#: What the pre-S24 code said at every round of every bout, and its readouts.
EXPECTED = {'extra': {'paired_req': {'nominal_alpha': 0.00672,
                          'peek_penalty': 7.44,
                          'restrictive': False,
                          'streak_pairs': 8,
                          'sweep_pairs': 8,
                          'win_rate_needed': None,
                          'wins_needed': None},
           'peek': [2.0, 2.9786414378371417, 4.991012305690591, 8.927282875674283],
           'sprt_req': {'restrictive': False,
                        'sweep_pairs': 9,
                        'win_rate_needed': 0.8,
                        'wins_needed': 16},
           'wil': [1.0, 0.03125, 5.9317496474919324e-05]},
 'steps': [[[None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            ['challenger', 'margins consistently one-sided (p=0.0078 ≤ 0.0083, median Δ +0.85)'],
            [None, ''],
            ['challenger', 'margins consistently one-sided (p=0.0049 ≤ 0.0083, median Δ +0.85)']],
           [[None, ''],
            [None, ''],
            ['challenger',
             '3 rounds in a row (median Δ +0.20) — the signed-rank test needs 4 rounds to say '
             'anything, so it has no opinion yet']],
           [[None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            ['challenger',
             'narrow win — ahead 6–2 on rounds, median Δ +0.15; not statistically clear '
             '(p=0.071)']],
           [[None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            ['challenger', 'SPRT boundary crossed'],
            ['challenger', 'SPRT boundary crossed']],
           [[None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            [None, ''],
            ['draw', 'no decision in 10 rounds']]]}


def _bouts():
    out = []
    for method, cfg, deltas in CASES:
        seat = d._Seat("c", {}, "", "b", **cfg)
        steps = []
        for x in deltas:
            seat.deltas.append(x)
            seat.sprt.add_pair(x > 0)
            seat.paired.add(x)
            steps.append(list(d._adjudicate(seat, method=method, min_pairs=cfg["min_pairs"],
                                            max_pairs=cfg["max_pairs"], min_margin=cfg["min_margin"])))
        out.append(steps)
    return out


def test_a_fixed_bout_gets_the_same_verdict_as_before_the_move():
    assert _bouts() == EXPECTED["steps"]


def test_the_rule_readouts_are_unchanged():
    extra = EXPECTED["extra"]
    assert [d.peek_penalty(n) for n in (2, 6, 11, 36)] == extra["peek"]
    assert [d.wilcoxon_p([1.0, 2.0, 3.0]), d.wilcoxon_p([1, 2, 3, 4, 5.0]),
            d.wilcoxon_p([0.5 * i - 3 for i in range(30)])] == extra["wil"]
    assert d.paired_requirements(0.05, 8, 30) == extra["paired_req"]
    assert d.sprt_requirements(0.7, 0.05, 5, 20) == extra["sprt_req"]
