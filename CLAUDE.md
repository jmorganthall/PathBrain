# CLAUDE.md — PathBrain developer guide

PathBrain is an empirical network-optimization platform that maximizes
*human-perceived responsiveness* — scored as the **Responsiveness / Smoothness /
Speed** axes (+ an **Overall** corner roll-up), not raw ping/throughput. (The
original single *Seat of Pants Score* was split into these axes; SOPS is now
legacy.) The optimizer is classical (deterministic sweep + hysteresis), not
LLM-based. See `README.md` for the product overview.

## Layout

- `backend/pathbrain/` — FastAPI app (the core); `backend/tests/` — pytest suite.
- `frontend/` — React/MUI app, built into the backend image.
- `docs/` — plans and `methodology.md`. `docs/history/` — narrative that used to live here:
  `claude-md-diary.md` (why each module is the way it is) and
  `claude-md-conventions-and-phases.md` (the old long-form Conventions and Phase map).
  History, not rules: when it disagrees with this file or the code, those win.

Key backend modules (`backend/pathbrain/`):

- `main.py` — app entry; `api/routes_*.py` — one router per area.
- `runner.py` — executes a run (plugins → raw → derive → score), re-grade and verify-derivation.
- `plugins/` — probes (`base.py`: `PluginResult`, `timed()`; `benchmark_browser.py` etc.).
- `interpret/` — derives metrics from raw (`derive.py`, `smoothness.py`, `waterfall.py`).
- `raw_access.py` — the single reader of a stored `BenchmarkResult.raw`'s nesting.
- `metrics.py` — metric registry and roles ledger (`METRIC_ROLES`, `RANKABLE_ROLES`).
- `methodology.py` — versioned rubrics (`METHODOLOGY_REGISTRY`, `CURRENT_METHODOLOGY`),
  `required_metric_keys`, `comparability()` / `is_comparable()`.
- `scoring/engine.py` — threshold interpolation and axis/Overall scores.
- `shaper_fields.py` — the shaper-field registry (identity / writable / sweepable).
- `firewall_write.py` — the only firewall write path; `firewall_guard.py` — its guard and ledger.
- `providers/` — firewall providers (`opnsense.py`, `mock.py`).
- `coordinator.py` — the apply-firewall + benchmark lock; `job_queue.py` — the universal queue.
- `duel.py`, `explore.py`, `refresh.py`, `profile_test.py`, `challenger.py`, `sweep.py`,
  `experiment.py`, `baseline_test.py`, `crown_follower.py` — engines that apply profiles.
- `instrument_health.py`, `host_pressure.py` — is the measuring machine itself well.
- `config.py` — env-only infra config; `config_store.py` — DB-backed runtime config.
- `models.py` / `database.py` — ORM and DB session.

## Commands

```bash
# Backend tests (from backend/)
cd backend && pip install -r requirements-dev.txt && python -m pytest

# Run backend (dev)
cd backend && uvicorn pathbrain.main:app --reload --port 8000

# Frontend (dev, proxies /api -> :8000)
cd frontend && npm install && npm run dev

# Frontend build (must pass before commit)
cd frontend && npm run build

# Full stack via Docker
docker compose up --build   # -> http://localhost:8000
```

## Rules

### Data: raw is never rewritten (bronze / silver / gold)

- **Raw (bronze) is immutable.** Plugins store raw observations in `BenchmarkResult.raw`;
  nothing edits it. Read its nesting only through `raw_access.py`
  (`test_pause_diagnostics.py::test_raw_access_readers_agree_on_the_stored_nesting`).
- **Derived metrics (silver) come from raw** in `interpret/`; a formula change re-derives
  from raw, it never re-measures. `runner.verify_run_derivation` audits it
  (`test_verify_derivation.py`).
- **Scores (gold) are new rows.** A re-grade (`POST /api/score/regrade`) writes new `Score`
  rows under the current methodology and leaves other versions alone
  (`test_regrade.py::test_regrade_does_not_mutate_other_versions_at_measure`).
  `/score/rescore` and `/score/rederive` are legacy in-place paths; do not build on them.
- **Unmeasurable is absent, never a sentinel.** A derive function returns `None` / omits a
  metric it cannot compute; never default to `0`
  (`test_methodology.py::test_unmeasurable_crown_metric_is_quarantined`).
- **Comparability has one gate.** Every scored view filters through
  `methodology.is_comparable()`; do not add ad-hoc filters
  (`test_methodology.py::test_every_current_crown_metric_gates_comparability`).
  Instrument health quarantines through the same gate (`instrument_health.py`,
  `test_instrument_health.py`).

### Where crown rules and metrics may live

- **A crown rule lives only in `methodology.py`.** Axes, weights, thresholds and the Overall
  spec are a versioned entry in `METHODOLOGY_REGISTRY`; published versions are frozen
  (`test_methodology.py::test_v3_methodology_still_frozen`). Change the rubric by adding a
  version, never by editing one. GUI re-anchor and site changes also publish a new version
  (`POST /api/methodologies/reanchor`, `POST /api/methodologies/sites`).
- **The required set has one accessor:** `methodology.required_metric_keys()`
  (`test_methodology.py::test_required_metric_keys_is_the_single_required_set`).
- **A metric is one entry in `metrics.py`**; scoring sources, config defaults and the role
  ledger derive from it (`test_metrics.py`).
- **A shaper field is one entry in `shaper_fields.py`**; identity / writable / sweepable views
  derive from it (`test_shaper_fields.py`). No call site re-lists metrics or fields.
- **The frontend names no crown metric.** Dashboard, Scores over time and Settings-Impact read
  the crown from the API (`overall_metrics`, series `role`) (`test_scores.py`).
- **Publishing a methodology needs follow-through:** check crown agreement
  (`crown_agreement.py`), then re-grade history (`POST /api/score/regrade`);
  `publish_followthrough.py` runs what follows a re-grade (`test_publish_followthrough.py`).
  The PR template's Follow-through checklist lists it.

### One chain: the Verdict decides the crown

- **The crown is decided in one place:** `verdict.current()` returns a typed `Verdict`
  (`domain/verdict.py`). Auto-apply, the API and every view read it (or `GET /api/verdict`);
  no reader calls `crowning.resolve` or a single judge's crown to act on
  (`test_archguard_chain.py`). Other judges' crowns are `Verdict.evidence`, shown, never followed.
- **archguard ratchets the chain:** `node scripts/archguard.mjs` (CI: `.github/workflows/archguard.yml`)
  counts readers that still judge for themselves, promoters other than Auto-apply, and views
  that read a crown field other than the Verdict, plus the data-framework rules. A count may
  go down (`--lower-baseline`), never up. Never raise `.archguard-baseline.json` to pass.

### Firewall write path

- **Every firewall write goes through `firewall_write.py`**, which calls the provider; an AST
  scan refuses any other caller
  (`test_firewall_write.py::test_no_module_writes_the_firewall_except_through_firewall_write`).
  No new write path.
- **`firewall_guard.before_write` refuses non-writable fields** and ledgers every write
  (`test_firewall_guard.py`). A field PathBrain never writes is never offered either
  (`test_no_unwritable_field_is_offered.py`; `flows` stays out: `test_shaper_fields.py`).
- **Writes snapshot and restore** (baseline persisted and reconciled on startup), or are
  explicitly armed and supervised (`crown_follower.py`, disarmed by default).
- **Apply + benchmark sessions hold the `coordinator.py` lock** (`test_coordinator.py`);
  `runner.execute_run` re-reads the fingerprint after a run and fails it on drift.
- **PRs that touch the firewall path need the `## Firewall interaction` section** of
  `.github/pull_request_template.md` (writes per session before/after, on a timeout, on a
  reboot), measured with `backend/tests/faults.py`'s `FaultyProvider`. CI enforces it:
  `.github/workflows/firewall-gate.yml`. A timed-out write is never reissued.

### Plugins and engines

- **Plugins never raise for measurement failures.** Return `PluginResult(success=False,
  error=...)`, use `timed()` (`plugins/base.py`; `test_browser.py::test_browser_missing_playwright_is_graceful`,
  `test_browser.py::test_browser_no_urls_fails_gracefully`).
- **Plugins emit raw only**; statistics and aggregation belong in `interpret/`.
- **User-triggered sessions go through `job_queue.submit()`**, never a 409 for a busy
  pipeline; return the standard placement block, register a starter in
  `job_queue.register_engines`, and drive it from the frontend with `useQueuedAction`
  (`test_universal_queue_contract.py`, `test_queue_durability.py`).
- **Config:** runtime config is DB-backed via `/api/config` (`config_store.py`); infra config
  (DB URL, OPNsense creds) is env-only (`config.py`).
- **Timestamps are stored UTC**; the frontend parses them with `parseApiDate`
  (`frontend/src/utils/format.ts`).
- **Log every action** with `logging_config.get_logger`.

### Testing

- `cd backend && python -m pytest` must pass before every PR; a change must not add failures.
- Frontend changes also pass `cd frontend && npm run build`.
- Pin a behaviour with an executable test, not a comment. Use `backend/tests/faults.py` for
  firewall fault cases and `providers/mock.py` instead of a real firewall.
- Never run anything against a real network device from a dev session or test.

### PR style

- Fill in `.github/pull_request_template.md`: what changed and what it replaced, the evidence
  (measured, not asserted), follow-through, Firewall interaction when required, and the test
  that pins it.
- One focused change per PR. Narrative and rationale go in the PR body or `docs/history/`,
  not in this file.
