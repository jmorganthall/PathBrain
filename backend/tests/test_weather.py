"""Tests for the measured-weather severity + cohort residual ("wins above the weather")."""
from __future__ import annotations

from pathbrain.weather import (
    WEATHER_MIN_COVARIATES,
    cohort_residuals,
    run_severities,
)


def test_run_severity_ranks_conditions_by_own_covariates():
    covs = ["dns", "tls", "latency"]
    # Three calm runs (low times) + three harsh runs (high times) + one thin run (too few covs).
    runs = [
        {"dns": 1.0, "tls": 20.0, "latency": 10.0},
        {"dns": 1.1, "tls": 21.0, "latency": 11.0},
        {"dns": 0.9, "tls": 19.0, "latency": 9.0},
        {"dns": 4.0, "tls": 45.0, "latency": 25.0},
        {"dns": 4.2, "tls": 50.0, "latency": 28.0},
        {"dns": 3.8, "tls": 42.0, "latency": 24.0},
        {"dns": 2.0},  # only 1 covariate < WEATHER_MIN_COVARIATES → no severity
    ]
    sev = run_severities(runs, covs)
    assert sev[6] is None  # thin run gets no severity, never a fabricated one
    calm, harsh = sev[:3], sev[3:6]
    assert all(s is not None for s in calm + harsh)
    # Every harsh run ranks above every calm run ("DNS at 4ms instead of 1" = high severity).
    assert min(harsh) > max(calm)
    assert WEATHER_MIN_COVARIATES == 3


def test_cohort_residual_flags_the_weather_beater():
    """A profile measured only in harsh weather, delivering average-when-others-drop outcomes,
    gets a positive residual — while raw pooling would rank it at the bottom."""
    fps, overalls, sevs = [], [], []
    # The field: 4 profiles measured in calm weather (severity 10) scoring ~80.
    for i in range(4):
        for _ in range(5):
            fps.append(f"calm-{i}")
            overalls.append(80.0)
            sevs.append(10.0)
    # The same 4 profiles also measured in harsh weather (severity 90) scoring ~50 (weather hurts).
    for i in range(4):
        for _ in range(5):
            fps.append(f"calm-{i}")
            overalls.append(50.0)
            sevs.append(90.0)
    # The candidate: measured ONLY in harsh weather, but scoring 60 where others score 50.
    for _ in range(5):
        fps.append("candidate")
        overalls.append(60.0)
        sevs.append(90.0)

    residuals, sev_medians = cohort_residuals(fps, overalls, sevs)
    cand = residuals["candidate"]
    # +10 over what the field delivers in the same weather, full coverage.
    assert cand["delta_median"] == 10.0
    assert cand["coverage"] == 1.0
    # Its raw pooled overall (60) is the worst in the field — the residual is the only view
    # that surfaces it. And its severity median records the harsh sampling.
    assert sev_medians["candidate"] == 90.0
    # The calm profiles' residuals hover near zero (they ARE the field).
    for i in range(4):
        assert abs(residuals[f"calm-{i}"]["delta_median"]) <= 5.0


def test_cohort_excludes_own_runs_so_a_burst_cannot_define_its_typical():
    fps, overalls, sevs = [], [], []
    # A dominant profile with a big burst in one band scoring 40.
    for _ in range(20):
        fps.append("burst")
        overalls.append(40.0)
        sevs.append(50.0)
    # A handful of other-profile runs in the same band scoring 70.
    for i in range(4):
        fps.append(f"other-{i}")
        overalls.append(70.0)
        sevs.append(50.0)

    residuals, _ = cohort_residuals(fps, overalls, sevs)
    # The burst profile is judged against the OTHERS' 70, not its own 40s: residual -30.
    assert residuals["burst"]["delta_median"] == -30.0


def test_no_cohort_no_claim():
    # A profile alone in its weather band (no other-profile runs) gets no residual.
    fps = ["solo"] * 6
    overalls = [80.0] * 6
    sevs = [50.0] * 6
    residuals, sev_medians = cohort_residuals(fps, overalls, sevs)
    assert "solo" not in residuals
    assert sev_medians["solo"] == 50.0


def test_profiles_endpoint_carries_weather_fields(client):
    body = client.get("/api/settings/profiles").json()
    assert "weather_crown_suspect" in body
    for p in body["profiles"]:
        assert "weather_relative" in p
        assert "weather_severity" in p
        assert "weather_beater" in p
        if p["weather_relative"] is not None:
            assert set(p["weather_relative"]) >= {"delta_median", "p25", "p75", "count", "coverage"}
