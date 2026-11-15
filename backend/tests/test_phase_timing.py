"""Request-scoped laps: booked inside a recording, free and silent outside one."""
from pathbrain import phase_timing


def test_laps_outside_a_recording_do_nothing():
    lap = phase_timing.laps("x")
    lap("a")  # no recording open: nothing to book into, and nothing raises
    assert lap is phase_timing._noop


def test_laps_book_each_step_under_its_prefix_and_sum_repeats():
    with phase_timing.recording() as rec:
        for _ in range(2):
            lap = phase_timing.laps("ranking")
            lap("fit")
            lap("verdict")
    assert list(rec) == ["ranking-fit", "ranking-verdict"]
    assert rec["ranking-fit"][1] == 2
    assert all(ms >= 0 for ms, _n in rec.values())


def test_a_recording_ends_with_its_block():
    with phase_timing.recording():
        pass
    assert phase_timing._REC.get() is None
