# CLAUDE.md — PathBrain developer guide

PathBrain is an empirical network-optimization platform that maximizes
*human-perceived responsiveness* — scored as the **Responsiveness / Smoothness /
Speed** axes (+ an **Overall** corner roll-up), not raw ping/throughput. (The
original single *Seat of Pants Score* was split into these axes; SOPS is now
legacy.) The optimizer is classical (deterministic sweep + hysteresis), not
LLM-based. See `README.md` for the product overview.

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
  - `scoring/engine.py` — the generic score **primitive**: `compute_score` takes a
    metric set + weights + thresholds and returns a 0–100 weighted average on a
    perception-calibrated log curve, redistributing missing-metric weight. Axis-
    agnostic; *which* metrics form *which* axis lives in `methodology.py`.
  - `methodology.py` — **the published, versioned rubric** (derivation + axis
    weights/thresholds), append-only. `CURRENT_METHODOLOGY` = `speed-smoothness-v4`,
    which scores **three headline axes** (the temporal phases of a load; each metric
    maps to exactly one axis):
    - **Responsiveness** (time-to-first): byte-earliness (30) + FCP (25) + TTFB (15).
    - **Smoothness** (steady fill): longest-stall (40, required) + perceived-time (30)
      + cadence (15) + evenness (15).
    - **Speed** (time-to-last + interactive): LCP (40) + INP (40) + render (20).
    Plus secondary **Stability** (CLS) and **Completion** (DNS/TCP/TLS/jitter/loss),
    kept out of the headline since they barely move human feel.
    `runner._score_multi_axis` scores every axis generically via `axis_rubric` +
    `compute_score`, persisting per-axis results to `Score.axis_scores` (JSON).
    Predecessors (`speed-smoothness-v1..v3`, original blended single-SOPS rubrics) are
    frozen for old at-measure scores. The **Overall** corner-score is a *derived
    presentation* roll-up in `routes_settings` — not a scored axis, never persisted.
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
  - `scheduler.py` — daemon thread: watchdog → (yield while the coordination lock is
    held) → experiment step → monitoring run (serialized so benchmark runs never overlap).
  - `experiment.py` — autonomous window-gated single-parameter shaper sweep
    (writes via `provider.apply()`; disarmed + dry-run by default; restores baseline).
  - `coordinator.py` — process-wide lock that serializes any apply-firewall + benchmark
    session (sweep, profile test, experiment, monitoring, manual run): user-triggered
    ones `hold` (queue), periodic ones `try_hold` (defer). Pairs with the read-before/
    read-after fingerprint check in `runner.execute_run` (FAILs a run on mid-run drift).
  - `jobs.py` — in-process background-job registry (progress/status/recent history).
    The heavy score passes (`/api/score/regrade|rescore|rederive`) run as jobs and
    return `202 {job_id}`; `/api/jobs` (`api/routes_jobs.py`) merges them with read-only
    adapters for active runs/sweep/profile-test/experiment so the top-right jobs
    dropdown shows everything. History is in-memory (durable ops live in their DB rows).
  - `profile_test.py` — **Test to minimum**: apply a stored profile, run exactly the
    iterations still needed to reach `correlation.min_iterations`, then **restore the
    baseline** (persisted to a `ProfileTest` row; `reconcile_interrupted_profile_tests`
    restores on startup). `/api/settings/test-profile`.
  - `challenger.py` — **Challenger Race**: the adaptive, multi-profile sibling of
    `profile_test`. A time-boxed loop that runs **one iteration at a time** on the most
    promising under-minimum profile, re-ranks via `rank_challengers`, and **eliminates**
    any whose *optimistic* Overall (corner over each axis's p75 upper estimate;
    `routes_settings.optimistic_overall`) can no longer beat the confident best. A
    challenger that reaches the minimum and beats the best raises the bar. At the end it
    **restores the baseline**, or applies the winner when `auto_promote`. Own thread
    under the `coordinator` lock (so the scheduler defers via `coordinator.busy()`);
    persisted to a `ChallengerRace` row; `reconcile_interrupted_challenges` restores on
    startup. `/api/settings/race` (+ `/race/cancel`).
  - `settings_profile.py` — normalize/fingerprint/summarize firewall profiles for
    settings-vs-responsiveness correlation (`/api/settings/*`). Profile confidence is
    gated on **total iterations** (`correlation.min_iterations`, default 15). The
    crowned **"best"** profile is the confident one closest to the perfect corner —
    `/api/settings/profiles` derives an **Overall** = closeness to the (100, 100, 100)
    corner across the three headline axes (`_CORNER_AXES` =
    Responsiveness/Smoothness/Speed; `_corner_overall`), ranks + returns
    `best_fingerprint` by it, and aggregates per profile the median of every axis score
    *and* every metric we collect (`metrics.all_metric_sources`) to power the dynamic
    quadrant + table column selector.
  - `database.py` — engine/session + additive SQLite `_migrate()` (ALTER for new
    columns; `create_all` for new tables).
  - `api/` — REST routers mounted at `/api`.
- `frontend/` — React + TS + Vite + MUI dashboard (dark mode). Pages: Dashboard,
  History, Trends, Compare, Settings Impact (**paginated** sortable table — 25/page —
  with standard **Overall / Responsiveness / Smoothness / Speed** columns + an optional
  column selector; a **dynamic** any-metric quadrant where X/Y pick the axes, a **Shade**
  picker encodes a third field as dot **opacity** (brighter = better; `ProfileQuadrant`),
  and the crowned profile is ringed; plus "Test to minimum" and **"Race challengers"**),
  Experiments, Shotgun Sweep, Config, Methodology, Plugins, Data Dump, Run Detail. A
  top-right **jobs dropdown** (`JobStatus`) shows every running/recent background job
  (re-grade, sweep, run, profile test, challenger race, …).
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
  are interpolated on a perception-calibrated log curve (Weber–Fechner). The
  rubric (axes+weights+thresholds) is bundled into a versioned **methodology**.
  **Re-grade paths:** `POST /api/score/regrade` re-scores every run from raw under
  the current methodology, writing new `Score` rows (use this after publishing a new
  methodology — e.g. the v4 axis split); `POST /api/score/rescore` / `rederive` are
  the legacy in-place paths over cached scalars / raw.
- A run repeats the suite `iterations` times; each headline axis is the **median**
  over iterations, with a confidence band. The Dashboard shows a windowed
  **rolling** score (`/api/score/rolling`, 24h median + IQR) plus a **"vs typical"**
  delta vs the day/hour historical baseline (`trends.py`).
- **Current vs. legacy scoring (no dual-score machinery).** A run scored before
  the current rubric (no longest-stall / byte-arrival metrics —
  `metrics.has_latest_metrics`, keyed off `marks_latest`, now `longest_stall`) isn't
  comparable, so it's **quarantined**, not
  reconciled: Dashboard rolling + History trend exclude legacy; the History list
  hides it behind a "Show legacy" toggle; Run Detail/Compare flag it
  (`ScoreOut.legacy`/`RunSummary.legacy`); Settings Impact aggregates `complete_only`
  (default true). Legacy runs are kept for their *settings* history, not their score.
  Responsiveness/Smoothness/Speed (+ the Overall roll-up) are the ranked headlines;
  Stability and Completion are opt-in diagnostics.
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
- **Phase 6 (done):** **three-axis headline** (methodology `speed-smoothness-v4`):
  split the blended Speed into **Responsiveness** (time-to-first) + a redefined
  **Speed** (time-to-last + interactive), with a derived **Overall** corner roll-up;
  Settings Impact gained the dynamic any-metric quadrant (opacity-shaded third axis) +
  a paginated, column-selectable table; and the **Challenger Race** (`challenger.py`) —
  an adaptive, time-boxed elimination race that promotes limited-data profiles toward
  confidence one iteration at a time.
- **Next:** speed test / bufferbloat (latency-under-load), multi-parameter Bayesian
  search + interleaved A/B with effect-size/CI + hysteresis, routing intelligence /
  SD-WAN.

⚠️ Firewall **writes** go only through `provider.apply()`. Six callers use it, all
snapshot/restore or are reversible: the experiment engine (disarmed + dry-run by
default), the Shotgun Sweep (restores baseline at end + on startup), config
test-apply (+1 then revert), sweep apply-best (explicit, supervised), the
profile test (`profile_test.py`: apply → benchmark → restore, baseline persisted +
reconciled on startup), and the **challenger race** (`challenger.py`: time-boxed
apply → 1 iteration → re-rank, restoring the baseline at the end — or applying the
winner when `auto_promote` — baseline persisted + reconciled on startup). Keep new
write paths to `provider.apply()` and always snapshot/restore.

⚠️ Any **apply-firewall + benchmark** session must hold the `coordinator.py` lock so
two never overlap (user-triggered ones — sweep, profile test, challenger race, manual
`/api/run` — `hold` and queue; periodic ones — monitoring, experiment — `try_hold` and
defer).
`runner.execute_run` independently re-reads the firewall fingerprint **after** the
run and FAILs it on drift (the read-before/read-after integrity check), so "what we
tested" always matches "what we thought". A profile is **confident** once its runs
total ≥ `correlation.min_iterations` (default 15) — iterations, not run count, are
the unit of signal.

The browser engine imports Playwright lazily, so the plugin registry still loads
where Playwright/Chromium isn't installed (it returns `success=False` and the
browser metrics' weight is redistributed). The byte-arrival smoothness metrics need
only Resource Timing (always present); the opt-in filmstrip/Speed Index degrade
gracefully without CDP screencast or Pillow. Chromium is installed in the Docker image.
