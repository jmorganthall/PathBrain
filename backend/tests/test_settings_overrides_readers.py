"""S22b: every reader sees a run's *effective* settings — its newest override, else its row.

Three groups of reader, one test each, each seeing an override-only run (row settings and
fingerprint NULL, both supplied by overrides) as stamped: a join reader (the rolling score
window, Score ⋈ Run), a single-run route (``/results/{id}``) and the research export (which
also carries the raw row values and the override rows). Then the refresh cache, which must
notice an override that re-keys an old run, ``/settings/refingerprint``, which re-keys an
overridden run by appending an override (never writing its row), and a ratchet on direct
reads of the row columns so a new one fails CI.
"""
from __future__ import annotations

import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from pathbrain import refresh, research_export
from pathbrain.api.routes_score import _window_scores
from pathbrain.config_store import get_config
from pathbrain.database import session_scope
from pathbrain.methodology import ensure_current_methodology
from pathbrain.models import Run, RunStatus, Score, SettingsOverride
from pathbrain.settings_overrides import record

FP = "s22b-override-only"
SETTINGS = [{"label": "wan", "enabled": True, "quantum": 1514}]


@pytest.fixture()
def override_only():
    """One completed, scored run whose row carries no settings; overrides supply both."""
    with session_scope() as s:
        version = ensure_current_methodology(s, get_config(s)).version
        run = Run(status=RunStatus.COMPLETE, iterations=2, iterations_completed=2,
                  created_at=datetime.now(timezone.utc).replace(tzinfo=None),
                  methodology_version=version)
        s.add(run)
        s.flush()
        s.add(Score(run_id=run.id, methodology_version=version, is_at_measure=True,
                    comparability="exact", axis_scores={"overall": 77.0},
                    subscores={}, weights_used={}, metric_values={}))
        record(s, run.id, "settings", SETTINGS, source="test", reason="S22b reader test")
        record(s, run.id, "settings_fingerprint", FP, source="test", reason="S22b reader test")
        rid = run.id
    yield rid
    with session_scope() as s:
        s.execute(delete(SettingsOverride).where(SettingsOverride.run_id == rid))
        s.execute(delete(Score).where(Score.run_id == rid))
        s.execute(delete(Run).where(Run.id == rid))
    refresh.invalidate_seed_cache()


def test_a_join_reader_sees_the_override_only_run_as_stamped(override_only):
    with session_scope() as s:
        _, rows = _window_scores(s, 1, FP)
        assert [r.run_id for r in rows] == [override_only]
        assert s.get(Run, override_only).settings_fingerprint is None  # the row is untouched


def test_a_single_run_route_sees_the_override_only_run_as_stamped(override_only, client):
    body = client.get(f"/api/results/{override_only}").json()
    assert body["settings_fingerprint"] == FP
    assert body["settings"] == SETTINGS
    assert client.get("/api/history/count", params={"fingerprint": FP}).json()["count"] == 1
    with session_scope() as s:
        run = s.get(Run, override_only)
        assert run.settings is None and run.settings_fingerprint is None  # nothing flushed back


def test_the_research_export_carries_effective_and_raw_values_and_the_overrides(
    override_only, tmp_path, monkeypatch
):
    monkeypatch.setattr(research_export, "_DIR_OVERRIDE", tmp_path)
    with session_scope() as s:
        path = research_export.build(s)
    with zipfile.ZipFile(path) as zf:
        files = {n: zf.read(n).decode() for n in zf.namelist()}
    lines = lambda name: [json.loads(line) for line in files[name].splitlines()]  # noqa: E731
    manifest = json.loads(files["manifest.json"])
    assert manifest["counts"]["settings_overrides.jsonl"] == len(lines("settings_overrides.jsonl"))

    run = next(r for r in lines("runs.jsonl") if r["id"] == override_only)
    assert run["fingerprint"] == FP and run["fingerprint_raw"] is None
    prof = next(p for p in lines("profiles.jsonl") if p["fingerprint"] == FP)
    assert prof["settings"] == SETTINGS and prof["settings_raw"] is None and prof["runs"] == 1
    ours = [o for o in lines("settings_overrides.jsonl") if o["run_id"] == override_only]
    assert [(o["field"], o["value"], o["source"]) for o in ours] == [
        ("settings", SETTINGS, "test"), ("settings_fingerprint", FP, "test"),
    ]


def test_the_profile_cache_notices_an_override_on_an_old_run(override_only):
    refresh.invalidate_seed_cache()
    with session_scope() as s:
        assert FP in refresh._latest_settings_by_fingerprint(s)
        # Re-key the run: neither the max run id nor the count moves.
        record(s, override_only, "settings_fingerprint", FP + "-moved", source="test")
    with session_scope() as s:
        latest = refresh._latest_settings_by_fingerprint(s)
        assert FP + "-moved" in latest and FP not in latest


def test_refingerprint_appends_an_override_for_an_overridden_run(override_only, client, monkeypatch):
    from pathbrain import crown_follower
    from pathbrain.settings_profile import fingerprint

    # A re-key wakes the crown follower; keep its background check out of the shared suite.
    monkeypatch.setattr(crown_follower, "poke", lambda *a, **k: None)

    body = client.post("/api/settings/refingerprint").json()
    assert body["rekeyed"] >= 1
    with session_scope() as s:
        run = s.get(Run, override_only)
        assert run.settings is None and run.settings_fingerprint is None  # row never written
        rows = s.scalars(
            select(SettingsOverride).where(SettingsOverride.run_id == override_only)
            .order_by(SettingsOverride.id)
        ).all()
        assert [(r.field, r.source) for r in rows] == [
            ("settings", "test"), ("settings_fingerprint", "test"),
            ("settings_fingerprint", "refingerprint"),
        ]
        assert rows[-1].value == fingerprint(SETTINGS)
    # Idempotent: a second pass finds this run already keyed.
    client.post("/api/settings/refingerprint")
    with session_scope() as s:
        assert len(s.scalars(
            select(SettingsOverride).where(SettingsOverride.run_id == override_only)
        ).all()) == 3


# ── the ratchet ──────────────────────────────────────────────────────────────

PACKAGE = Path(__file__).resolve().parents[1] / "pathbrain"
DIRECT = re.compile(r"\bRun\.settings(?:_fingerprint)?\b|\brun\.settings_fingerprint\b")

#: file (relative to pathbrain/) → (count, why). ``settings_overrides.py`` is the accessor
#: and is not scanned. ``run.settings_fingerprint`` on a Run object is fine *after* an
#: ``overlay``; those uses are counted here so a new one has to say which it is.
ALLOWED: dict[str, tuple[int, str]] = {
    "runner.py": (3, "capture: the write of the just-captured run's fingerprint, and two "
                     "reads of that same run on the capture path (no override can exist yet)"),
    "portable.py": (2, "record_server_run copies the just-captured run's own value at capture; "
                       "serialize_run reads a PortableRun, not a Run"),
    "api/routes_settings.py": (5, "refingerprint: the row-only in-place re-key (write) and the "
                                  "row fallbacks beside overrides_for (3); two reads of runs "
                                  "from _completed_runs_with_scores, which overlays"),
    "research_export.py": (2, "the raw row values exported beside the effective ones"),
    "ranking.py": (5, "reads of runs overlaid in _completed_runs_with_scores (and a comment)"),
    "api/routes_results.py": (2, "_serialize_run overlays first; the baseline overlays its rows"),
    "api/routes_history.py": (1, "history_dump overlays its runs"),
    "api/routes_smoothness.py": (2, "the single run and the compared rows are overlaid"),
}


def _counts() -> dict[str, int]:
    out: dict[str, int] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE).as_posix()
        if rel == "settings_overrides.py":
            continue
        n = len(DIRECT.findall(path.read_text(encoding="utf-8")))
        if n:
            out[rel] = n
    return out


def test_no_new_direct_read_of_the_run_settings_columns():
    counts = _counts()
    grown = {f: n for f, n in counts.items() if n > ALLOWED.get(f, (0, ""))[0]}
    assert not grown, (
        "Direct reads of Run.settings / Run.settings_fingerprint / run.settings_fingerprint "
        f"grew: {grown}. Read effective values through settings_overrides "
        "(effective_fingerprint / effective_settings / overlay), or, for a capture write or a "
        "read that must see the raw row, add it to ALLOWED with why."
    )


def test_the_ratchet_allow_list_is_not_stale():
    counts = _counts()
    stale = {f: (counts.get(f, 0), n) for f, (n, _) in ALLOWED.items() if counts.get(f, 0) < n}
    assert not stale, f"lower these ALLOWED counts to what is left (found, allowed): {stale}"
