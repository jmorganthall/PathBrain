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
Internet connection actually *feels*.** It runs real benchmark suites and grades
them across four perception-led axes — **Speed, Smoothness, Stability &
Interactivity, and Completion** — through a **versioned, RTINGS-style methodology**,
then tracks those scores over time — so you can finally answer *"when was the
Internet fastest?"* and *"did that change make it feel better or worse?"* with data
instead of folklore.

Most tools optimize for **ping**, **throughput**, or synthetic scores. None of
those reliably answer what a human cares about: *when I click something, how fast
does it feel?* PathBrain is built for exactly that — and deliberately keeps raw
ping from dominating.

> The headline used to be a single **Seat of Pants Score (SOPS)**; it's now the
> Speed + Smoothness axes (with Stability and Completion alongside). The "seat of
> the pants" philosophy — score what a human *feels* — is unchanged.

Where it gets powerful: PathBrain can **correlate your score with the network
settings that were live when each run ran.** Its first-class integration is the
**[OPNsense](https://opnsense.org/) API**, which it uses to discover your
**FQ-CoDel / SQM** traffic-shaper configuration (bandwidth, quantum, limit,
target, interval, ECN, flows, …). That turns the eternal SQM question — *what
settings are actually best?* — into an empirical, measured answer.

- **No firewall?** It's still a first-class **responsiveness tracker** for your
  connection.
- **Running OPNsense SQM?** You also get settings-vs-responsiveness correlation
  and (on the roadmap) closed-loop autonomous tuning — apply a candidate,
  benchmark, keep it if the score improved, roll back if not, always snapshot-first.

> The provider layer is pluggable (pfSense / Linux `tc` can follow), with OPNsense
> traffic shaping as the first-class integration.

> **Philosophy:** Empirical. Never assume. Never rely on folklore. Every
> optimization is tested, measured, scored, and historically tracked.

### The scoring axes

PathBrain grades each run on **four 0–100 axes**, never blended into one number.
Each metric is normalized to a 0–100 subscore against configurable *best/worst*
thresholds (perception-calibrated log curve, anchored to Core Web Vitals / Nielsen
limits), then combined by weight. The scoring is **trajectory-aware**: it rewards a
page whose bytes arrive *early and steadily*, not one that merely finishes first.

| Axis | Role | What it captures | Lead metrics |
| --- | --- | --- | --- |
| **Speed** | headline | How fast content arrives | Byte earliness, FCP, LCP, TTFB |
| **Smoothness** | headline | How *steadily* it arrives (no stalls) | Longest stall, cadence CoV, perceived time, render-to-networkidle |
| **Stability & Interactivity** | secondary | Responsiveness + layout calm | INP, CLS |
| **Completion** | secondary | Raw infra timing | DNS, TCP, TLS, jitter, loss |

The **Smoothness** axis is the distinctive one: a **byte-arrival smoothness
instrument** derived purely from Resource Timing + Long Animation Frames (no pixel
screencast), so it isolates the *network* layer you can actually tune. Speed Index /
paint cadence from the optional browser filmstrip are now **display-only
diagnostics**.

Three deliberate design choices:

- **The journey beats the endpoint.** Early, steady byte arrival (Speed +
  Smoothness) is the headline; raw completion time is deliberately de-emphasized.
- **Infra timing is its own axis.** DNS/TCP/TLS/jitter/loss form the secondary
  **Completion** score — diagnostic, since it barely moves human feel.
- **Missing metrics never penalize.** An unavailable metric (e.g. a probe that
  failed, or browser metrics where Chromium didn't run) has its weight redistributed
  across the metrics that *are* present, keeping a stable 0–100 scale.

**Methodology, RTINGS-style.** The governing invariant is `raw + methodology →
score`, deterministically. Plugins are **pure sensors** that store raw observations;
all interpretation (jitter = stddev of pings, the smoothness metrics, the score
itself) lives in a separate **versioned methodology** (immutable + append-only — a
weight/threshold/metric change publishes a new version). Every run keeps its
*score-at-measure* (how it scored under the methodology current when collected) and
can be re-scored *at-present* from stored raw (`POST /api/score/regrade`), with honest
**exact / partial / incomparable** comparability. So a new metric or changed formula
re-derives over history without re-collecting. All weights and thresholds are editable
at runtime from the UI or `PUT /api/config`. See [`docs/methodology.md`](docs/methodology.md).

---

## Status — what works today ✅

- 🔌 **Plugin benchmark engine** — six registered benchmarks (**pure sensors** that
  store raw observations): `icmp` (per-ping RTT series), `dns` (per-resolver lookup),
  `tcp` (connect), `tls` (handshake), `http` (TTFB / bytes / timing), and `browser`
  (headless-Chromium nav/paint timing + a **filmstrip**, with screenshot & HAR).
- 🧮 **Four-axis perceptual scoring** — Speed / Smoothness / Stability / Completion
  on a perception-calibrated **log curve** (Weber–Fechner) with CWV/Nielsen-anchored
  thresholds, led by a **byte-arrival smoothness instrument** (Resource Timing + LoAF,
  no pixel capture) so a smoothly-progressive load wins over one that finishes first.
- 📐 **Versioned methodology layer** (`raw + methodology → score`) — immutable
  published versions, score-at-measure vs score-at-present, exact/partial/incomparable
  comparability, and a **Methodology** tab. Re-grade history under the current
  methodology from stored raw via `POST /api/score/regrade` — no re-collection.
- 🌦️ **Historical trends + "vs typical"** — per-metric baselines by day-of-week ×
  hour-of-day (`/api/trends/*`); the Dashboard, a dedicated **Trends** page, and
  **Settings Impact** read each result *relative to its historical norm* ("wins
  above replacement"), so a config is judged fairly for the times it actually ran.
- 🎯 **Shotgun Sweep** — an on-demand grid sweep over pipe × quantum × target
  (download **and** upload, one pipe at a time): applies each variant for real,
  benchmarks it, ranks by Smoothness/Speed + "vs typical", and **restores the
  baseline** at the end (and on startup if interrupted). Plus a reversible **config
  write-test** (`POST /api/config/test-apply`) to validate the firewall apply path.
- 🔁 **Multi-iteration runs** — repeat the suite N times and take the **median**,
  with a per-run **confidence band** (± / range) and an **ETA**.
- 📈 **Continuous monitoring + rolling score** — optional scheduler runs the suite
  on an interval; the Dashboard shows a windowed **median (24h) + IQR** so
  "current responsiveness" is stable, not point-in-time noise.
- 🔍 **OPNsense discovery + settings correlation** — each run captures the live
  FQ-CoDel/SQM settings + a **fingerprint**; runs group into **profiles** with
  their per-axis score distribution, and a **significant-change** banner (effect ≥
  threshold, with a min-runs confidence guard). Mock provider for offline dev.
- 🧪 **Experiment engine** — within a configurable **window**, sweep one shaper
  parameter across candidates, benchmark each, and **restore the pre-window
  baseline** at close (or auto-promote a clear winner). **Disarmed + dry-run by
  default.** Firewall writes go only through `provider.apply()` (experiment, Shotgun
  Sweep, config write-test) — each reversible and snapshot/restore.
- 🛡️ **Run-lifecycle safety** — startup reconciliation + a watchdog timeout +
  manual cancel so a restart/hang never leaves a zombie "running" job.
- 📊 **Web dashboard** — React + MUI, dark mode: Dashboard (rolling Speed/Smoothness
  gauges + "vs typical" + config-tag filter + axis-series), History, **Trends**
  (day/hour heatmaps), Compare, Settings Impact, Experiments, **Shotgun Sweep**,
  Config, **Methodology**, Run Detail (at-measure vs at-present, with filmstrip).
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
> **experiment engine, Shotgun Sweep, and config write-test additionally write** to
> the shaper (`setPipe` + `reconfigure`), so those need write access; each is
> reversible and snapshots/restores the baseline (the experiment engine is also
> disarmed + dry-run by default).

### Runtime (DB-backed, edit on the Config page or `PUT /api/config`)

All of this is stored in the database and deep-merged over defaults, so the first
run needs no setup:

- **Benchmark targets** — ICMP/DNS/TCP/TLS/HTTP/browser hosts & URLs.
- **`iterations`** — suite repeats per run; each axis score is the **median**.
- **`monitoring`** — `enabled`, `interval_minutes`, `run_timeout_minutes` (watchdog).
- **`correlation`** — `significant_change_pct`, `min_runs` (settings-impact guards).
- **`trends`** — `lookback_days`, `window_hours`, `min_samples` (historical baselines).
- **`rubric_version` / `methodology_version` / `weights` / `thresholds`** — the
  scoring methodology (perception curve). After editing, **Save → Re-grade history
  under current** (`POST /api/score/regrade`) to keep the timeline comparable.
- **`experiment`** — `enabled`, `dry_run`, `auto_promote`, `param`, `candidates`,
  `window` (days/hours, container `TZ`), `dwell_minutes`, `min_trials_per_value`,
  `improve_pct`. Disarmed + dry-run by default.

Run results show per-metric **median ± stdev** and per-axis confidence bands; an ETA
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
| `GET /api/history/series` | Time-series of axis scores + metrics for charts |
| `GET /api/score/{id}` / `…/weights` | Run score / current weights + thresholds |
| `GET /api/score/{id}/methodologies` | A run scored under every methodology (at-measure + at-present) |
| `GET /api/score/rolling` | Windowed per-axis median + IQR/p95 (optional `fingerprint` filter) |
| `GET /api/score/axis-series` | Per-axis time-series for charts |
| `POST /api/score/regrade` | Re-grade history under the current methodology from stored raw |
| `POST /api/score/preview` | Score ad-hoc metrics with current weights |
| `GET /api/methodologies` / `…/current` / `…/{version}` | List / current / full definition of published methodologies |
| `GET /api/trends/heatmap` | Per-metric baseline grid by day-of-week × hour-of-day |
| `GET /api/trends/relative` | Current reading vs. its historical baseline ("vs typical") |
| `GET /api/monitoring` | Continuous-monitoring scheduler status |
| `GET /api/config` / `PUT` / `POST …/reset` | Read / update / reset benchmark config |
| `POST /api/config/adopt-rubric` | Load perception-calibrated default rubric |
| `GET /api/config/provider` | Discovery provider health (cause shown if down) |
| `POST /api/config/discover` | Discover FQ-CoDel settings + store a snapshot |
| `POST /api/config/test-apply` | Reversible write-path test (nudge quantum +1, then restore) |
| `GET /api/config/snapshots` | List stored config snapshots |
| `GET /api/settings/profiles` | Per-settings-profile Smoothness/Speed distribution |
| `GET /api/settings/impact` | Significance of the latest settings change |
| `POST /api/settings/apply-profile` | Write a profile's shaper settings to the firewall |
| `POST /api/settings/backfill` | Stamp current settings onto unattributed runs |
| `GET /api/settings/diagnostics` | Settings-capture diagnostics (stamped/unstamped) |
| `GET /api/experiments` / `…/{id}` | Experiment status + history / one experiment's trials |
| `POST /api/experiments/abort` | Abort the running experiment, restore baseline |
| `POST /api/sweep/preview` | Shotgun Sweep variant count + ETA for a spec |
| `GET /api/sweep/pipes` | List shaper pipes available to sweep (download/upload) |
| `POST /api/sweep` · `GET /api/sweep/current` | Start a sweep / poll the active (or latest) sweep |
| `POST /api/sweep/{id}/cancel` · `…/apply-best` | Cancel (restores baseline) / apply the winning variant |
| `GET /api/plugins` | List registered benchmark plugins |
| `GET /api/health` | Liveness / version |

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
  models.py          ORM: Run, BenchmarkResult (+raw), Methodology, Score (run×methodology),
                     ScoreResult (legacy), ConfigSnapshot, AppConfig, Experiment, ExperimentTrial, Sweep
  methodology.py     Versioned methodology registry; raw+methodology→score; comparability
  schemas.py         Pydantic request/response models
  runner.py          Run orchestration; median aggregation; multi-axis scoring; reconcile/watchdog/regrade
  scheduler.py       Daemon thread: watchdog → (yield to sweep) → experiment → monitoring
  experiment.py      Window-gated autonomous shaper sweep (writes via provider.apply)
  sweep.py           Shotgun Sweep: on-demand grid sweep, applies + restores baseline
  trends.py          Day/hour historical baselines + time-adjusted "vs typical"
  settings_profile.py  Normalize/fingerprint/summarize firewall profiles
  config_store.py    DB-backed runtime config + defaults
  logging_config.py  Structured logging
  api/               REST routers (one module per resource)
  plugins/           Benchmark plugins + registry (base.py) — pure sensors (raw only)
  interpret/         Raw observations → metric values (derive.py, versioned)
  providers/         Config discovery + apply (opnsense.py, mock.py)
  scoring/           Multi-axis score engine (engine.py, perception-calibrated)
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

- [x] **Phase 1 — Foundation:** benchmark engine, scoring, history, config
      discovery, REST API, dashboard.
- [x] **Phase 2 — Browser engine:** headless Chromium via Playwright (navigation
      timing, DOMContentLoaded, load, network idle, total render, screenshot +
      HAR). The `render` weight activates automatically.
- [x] **Continuous monitoring:** scheduled recurring runs + a windowed rolling
      score (median + IQR) for stable "current responsiveness."
- [x] **Settings-vs-responsiveness correlation:** each run is fingerprinted with
      the live FQ-CoDel/SQM settings; runs group into profiles with their per-axis
      score distribution, and a significant-change banner (with a min-runs guard).
- [x] **Trajectory-aware scoring:** raw-only collection + a versioned interpretation
      layer; `regrade` re-applies new metrics to history from stored raw.
- [x] **Historical trends + relative scoring:** day-of-week × hour-of-day baselines
      and a time-adjusted "vs typical" reading on the Dashboard, Trends page, and
      Settings Impact.
- [x] **Experiment engine:** within an experimentation window, sweep one shaper
      parameter across candidates (interleaved), benchmark each, and **restore the
      pre-window baseline** when the window closes (or auto-promote a clear
      winner). Disarmed + dry-run by default; manual abort restores baseline.
      *Requires OPNsense write (apply) access; window uses the container `TZ`.*
- [x] **Shotgun Sweep:** on-demand grid sweep (pipe × quantum × target, download +
      upload) that applies each variant, benchmarks it, ranks by Smoothness/Speed +
      "vs typical", and restores the baseline — plus a reversible config write-test.
      *Requires apply access.*
- [x] **Perceived load-smoothness instrument:** byte-arrival smoothness from Resource
      Timing + LoAF (no pixel capture), with network-vs-render stall attribution.
- [x] **Methodology layer + four-axis scoring:** `raw + methodology → score` made
      first-class — immutable `Methodology` + `(run × methodology)` `Score` tables,
      score-at-measure vs at-present, exact/partial/incomparable comparability, and a
      Methodology tab. SOPS replaced by Speed / Smoothness / Stability / Completion.
- [ ] **Speed test, bufferbloat (latency-under-load), A/B weight calibration.**
- [ ] **Phase 4 — Fuller autonomy:** Bayesian search over multiple parameters,
      interleaved A/B with effect-size + CI, hysteresis across windows.
- [ ] **Phase 5 — Routing intelligence / SD-WAN** with hysteresis (no route
      flapping; require sustained, meaningful wins before re-routing).
- [ ] Postgres / InfluxDB backends, OAuth/OIDC auth.

---

## License

[MIT](LICENSE) © 2026 jmorganthall
