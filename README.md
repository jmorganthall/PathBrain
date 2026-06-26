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

### The Seat of Pants Score (SOPS)

SOPS is a single **0–100** number. It's **trajectory-aware**: it rewards a page
that paints *early and steadily*, not one that merely finishes first. Each metric
is normalized to a 0–100 subscore against configurable *best/worst* thresholds
(perception-calibrated log curve), then combined by weight (rubric `perceptual-v3`):

| Metric | Source | Default weight | Why |
| --- | --- | ---: | --- |
| Speed Index | Browser filmstrip | 30% | Average time content is visible — early, progressive paint |
| First Contentful Paint | Browser | 20% | First sign content is appearing |
| Paint smoothness (cadence) | Browser filmstrip | 10% | Steady fill vs. stall-then-dump |
| Largest Contentful Paint | Browser | 10% | Main content visible (a completion milestone) |
| Interaction to Next Paint | Browser | 10% | Responsiveness to input |
| Time to First Byte | HTTP | 10% | Server starting to respond |
| Layout stability (CLS) | Browser | 5% | Janky reflow feels worse |
| Total render (to network-idle) | Browser | 5% | Pure completion time — deliberately low |

Three deliberate design choices:

- **The journey beats the endpoint.** Speed Index + FCP + cadence + CLS (65%)
  dominate "how it unfolded"; completion times (LCP + render = 15%) are kept low.
- **Infra timing is a separate axis.** Raw DNS/TCP/TLS/jitter/loss form the
  secondary **Completion** score — diagnostic only, never folded into SOPS, since
  it barely moves human feel.
- **Missing metrics never penalize.** If a metric is unavailable (e.g. Speed Index
  where the browser/filmstrip didn't run, or a failed probe), its weight is
  redistributed across the metrics that *are* present, keeping a stable 0–100 scale.

Plugins are **pure sensors** that store raw observations; all interpretation
(jitter = stddev of pings, Speed Index from the filmstrip, the score itself) lives
in a separate, versioned layer — so a new metric or a changed formula can be
re-derived over history without re-collecting (`POST /api/score/rederive`). All
weights and thresholds are editable at runtime from the UI or `PUT /api/config`.

---

## Status — what works today ✅

- 🔌 **Plugin benchmark engine** — six registered benchmarks (**pure sensors** that
  store raw observations): `icmp` (per-ping RTT series), `dns` (per-resolver lookup),
  `tcp` (connect), `tls` (handshake), `http` (TTFB / bytes / timing), and `browser`
  (headless-Chromium nav/paint timing + a **filmstrip**, with screenshot & HAR).
- 🧮 **Trajectory-aware SOPS** — perception-calibrated **log curve** (Weber–Fechner),
  led by **Speed Index**, paint cadence and CLS (from the filmstrip) so a smoothly-
  progressive load wins over one that finishes first. Raw-only collection + a
  **versioned interpretation layer**: `POST /api/score/rescore` re-grades under a
  new rubric, `POST /api/score/rederive` re-runs derivation from stored raw (new
  metric / formula) — neither re-collects.
- 🌦️ **Historical trends + "vs typical"** — per-metric baselines by day-of-week ×
  hour-of-day (`/api/trends/*`); the Dashboard, a dedicated **Trends** page, and
  **Settings Impact** read each result *relative to its historical norm* ("wins
  above replacement"), so a config is judged fairly for the times it actually ran.
- 🎯 **Shotgun Sweep** — an on-demand grid sweep over quantum × target: applies each
  variant for real, benchmarks it, ranks by SOPS + "vs typical", and **restores the
  baseline** at the end (and on startup if interrupted). Plus a reversible **config
  write-test** (`POST /api/config/test-apply`) to validate the firewall apply path.
- 🔁 **Multi-iteration runs** — repeat the suite N times and take the **median**,
  with a per-run **confidence band** (± / range) and an **ETA**.
- 📈 **Continuous monitoring + rolling score** — optional scheduler runs the suite
  on an interval; the Dashboard shows a windowed **median (24h) + IQR** so
  "current responsiveness" is stable, not point-in-time noise.
- 🔍 **OPNsense discovery + settings correlation** — each run captures the live
  FQ-CoDel/SQM settings + a **fingerprint**; runs group into **profiles** with
  their SOPS distribution, and a **significant-change** banner (effect ≥ threshold,
  with a min-runs confidence guard). Mock provider for offline dev.
- 🧪 **Experiment engine** — within a configurable **window**, sweep one shaper
  parameter across candidates, benchmark each, and **restore the pre-window
  baseline** at close (or auto-promote a clear winner). **Disarmed + dry-run by
  default.** Firewall writes go only through `provider.apply()` (experiment, Shotgun
  Sweep, config write-test) — each reversible and snapshot/restore.
- 🛡️ **Run-lifecycle safety** — startup reconciliation + a watchdog timeout +
  manual cancel so a restart/hang never leaves a zombie "running" job.
- 📊 **Web dashboard** — React + MUI, dark mode: Dashboard (rolling score + "vs
  typical" + metric breakdown), History, **Trends** (day/hour heatmaps), Compare,
  Settings Impact, Experiments, **Shotgun Sweep**, Config, Run Detail (with filmstrip).
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
- **`iterations`** — suite repeats per run; the headline SOPS is the **median**.
- **`monitoring`** — `enabled`, `interval_minutes`, `run_timeout_minutes` (watchdog).
- **`correlation`** — `significant_change_pct`, `min_runs` (settings-impact guards).
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
| `GET /api/score/{id}` / `…/weights` | Run score / current weights + thresholds |
| `GET /api/score/rolling` | Windowed median SOPS + IQR + aggregated subscores |
| `POST /api/score/rescore` | Re-grade all history with the current rubric |
| `POST /api/score/rederive` | Re-run derivation+scoring from stored raw (new metric/formula) |
| `POST /api/score/preview` | Score ad-hoc metrics with current weights |
| `GET /api/trends/heatmap` | Per-metric baseline grid by day-of-week × hour-of-day |
| `GET /api/trends/relative` | Current reading vs. its historical baseline ("vs typical") |
| `GET /api/monitoring` | Continuous-monitoring scheduler status |
| `GET /api/config` / `PUT` / `POST …/reset` | Read / update / reset benchmark config |
| `POST /api/config/adopt-rubric` | Load perception-calibrated default rubric |
| `GET /api/config/provider` | Discovery provider health (cause shown if down) |
| `POST /api/config/discover` | Discover FQ-CoDel settings + store a snapshot |
| `POST /api/config/test-apply` | Reversible write-path test (nudge quantum +1, then restore) |
| `GET /api/config/snapshots` | List stored config snapshots |
| `GET /api/settings/profiles` | Per-settings-profile SOPS distribution |
| `GET /api/settings/impact` | Significance of the latest settings change |
| `POST /api/settings/backfill` | Stamp current settings onto unattributed runs |
| `GET /api/settings/diagnostics` | Settings-capture diagnostics (stamped/unstamped) |
| `GET /api/experiments` / `…/{id}` | Experiment status + history / one experiment's trials |
| `POST /api/experiments/abort` | Abort the running experiment, restore baseline |
| `POST /api/sweep/preview` | Shotgun Sweep variant count + ETA for a spec |
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
  models.py          ORM: Run, BenchmarkResult (+raw), ScoreResult, ConfigSnapshot,
                     AppConfig, Experiment, ExperimentTrial, Sweep
  schemas.py         Pydantic request/response models
  runner.py          Run orchestration; median aggregation; reconcile/watchdog/rescore/rederive
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
- [x] **Settings-vs-responsiveness correlation:** each run is fingerprinted with
      the live FQ-CoDel/SQM settings; runs group into profiles with their SOPS
      distribution, and a significant-change banner (with a min-runs guard).
- [x] **Trajectory-aware scoring:** raw-only collection + a versioned interpretation
      layer; **Speed Index**, paint cadence and CLS from a browser filmstrip lead the
      rubric (`perceptual-v3`); `rederive` re-applies new metrics to history.
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
