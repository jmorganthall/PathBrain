# CLAUDE.md — PathBrain developer guide

PathBrain is an empirical network-optimization platform that maximizes a
**Seat of Pants Score (SOPS)** — a measure of *human-perceived responsiveness*,
not raw ping/throughput. The optimizer is classical (deterministic sweep +
hysteresis), not LLM-based. See `README.md` for the product overview.

## Layout

- `backend/pathbrain/` — FastAPI app (the core). Key modules:
  - `plugins/` — independent benchmark plugins (`icmp/dns/tcp/tls/http/browser`);
    `base.py` defines the contract + registry. Add one by subclassing
    `BenchmarkPlugin` and `@register`.
  - `providers/` — firewall config discovery + **apply** (`opnsense.py`,
    `mock.py`); pick via `PATHBRAIN_CONFIG_PROVIDER`. OPNsense reads/writes
    fq_codel fields (`fqcodel_quantum/limit/flows`, `codel_target/interval/ecn`);
    `apply()` does `setPipe` + `reconfigure` and is the **only firewall-write path**.
  - `metrics.py` — **single source of truth for metrics.** Each `MetricDef` (key,
    plugin+source_key, axis, default weight/thresholds, label/description/unit/
    direction, `marks_latest`) is defined once; `METRIC_SOURCES`, the config
    weights/thresholds, `LATEST_METRIC_KEYS`, and the `/api/metrics` catalog (which
    the frontend's `MetricCatalogProvider`/`useMetricMeta` consume) are all derived
    from it. Adding a measurement = one entry here (+ the plugin emitting it).
  - `scoring/engine.py` — score computation (weighted, perception-calibrated log
    curve, redistributes missing-metric weight). **Two axes, never blended:**
    **SOPS** is the headline *human-feel* score — perception-led: paint timing
    (FCP/LCP/INP) + TTFB + render (`METRIC_SOURCES`); it's what we rank/chart/
    optimize. **Completion** is the secondary infra axis (DNS/TCP/TLS/jitter/loss,
    `COMPLETION_METRIC_SOURCES`, `compute_completion`) — raw timing that barely
    moves human feel, so it's kept out of SOPS. Completion persists on
    `ScoreResult.completion` (+ sub/weights/values), reusing the legacy
    `responsiveness`/`perceptual_*` DB columns via attribute mapping (no migration;
    deeper column rename deferred). Aggregated per profile by `/api/settings/
    profiles`. Rubric keys: `weights`/`thresholds` (SOPS) + `completion_weights`/
    `completion_thresholds`.
  - `config_store.py` — DB-backed runtime config + defaults (targets, weights,
    thresholds, `iterations`, `monitoring`, `correlation`, `experiment`,
    `rubric_version`).
  - `runner.py` — orchestrates a run across plugins (median-aggregated over
    iterations, per-run SOPS confidence band), captures the firewall settings +
    fingerprint per run, and holds run-lifecycle safety
    (`reconcile_interrupted_runs`, `fail_stale_runs`, `rescore_run`).
  - `scheduler.py` — daemon thread: watchdog → experiment step → monitoring run
    (serialized so they never overlap).
  - `experiment.py` — autonomous window-gated single-parameter shaper sweep
    (writes via `provider.apply()`; disarmed + dry-run by default; restores baseline).
  - `settings_profile.py` — normalize/fingerprint/summarize firewall profiles for
    settings-vs-responsiveness correlation (`/api/settings/*`).
  - `database.py` — engine/session + additive SQLite `_migrate()` (ALTER for new
    columns; `create_all` for new tables).
  - `api/` — REST routers mounted at `/api`.
- `frontend/` — React + TS + Vite + MUI dashboard (dark mode). Pages: Dashboard,
  History, Compare, Settings Impact, Experiments, Config, Plugins, Run Detail.
- `Dockerfile` (Playwright base image) / `docker-compose.yml` +
  `docker-compose.ghcr.yml` — single-container deploy (API serves UI). CI publishes
  `ghcr.io/jmorganthall/pathbrain:latest` via `.github/workflows/docker-publish.yml`.

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

## Conventions

- Plugins must never raise for *measurement* failures — return a `PluginResult`
  with `success=False` and an `error`. Use the `timed()` helper.
- All runtime config (targets/weights/thresholds) is DB-backed and editable via
  `/api/config`; infra config (DB URL, OPNsense creds) is env-only (`config.py`).
- Lower-is-better for all current SOPS metrics; thresholds define best/worst and
  are interpolated on a perception-calibrated log curve (Weber–Fechner). The
  rubric (weights+thresholds+`rubric_version`) is versioned; changing it should be
  followed by `POST /api/score/rescore` to re-grade history from stored raw
  measurements (runs keep `metric_values` + per-iteration metrics for this).
- A run repeats the suite `iterations` times; the headline SOPS is the **median**
  over iterations, with a confidence band (`sops_stdev/min/max`). The Dashboard
  shows a windowed **rolling** score (`/api/score/rolling`, 24h median + IQR).
- **Current vs. legacy scoring (no dual-score machinery).** A run scored before
  the current rubric's paint metrics (FCP/LCP absent — `metrics.has_latest_metrics`,
  keyed off `marks_latest`) isn't comparable, so it's **quarantined**, not
  reconciled: Dashboard rolling + History trend exclude legacy; the History list
  hides it behind a "Show legacy" toggle; Run Detail/Compare flag it
  (`ScoreOut.legacy`/`RunSummary.legacy`); Settings Impact aggregates `complete_only`
  (default true). Legacy runs are kept for their *settings* history, not their score.
  SOPS is the sole ranked headline everywhere; Completion is an opt-in diagnostic.
- Each run captures the live firewall settings + a stable **fingerprint** at start
  (best-effort). Runs group into **profiles**; `/api/settings/impact` flags a
  change significant only with ≥ `correlation.min_runs` per side. `/api/settings/
  backfill` stamps current settings onto unstamped historical runs.
- **Run lifecycle safety:** `reconcile_interrupted_runs()` (startup) + scheduler
  watchdog `fail_stale_runs()` (`monitoring.run_timeout_minutes`, default 30) +
  manual `POST /api/runs/{id}/cancel` resolve orphaned/hung runs. These mark the
  DB row FAILED; a live benchmark thread can't be force-killed mid-call.
- Timestamps are stored UTC (naive in SQLite); the frontend (`parseApiDate`)
  treats them as UTC so they render in the viewer's local zone. Experiment-window
  hours use the container `TZ`.
- Every action should be logged (`logging_config.get_logger`).

## Phase map

- **Phase 1 (done):** benchmark engine (ICMP/DNS/TCP/TLS/HTTP), SOPS scoring,
  history, config discovery (OPNsense/mock), REST API, dashboard.
- **Phase 2 (done):** Playwright browser engine — `benchmark_browser` emits
  `total_render_ms`, **paint timing** (`fcp_ms`/`lcp_ms`/`inp_ms` — the core of
  the perception-led SOPS; INP is best-effort via a synthetic interaction), and
  nav timings; captures screenshot/HAR to the artifact dir, served at
  `/artifacts`. (Speed Index is the first deferred perceptual metric.)
- **Phase 3 (done):** continuous monitoring (`scheduler.py`) + rolling score;
  settings-vs-responsiveness correlation (`settings_profile.py`, `/api/settings/*`);
  perception-calibrated rubric (Weber–Fechner) with versioned re-scoring; and the
  **experiment engine** (`experiment.py`): window-gated single-parameter sweep
  that writes to the firewall via `provider.apply()`, disarmed + dry-run by
  default, restoring the pre-window baseline at window close. Experiments run in
  the scheduler thread (priority over monitoring).
- **Next:** real-world profiles, speed test, bufferbloat, multi-parameter
  Bayesian search + interleaved A/B with effect-size/CI + hysteresis, routing
  intelligence / SD-WAN.

⚠️ The experiment engine is the only path that *writes* to the firewall. Keep it
disarmed (`experiment.enabled=false`) / dry-run by default; always snapshot the
baseline and restore it at window close.

The browser engine imports Playwright lazily, so the plugin registry still loads
where Playwright/Chromium isn't installed (it returns `success=False` and the
`render` weight is redistributed). Chromium is installed in the Docker image.
