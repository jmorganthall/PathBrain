<h1 align="center">PathBrain</h1>

<p align="center">
  <b>A Seat of Pants Score engine for your Internet connection.</b><br>
  It doesn't ask "is your ping low?" — it asks <i>"does the Internet actually <b>feel</b> faster?"</i><br>
  …then tracks that score over time and correlates it with your network settings
  (OPNsense FQ-CoDel / SQM being the first-class integration).
</p>

<p align="center">
  <img alt="OPNsense" src="https://img.shields.io/badge/firewall-OPNsense-D94F00?logo=opnsense&logoColor=white">
  <img alt="SQM" src="https://img.shields.io/badge/SQM-FQ--CoDel-blueviolet">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white">
  <img alt="React" src="https://img.shields.io/badge/UI-React%20%2B%20MUI-61DAFB?logo=react&logoColor=black">
  <img alt="Docker" src="https://img.shields.io/badge/deploy-Docker-2496ED?logo=docker&logoColor=white">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green">
</p>

---

## What is PathBrain?

PathBrain measures the one thing common tools don't: **how responsive your
Internet connection actually *feels*.** It runs real benchmark suites, distills
them into a single **Seat of Pants Score (SOPS)** that models **human-perceived
responsiveness**, and tracks that score over time — so you can finally answer
*"when was the Internet fastest?"* and *"did that change make it feel better or
worse?"* with data instead of folklore.

Most tools optimize for **ping**, **throughput**, or synthetic scores. None of
those reliably answer what a human cares about: *when I click something, how fast
does it feel?* SOPS is built for exactly that — and deliberately keeps raw ping
from dominating.

Where it gets powerful: PathBrain can **correlate your score with the network
settings that were live when each run ran.** Its first-class integration is the
**[OPNsense](https://opnsense.org/) API**, which it uses to discover your
**FQ-CoDel / SQM** traffic-shaper configuration (bandwidth, quantum, limit,
target, interval, ECN, flows, …). That turns the eternal SQM question — *what
settings are actually best?* — into an empirical, measured answer.

- **No firewall?** It's still a first-class **SOPS tracker** for your connection.
- **Running OPNsense SQM?** You also get settings-vs-responsiveness correlation
  and (on the roadmap) closed-loop autonomous tuning — apply a candidate,
  benchmark, keep it if SOPS improved, roll back if not, always snapshot-first.

> The provider layer is pluggable (pfSense / Linux `tc` can follow), with OPNsense
> traffic shaping as the first-class integration.

> **Philosophy:** Empirical. Never assume. Never rely on folklore. Every
> optimization is tested, measured, scored, and historically tracked.

### The score: three perceptual axes (methodology `speed-smoothness-v6`)

PathBrain scores the **three temporal phases of a page load** as independent
0–100 axes, rather than blending them into one number. (The original single
*Seat of Pants Score* was split into these — SOPS is now legacy.) Each metric is
normalized to a 0–100 subscore against configurable *best/worst* thresholds
(perception-calibrated log curve), then weight-averaged within its axis:

| Axis | Answers | Metrics (weights) |
| --- | --- | --- |
| **Responsiveness** | How fast does the *first* content appear? | byte-earliness (30) · FCP (25) · TTFB (15) |
| **Smoothness** | How steadily does it fill in (minimized wait)? | longest-stall (40, required) · total-stall (30) · cadence (15) · evenness (15) |
| **Speed** | How soon is it *fully visible + interaction-ready*? | LCP (40) · INP (40) · render (20) · load-event (20) |

Plus two **secondary** axes: **Stability** (layout shift / CLS) and **Completion**
(raw DNS/TCP/TLS/jitter/loss infra timing) — diagnostic only, never folded into the
headline axes since they barely move human feel.

**Overall** is a single higher-is-better roll-up — and since `speed-smoothness-v5`
it's a **first-class, versioned, persisted** quantity, not just a presentation
measure. It's the **corner** (closeness to the perfect 100-corner) over a small,
hand-picked **crown metric set** — under v6 that's **FCP × total-stall × load-event**
(quickest first response × cumulative dead-air × page-load time). It's an
*intersection*, not a mean: one weak metric pulls Overall down through the corner
geometry and can't be averaged away. The crowned **"best"** profile is the confident
one with the highest **probability of being the true best** (a Bayesian/Thompson
Monte-Carlo over each profile's posterior), so it weighs both *how good* and *how
sure*.

Design choices:

- **The journey beats the endpoint.** Smoothness isolates *how* the page filled in
  (the network layer you can actually tune), kept distinct from when it started
  (Responsiveness) and when it finished (Speed).
- **Missing metrics never penalize.** If a metric is unavailable (e.g. a paint
  metric where the browser engine didn't run, or a failed probe), its weight is
  redistributed across the metrics that *are* present within the axis.
- **Axes are never blended.** Each is reported and ranked on its own.

Plugins are **pure sensors** that store raw observations; all interpretation
(jitter = stddev of pings, byte-earliness = area over the cumulative-bytes curve,
the axis scores themselves) lives in a separate, **versioned methodology** layer —
so a new metric, a re-weight, or an axis re-partition is published as a new
methodology version and re-graded over history straight from raw, without
re-collecting (`POST /api/score/regrade`). `speed-smoothness-v6` is the
published-now version. A too-lenient threshold can even be **re-anchored from the
UI** (`POST /api/methodologies/reanchor` forks the current version with a tightened
`best` and re-grades), and every shaper field PathBrain reads/writes/sweeps is
declared once in a single **`shaper_fields` registry** so identity/writable/sweepable
can't drift apart.

---

## Status — what works today ✅

- 🔌 **Plugin benchmark engine** — six registered benchmarks (**pure sensors** that
  store raw observations): `icmp` (per-ping RTT series), `dns` (per-resolver lookup),
  `tcp` (connect), `tls` (handshake), `http` (TTFB / bytes / timing), and `browser`
  (headless-Chromium nav/paint timing + a **filmstrip**, with screenshot & HAR).
- 🧮 **Three perceptual axes** — perception-calibrated **log curve** (Weber–Fechner):
  **Responsiveness** (time-to-first), **Smoothness** (the steady fill, led by byte-
  arrival metrics — longest-stall/total-stall/cadence/evenness), and **Speed** (time-to-
  last + interactive), plus a first-class **Overall** corner (v6: FCP × total-stall ×
  load-event). Raw-only collection + a **versioned methodology**: `POST /api/score/regrade`
  re-scores history from raw under any published methodology — without re-collecting.
- 🌦️ **Historical trends + "vs typical"** — per-metric baselines by day-of-week ×
  hour-of-day (`/api/trends/*`); the Dashboard, a dedicated **Trends** page, and
  **Settings Impact** read each result *relative to its historical norm* ("wins
  above replacement"), so a config is judged fairly for the times it actually ran.
- 🎯 **Shotgun Sweep** — an on-demand grid sweep over the registry's sweepable shaper
  fields (quantum × target today): applies each variant for real, benchmarks it, ranks
  by SOPS + "vs typical", and **restores the baseline** at the end (and on startup if
  interrupted). Marking another field sweepable surfaces it end to end — engine *and* UI
  control — with no code branch. Plus a reversible **config write-test**
  (`POST /api/config/test-apply`) to validate the firewall apply path.
- 🔁 **Multi-iteration runs** — repeat the suite N times and take the **median**,
  with a per-run **confidence band** (± / range) and an **ETA**. Per-plugin iteration
  caps keep runs fast: the heavy browser runs fewer iterations than the cheap network
  probes and **reuses one Chromium** across them, and unscored captures (screenshot/HAR)
  are off by default — without changing what's scored.
- 📈 **Continuous monitoring + rolling score** — optional scheduler runs the suite
  on an interval; the Dashboard shows a windowed **median (24h) + IQR** so
  "current responsiveness" is stable, not point-in-time noise.
- 🔍 **OPNsense discovery + settings correlation** — each run captures the live
  FQ-CoDel/SQM settings + a **fingerprint**; runs group into **profiles** with their
  score distribution and a **significant-change** banner (effect ≥ threshold). The
  **crowned "best"** profile is the confident one with the highest **probability of being
  the true best** over its **Overall** — the v6 corner over **FCP × total-stall ×
  load-event** — so "best" is genuinely *starts fast, stays smooth, **and** finishes
  fast*. The quadrant is **dynamic** (plot any two numeric fields we collect; the crowned
  profile is ringed; a **Shade** picker encodes a third field as dot **opacity** — brighter
  = better — and it **warns when an axis is saturated**, i.e. every profile already past
  the methodology's `best` threshold so the spread carries no score signal), and the
  **paginated** profiles table (25/page) has standard Overall/Responsiveness/Smoothness/
  Speed columns plus an **optional column selector**. A page-level **saturation check**
  flags any too-lenient threshold and offers a one-click **re-anchor** (forks the
  methodology with a tightened `best` and re-grades). A profile is **confident** once its
  runs total ≥ `correlation.min_iterations` (default **15**). Beyond the crown there's a
  **"Heirs to the crown"** card — the limited-data / stale profiles whose *optimistic
  ceiling* could still dethrone it, ranked by margin (and filtered to profiles the live
  firewall can actually be driven to). To collect the data: **"Test to minimum"** tops one
  profile up (apply → run the iterations still needed → **restore**); **"Race
  challengers"** is a time-boxed, adaptive race that tests promising profiles **one
  iteration at a time**, eliminating any that can't overtake the best (and skipping any it
  can't reach), optionally auto-promoting a confirmed winner; **"Re-run all profiles"**
  re-benchmarks every stored profile for fresh, comparable data after a methodology change.
  Mock provider for offline dev.
- ⬆️ **Version awareness** — the image is stamped with its build commit; the app does a
  cached, best-effort check against the latest commit on `main` (`GET /api/version`) and
  shows an **"Update available"** chip in the top bar when a newer `:latest` is pullable.
  On by default; set `PATHBRAIN_UPDATE_CHECK=false` to disable.
- 🔔 **Background jobs + status dropdown** — long operations (re-grade / re-score /
  re-derive history) run **in the background** with live progress instead of blocking;
  a top-right notifications dropdown (`GET /api/jobs`) shows every active +
  recently-finished job — score passes, benchmark runs, sweeps, profile tests,
  experiments — in one place.
- 🔒 **Firewall/benchmark coordination** — a single in-process lock (`coordinator.py`)
  serializes every apply-firewall-and-benchmark session (sweep, profile test,
  experiment, monitoring, manual run) so two never overlap. Each run also re-reads the
  firewall fingerprint **before and after** measuring and is marked FAILED on drift —
  so "what we tested" always matches "what we thought we tested".
- 🧾 **Data Dump** — one consolidated JSON export of the last *N* runs, including each
  plugin's **raw observations** per iteration (the per-run view omits raw); view, copy,
  or download (`GET /api/history/dump`).
- 🧪 **Experiment engine** — within a configurable **window**, sweep one shaper
  parameter across candidates, benchmark each, and **restore the pre-window
  baseline** at close (or auto-promote a clear winner). **Disarmed + dry-run by
  default.** Firewall writes go only through `provider.apply()` (experiment, Shotgun
  Sweep, config write-test, profile test, sweep apply-best) — each reversible and
  snapshot/restore, and serialized by the coordination lock above.
- 🛡️ **Run-lifecycle safety** — startup reconciliation + a watchdog timeout +
  manual cancel so a restart/hang never leaves a zombie "running" job.
- 📊 **Web dashboard** — React + MUI, dark mode: Dashboard (rolling score + "vs
  typical" + metric breakdown), History, **Trends** (day/hour heatmaps), Compare,
  Settings Impact (sortable profiles table with an **Overall** column + column
  selector, a **dynamic** any-metric quadrant with saturation warnings, **Heirs to the
  crown**, "Test to minimum" / "Race challengers" / "Re-run all profiles"),
  Experiments, **Shotgun Sweep**, Config, **Methodology** (versioned rubric + one-click
  re-grade / re-anchor), Plugins, **Data Dump**, Run Detail (with filmstrip), and a global
  **jobs** status dropdown.
- 💾 **SQLite persistence** with additive auto-migrations; background execution.

**Next:** speed test / bufferbloat (latency-under-load), A/B weight calibration from
blind ratings, multi-parameter Bayesian search + interleaved A/B with effect-size/CI
+ hysteresis, routing intelligence / SD-WAN.

---

## Quick start (Docker)

The whole stack runs as a **single container** — the API serves the built UI.

### Option A — pull the pre-built image (recommended)

A GitHub Action publishes a ready-to-run image to the **GitHub Container
Registry** on every push. No source checkout, no build:

```bash
# Grab just the compose file
curl -O https://raw.githubusercontent.com/jmorganthall/pathbrain/main/docker-compose.ghcr.yml

docker compose -f docker-compose.ghcr.yml up -d
```

Update later with `docker compose -f docker-compose.ghcr.yml pull && docker compose -f docker-compose.ghcr.yml up -d`.
Pin a release by changing `:latest` to a tag like `:v0.1.0`.

> If the GHCR package is **private**, log in once first:
> `docker login ghcr.io -u <you> -p <token-with-read:packages>`.

### Option B — build from source

```bash
git clone https://github.com/jmorganthall/pathbrain.git
cd pathbrain
docker compose up --build
```

Then open **http://localhost:8000** and click **Run Benchmark**.

Persistent state (SQLite DB, snapshots, browser artifacts) lives in the
`pathbrain-data` Docker volume, so it survives restarts and image rebuilds.

### `docker-compose.yml`

The bundled compose file (excerpt) — point it at your firewall by setting the
`PATHBRAIN_*` variables (see [Configuration](#configuration)):

```yaml
services:
  pathbrain:
    build: .                       # or: image: pathbrain:latest
    container_name: pathbrain
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      PATHBRAIN_DATABASE_URL: "sqlite:////data/pathbrain.db"
      PATHBRAIN_LOG_LEVEL: "INFO"
      PATHBRAIN_CONFIG_PROVIDER: "${PATHBRAIN_CONFIG_PROVIDER:-mock}"
      PATHBRAIN_OPNSENSE_URL: "${PATHBRAIN_OPNSENSE_URL:-}"
      PATHBRAIN_OPNSENSE_API_KEY: "${PATHBRAIN_OPNSENSE_API_KEY:-}"
      PATHBRAIN_OPNSENSE_API_SECRET: "${PATHBRAIN_OPNSENSE_API_SECRET:-}"
      PATHBRAIN_OPNSENSE_VERIFY_TLS: "${PATHBRAIN_OPNSENSE_VERIFY_TLS:-false}"
    volumes:
      - pathbrain-data:/data

volumes:
  pathbrain-data:
```

> **Unraid:** the simplest path is the **Docker Compose Manager** plugin —
> create a stack from [`docker-compose.ghcr.yml`](docker-compose.ghcr.yml) and
> drop a `.env` file (copied from [`.env.example`](.env.example)) next to it;
> Compose auto-loads it. Publish port `8000` and keep the single `/data` volume.
> Because PathBrain measures *your* path to the Internet, run it on the network
> whose responsiveness you want to score.

---

## Configuration

PathBrain separates **infrastructure** config (env-only) from **runtime**
benchmark config (DB-backed, editable live).

### Infrastructure (environment variables)

Copy [`.env.example`](.env.example) to `.env` and edit. Most are prefixed
`PATHBRAIN_` (plus the standard `TZ`).

| Variable | Default | Purpose |
| --- | --- | --- |
| `PATHBRAIN_DATABASE_URL` | `sqlite:///./data/pathbrain.db` | SQLAlchemy DB URL (Postgres later) |
| `PATHBRAIN_ARTIFACT_DIR` | `./data/artifacts` | Browser screenshots / HAR files |
| `PATHBRAIN_HOST` / `PATHBRAIN_PORT` | `0.0.0.0` / `8000` | Bind address / port |
| `PATHBRAIN_LOG_LEVEL` | `INFO` | Log verbosity |
| `TZ` | `UTC` | Local timezone for the experiment **window** hours (and logs) |
| `PATHBRAIN_CONFIG_PROVIDER` | `mock` | `mock` or `opnsense` |
| `PATHBRAIN_OPNSENSE_URL` | — | OPNsense base URL, e.g. `https://192.168.1.1` |
| `PATHBRAIN_OPNSENSE_API_KEY` | — | OPNsense API key |
| `PATHBRAIN_OPNSENSE_API_SECRET` | — | OPNsense API secret |
| `PATHBRAIN_OPNSENSE_VERIFY_TLS` | `false` | Verify the firewall's TLS cert |

**Example `.env`:**

```dotenv
# Storage
PATHBRAIN_DATABASE_URL=sqlite:///./data/pathbrain.db
PATHBRAIN_LOG_LEVEL=INFO

# Use the live firewall instead of the mock provider
PATHBRAIN_CONFIG_PROVIDER=opnsense
PATHBRAIN_OPNSENSE_URL=https://192.168.1.1
PATHBRAIN_OPNSENSE_API_KEY=your_api_key_here
PATHBRAIN_OPNSENSE_API_SECRET=your_api_secret_here
PATHBRAIN_OPNSENSE_VERIFY_TLS=false
```

> **OPNsense permissions.** Create the API key/secret under **System → Access →
> Users → (your user) → API keys**. The user needs **traffic-shaper read** access
> (page privilege **"Firewall: Shaper"**, and/or **"System: Settings: Traffic
> Shaper"**), or use an admin account — without it discovery returns 403. The
> **experiment engine, Shotgun Sweep, config write-test, and profile "Test to
> minimum" additionally write** to the shaper (`setPipe` + `reconfigure`), so those
> need write access; each is reversible and snapshots/restores the baseline (the
> experiment engine is also disarmed + dry-run by default).

### Runtime (DB-backed, edit on the Config page or `PUT /api/config`)

All of this is stored in the database and deep-merged over defaults, so the first
run needs no setup:

- **Benchmark targets** — ICMP/DNS/TCP/TLS/HTTP/browser hosts & URLs.
- **`iterations`** — suite repeats per run; the headline SOPS is the **median**.
- **`monitoring`** — `enabled`, `interval_minutes`, `run_timeout_minutes` (watchdog).
- **`correlation`** — `significant_change_pct`, `min_iterations` (default 15; the
  total-iterations bar a profile must clear to count as confident), `min_runs` (legacy).
- **`trends`** — `lookback_days`, `window_hours`, `min_samples` (historical baselines).
- **`rubric_version` / `weights` / `thresholds`** — the scoring rubric (perception
  curve). After editing, **Save → Re-score history** to keep the timeline comparable.
- **`experiment`** — `enabled`, `dry_run`, `auto_promote`, `param`, `candidates`,
  `window` (days/hours, container `TZ`), `dwell_minutes`, `min_trials_per_value`,
  `improve_pct`. Disarmed + dry-run by default.

Run results show per-metric **median ± stdev** and a SOPS confidence band; an ETA
is estimated from recent runs.

---

## API reference

Interactive docs are served at `/docs` (Swagger) and `/redoc`. Base path: `/api`.

| Method & path | Description |
| --- | --- |
| `POST /api/run` | Trigger a benchmark suite (body: optional `iterations`) |
| `POST /api/runs/{id}/cancel` | Cancel an in-progress run (manual stop / unstick) |
| `GET /api/runs/estimate` | Mean per-iteration duration from recent runs (ETA) |
| `GET /api/results/latest` / `…/{id}` | Latest / specific run detail (poll while running) |
| `GET /api/history` | Paginated runs list (`limit`, `offset`) |
| `GET /api/history/count` | Total run count (for pagination) |
| `GET /api/history/series` | Time-series of SOPS + metrics for charts |
| `GET /api/history/dump` | Consolidated JSON of the last `limit` runs incl. raw observations |
| `GET /api/score/{id}` / `…/weights` | Run score / current weights + thresholds |
| `GET /api/score/rolling` | Windowed median SOPS + IQR + aggregated subscores |
| `POST /api/score/regrade` | Re-score history under the current methodology (background job → `202 {job_id}`) |
| `POST /api/score/rescore` | Re-grade all history with the current rubric (background job) |
| `POST /api/score/rederive` | Re-run derivation+scoring from stored raw (background job) |
| `POST /api/score/preview` | Score ad-hoc metrics with current weights |
| `GET /api/jobs` | Active + recently-finished background jobs (powers the status dropdown) |
| `GET /api/trends/heatmap` | Per-metric baseline grid by day-of-week × hour-of-day |
| `GET /api/trends/relative` | Current reading vs. its historical baseline ("vs typical") |
| `GET /api/monitoring` | Continuous-monitoring scheduler status |
| `GET /api/config` / `PUT` / `POST …/reset` | Read / update / reset benchmark config |
| `POST /api/config/adopt-rubric` | Load perception-calibrated default rubric |
| `GET /api/config/provider` | Discovery provider health (cause shown if down) |
| `POST /api/config/discover` | Discover FQ-CoDel settings + store a snapshot |
| `POST /api/config/test-apply` | Reversible write-path test (nudge quantum +1, then restore) |
| `GET /api/config/snapshots` | List stored config snapshots |
| `GET /api/settings/profiles` | Per-profile scores + per-metric medians, `overall`/`best_fingerprint` (corner), selectable `fields` |
| `GET /api/settings/impact` | Significance of the latest settings change |
| `POST /api/settings/apply-profile` | Write a stored profile to the firewall (`preview` for a dry diff) |
| `POST /api/settings/test-profile` · `GET …/test-profile/current` | "Test to minimum": apply → run → restore / poll its status |
| `POST /api/settings/race` · `GET …/race` · `POST …/race/cancel` | "Race challengers": start/poll/cancel the adaptive time-boxed elimination race |
| `POST /api/settings/backfill` | Stamp current settings onto unattributed runs |
| `GET /api/settings/diagnostics` | Settings-capture diagnostics (stamped/unstamped) |
| `GET /api/experiments` / `…/{id}` | Experiment status + history / one experiment's trials |
| `POST /api/experiments/abort` | Abort the running experiment, restore baseline |
| `POST /api/sweep/preview` | Shotgun Sweep variant count + ETA for a spec |
| `POST /api/sweep` · `GET /api/sweep/current` | Start a sweep / poll the active (or latest) sweep |
| `POST /api/sweep/{id}/cancel` · `…/apply-best` | Cancel (restores baseline) / apply the winning variant |
| `GET /api/plugins` | List registered benchmark plugins |
| `GET /api/health` | Liveness / version |
| `GET /api/version` | Build commit + cached "newer build available to pull" check |

---

## Running from source

**Backend** (FastAPI):

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn pathbrain.main:app --reload --host 0.0.0.0 --port 8000
```

**Frontend** (Vite dev server, proxies `/api` → `:8000`):

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

**Tests:**

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest
```

---

## Project structure

```
backend/pathbrain/
  main.py            FastAPI app (serves UI; startup reconcile; /artifacts)
  config.py          Env-driven infrastructure settings
  database.py        SQLAlchemy engine/session + additive SQLite migrations
  models.py          ORM: Run, BenchmarkResult (+raw), ScoreResult, ConfigSnapshot,
                     AppConfig, Methodology, Score, Experiment, ExperimentTrial,
                     Sweep, ProfileTest
  schemas.py         Pydantic request/response models
  coordinator.py     Process-wide lock: serializes apply-firewall + benchmark sessions
  jobs.py            In-process background-job registry (progress) for the /api/jobs feed
  runner.py          Run orchestration; median aggregation; read-before/after integrity;
                     reconcile/watchdog/rescore/rederive
  scheduler.py       Daemon thread: watchdog → (yield while a session holds the lock) →
                     experiment → monitoring
  experiment.py      Window-gated autonomous shaper sweep (writes via provider.apply)
  sweep.py           Shotgun Sweep: on-demand grid sweep, applies + restores baseline
  profile_test.py    "Test to minimum": apply a profile, run the needed iterations, restore
  challenger.py      "Race challengers": adaptive 1-iteration-at-a-time elimination race
  trends.py          Day/hour historical baselines + time-adjusted "vs typical"
  settings_profile.py  Normalize/fingerprint/summarize firewall profiles
  config_store.py    DB-backed runtime config + defaults
  logging_config.py  Structured logging
  api/               REST routers (one module per resource)
  plugins/           Benchmark plugins + registry (base.py) — pure sensors (raw only)
  interpret/         Raw observations → metric values (derive.py, versioned)
  providers/         Config discovery + apply (opnsense.py, mock.py)
  scoring/           SOPS engine (engine.py, perception-calibrated)
frontend/            React + TS + Vite + MUI dashboard (dark mode)
Dockerfile           Playwright base image; build UI, serve from API
docker-compose*.yml  Build (.yml) and pull-from-GHCR (.ghcr.yml) deploys
.github/workflows/   docker-publish.yml → ghcr.io/jmorganthall/pathbrain:latest
```

### Extending PathBrain

- **New benchmark:** drop a module in `plugins/`, subclass `BenchmarkPlugin`,
  decorate with `@register`, and return a `PluginResult` with **raw observations
  only** (`raw=…`) — derive the scoreable metrics in `interpret/derive.py`. Plugins
  must never raise for measurement failures — return `success=False` with an `error`.
- **New firewall:** subclass `ConfigProvider` in `providers/` and implement
  `discover()` / `snapshot()` (and `apply()` to support the experiment engine).

---

## Roadmap

- [x] **Phase 1 — Foundation:** benchmark engine, SOPS scoring, history, config
      discovery, REST API, dashboard.
- [x] **Phase 2 — Browser engine:** headless Chromium via Playwright (navigation
      timing, DOMContentLoaded, load, network idle, total render, screenshot +
      HAR). The `render` SOPS weight activates automatically.
- [x] **Continuous monitoring:** scheduled recurring runs + a windowed rolling
      score (median + IQR) for stable "current responsiveness."
- [x] **Settings-vs-responsiveness correlation:** each run is fingerprinted with the
      live FQ-CoDel/SQM settings; runs group into profiles (confidence gated on **total
      iterations**, default 15) with a significant-change banner. "Best" = the profile
      closest to the perfect **Responsiveness / Smoothness / Speed** corner (a single
      **Overall** score), shown on a **dynamic any-metric quadrant** with the crowned
      profile ringed, plus a sortable table with an optional **column selector** for any
      metric.
- [x] **Firewall/benchmark coordination + integrity:** a single lock serializes every
      apply-and-benchmark session, and each run re-reads the firewall before/after
      measuring (FAILed on drift). A **"Test to minimum"** action tops a limited-data
      profile up to the confidence bar, then restores the prior settings. Plus a
      **Data Dump** export (last *N* runs incl. raw) and a **background-jobs** system
      (long score passes run async with a top-right progress dropdown).
- [x] **Trajectory-aware scoring + first-class methodology:** raw-only collection + a
      versioned methodology layer; byte-arrival smoothness metrics lead the score. The
      headline split into **Responsiveness / Smoothness / Speed** + an **Overall**
      roll-up; the Overall is first-class & persisted since `speed-smoothness-v5` and the
      crown decomposed to **FCP × total-stall × load-event** in **`speed-smoothness-v6`**
      (the published-now version); `regrade` re-scores history from raw.
- [x] **Crown intelligence + unified field model:** the crown is the **probability-of-
      best** profile; a **"Heirs to the crown"** card surfaces reachable contenders that
      could still dethrone it; a **saturation check** flags too-lenient thresholds with a
      one-click **re-anchor**; **"Re-run all profiles"** collects fresh comparable data;
      and every shaper field is declared once in a **`shaper_fields` registry** that the
      settings layer, providers, challenger, sweep engine, and sweep UI all derive from.
- [x] **Historical trends + relative SOPS:** day-of-week × hour-of-day baselines and
      a time-adjusted "vs typical" reading on the Dashboard, Trends page, and
      Settings Impact.
- [x] **Experiment engine:** within an experimentation window, sweep one shaper
      parameter across candidates (interleaved), benchmark each, and **restore the
      pre-window baseline** when the window closes (or auto-promote a clear
      winner). Disarmed + dry-run by default; manual abort restores baseline.
      *Requires OPNsense write (apply) access; window uses the container `TZ`.*
- [x] **Shotgun Sweep:** on-demand grid sweep (quantum × target) that applies each
      variant, benchmarks it, ranks by SOPS + "vs typical", and restores the
      baseline — plus a reversible config write-test. *Requires apply access.*
- [ ] **Speed test, bufferbloat (latency-under-load), A/B weight calibration.**
- [ ] **Phase 4 — Fuller autonomy:** Bayesian search over multiple parameters,
      interleaved A/B with effect-size + CI, hysteresis across windows.
- [ ] **Phase 5 — Routing intelligence / SD-WAN** with hysteresis (no route
      flapping; require sustained, meaningful wins before re-routing).
- [ ] Postgres / InfluxDB backends, OAuth/OIDC auth.

---

## License

[MIT](LICENSE) © 2026 jmorganthall
