# CLAUDE.md — PathBrain developer guide

PathBrain is an empirical network-optimization platform that rates
*human-perceived responsiveness* (not raw ping/throughput) across four scored
axes — **Speed, Smoothness, Stability & Interactivity, and Completion** —
interpreted through a **versioned, RTINGS-style methodology** (`raw + methodology
→ score`, reproducibly; see `docs/methodology.md`). The optimizer is classical
(deterministic sweep + hysteresis), not LLM-based. See `README.md` for the
product overview.

> History note: the project began with a single headline **Seat of Pants Score
> (SOPS)**; it was replaced by the four-axis model. Some legacy code/columns still
> carry `sops`/`responsiveness`/`perceptual_*` names (no rename migration) — read
> them as the corresponding axis.

## Layout

- `backend/pathbrain/` — FastAPI app (the core). Key modules:
  - `plugins/` — benchmark plugins (`icmp/dns/tcp/tls/http/browser`) that are
    **pure sensors: they emit raw observations only** (`PluginResult.raw`) and never
    interpret. `base.py` defines the contract + registry. Add one by subclassing
    `BenchmarkPlugin` and `@register`. (icmp emits per-ping RTT series, http emits
    bytes+timing, browser emits raw nav/paint/CLS/long-task + Resource Timing/LoAF
    entries, and an optional filmstrip.)
  - `interpret/` — **the interpretation layer** (`derive.py`, versioned
    `DERIVATION_VERSION`). Turns raw observations → scoreable metric values:
    `jitter`=stddev(RTTs), `latency`=mean, `transfer`=bytes·8/dl, the **byte-arrival
    smoothness** metrics (`smoothness.py`: longest stall / cadence CoV / byte
    earliness / delivery Gini / perceived time / network-vs-render stall attribution,
    all from Resource Timing + LoAF — no pixels), and the pixel diagnostics (Speed
    Index / paint cadence / CLS from the optional filmstrip); `fcp`/`lcp` are identity
    pass-throughs. This is the **only** place interpretation lives, so a new metric or
    changed formula can be re-derived over history without re-collecting.
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
  - `scoring/engine.py` — generic multi-axis score computation (weighted,
    perception-calibrated log curve, redistributes missing-metric weight).
    `compute_score(metric_sources=…)` scores **any** axis from a methodology
    definition; `compute_completion` is the same machinery for Completion. **Four
    axes, never blended:** **Speed** (byte earliness / FCP / LCP / TTFB — how fast
    content arrives), **Smoothness** (longest stall / cadence CoV / perceived time /
    render-to-networkidle — how *steadily* it arrives; the byte-arrival metrics
    isolate the tunable network layer with no pixel screencast), **Stability &
    Interactivity** (INP / CLS), and **Completion** (the secondary infra axis —
    DNS/TCP/TLS/jitter/loss raw timing that barely moves human feel). Speed +
    Smoothness are the headline axes. Speed Index / paint cadence are now display-only
    diagnostics (require the opt-in `browser.filmstrip`). Thresholds anchored to
    CWV/Nielsen (rubric `perceptual-v5`).
  - `methodology.py` — **the methodology layer** (`docs/methodology.md`). The
    `raw + methodology → score` invariant. `METHODOLOGY_REGISTRY` holds declarative
    version specs; `CURRENT_METHODOLOGY = "speed-smoothness-v2"`;
    `build_definition_from_spec` expands a spec → a frozen, self-contained
    `definition` (axes + metrics + weights + thresholds); `ensure_current_methodology`
    inserts the current row on startup; `current_version(config)` honors a config
    `methodology_version` override; `comparability(...)` grades a run vs a methodology
    as **exact / partial / incomparable** (replaces the old binary legacy flag).
    Methodologies are immutable + append-only — a new weight/threshold/metric = a new
    version. Surfaced at `/api/methodologies*`.
  - `metrics.py` — **single source of truth for metric *definitions*** (each
    `MetricDef`: key, plugin+source_key, axis, default weight/thresholds, label/unit/
    direction, `marks_latest` — now on `longest_stall`). The four-axis methodology
    definitions in `methodology.py` are built from these. Adding a measurement = one
    entry here (+ the plugin emitting it).
  - `config_store.py` — DB-backed runtime config + defaults (targets, weights,
    thresholds, `iterations`, `monitoring`, `correlation`, `trends`, `experiment`,
    `rubric_version` = `perceptual-v5`, `methodology_version`).
  - `runner.py` — orchestrates a run across plugins, derives metrics from raw via
    `interpret` (median-aggregated over iterations, per-axis confidence bands), stores
    `BenchmarkResult.raw` as the source of truth, captures the firewall settings +
    fingerprint per run. At capture it scores via the generic
    `score_metrics_under` / `score_run_under` and writes the **at-measure `Score` row**
    under the current methodology (and still dual-writes the legacy `ScoreResult`).
    `score_history_under_current` re-scores history from raw under the current
    methodology. Run-lifecycle safety: `reconcile_interrupted_runs`, `fail_stale_runs`,
    plus the legacy `rescore_run`/`rederive_run`.
  - `trends.py` — historical baselines by day-of-week × hour-of-day (viewer-local);
    `relative_reading`/`profile_relative` give a time-adjusted "vs typical" delta
    ("wins above replacement"). Powers `/api/trends/*`, the Dashboard delta chip,
    and the Settings-Impact "vs typical" column.
  - `sweep.py` — **Shotgun Sweep**: an on-demand foreground sweep of a
    pipe × quantum × target grid (varies one pipe at a time across **download +
    upload**, each with its own `_baseline`/`_restore_pipe`). Applies each variant for
    real, benchmarks it, **restores the baseline at the end**
    (`reconcile_interrupted_sweeps` restores on startup too). Runs in its own thread;
    the scheduler yields while `sweep.active()`. `/api/sweep/pipes` lists pipes.
  - `scheduler.py` — daemon thread: watchdog → (yield if a sweep is active) →
    experiment step → monitoring run (serialized so benchmark runs never overlap).
  - `experiment.py` — autonomous window-gated single-parameter shaper sweep
    (writes via `provider.apply()`; disarmed + dry-run by default; restores baseline).
  - `settings_profile.py` — normalize/fingerprint/summarize firewall profiles for
    settings-vs-responsiveness correlation (`/api/settings/*`).
  - `models.py` — ORM models, including the methodology layer: **`Methodology`**
    (immutable published versions) and **`Score`** (`run × methodology`; axis_scores /
    subscores / weights_used / metric_values / bands, `is_at_measure`,
    `comparability`, `missing_metrics`; `UNIQUE(run_id, methodology_version)`).
    `Score` is the source of truth for all current read paths; `ScoreResult` is the
    legacy lane.
  - `database.py` — engine/session + additive SQLite `_migrate()` (ALTER for new
    columns; `create_all` for new tables, incl. `methodology`/`score`).
  - `api/` — REST routers mounted at `/api` (incl. `routes_methodology.py`,
    `routes_score.py` with methodology-aware `/score/rolling`, `/score/axis-series`,
    `/score/regrade`, `/score/{id}/methodologies`).
- `frontend/` — React + TS + Vite + MUI dashboard (dark mode). Pages: Dashboard
  (Speed/Smoothness gauges, config-tag filter, p95, axis-series chart), History,
  Trends, Compare, Settings Impact, Experiments, Shotgun Sweep (pipe picker), Config,
  Plugins, Methodology, Run Detail (at-measure vs at-present).
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
  with `success=False` and an `error`. Use the `timed()` helper. Plugins emit
  **raw observations only** (`raw=…`); the `interpret` layer derives metrics — keep
  statistics/aggregation out of the probe.
- All runtime config (targets/weights/thresholds) is DB-backed and editable via
  `/api/config`; infra config (DB URL, OPNsense creds) is env-only (`config.py`).
- Lower-is-better for all current axis metrics; thresholds define best/worst and
  are interpolated on a perception-calibrated log curve (Weber–Fechner), anchored to
  CWV/Nielsen (rubric `perceptual-v5`). **Methodologies are versioned and immutable**
  (`methodology.py`): a weight/threshold/metric change = a **new** version. Re-grade
  via `POST /api/score/regrade` ("Re-grade history under current") — it **derives
  scores from `BenchmarkResult.raw`** and **writes new `Score` rows**, never mutating
  the at-measure rows. (The legacy `POST /api/score/rescore`/`rederive` paths remain
  for the `ScoreResult` lane only.) There is **no backfill of historical scores** —
  the `Score`/`Methodology` tables populate from raw on demand; only raw is retained
  across the reset.
- A run repeats the suite `iterations` times; each axis is the **median** over
  iterations, with confidence bands (stdev/min/max + p75/p95). The Dashboard shows
  windowed **rolling** per-axis scores (`/api/score/rolling`, 24h median + IQR/p95,
  optional `fingerprint`/config-tag filter) plus a **"vs typical"** delta vs the
  day/hour historical baseline (`trends.py`).
- **Comparability replaces the binary legacy flag.** For a `(run, methodology)`,
  `methodology.comparability(...)` grades **exact** (every required metric
  reproducible — a pure re-weight), **partial** (some metrics missing; scored with
  weight redistribution + `missing_metrics`), or **incomparable** (a required metric
  the raw never captured — no faithful at-present score). Runs that aren't comparable
  under the current methodology are quarantined from rolling/trend aggregates and
  flagged in Run Detail/Compare; they're kept for their *settings* history. Speed +
  Smoothness are the ranked headline axes everywhere; Completion is an opt-in
  diagnostic.
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
- **Phase 2 (done):** Playwright browser engine — `benchmark_browser` emits raw
  nav timings, **paint events** (`fcp`/`lcp`/`inp`), **Resource Timing + LoAF** (for
  smoothness), and an **optional filmstrip** (CDP screencast, gated by
  `browser.filmstrip`, off by default — it only feeds the pixel Speed Index/cadence
  diagnostics); captures screenshot/HAR to the artifact dir, served at `/artifacts`.
- **Phase 3 (done):** continuous monitoring (`scheduler.py`) + rolling score;
  settings-vs-responsiveness correlation (`settings_profile.py`, `/api/settings/*`);
  perception-calibrated rubric (Weber–Fechner) with versioned re-scoring; and the
  **experiment engine** (`experiment.py`): window-gated single-parameter sweep
  that writes to the firewall via `provider.apply()`, disarmed + dry-run by
  default, restoring the pre-window baseline at window close.
- **Phase 4 (done):** **historical trends + relative SOPS** (`trends.py`,
  `/api/trends/*`) and time-adjusted Settings-Impact ("vs typical"); **raw-only
  collection + a re-runnable interpretation layer** (`interpret/derive.py`,
  `BenchmarkResult.raw`, `/api/score/rederive`); **trajectory-aware scoring**
  (Speed Index / paint cadence / CLS from the filmstrip; rubric `perceptual-v3`,
  Pillow dep); a reversible **config write-test** (`POST /api/config/test-apply`);
  and the **Shotgun Sweep** (`sweep.py`, `/api/sweep/*`) — an on-demand grid sweep
  that applies each variant, benchmarks it, ranks by SOPS + "vs typical", and
  restores the baseline.
- **Phase 5 (done):** **perceived load-smoothness instrument** — byte-arrival
  smoothness metrics from Resource Timing + LoAF (`interpret/smoothness.py`), with
  network-vs-render stall attribution and protocol mix. Per-run records + two-config
  comparison at `/api/smoothness/*` (keyed on `settings_fingerprint`); an offline
  **calibration harness** (`calibration/`) fits the perceived-time weight ratio to
  subjective 1–10 ratings.
- **Phase 6 (done):** **methodology layer + four-axis scoring** (`docs/methodology.md`).
  The `raw + methodology → score` invariant made first-class: immutable
  `Methodology` + `(run × methodology)` `Score` tables, score-at-measure vs
  score-at-present, and exact/partial/incomparable comparability. SOPS replaced by
  **Speed / Smoothness / Stability / Completion** axes (methodology
  `speed-smoothness-v2`, derivation `derive-v3`, rubric `perceptual-v5` with
  CWV/Nielsen-anchored thresholds). Generic multi-axis scoring; all read paths
  (Dashboard, History, Trends, Settings Impact) read the `Score` table; Methodology
  tab + Run Detail at-measure/at-present surfacing. Multi-pipe Shotgun Sweep
  (download + upload). No score backfill — history re-scores from raw on demand.
- **Next:** speed test / bufferbloat (latency-under-load), multi-parameter Bayesian
  search + interleaved A/B with effect-size/CI + hysteresis, routing intelligence /
  SD-WAN.

⚠️ Firewall **writes** go only through `provider.apply()`. Four callers use it, all
snapshot/restore or are reversible: the experiment engine (disarmed + dry-run by
default), the Shotgun Sweep (restores baseline at end + on startup), config
test-apply (+1 then revert), and sweep apply-best (explicit, supervised). Keep new
write paths to `provider.apply()` and always snapshot/restore.

The browser engine imports Playwright lazily, so the plugin registry still loads
where Playwright/Chromium isn't installed (it returns `success=False` and the
browser metrics' weight is redistributed). The byte-arrival smoothness metrics need
only Resource Timing (always present); the opt-in filmstrip/Speed Index degrade
gracefully without CDP screencast or Pillow. Chromium is installed in the Docker image.
