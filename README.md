<h1 align="center">PathBrain</h1>

<p align="center">
  <b>An AI-driven Network Optimization &amp; SD-WAN Intelligence Platform.</b><br>
  It doesn't ask "is your ping low?" — it asks <i>"does the Internet actually <b>feel</b> faster?"</i>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white">
  <img alt="React" src="https://img.shields.io/badge/UI-React%20%2B%20MUI-61DAFB?logo=react&logoColor=black">
  <img alt="Docker" src="https://img.shields.io/badge/deploy-Docker-2496ED?logo=docker&logoColor=white">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green">
</p>

---

## What is PathBrain?

Most network tools optimize for **ping**, **throughput**, or synthetic benchmark
numbers. None of those reliably answer the only question a human actually cares
about: *when I click something, how fast does it feel?*

PathBrain optimizes for that directly. It runs real benchmark suites, scores them
against a proprietary **Seat of Pants Score (SOPS)** that models **human-perceived
responsiveness**, stores every run, and lets you compare configurations over time.
The long-term goal is a self-tuning home SD-WAN controller that empirically
discovers the settings that make the Internet *feel* fastest — safely, with
snapshots and rollback.

> **Philosophy:** Empirical. Never assume. Never rely on folklore. Every
> optimization is tested, measured, scored, and historically tracked.

### The Seat of Pants Score (SOPS)

SOPS is a single **0–100** number. Each contributing metric is normalized to a
0–100 subscore against configurable *best/worst* thresholds (lower latency →
higher score), then combined by weight:

| Metric | Source | Default weight | Why |
| --- | --- | ---: | --- |
| Render | Browser (Playwright, *Phase 2*) | 25% | What the user literally watches load |
| TTFB | HTTP | 20% | First sign the page is responding |
| TLS | TLS handshake | 20% | Felt on every new secure connection |
| TCP | TCP connect | 15% | Connection setup latency |
| DNS | Resolver lookup | 10% | The very first hop of every request |
| Jitter | ICMP | 5% | Consistency matters, raw ping does not |
| Packet loss | ICMP | 5% | Retransmits = stalls |

Two deliberate design choices:

- **Ping does not dominate.** Latency-derived metrics carry only 10% by default;
  perceptual metrics (render, TTFB, TLS) carry the most.
- **Missing metrics never penalize.** If a metric is unavailable (e.g. `render`
  before the browser engine ships, or a failed probe), its weight is
  redistributed proportionally across the metrics that *are* present — so the
  score stays on a stable, comparable 0–100 scale.

All weights and thresholds are editable at runtime from the UI or `PUT /api/config`.

---

## Status — Phase 1 (Foundation) ✅

The core end-to-end loop works today: **run a suite → score it → store history →
explore it in the dashboard.**

**Implemented**

- 🔌 **Plugin benchmark engine** — five independent, registered benchmarks:
  `icmp` (latency / jitter / loss), `dns` (per-resolver lookup), `tcp` (connect),
  `tls` (handshake), `http` (TTFB / download / transfer speed).
- 🧮 **SOPS scoring engine** — weighted, normalized, with proportional weight
  redistribution.
- 🔍 **Configuration discovery** — pluggable providers: a real **OPNsense API**
  provider (FQ-CoDel / traffic shaper) and a **mock** provider for offline dev.
- 🌐 **REST API** — `/run`, `/results`, `/history`, `/config`, `/score`,
  `/plugins`, `/experiments`, plus discovery & chart-series endpoints.
- 📊 **Web dashboard** — React + MUI, dark mode: trigger runs, watch SOPS over
  time, drill into a run, compare two runs, edit config, discover the firewall.
- 💾 **SQLite persistence** with background run execution and status tracking.

**Planned (next phases)** — scaffolding and scoring hooks already in place:
Playwright **browser engine** (the `render` weight activates automatically),
real-world test profiles, speed test, bufferbloat test, **experiment engine**
(apply → benchmark → keep/rollback), **autonomous closed-loop optimization**, and
**routing intelligence / SD-WAN** with hysteresis.

---

## Quick start (Docker)

The whole stack runs as a **single container** — the API serves the built UI.

```bash
git clone https://github.com/jmorganthall/pathbrain.git
cd pathbrain
docker compose up --build
```

Then open **http://localhost:8000** and click **Run Benchmark**.

Persistent state (SQLite DB, snapshots) lives in the `pathbrain-data` Docker
volume, so it survives restarts and image rebuilds.

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

> **Unraid:** add the container with port `8000` published and a single volume
> mapped to `/data`. Set the `PATHBRAIN_*` variables as container variables.
> Because PathBrain measures *your* path to the Internet, run it on the network
> whose responsiveness you want to score.

---

## Configuration

PathBrain separates **infrastructure** config (env-only) from **runtime**
benchmark config (DB-backed, editable live).

### Infrastructure (environment variables)

Copy [`.env.example`](.env.example) to `.env` and edit. All variables are prefixed
`PATHBRAIN_`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PATHBRAIN_DATABASE_URL` | `sqlite:///./data/pathbrain.db` | SQLAlchemy DB URL (Postgres later) |
| `PATHBRAIN_HOST` | `0.0.0.0` | Bind address |
| `PATHBRAIN_PORT` | `8000` | Bind port |
| `PATHBRAIN_LOG_LEVEL` | `INFO` | Log verbosity |
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

> Create the API key/secret in OPNsense under **System → Access → Users → (your
> user) → API keys**. PathBrain Phase 1 only *reads* configuration; applying
> changes arrives with the experiment engine and is always preceded by a snapshot.

### Runtime (benchmark targets, weights, thresholds)

ICMP/DNS/TCP/TLS/HTTP targets, SOPS weights, and normalization thresholds are
stored in the database and editable at runtime via the **Config** page or
`PUT /api/config`. Sensible defaults (Cloudflare/Google/Quad9, common sites) are
seeded automatically — no config required to take the first run.

---

## API reference

Interactive docs are served at `/docs` (Swagger) and `/redoc`. Base path: `/api`.

| Method & path | Description |
| --- | --- |
| `POST /api/run` | Trigger a benchmark suite (runs in background) |
| `GET /api/results/latest` | Latest completed run with metrics + score |
| `GET /api/results/{id}` | Full detail for one run (poll while running) |
| `GET /api/history` | List recent runs (id, time, label, status, SOPS) |
| `GET /api/history/series` | Time-series of SOPS + metrics for charts |
| `GET /api/score/{id}` | Score breakdown for a run |
| `GET /api/score/weights` | Current weights + thresholds |
| `POST /api/score/preview` | Score ad-hoc metrics with current weights |
| `GET /api/config` | Effective benchmark config |
| `PUT /api/config` | Update (deep-merge) benchmark config |
| `POST /api/config/reset` | Reset config to defaults |
| `GET /api/config/provider` | Discovery provider health |
| `POST /api/config/discover` | Discover FQ-CoDel settings + store a snapshot |
| `GET /api/config/snapshots` | List stored config snapshots |
| `GET /api/plugins` | List registered benchmark plugins |
| `GET /api/experiments` | Experiment engine (stub, Phase 3) |
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
  main.py            FastAPI app + serves the built frontend
  config.py          Env-driven infrastructure settings
  database.py        SQLAlchemy engine/session (SQLite)
  models.py          ORM: Run, BenchmarkResult, ScoreResult, ConfigSnapshot, AppConfig
  schemas.py         Pydantic request/response models
  runner.py          Orchestrates a run across plugins; stores results + score
  config_store.py    DB-backed runtime config + defaults
  logging_config.py  Structured logging
  api/               REST routers (one module per resource)
  plugins/           Benchmark plugins + registry (base.py)
  providers/         Config discovery (opnsense.py, mock.py)
  scoring/           SOPS engine (engine.py)
frontend/            React + TS + Vite + MUI dashboard (dark mode)
Dockerfile           Multi-stage: build UI, serve from API
docker-compose.yml   Single-container deployment
```

### Extending PathBrain

- **New benchmark:** drop a module in `plugins/`, subclass `BenchmarkPlugin`,
  decorate with `@register`, and return a `PluginResult`. Plugins must never
  raise for measurement failures — return `success=False` with an `error`.
- **New firewall:** subclass `ConfigProvider` in `providers/` and implement
  `discover()` / `snapshot()`.

---

## Roadmap

- [x] **Phase 1 — Foundation:** benchmark engine, SOPS scoring, history, config
      discovery, REST API, dashboard.
- [ ] **Phase 2 — Browser engine:** headless Chromium via Playwright (navigation
      timing, DOMContentLoaded, load, network idle, total render, screenshot +
      HAR). The `render` SOPS weight activates automatically.
- [ ] **Phase 2 — Real-world profiles, speed test, bufferbloat test.**
- [ ] **Phase 3 — Experiment engine:** apply candidate → wait → benchmark →
      keep/rollback, always snapshot-first.
- [ ] **Phase 4 — Autonomous closed-loop optimization.**
- [ ] **Phase 5 — Routing intelligence / SD-WAN** with hysteresis (no route
      flapping; require sustained, meaningful wins before re-routing).
- [ ] Postgres / InfluxDB backends, OAuth/OIDC auth.

---

## License

[MIT](LICENSE) © 2026 jmorganthall
