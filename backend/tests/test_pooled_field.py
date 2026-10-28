"""The pooled field, graded off the rollup — the fused ranking's anchors.

These tests pin the properties that make those anchors trustworthy rather than merely
present: the standard error is the **standings' own** (IQR over runs / √runs, floored by the
field's per-run noise), the tie test it feeds is the standings' own test and not a second
opinion about what "separated" means, the field is **cheap** (never a `compute_profiles`
pass — it is read on every Dashboard load), and SQM off is keyed **one way on every
surface**. The module was `verdict.py` with a second "which profile should I run?" endpoint
nobody rendered; `test_the_verdict_route_is_retired` pins that it stays gone.
"""
from __future__ import annotations

import copy

import pytest

from pathbrain import overall_ranking, pooled_field, profile_aggregates, refresh
from pathbrain.api import routes_settings
from pathbrain.config_store import get_config, save_config
from pathbrain.database import session_scope
from pathbrain.methodology import CURRENT_METHODOLOGY, ensure_current_methodology, overall_metrics
from pathbrain.models import Methodology, ProfileAggregate, Run, RunStatus, Score
from pathbrain.settings_profile import SQM_OFF_FINGERPRINT

#: The module publishes its own methodology version. A pooled field is a statement *about
#: the field*, so these tests need a field they own entirely — and the obvious way to get one,
#: emptying `runs` and `scores`, is a trap: SQLite recycles rowids, so deleting another
#: suite's runs strands its `benchmark_results` against ids the next suite's runs are then
#: issued, and a later test reads someone else's raw. Scoping by version deletes nothing:
#: only rows scored under this version reach the rollup, so every other suite is simply
#: invisible here. The *definition* is the shipped one, copied — the field has to be
#: exercised against the real crown, not a stub of it. (The version string keeps its old
#: name: other suites import this fixture by it.)
VERSION = "verdict-test-v1"

FP_PREFIX = "verdict-test-"


def _settings(fp: str, *, enabled: bool | None = None) -> list[dict]:
    """Any distinct settings will do: `list_profiles` keys on the run's stored fingerprint
    and nothing here re-derives one. ``enabled=False`` is a pipe with shaping switched off —
    what a baseline run recorded before the SQM-off collapse looks like."""
    pipe = {"pipe": "wan-download", "label": "wan-download", "bandwidth": fp}
    if enabled is not None:
        pipe["enabled"] = enabled
    return [pipe]


@pytest.fixture(autouse=True)
def _own_field():
    def _drop():
        with session_scope() as s:
            ids = [r for (r,) in s.query(Score.run_id).filter(
                Score.methodology_version == VERSION).all()]
            s.execute(Score.__table__.delete().where(Score.run_id.in_(ids)))
            s.execute(Run.__table__.delete().where(Run.id.in_(ids)))
            s.execute(ProfileAggregate.__table__.delete().where(
                ProfileAggregate.methodology_version == VERSION))
        refresh.invalidate_seed_cache()
        profile_aggregates.invalidate(version=VERSION)

    with session_scope() as s:
        shipped = ensure_current_methodology(s, get_config(s))
        previous = shipped.version
        row = s.get(Methodology, VERSION)
        if row is None:
            row = Methodology(version=VERSION, rubric_version=VERSION,
                              derivation_version=shipped.derivation_version, is_current=False)
            s.add(row)
        # Re-copied every test, not just on creation: a test that edits the rubric (the
        # field-relative one does) must not leave the next test grading under it.
        row.definition = copy.deepcopy(shipped.definition)
        save_config(s, {"methodology_version": VERSION})
    _drop()
    yield
    _drop()
    with session_scope() as s:
        save_config(s, {"methodology_version": previous if previous != CURRENT_METHODOLOGY else ""})
        ensure_current_methodology(s, get_config(s))


def _fp(name: str) -> str:
    return name if name == SQM_OFF_FINGERPRINT else f"{FP_PREFIX}{name}"


def _crown() -> list[str]:
    with session_scope() as s:
        definition = ensure_current_methodology(s, get_config(s)).definition or {}
    return overall_metrics(definition)[0]


def _add(fp: str, subscore: float, *, iterations: int = 20, runs: int = 1,
         spread: float = 0.0, enabled: bool | None = None) -> None:
    """`runs` completed runs on profile `fp`, each scoring `subscore` on every crown metric.

    Every crown leg carries the same value, so the weighted mean *is* that value whatever
    the methodology's weights are — the test says what it means to say without restating
    the rubric's arithmetic. `spread` fans the runs symmetrically so the profile has a real
    IQR, which is what the tie test reads.
    """
    fp = _fp(fp)
    crown = _crown()
    with session_scope() as s:
        for i in range(runs):
            offset = 0.0 if runs == 1 else (i / (runs - 1) - 0.5) * 2 * spread
            run = Run(status=RunStatus.COMPLETE, iterations=iterations,
                      settings_fingerprint=fp, settings=_settings(fp, enabled=enabled))
            s.add(run)
            s.flush()
            s.add(Score(run_id=run.id, methodology_version=VERSION, is_at_measure=False,
                        comparability="exact",
                        subscores={m: subscore + offset for m in crown},
                        axis_scores={}, weights_used={}, metric_values={}))


def _field() -> dict[str, dict]:
    with session_scope() as s:
        return {p["fingerprint"]: p for p in pooled_field.graded_field(s, VERSION)}


# ── The SE is the standings' own, and the tie test it feeds is theirs ─────────


def test_the_tie_test_the_anchors_feed_is_the_standings_own_test_not_a_second_opinion():
    """The anti-drift pin. Two components deciding separately what "tied" means is how a
    card and a chip come to contradict each other on one screen, so the pooled-SE test over
    these rows is checked against `routes_settings._clearly_better` over the very same rows —
    including the single-run case, whose own spread is zero and whose SE is therefore the
    FIELD's per-run noise (`shrinkage.profile_se`), never a zero that would let the crown be
    called clearly better than a profile measured once."""
    _add("mirror-a", 80.0, runs=6, spread=3.0)
    _add("mirror-b", 79.6, runs=6, spread=3.0)
    _add("mirror-c", 50.0, runs=6, spread=3.0)
    _add("mirror-thin", 79.9, runs=1)          # one run → no measurable spread

    field = _field()
    best = max(field.values(), key=lambda p: p["overall"])
    assert best["fingerprint"] == _fp("mirror-a")

    def _as_standings(p: dict) -> dict:
        se = p["se"]
        # `_overall_se` reads IQR/√runs off p25/p75 + count (the RUN count, as the standings
        # fill it); hand it a band that reproduces the same SE so the two implementations are
        # compared on identical inputs. An earlier version of this helper passed the
        # iteration total as the count, which is exactly the mistake the old card made — so
        # it manufactured agreement instead of testing for it.
        band = None if se is None else se * (p["runs"] ** 0.5)
        return {
            "overall": p["overall"],
            "count": p["runs"],
            "overall_p25": None if band is None else 0.0,
            "overall_p75": band,
        }

    for fp, p in field.items():
        if fp == best["fingerprint"]:
            continue
        tied_here = best["overall"] - p["overall"] <= max(0.5, 2.0 * pooled_field.pooled_se(best, p))
        separated = routes_settings._clearly_better(
            _as_standings(best), _as_standings(p), min_margin=0.5, sigma=2.0,
        )
        assert tied_here == (not separated), fp
    # And the case that makes the convention matter was actually exercised: a lone run has
    # no spread of its own, so its SE is the field's per-run noise over √1 — the same floor
    # the standings read — rather than the zero its own IQR would say.
    from pathbrain import shrinkage

    noise, _used, basis = shrinkage.run_noise([(r["iqr"], r["runs"]) for r in field.values()])
    assert basis == "fitted"
    assert field[_fp("mirror-thin")]["se"] == pytest.approx(noise) and noise > 0


def test_the_se_divides_the_run_band_by_the_run_count_not_the_iteration_total():
    """The band is over per-run Overalls — one Score per run — so its standard error is
    IQR/√runs. Two profiles with the same six runs and the same spread must carry the same
    SE however many iterations each run medianed; dividing by the iteration total (as the
    old card and the fused fit once did) read the 100-iteration profile as √5 more certain
    than the 20-iteration one on identical per-run evidence, and over-trusted every pooled
    anchor the fused ranking was handed."""
    _add("se-twenty", 80.0, runs=6, iterations=20, spread=3.0)
    _add("se-hundred", 80.0, runs=6, iterations=100, spread=3.0)

    field = _field()
    a, b = field[_fp("se-twenty")], field[_fp("se-hundred")]
    assert a["runs"] == b["runs"] == 6 and a["iterations"] == 120 and b["iterations"] == 600
    assert a["se"] == pytest.approx(b["se"]) and a["se"] > 0
    # And it IS the run-count convention the standings use: IQR over the six per-run values.
    assert a["se"] == pytest.approx(profile_aggregates.median_se(a["se"] * 6 ** 0.5, 6))
    assert profile_aggregates.median_se(3.0, 9) == pytest.approx(1.0)
    assert profile_aggregates.median_se(3.0, 0) is None and profile_aggregates.median_se(None, 9) is None


def test_an_unknown_spread_contributes_zero_noise_rather_than_cannot_say():
    """The defensive half of the same convention, which real rollups do not reach: a profile
    whose band could not be formed must not *inflate* the bar a rival has to clear —
    absent evidence of noise is not evidence of noise. `routes_settings._finite` makes the
    identical call, and the two must not drift."""
    known = {"se": 3.0}
    assert pooled_field.pooled_se(known, {"se": None}) == pytest.approx(3.0)
    assert pooled_field.pooled_se({"se": None}, {"se": None}) == 0.0


# ── SQM off is keyed one way on every surface ────────────────────────────────


def test_is_sqm_off_helpers():
    from pathbrain.settings_profile import is_sqm_off, is_sqm_off_profile

    assert is_sqm_off([{"enabled": False}, {"enabled": True}]) is True
    assert is_sqm_off([{"enabled": True}]) is False
    assert is_sqm_off([{}]) is False and is_sqm_off(None) is False and is_sqm_off([None]) is False
    assert is_sqm_off_profile(SQM_OFF_FINGERPRINT) is True
    assert is_sqm_off_profile("abc123", [{"enabled": False}]) is True
    assert is_sqm_off_profile("abc123", [{"enabled": True}]) is False
    assert is_sqm_off_profile("abc123") is False


def test_sqm_off_is_keyed_the_same_way_on_every_surface():
    """A baseline run recorded before the SQM-off collapse carries its own fingerprint, not
    `SQM_OFF_FINGERPRINT`, while its stored settings still say a pipe is disabled. Settings
    Impact read the settings and called it the baseline; the pooled field and the fused
    ranking read the fingerprint alone and let it contend for the crown — on one screen, the
    same profile was the control group and the leader. One predicate
    (`settings_profile.is_sqm_off_profile`) now answers on every surface."""
    _add("legacy-off", 95.0, iterations=40, runs=4, spread=1.0, enabled=False)
    _add("shaped-best", 88.0, iterations=40, runs=4, spread=1.0)
    _add("shaped-next", 84.0, iterations=40, runs=4, spread=1.0)
    _add(SQM_OFF_FINGERPRINT, 80.0, iterations=40, runs=4, spread=1.0)

    field = _field()
    assert field[_fp("legacy-off")]["is_sqm_off"] is True
    assert field[SQM_OFF_FINGERPRINT]["is_sqm_off"] is True
    assert field[_fp("shaped-best")]["is_sqm_off"] is False

    with session_scope() as s:
        out = overall_ranking.ranking(s, backtest=False, live=False)
    rows = {r["fingerprint"]: r for r in out["profiles"]}
    assert rows[_fp("legacy-off")]["is_sqm_off"] is True
    assert rows[_fp("legacy-off")]["eligible"] is False
    assert out["best"]["fingerprint"] == _fp("shaped-best"), "the highest-scoring baseline never wins"
    assert _fp("legacy-off") not in {r["fingerprint"] for r in out["profiles"] if r["eligible"]}
    # Settings Impact's own reading of the same stored-settings case is pinned by
    # `test_settings.test_pct_vs_sqm_off_is_computed_from_overall`; `compute_profiles` now
    # calls the same `is_sqm_off_profile`, so the three surfaces share one predicate.


# ── Cheap by construction ────────────────────────────────────────────────────


def test_graded_field_never_runs_a_field_pass(monkeypatch):
    """This is read on every Dashboard load through `GET /api/overall`. `compute_profiles`
    is the most expensive thing PathBrain does and holds the GIL for its whole duration — a
    page-load field pass is the documented way to take the process down, so the guarantee
    is worth a test rather than a comment."""
    def _boom(*a, **k):  # pragma: no cover — the point is that it is never reached
        raise AssertionError("the pooled field must not trigger a field pass")

    monkeypatch.setattr(routes_settings, "compute_profiles", _boom)
    monkeypatch.setattr("pathbrain.settings_profile.compute_profiles", _boom, raising=False)

    _add("cheap-a", 80.0, iterations=40)
    _add("cheap-b", 70.0, iterations=40)
    field = _field()
    assert max(field.values(), key=lambda p: p["overall"])["fingerprint"] == _fp("cheap-a")


# ── The route is gone ────────────────────────────────────────────────────────


def test_the_verdict_route_is_retired():
    """`GET /api/settings/verdict` was a second "which profile should I run?" answer — the
    pooled argmax with a sentence — that no page read: the Dashboard's verdict card reads
    `GET /api/overall`, the fused ranking the crowning policy acts on. Two "the answer"
    endpoints with one dead is how a card and a chip come to disagree, so it stays gone.
    The app mounts the built frontend as a catch-all, so a status assertion proves nothing
    either way — ask the router."""
    from pathbrain.main import app

    assert "/api/settings/verdict" not in {getattr(r, "path", None) for r in app.routes}
    assert "/api/overall" in {getattr(r, "path", None) for r in app.routes}


# ── The mathematical crown: lowest median FCP × median LCP, raw ms ─────────────


def _add_raw(name: str, fcp: float, lcp: float, *, iterations: int = 20, runs: int = 3,
             enabled: bool | None = None) -> None:
    fp = _fp(name)
    crown = _crown()
    with session_scope() as s:
        for _ in range(runs):
            run = Run(status=RunStatus.COMPLETE, iterations=iterations,
                      settings_fingerprint=fp, settings=_settings(fp, enabled=enabled))
            s.add(run)
            s.flush()
            s.add(Score(run_id=run.id, methodology_version=VERSION, is_at_measure=False,
                        comparability="exact", subscores={m: 80.0 for m in crown},
                        axis_scores={}, weights_used={},
                        metric_values={"fcp": fcp, "lcp": lcp}))


def test_the_math_crown_is_the_lowest_fcp_times_lcp_among_confident_profiles():
    """Pure milliseconds: the product decides, so a profile best on only one leg loses to
    one that is close on both. A thin profile and the unshaped baseline are never it."""
    _add_raw("balanced", fcp=300.0, lcp=700.0)          # 210,000
    _add_raw("fast_fcp", fcp=250.0, lcp=900.0)          # 225,000 — best FCP, loses
    _add_raw("thin", fcp=100.0, lcp=200.0, iterations=2, runs=1)   # below the bar
    _add_raw(SQM_OFF_FINGERPRINT, fcp=90.0, lcp=150.0)  # the baseline, never crowned
    with session_scope() as s:
        crown = pooled_field.math_crown(s, VERSION, 15)
    assert crown is not None and crown["fingerprint"] == _fp("balanced")
    assert (crown["fcp_ms"], crown["lcp_ms"], crown["product"]) == (300.0, 700.0, 210000.0)
    assert crown["runner_up"]["fingerprint"] == _fp("fast_fcp")
    assert crown["profiles"] == 2


def test_the_rollup_keeps_raw_medians_and_rebuilds_a_row_written_before_them():
    _add_raw("old", fcp=320.0, lcp=710.0)
    with session_scope() as s:
        agg = profile_aggregates.aggregates(s, VERSION, [_fp("old")])
    assert agg[_fp("old")]["raw"]["fcp"]["median"] == 320.0
    # A row persisted before the column existed reads raw=None and is rebuilt, once.
    with session_scope() as s:
        row = s.query(ProfileAggregate).filter(
            ProfileAggregate.methodology_version == VERSION,
            ProfileAggregate.settings_fingerprint == _fp("old")).one()
        row.raw = None
    with session_scope() as s:
        agg = profile_aggregates.aggregates(s, VERSION, [_fp("old")])
    assert agg[_fp("old")]["raw"]["lcp"]["median"] == 710.0
    with session_scope() as s:
        row = s.query(ProfileAggregate).filter(
            ProfileAggregate.methodology_version == VERSION,
            ProfileAggregate.settings_fingerprint == _fp("old")).one()
        assert row.raw and row.raw["lcp"]["median"] == 710.0


def test_the_overall_corners_carry_the_math_crown_and_agree_ignores_it():
    _add_raw("balanced", fcp=300.0, lcp=700.0)
    _add_raw("fast_fcp", fcp=250.0, lcp=900.0)
    with session_scope() as s:
        out = overall_ranking.ranking(s, backtest=False)
    corners = out["corners"]
    assert corners["math"]["fingerprint"] == _fp("balanced")
    assert set(corners) >= {"pooled", "ring", "fused", "math", "agree"}
