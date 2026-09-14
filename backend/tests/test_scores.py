"""Tests for the (run × methodology) Score table (Phase 2)."""
from __future__ import annotations

from sqlalchemy import select

from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.methodology import (
    ensure_current_methodology,
    record_at_measure,
    score_fields_from_score_result,
)
from pathbrain.models import Run, RunStatus, Score, ScoreResult


def _score_for(session, run_id: int, version: str) -> Score | None:
    return session.scalar(
        select(Score).where(Score.run_id == run_id, Score.methodology_version == version)
    )


def test_score_fields_merge_both_axes():
    sr = ScoreResult(
        run_id=1, sops=78.0, sops_stdev=1.0, sops_min=76.0, sops_max=80.0,
        subscores={"byte_earliness": 90.0}, weights_used={"byte_earliness": 1.0},
        metric_values={"byte_earliness_ms": 351.0, "longest_stall": 398.0},
        completion=70.0, completion_subscores={"dns": 88.0},
        completion_weights_used={"dns": 1.0}, completion_metric_values={"lookup_ms": 12.0},
    )
    f = score_fields_from_score_result(sr)
    assert f["axis_scores"] == {"sops": 78.0, "completion": 70.0}
    assert f["subscores"] == {"byte_earliness": 90.0, "dns": 88.0}
    assert f["metric_values"]["lookup_ms"] == 12.0 and f["metric_values"]["longest_stall"] == 398.0
    assert f["bands"]["sops"] == {"stdev": 1.0, "min": 76.0, "max": 80.0}
    assert f["comparability"] == "exact"  # carries the longest_stall marker


def test_legacy_run_is_incomparable_at_measure():
    # A run lacking the current-rubric marker is incomparable under its methodology.
    sr = ScoreResult(
        run_id=1, sops=95.0, subscores={"fcp": 90.0}, weights_used={"fcp": 1.0},
        metric_values={"fcp": 300.0},  # no longest_stall -> legacy
    )
    f = score_fields_from_score_result(sr)
    assert f["comparability"] == "incomparable"
    assert "longest_stall" in (f["missing_metrics"] or [])


def test_record_at_measure_writes_score_and_stamps_run():
    with session_scope() as s:
        version = ensure_current_methodology(s, get_config(s)).version
        run = Run(status=RunStatus.COMPLETE)
        s.add(run)
        s.flush()
        sr = ScoreResult(
            run_id=run.id, sops=72.0, rubric_version=version,
            subscores={"longest_stall": 50.0}, weights_used={"longest_stall": 1.0},
            metric_values={"longest_stall": 300.0},
        )
        s.add(sr)
        score = record_at_measure(s, run, sr, version)
        s.commit()
        rid = run.id

    with session_scope() as s:
        row = _score_for(s, rid, version)
        assert row is not None and row.is_at_measure and row.comparability == "exact"
        assert row.axis_scores["sops"] == 72.0
        assert s.get(Run, rid).methodology_version == version


# ── The Dashboard's Overall card is wired to the methodology, not to a frozen definition ──
#
# The hero card used to show the three headline AXES under the Overall gauge, captioned as
# "the axes the Overall is built from". That stopped being true at v5, when the Overall
# became a first-class quantity computed from the crown metrics. By v16 the axes are
# dominated by metrics the Overall never reads (render, load_event, cadence, evenness, byte
# earliness, CLS), and the Completion axis it also rendered had lost every one of its
# metrics — a headline describing a rubric that had not been current for months.


def test_rolling_reports_the_crown_the_overall_is_actually_computed_from(client):
    """The card reads these; if they are absent it falls back to a static list, which is
    exactly the failure mode. They must come off the methodology on every request."""
    from pathbrain.methodology import (
        CURRENT_METHODOLOGY,
        METHODOLOGY_REGISTRY,
        overall_metrics,
        build_definition_from_spec,
    )

    body = client.get("/api/score/rolling?hours=24").json()
    for key in ("overall_metrics", "overall_method", "overall_weights"):
        assert key in body, f"the rolling payload must carry {key}"

    definition = build_definition_from_spec(METHODOLOGY_REGISTRY[CURRENT_METHODOLOGY])
    expected, _required = overall_metrics(definition)
    assert body["overall_metrics"] == expected
    # Under v16 that is the browser crown, and specifically NOT the axis keys.
    assert set(body["overall_metrics"]).isdisjoint({a["key"] for a in body["axes"]})


def test_the_crown_follows_the_methodology_rather_than_a_frozen_list(client, monkeypatch):
    """The point of the fix: publish a different rubric and the card re-points itself.

    Driven through the real endpoint against a real registry entry, so a future version
    that changes its crown is picked up with no frontend edit — which is the property the
    Settings-Impact view already has and this card did not.
    """
    from pathbrain import methodology as m
    from pathbrain.api import routes_score

    # v13 corners over the same three; v6 is a genuinely different set, which is what makes
    # this a test of wiring rather than of a coincidence.
    v6 = m.build_definition_from_spec(m.METHODOLOGY_REGISTRY["speed-smoothness-v6"])
    v6_keys, _ = m.overall_metrics(v6)
    assert v6_keys == ["fcp", "total_stall", "load_event"]

    class _Stub:
        version = "speed-smoothness-v6"
        definition = v6

    monkeypatch.setattr(routes_score, "_window_scores", lambda *a, **k: (_Stub(), []))
    body = client.get("/api/score/rolling?hours=24").json()
    assert body["overall_metrics"] == v6_keys
    assert body["methodology"] == "speed-smoothness-v6"
    # Weights are reported too: they are what "×1.0" under a gauge means, and a corner
    # methodology has none, so the card must be told which it is looking at.
    assert body["overall_method"] == m.overall_method(v6)


def test_an_empty_window_still_describes_the_crown(client, monkeypatch):
    """With no runs the card still has to say what it *would* be showing — otherwise a
    quiet night renders a headline with no legs and no explanation."""
    from pathbrain import methodology as m
    from pathbrain.api import routes_score

    current = m.build_definition_from_spec(m.METHODOLOGY_REGISTRY[m.CURRENT_METHODOLOGY])

    class _Stub:
        version = m.CURRENT_METHODOLOGY
        definition = current

    monkeypatch.setattr(routes_score, "_window_scores", lambda *a, **k: (_Stub(), []))
    body = client.get("/api/score/rolling?hours=24").json()
    assert body["count"] == 0
    assert body["overall_metrics"] == m.overall_metrics(current)[0]


# ── the over-time chart trends the crown legs, not the axes ───────────────────


def test_the_over_time_series_is_the_crown_not_the_axes(client):
    """The same correction the hero card carries, applied to "Scores over time".

    It trended Overall beside Responsiveness / Smoothness / Speed, which invites the one
    wrong reading available: that the top line is a roll-up of the three under it. It
    stopped being that at v5, and by v16 those axes are dominated by metrics the Overall
    never reads — so a dip in the leg that actually moved the Overall could sit in none of
    the lines drawn.
    """
    from pathbrain.methodology import (
        CURRENT_METHODOLOGY,
        METHODOLOGY_REGISTRY,
        build_definition_from_spec,
        overall_metrics,
        scored_axes,
    )

    body = client.get("/api/score/axis-series?limit=5").json()
    definition = build_definition_from_spec(METHODOLOGY_REGISTRY[CURRENT_METHODOLOGY])
    crown, _required = overall_metrics(definition)

    headline = [s for s in body["axes"] if s["role"] == "headline"]
    assert [s["key"] for s in headline] == ["overall", *crown], (
        "the headline series must be the Overall and the legs it is computed from"
    )
    # The axes are kept, and demoted — a real decomposition, no longer presented as the
    # Overall's parts. They are already on the Score row, so carrying them costs nothing and
    # dropping a series the response shape already had would be a silent loss.
    axis_keys = {a["key"] for a in scored_axes(definition)}
    demoted = {s["key"] for s in body["axes"] if s["role"] == "axis"}
    assert demoted == axis_keys and not axis_keys & {s["key"] for s in headline}
    # Each series says where its number comes from, so the point builder never has to guess
    # which dict a key lives in — a crown leg is a subscore, an axis is an axis score.
    assert {s["source"] for s in headline if s["key"] != "overall"} == {"metric"}
    assert {s["source"] for s in body["axes"] if s["role"] == "axis"} == {"axis"}


def test_the_over_time_series_follows_a_publish_with_no_frontend_edit(client, monkeypatch):
    """Point the endpoint at another rubric and the chart re-points itself.

    The frontend filters on ``role`` and never names a metric, so this is the whole of the
    wiring: if the server's headline set follows the methodology, so does the chart.
    """
    from pathbrain import methodology as m

    v6 = m.build_definition_from_spec(m.METHODOLOGY_REGISTRY["speed-smoothness-v6"])
    v6_keys, _ = m.overall_metrics(v6)
    assert v6_keys == ["fcp", "total_stall", "load_event"]  # a genuinely different set

    class _Stub:
        version = "speed-smoothness-v6"
        definition = v6

    # The route imports this inside the function, so the source module is what to patch —
    # patching the route module would silently not apply and the test would pass on the
    # current methodology instead, which is the assertion it exists to avoid.
    monkeypatch.setattr(m, "ensure_current_methodology", lambda *a, **k: _Stub())
    body = client.get("/api/score/axis-series?limit=5").json()
    assert [s["key"] for s in body["axes"] if s["role"] == "headline"] == ["overall", *v6_keys]
    assert body["overall_metrics"] == v6_keys
    assert body["overall_method"] == m.overall_method(v6)


def test_every_crown_leg_is_labelled_and_weighted_from_the_frozen_definition(client):
    """A chart legend of raw metric keys is not a legend, and the weights are what make
    "these three, unevenly" legible. Both come from the definition rather than the live
    registry: the definition is the snapshot the version was published with."""
    body = client.get("/api/score/axis-series?limit=5").json()
    legs = [s for s in body["axes"] if s["role"] == "headline" and s["key"] != "overall"]
    assert legs
    for leg in legs:
        assert leg["label"] and leg["label"] != leg["key"], f"{leg['key']} has no label"
        assert leg["weight"] == body["overall_weights"].get(leg["key"])
