"""The site list as part of the methodology, and seeding from the prior version.

Every browser metric is a mean over the pages loaded, so a methodology that declares its
sites quarantines runs measured against any other set; and right after a publish, when
nothing is comparable yet, the prior version's standings order who gets measured first.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from pathbrain import challenger, duel, refresh
from pathbrain.api import routes_settings as rs
from pathbrain.config_store import get_config, save_config
from pathbrain.database import session_scope
from pathbrain.methodology import (
    CURRENT_METHODOLOGY,
    METHODOLOGY_REGISTRY,
    SITE_SET_MARKER,
    apply_collection,
    build_definition_from_spec,
    collection,
    collection_from_lists,
    comparability,
    ensure_current_methodology,
    publish_sites,
    site_set_from_config,
)
from pathbrain.models import Methodology, Run, RunStatus, Score
from pathbrain.runner import create_run, score_metrics_under

SITES_A = ["https://a.example/", "https://b.example/"]
SITES_B = ["https://a.example/", "https://c.example/"]


def _definition_with(browser, http=()):
    d = build_definition_from_spec(METHODOLOGY_REGISTRY[CURRENT_METHODOLOGY])
    return {**d, "collection": collection_from_lists(list(browser), list(http))}


def _complete_metrics(definition) -> dict:
    """A metric_values dict with every scored metric present (all comparable)."""
    return {m["key"]: 1.0 for m in definition["metrics"] if m.get("axis")}


def _restore_scoring_state(pin, browser_urls, http_urls):
    """Put the shared test DB back on the shipped methodology with the config's own URLs."""
    with session_scope() as s:
        save_config(s, {
            "methodology_version": pin,
            "browser": {"urls": browser_urls},
            "http": {"urls": http_urls},
        })
        ensure_current_methodology(s, get_config(s))
        s.execute(delete(Methodology).where(Methodology.version.like("%+sites-%")))


def _snapshot_scoring_state():
    with session_scope() as s:
        cfg = get_config(s)
        return (
            cfg.get("methodology_version"),
            list((cfg.get("browser") or {}).get("urls") or []),
            list((cfg.get("http") or {}).get("urls") or []),
        )


# ── comparability ───────────────────────────────────────────────────────────────


def test_comparability_gates_on_the_declared_site_set():
    d = _definition_with(SITES_A)
    mv = _complete_metrics(d)
    declared = d["collection"]["site_set"]
    assert comparability(d, mv, site_set=declared) == ("exact", [])
    # Another set, or a run whose config never recorded one, is quarantined under one token
    # a reader can tell apart from a missing instrument.
    other = collection_from_lists(SITES_B, [])["site_set"]
    assert comparability(d, mv, site_set=other) == ("incomparable", [SITE_SET_MARKER])
    assert comparability(d, mv, site_set=None) == ("incomparable", [SITE_SET_MARKER])
    # A version that declares no sites measures whatever config says and ignores the stamp.
    plain = build_definition_from_spec(METHODOLOGY_REGISTRY[CURRENT_METHODOLOGY])
    assert comparability(plain, _complete_metrics(plain), site_set=other)[0] == "exact"
    # Missing required metrics still win: that is the older, more specific quarantine.
    short = {k: v for k, v in mv.items() if k != "fcp"}
    tag, missing = comparability(d, short, site_set=other)
    assert tag == "incomparable" and "fcp" in missing and SITE_SET_MARKER not in missing


def test_site_set_stamp_is_order_free_and_hashes_both_lists():
    assert site_set_from_config({"browser": {"urls": SITES_A}}) == site_set_from_config(
        {"browser": {"urls": list(reversed(SITES_A))}}
    )
    assert site_set_from_config({"browser": {"urls": SITES_A}}) != site_set_from_config(
        {"browser": {"urls": SITES_A}, "http": {"urls": ["https://h.example/"]}}
    )
    assert site_set_from_config({}) is None
    assert site_set_from_config({"browser": {"urls": []}, "http": {"urls": []}}) is None


def test_apply_collection_overlays_both_lists_so_the_stamp_matches_the_declaration():
    d = _definition_with(SITES_A)  # declares no HTTP URLs
    cfg = {"browser": {"urls": ["https://old.example/"], "timeout_s": 5},
           "http": {"urls": ["https://keep.example/"]}}
    out = apply_collection(cfg, d)
    assert out["browser"]["urls"] == SITES_A and out["browser"]["timeout_s"] == 5
    assert out["http"]["urls"] == []  # config's HTTP list must not leak into the stamp
    assert site_set_from_config(out) == d["collection"]["site_set"]
    assert cfg["browser"]["urls"] == ["https://old.example/"]  # input untouched
    assert apply_collection(cfg, {"metrics": []}) is cfg  # no declaration → unchanged


# ── publishing ──────────────────────────────────────────────────────────────────


def test_publish_sites_forks_the_current_version_pins_it_and_writes_config():
    pin, b_urls, h_urls = _snapshot_scoring_state()
    try:
        with session_scope() as s:
            base = ensure_current_methodology(s, get_config(s))
            row, info = publish_sites(s, SITES_A, ["https://h.example/"])
            s.commit()
            expected = f"{base.version}+sites-{collection_from_lists(SITES_A, ['https://h.example/'])['site_set']}"
            assert info["changed"] and info["version"] == expected == row.version
            assert row.is_current and not s.get(Methodology, base.version).is_current
            assert collection(row.definition)["browser_urls"] == SITES_A
            # The rubric is untouched — only the collection was added.
            assert row.definition["metrics"] == (base.definition or {})["metrics"]
            assert row.definition["axes"] == (base.definition or {})["axes"]
            cfg = get_config(s)
            assert cfg["methodology_version"] == expected
            assert cfg["browser"]["urls"] == SITES_A
            assert cfg["http"]["urls"] == ["https://h.example/"]
            assert set(info["added"]) >= set(SITES_A)

        # The same list again publishes nothing.
        with session_scope() as s:
            row2, info2 = publish_sites(s, SITES_A, ["https://h.example/"])
            assert not info2["changed"] and row2.version == expected

        # A second change REPLACES the site segment rather than chaining a new one.
        with session_scope() as s:
            row3, info3 = publish_sites(s, SITES_B, ["https://h.example/"])
            s.commit()
            assert info3["changed"]
            assert row3.version.count("+sites-") == 1
            assert row3.version.startswith(f"{base.version}+sites-")
            assert info3["added"] == ["https://c.example/"] and info3["removed"] == ["https://b.example/"]
            assert row3.is_current and not s.get(Methodology, expected).is_current

        # No browser URL at all is refused before anything is written.
        with session_scope() as s:
            try:
                publish_sites(s, [], [])
                raise AssertionError("expected ValueError")
            except ValueError:
                pass
    finally:
        _restore_scoring_state(pin, b_urls, h_urls)


def test_adopting_a_shipped_version_carries_the_site_list_forward(monkeypatch):
    # A code-shipped version knows nothing about this deployment's sites; adopting it after
    # a site publish must keep enforcing the declared set, not silently revert to config.
    pin, b_urls, h_urls = _snapshot_scoring_state()
    synthetic = "test-carry-forward-v0"
    monkeypatch.setitem(
        METHODOLOGY_REGISTRY, synthetic, dict(METHODOLOGY_REGISTRY[CURRENT_METHODOLOGY])
    )
    try:
        with session_scope() as s:
            publish_sites(s, SITES_A, [])
            s.commit()
        with session_scope() as s:
            save_config(s, {"methodology_version": synthetic})
            row = ensure_current_methodology(s, get_config(s))
            assert row.version == synthetic and row.is_current
            assert collection(row.definition)["browser_urls"] == SITES_A
            assert "carried forward" in (row.notes or "")
    finally:
        with session_scope() as s:
            s.execute(delete(Methodology).where(Methodology.version == synthetic))
        _restore_scoring_state(pin, b_urls, h_urls)


def test_publish_sites_endpoint_publishes_and_kicks_the_regrade(client, monkeypatch):
    import pathbrain.api.routes_methodology as rm

    started: list[str] = []
    monkeypatch.setattr(rm.jobs, "start", lambda *a, **k: started.append(a[1]) or "job-test")
    pin, b_urls, h_urls = _snapshot_scoring_state()
    try:
        r = client.post("/api/methodologies/sites", json={"browser_urls": SITES_A, "http_urls": []})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["changed"] and body["job_id"] == "job-test" and "+sites-" in body["version"]
        assert started and body["version"] in started[0]
        cur = client.get("/api/methodologies/current").json()
        assert cur["version"] == body["version"]
        assert cur["collection"]["browser_urls"] == SITES_A
        assert cur["definition"]["collection"]["site_set"] == cur["collection"]["site_set"]
        # Unchanged list → nothing published, no job.
        r = client.post("/api/methodologies/sites", json={"browser_urls": SITES_A, "http_urls": []})
        assert r.status_code == 202 and r.json()["changed"] is False and r.json()["job_id"] is None
        # Deferred re-grade → published, no job.
        r = client.post("/api/methodologies/sites",
                        json={"browser_urls": SITES_B, "http_urls": [], "regrade": False})
        assert r.status_code == 202 and r.json()["regrade_deferred"] is True and len(started) == 1
        # An empty list is a 400, not a fork.
        assert client.post("/api/methodologies/sites", json={"browser_urls": []}).status_code == 400
    finally:
        _restore_scoring_state(pin, b_urls, h_urls)


# ── the runner ──────────────────────────────────────────────────────────────────


def test_create_run_measures_the_versions_sites_and_stamps_them():
    pin, b_urls, h_urls = _snapshot_scoring_state()
    try:
        with session_scope() as s:
            row, _ = publish_sites(s, SITES_A, [])
            s.commit()
            declared = collection(row.definition)["site_set"]
            # Drift config away from the declaration: the run must still measure the
            # version's sites, because that is the set comparability will hold it to.
            save_config(s, {"browser": {"urls": ["https://drift.example/"]}})
        run_id = create_run(iterations=1)
        with session_scope() as s:
            run = s.get(Run, run_id)
            assert run.config_used["browser"]["urls"] == SITES_A
            assert site_set_from_config(run.config_used) == declared
            s.execute(delete(Run).where(Run.id == run_id))
    finally:
        _restore_scoring_state(pin, b_urls, h_urls)


def test_scoring_under_a_sites_version_quarantines_other_site_sets():
    d = _definition_with(SITES_A)
    version = "test-sites-scoring-v0"
    t0 = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        meth = Methodology(version=version, rubric_version=version,
                           derivation_version="derive-v14", definition=d, is_current=False)
        s.add(meth)
        run = Run(status=RunStatus.COMPLETE, created_at=t0, iterations=1)
        s.add(run)
        s.flush()
        run_id = run.id
        # Every scored metric present, so the only thing that can quarantine is the sites.
        iter_metrics = [{}]
        for m in d["metrics"]:
            if m.get("axis"):
                iter_metrics[0].setdefault(m["plugin"], {})[m["source_key"]] = 1.0
        try:
            declared = d["collection"]["site_set"]
            other = collection_from_lists(SITES_B, [])["site_set"]
            ok = score_metrics_under(s, run_id, version, meth, iter_metrics, site_set=declared)
            assert ok.comparability == "exact"
            s.flush()  # the upsert re-reads (run × version); an unflushed row would double-insert
            bad = score_metrics_under(s, run_id, version, meth, iter_metrics, site_set=other)
            assert bad.comparability == "incomparable" and bad.missing_metrics == [SITE_SET_MARKER]
            # It still SCORES — the number is kept for the version it was measured under; the
            # quarantine is what keeps it out of this version's pooled standings.
            assert bad.axis_scores.get("overall") is not None
        finally:
            s.execute(delete(Score).where(Score.run_id == run_id))
            s.execute(delete(Run).where(Run.id == run_id))
            s.execute(delete(Methodology).where(Methodology.version == version))


# ── seeding from the prior version ──────────────────────────────────────────────


def _seed_prior(session, version: str, specs, when):
    """A non-current methodology row (newest by created_at) with one scored run per spec."""
    session.add(Methodology(version=version, rubric_version=version, derivation_version="derive-v14",
                            definition={"axes": [], "metrics": []}, is_current=False,
                            created_at=when))
    ids = []
    for fp, ov, iters in specs:
        run = Run(status=RunStatus.COMPLETE, created_at=when, settings_fingerprint=fp,
                  settings=[{"label": "wan", "quantum": 1514, "scheduler": "fq_codel", "queues": 1}],
                  iterations=iters, per_iteration_ms=1000.0)
        session.add(run)
        session.flush()
        session.add(Score(run_id=run.id, methodology_version=version, comparability="exact",
                          axis_scores={"overall": ov}))
        ids.append(run.id)
    return ids


def _clear_prior(session, version, run_ids):
    session.execute(delete(Score).where(Score.run_id.in_(run_ids)))
    session.execute(delete(Run).where(Run.id.in_(run_ids)))
    session.execute(delete(Methodology).where(Methodology.version == version))


def test_prior_field_and_seeding_order_the_unknowns_by_the_previous_verdict():
    version = "test-seed-prior-v0"
    when = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=365)  # newest
    specs = [("seedhi000000", 90.0, 20), ("seedthin0000", 95.0, 2), ("seedlo000000", 30.0, 20)]
    with session_scope() as s:
        ids = _seed_prior(s, version, specs, when)
    try:
        with session_scope() as s:
            prior = refresh.prior_field(s, min_iterations=15)
            assert prior["version"] == version
            assert prior["overall"]["seedhi000000"] == 90.0
            # The prior crown is the best CONFIDENT profile — a thin lucky 95 doesn't seed the bar.
            assert prior["best_fingerprint"] == "seedhi000000"

            # A field with no crown gets seeded: every stored profile present, no-data entries
            # added, the prior crown standing in as best.
            field = {"profiles": [], "best_fingerprint": None, "min_iterations": 15}
            seeded = refresh.seed_field_from_prior(s, field, 15)
            assert seeded["seeded_from"] == version
            assert seeded["best_fingerprint"] == "seedhi000000"
            by_fp = {p["fingerprint"]: p for p in seeded["profiles"]}
            assert by_fp["seedlo000000"]["no_data"] and by_fp["seedlo000000"]["prior_overall"] == 30.0
            assert by_fp["seedhi000000"]["settings"]  # runnable — the ladder can apply it
            assert field["profiles"] == [] and field["best_fingerprint"] is None  # a copy

            # A field that already HAS a crown is never touched by the seed.
            live = {"profiles": [{"fingerprint": "x", "overall": 50.0}], "best_fingerprint": "x"}
            untouched = refresh.seed_field_from_prior(s, live, 15)
            assert "seeded_from" not in untouched and untouched["profiles"][0].get("prior_overall") is None
    finally:
        with session_scope() as s:
            _clear_prior(s, version, ids)


def test_prior_field_walks_past_a_version_that_scored_nothing():
    empty, scored = "test-seed-empty-v0", "test-seed-scored-v0"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with session_scope() as s:
        ids = _seed_prior(s, scored, [("seedwalk0000", 70.0, 20)], now + timedelta(days=364))
        s.add(Methodology(version=empty, rubric_version=empty, derivation_version="derive-v14",
                          definition={"axes": [], "metrics": []}, is_current=False,
                          created_at=now + timedelta(days=365)))  # newer, but scored nothing
    try:
        with session_scope() as s:
            assert refresh.prior_field(s)["version"] == scored
    finally:
        with session_scope() as s:
            _clear_prior(s, scored, ids)
            s.execute(delete(Methodology).where(Methodology.version == empty))


def _no_data(fp, prior=None):
    return {"fingerprint": fp, "label": fp, "confident": False, "overall": None, "optimistic": None,
            "crown_spreads": {}, "last_seen": None, "no_data": True, "iterations": 0,
            "settings": [{"label": "wan", "scheduler": "fq_codel", "queues": 1}],
            "prior_overall": prior}


def test_contender_order_breaks_unknown_ties_on_the_prior_overall():
    field = {"best_fingerprint": "champ", "profiles": [
        {"fingerprint": "champ", "label": "champ", "overall": None, "optimistic": None,
         "settings": [{"label": "wan", "scheduler": "fq_codel", "queues": 1}]},
        _no_data("u_low", 20.0), _no_data("u_none", None), _no_data("u_high", 80.0),
    ]}
    order = [c["fingerprint"] for c in duel.contender_order(field, {}, "champ")]
    assert order == ["u_high", "u_low", "u_none"]
    seeded_why = next(c for c in duel.contender_order(field, {}, "champ") if c["fingerprint"] == "u_high")
    assert "seeded" in seeded_why["why"] and seeded_why["prior_overall"] == 80.0


def test_race_and_heirs_measure_the_prior_winners_first(monkeypatch):
    monkeypatch.setattr(rs, "_discover_live_normalized", lambda: None)  # no reachability filter
    field = {"best_fingerprint": None, "min_iterations": 15, "profiles": [
        _no_data("n_low", 20.0), _no_data("n_none", None), _no_data("n_high", 80.0),
    ]}
    _best, _bar, leader, contenders, _newly = challenger.rank_challengers(field)
    assert [p["fingerprint"] for p, _ in contenders] == ["n_high", "n_low", "n_none"]
    assert leader["fingerprint"] == "n_high"

    with session_scope() as s:
        heirs = rs._compute_heirs(field, s)
    assert [h["fingerprint"] for h in heirs["items"]] == ["n_high", "n_low", "n_none"]
    assert heirs["items"][0]["prior_overall"] == 80.0 and heirs["items"][0]["reason"] == "untested"


def test_profiles_endpoint_reports_the_seed_without_scoring_it(client, monkeypatch):
    # With no crown under the current methodology, the endpoint says what seeded the heirs
    # and never lets a seeded row into the standings.
    version = "test-seed-endpoint-v0"
    when = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=365)
    with session_scope() as s:
        ids = _seed_prior(s, version, [("seedapi00000", 77.0, 20), ("seedapi2lo00", 40.0, 20)], when)
    empty_field = {"profiles": [], "best_fingerprint": None, "min_iterations": 15, "count": 0,
                   "min_runs": 1, "complete_only": True, "sqm_off_overall": None, "fields": [],
                   "best_diff": None, "overall_metrics": [], "co_leaders": []}
    monkeypatch.setattr(rs, "compute_profiles", lambda *a, **k: dict(empty_field))
    monkeypatch.setattr(rs, "_discover_live_normalized", lambda: None)  # no reachability filter
    monkeypatch.setattr(rs.crowning, "rank_field", lambda session, result: {
        "by_fingerprint": {}, "order": [], "ranking": "ring", "best_fingerprint": None,
        "best_source": None, "ring_rated": 0, "seeded": 0,
    })
    try:
        body = client.get("/api/settings/profiles").json()
        assert body["seeded"]["version"] == version
        assert body["seeded"]["best_fingerprint"] == "seedapi00000"
        assert body["seeded"]["best_prior_overall"] == 77.0
        assert body["seeded"]["profiles_without_data"] >= 1
        assert body["profiles"] == []  # nothing seeded is scored under the current version
        # The prior crown stands in as the bar the heirs are measured against (so it is not
        # itself an heir — the race re-measures its incumbent first, the ladder makes it
        # defend); the rest of the prior field leads the heirs, best prior Overall first.
        fps = [h["fingerprint"] for h in body["heirs"]["items"]]
        assert "seedapi00000" not in fps
        assert fps[0] == "seedapi2lo00" and body["heirs"]["items"][0]["prior_overall"] == 40.0
    finally:
        with session_scope() as s:
            _clear_prior(s, version, ids)
