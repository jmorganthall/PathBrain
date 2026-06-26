# CLAUDE.md — PathBrain developer guide

PathBrain is an empirical network-optimization platform that maximizes a
**Seat of Pants Score (SOPS)** — a measure of *human-perceived responsiveness*,
not raw ping/throughput. The optimizer is classical (deterministic sweep +
hysteresis), not LLM-based. See `README.md` for the product overview.

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
  - `scoring/engine.py` — score computation (weighted, perception-calibrated log
    curve, redistributes missing-metric weight). **Two axes, never blended:**
    **SOPS** is the headline *human-feel* score — perception-led and **delivery-
    aware**: byte earliness (25) + FCP (20) + longest stall (10) + perceived time (5)
    lead; LCP (10) + INP (10) + TTFB (10) + render-to-networkidle (5) trail
    (`METRIC_SOURCES`, rubric `perceptual-v4`). The byte-arrival smoothness metrics
    isolate the network layer (the tunable one) without the CPU cost of the pixel
    screencast; Speed Index / paint cadence are now display-only diagnostics (require
    the opt-in `browser.filmstrip`). It rewards delivering *early and steadily*, not
    finishing first. **Completion** is the secondary infra axis
    (DNS/TCP/TLS/jitter/loss,
    `COMPLETION_METRIC_SOURCES`, `compute_completion`) — raw timing that barely
    moves human feel, so it's kept out of SOPS. Completion persists on
    `ScoreResult.completion` (+ sub/weights/values), reusing the legacy
    `responsiveness`/`perceptual_*` DB columns via attribute mapping (no migration;
    deeper column rename deferred). Aggregated per profile by `/api/settings/
    profiles`. Rubric keys: `weights`/`thresholds` (SOPS) + `completion_weights`/
    `completion_thresholds`.
  - `config_store.py` — DB-backed runtime config + defaults (targets, weights,
    thresholds, `iterations`, `monitoring`, `correlation`, `trends`, `experiment`,
    `rubric_version`).
  - `runner.py` — orchestrates a run across plugins (derives metrics from raw via
    `interpret`, median-aggregated over iterations, per-run SOPS confidence band),
    stores `BenchmarkResult.raw` as the source of truth, captures the firewall
    settings + fingerprint per run, and holds run-lifecycle safety
    (`reconcile_interrupted_runs`, `fail_stale_runs`, `rescore_run` = re-grade cached
    scalars under a new rubric, `rederive_run` = re-run derivation+scoring from raw).
  - `trends.py` — historical baselines by day-of-week × hour-of-day (viewer-local);
    `relative_reading`/`profile_relative` give a time-adjusted "vs typical" delta
    ("wins above replacement"). Powers `/api/trends/*`, the Dashboard delta chip,
    and the Settings-Impact "vs typical" column.
  - `sweep.py` — **Shotgun Sweep**: an on-demand foreground sweep of a quantum ×
    target grid. Applies each variant for real, benchmarks it, **restores the
    baseline at the end** (`reconcile_interrupted_sweeps` restores on startup too).
    Runs in its own thread; the scheduler yields while `sweep.active()`.
  - `scheduler.py` — daemon thread: watchdog → (yield if a sweep is active) →
    experiment step → monitoring run (serialized so benchmark runs never overlap).
  - `experiment.py` — autonomous window-gated single-parameter shaper sweep
    (writes via `provider.apply()`; disarmed + dry-run by default; restores baseline).
  - `settings_profile.py` — normalize/fingerprint/summarize firewall profiles for
    settings-vs-responsiveness correlation (`/api/settings/*`).
  - `database.py` — engine/session + additive SQLite `_migrate()` (ALTER for new
    columns; `create_all` for new tables).
  - `api/` — REST routers mounted at `/api`.
- `frontend/` — React + TS + Vite + MUI dashboard (dark mode). Pages: Dashboard,
  History, Trends, Compare, Settings Impact, Experiments, Shotgun Sweep, Config,
  Plugins, Run Detail.
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
- Lower-is-better for all current SOPS metrics; thresholds define best/worst and
  are interpolated on a perception-calibrated log curve (Weber–Fechner). The
  rubric (weights+thresholds+`rubric_version`) is versioned. **Two re-grade paths:**
  `POST /api/score/rescore` re-applies the rubric to cached metric scalars (after a
  weight/threshold change); `POST /api/score/rederive` re-runs the whole
  interpretation from stored `BenchmarkResult.raw` (after a new metric or changed
  `DERIVATION_VERSION`), so history reflects it without re-collecting.
- A run repeats the suite `iterations` times; the headline SOPS is the **median**
  over iterations, with a confidence band (`sops_stdev/min/max`). The Dashboard
  shows a windowed **rolling** score (`/api/score/rolling`, 24h median + IQR) plus
  a **"vs typical"** delta vs the day/hour historical baseline (`trends.py`).
- **Current vs. legacy scoring (no dual-score machinery).** A run scored before
  the current rubric (no longest-stall / byte-arrival metrics —
  `metrics.has_latest_metrics`, keyed off `marks_latest`, now `longest_stall`) isn't
  comparable, so it's **quarantined**, not
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
  network-vs-render stall attribution and protocol mix. Promoted into SOPS (rubric
  `perceptual-v4`): byte earliness / longest stall / perceived time replace the
  pixel Speed Index / paint cadence (now opt-in diagnostics). Per-run records +
  two-config comparison at `/api/smoothness/*` (keyed on `settings_fingerprint`);
  an offline **calibration harness** (`calibration/`) fits the perceived-time
  weight ratio to subjective 1–10 ratings.
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
