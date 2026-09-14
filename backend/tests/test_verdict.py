"""The one card: *which profile should I run?*

PathBrain could tell you a dozen true things about its field and not that one. These tests
pin the three properties that make the answer trustworthy rather than merely present:

* the answer is the **argmax**, with no floor and no hysteresis — the user's own axiom,
  which the tie set annotates and never overrides;
* the tie set is the **standings' own test**, not a second opinion about what "separated"
  means;
* the card is **cheap** — it renders on the Dashboard, and a field pass on page load is the
  documented way to take the process down.
"""
from __future__ import annotations

import copy

import pytest

from pathbrain import profile_aggregates, refresh, verdict as verdict_mod
from pathbrain.api import routes_settings
from pathbrain.config_store import get_config, save_config
from pathbrain.database import session_scope
from pathbrain.methodology import CURRENT_METHODOLOGY, ensure_current_methodology, overall_metrics
from pathbrain.models import Methodology, ProfileAggregate, Run, RunStatus, Score
from pathbrain.settings_profile import SQM_OFF_FINGERPRINT

#: The module publishes its own methodology version. The verdict is a statement *about the
#: field*, so these tests need a field they own entirely — and the obvious way to get one,
#: emptying `runs` and `scores`, is a trap: SQLite recycles rowids, so deleting another
#: suite's runs strands its `benchmark_results` against ids the next suite's runs are then
#: issued, and a later test reads someone else's raw. Scoping by version deletes nothing:
#: only rows scored under this version reach the rollup, so every other suite is simply
#: invisible here. The *definition* is the shipped one, copied — the card has to be
#: exercised against the real crown, not a stub of it.
VERSION = "verdict-test-v1"

FP_PREFIX = "verdict-test-"


def _settings(fp: str) -> list[dict]:
    """Any distinct settings will do: `list_profiles` keys on the run's stored fingerprint
    and nothing here re-derives one."""
    return [{"pipe": "wan-download", "label": "wan-download", "bandwidth": fp}]


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
         spread: float = 0.0) -> None:
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
                      settings_fingerprint=fp, settings=_settings(fp))
            s.add(run)
            s.flush()
            s.add(Score(run_id=run.id, methodology_version=VERSION, is_at_measure=False,
                        comparability="exact",
                        subscores={m: subscore + offset for m in crown},
                        axis_scores={}, weights_used={}, metric_values={}))


def _verdict() -> dict:
    with session_scope() as s:
        return verdict_mod.verdict(s)


# ── The axiom: the argmax wins, full stop ─────────────────────────────────────


def test_the_argmax_wins_even_when_the_lead_is_inside_the_noise_bar():
    """The user's north star, stated as a test: *a profile that is better, even by a tiny
    margin, is better.* The grade rounds to one decimal, so 0.1 is the smallest lead this
    arithmetic can express — and it still crowns, with the tie recorded beside it rather
    than instead of it. A card that answered "it's a tie" here would have refused to answer
    the only question it exists for."""
    _add("axiom-hi", 70.1, runs=6, spread=4.0)
    _add("axiom-lo", 70.0, runs=6, spread=4.0)

    v = _verdict()
    assert v["best"]["fingerprint"] == _fp("axiom-hi")
    assert v["lead"] == pytest.approx(0.1)
    # Inside the bar — so the card says so, and still names a profile.
    assert v["clear"] is False
    assert [p["fingerprint"] for p in v["tied"]] == [_fp("axiom-lo")]
    assert v["verdict"].startswith("Run ")


def test_a_lead_that_clears_the_noise_is_reported_as_clear_with_nobody_tied():
    """The other side of the same rule: when the evidence *does* separate the field, the
    card must say so plainly rather than hedging every answer identically."""
    _add("clear-hi", 90.0, runs=6, spread=0.2)
    _add("clear-lo", 60.0, runs=6, spread=0.2)

    v = _verdict()
    assert v["best"]["fingerprint"] == _fp("clear-hi")
    assert v["clear"] is True
    assert v["tied"] == [] and v["tied_count"] == 0
    assert "a real lead" in v["verdict"]


def test_the_tie_set_is_the_standings_own_test_not_a_second_opinion():
    """The anti-drift pin. Two components deciding separately what "tied" means is how a
    card and a chip come to contradict each other on one screen, so the verdict's pooled-SE
    test is checked against `routes_settings._clearly_better` over the very same rows —
    including the single-run case, whose spread is zero and must therefore *widen* nothing."""
    _add("mirror-a", 80.0, runs=6, spread=3.0)
    _add("mirror-b", 79.6, runs=6, spread=3.0)
    _add("mirror-c", 50.0, runs=6, spread=3.0)
    _add("mirror-thin", 79.9, runs=1)          # one run → no measurable spread

    v = _verdict()
    best = v["best"]
    tied = {p["fingerprint"] for p in v["tied"]}

    with session_scope() as s:
        field = {p["fingerprint"]: p for p in verdict_mod._graded_field(s, VERSION)}

    def _as_standings(p: dict) -> dict:
        se = p["se"]
        # `_overall_se` reads IQR/√n off p25/p75 + count; hand it a band that reproduces the
        # same SE so the two implementations are compared on identical inputs.
        band = None if se is None else se * (p["iterations"] ** 0.5)
        return {
            "overall": p["overall"],
            "count": p["iterations"],
            "overall_p25": None if band is None else 0.0,
            "overall_p75": band,
        }

    for fp, p in field.items():
        if fp == best["fingerprint"]:
            continue
        separated = routes_settings._clearly_better(
            _as_standings(field[best["fingerprint"]]), _as_standings(p),
            min_margin=0.5, sigma=2.0,
        )
        assert (fp not in tied) == separated, fp
    # And the case that makes the convention matter was actually exercised: a lone run has
    # no spread to measure, so it must contribute nothing to the bar rather than excuse
    # itself from the comparison.
    assert field[_fp("mirror-thin")]["se"] == 0.0


def test_an_unknown_spread_contributes_zero_noise_rather_than_cannot_say():
    """The defensive half of the same convention, which real rollups do not reach: a profile
    whose band could not be formed must not *inflate* the bar a rival has to clear —
    absent evidence of noise is not evidence of noise. `routes_settings._finite` makes the
    identical call, and the two must not drift."""
    known = {"se": 3.0}
    assert verdict_mod._pooled(known, {"se": None}) == pytest.approx(3.0)
    assert verdict_mod._pooled({"se": None}, {"se": None}) == 0.0


# ── Confidence: a lucky reading never holds this card ─────────────────────────


def test_a_thin_profile_never_holds_the_card_however_well_it_scored():
    """The one statement the product exists to make must not be the least reliable thing on
    screen. A five-iteration 99 is a lucky reading, not a verdict."""
    _add("thin-star", 99.0, iterations=5)
    _add("measured", 60.0, iterations=20)

    v = _verdict()
    assert v["best"]["fingerprint"] == _fp("measured")
    assert v["confident_profiles"] == 1


def test_a_field_with_nothing_confident_says_so_instead_of_crowning_a_guess():
    _add("thin-a", 99.0, iterations=5)
    _add("thin-b", 98.0, iterations=4)

    v = _verdict()
    assert v["best"] is None and v["confident_profiles"] == 0
    assert "nothing" in v["verdict"] and "15 iterations" in v["verdict"]


def test_an_empty_field_is_an_invitation_not_an_error():
    v = _verdict()
    assert v["best"] is None
    assert v["verdict"].endswith("Measure one and this becomes an answer.")


# ── SQM off: the control group prices the choice, it never wins it ────────────


def test_sqm_off_is_excluded_from_the_crown_but_prices_what_the_choice_is_worth():
    """Turning the shaper off is the baseline test's supervised job, never something this
    card recommends — but it is the only reading that says whether picking between the
    leaders matters at all next to shaping itself."""
    _add(SQM_OFF_FINGERPRINT, 80.0, iterations=40)
    _add("shaped-best", 88.0, iterations=40)
    _add("shaped-next", 84.0, iterations=40)

    v = _verdict()
    assert v["best"]["fingerprint"] == _fp("shaped-best")
    assert v["sqm_off_overall"] == pytest.approx(80.0)
    assert v["vs_sqm_off"] == pytest.approx(10.0)      # 88 over 80
    assert SQM_OFF_FINGERPRINT not in {p["fingerprint"] for p in v["tied"]}
    assert "Shaping is worth +10.0%" in v["verdict"]


def test_no_baseline_measured_means_no_claim_about_what_shaping_is_worth():
    """Absent evidence is reported as absent. A card that quietly dropped the clause would
    read as "shaping is worth nothing", which is a different statement entirely."""
    _add("only-shaped", 88.0, iterations=40)
    v = _verdict()
    assert v["sqm_off_overall"] is None and v["vs_sqm_off"] is None
    assert "Shaping is worth" not in v["verdict"]


# ── The sentence ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("build", [
    pytest.param(lambda: (_add("solo", 70.0, iterations=40),), id="alone"),
    pytest.param(lambda: (_add("hi", 90.0, runs=6, spread=0.2),
                          _add("lo", 60.0, runs=6, spread=0.2)), id="clear"),
    pytest.param(lambda: (_add("hi", 70.1, runs=6, spread=4.0),
                          _add("lo", 70.0, runs=6, spread=4.0)), id="tied"),
])
def test_the_sentence_leads_with_the_profile_in_every_branch(build):
    """The question is "what do I run?". A sentence that opens with a caveat has answered a
    different one, however true the caveat is."""
    build()
    v = _verdict()
    name = v["best"].get("name") or v["best"]["label"]
    assert v["verdict"].startswith(f"Run {name} —"), v["verdict"]


def test_a_profile_alone_in_the_field_is_named_without_a_manufactured_lead():
    _add("solo", 70.0, iterations=40)
    v = _verdict()
    assert v["runner_up"] is None and v["lead"] is None and v["clear"] is None
    assert "nothing else is measured well enough" in v["verdict"].lower()


def test_the_tied_list_is_capped_but_the_count_never_is():
    """Past a handful the list stops being information and starts being the field; the
    number of profiles the evidence cannot separate is the finding, so it is always whole."""
    _add("cap-best", 70.5, runs=6, spread=6.0)
    for i in range(verdict_mod.MAX_TIED_LISTED + 4):
        _add(f"cap-{i}", 70.0, runs=6, spread=6.0)

    v = _verdict()
    assert v["tied_count"] == verdict_mod.MAX_TIED_LISTED + 4
    assert len(v["tied"]) == verdict_mod.MAX_TIED_LISTED


# ── The one methodology shape this card cannot answer for ────────────────────


def test_a_field_relative_crown_is_declined_in_words_rather_than_answered_wrongly():
    """A **weighted** crown grades each profile on its own, which is what makes the rollup's
    medians the standings' own arithmetic. A corner/percentile crown is field-relative — one
    run re-ranks everybody — so the same medians give a different ordering, and a headline
    card that quietly disagreed with the standings would be worse than one that says it
    cannot answer. `crown_follower._needs_full_check` draws the identical line. Under
    `speed-smoothness-v16` this never fires; it fires the day somebody publishes a corner."""
    _add("corner-a", 80.0, iterations=40)
    _add("corner-b", 70.0, iterations=40)
    assert _verdict()["best"] is not None          # weighted: answered

    with session_scope() as s:
        row = s.get(Methodology, VERSION)
        definition = copy.deepcopy(row.definition)
        definition["overall"] = {**(definition.get("overall") or {}), "method": "corner"}
        row.definition = definition

    v = _verdict()
    assert v["overall_method"] == "corner"
    assert v["best"] is None and v["runner_up"] is None
    assert "Settings Impact" in v["verdict"]
    # And it declines by naming the reason, not by pretending the field is empty.
    assert "nothing to crown" not in v["verdict"]


# ── Cheap by construction ────────────────────────────────────────────────────


def test_the_card_never_runs_a_field_pass(monkeypatch):
    """This renders on the Dashboard. `compute_profiles` is the most expensive thing
    PathBrain does and holds the GIL for its whole duration — a page-load field pass is the
    documented way to take the process down, so the guarantee is worth a test rather than a
    comment."""
    def _boom(*a, **k):  # pragma: no cover — the point is that it is never reached
        raise AssertionError("the verdict card must not trigger a field pass")

    monkeypatch.setattr(routes_settings, "compute_profiles", _boom)
    monkeypatch.setattr("pathbrain.settings_profile.compute_profiles", _boom, raising=False)

    _add("cheap-a", 80.0, iterations=40)
    _add("cheap-b", 70.0, iterations=40)
    assert _verdict()["best"]["fingerprint"] == _fp("cheap-a")


def test_an_unreadable_firewall_or_ledger_costs_a_reading_not_the_answer(monkeypatch):
    """Both extras are best-effort by design: the one card the product exists to render must
    not fail because the firewall would not answer."""
    monkeypatch.setattr("pathbrain.providers.get_provider",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("firewall gone")))
    monkeypatch.setattr("pathbrain.duel._ledger_sessions",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ledger gone")))

    _add("resilient", 80.0, iterations=40)
    v = _verdict()
    assert v["best"]["fingerprint"] == _fp("resilient")
    assert v["live"] is None and v["on_firewall"] is None and v["resolves"] is None


# ── The route ────────────────────────────────────────────────────────────────


def test_the_route_serves_the_same_answer(client):
    _add("route-a", 80.0, iterations=40)
    _add("route-b", 70.0, iterations=40)

    body = client.get("/api/settings/verdict").json()
    assert body["best"]["fingerprint"] == _fp("route-a")
    assert body["verdict"].startswith("Run ")
    assert body["methodology"] == VERSION


def test_the_route_is_registered_rather_than_swallowed_by_the_spa_catch_all(client):
    """The app mounts the built frontend as a catch-all, so an unrouted path answers 200 +
    index.html where `dist/` exists and 404 where it does not — a status assertion proves
    nothing either way. Ask the router."""
    from pathbrain.main import app
    assert "/api/settings/verdict" in {getattr(r, "path", None) for r in app.routes}
