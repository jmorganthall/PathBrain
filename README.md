# PathBrain

**An AI-driven Network Optimization and SD-WAN Intelligence Platform.**

PathBrain answers one question:

> "Does this change make the Internet actually *feel* faster?"

Instead of optimizing for raw ping or throughput, PathBrain optimizes for **human
perception of responsiveness** via a proprietary **Seat of Pants Score (SOPS)**.

---

## Status: Foundation (Phase 1)

This repository currently implements the **core vertical slice**: an end-to-end
path from *running a benchmark suite* → *scoring it* → *storing history* →
*viewing it in a dashboard*.

### What works today

- **Plugin-based benchmark engine** with five network benchmarks:
  - `benchmark_icmp` — latency, jitter, packet loss
  - `benchmark_dns` — resolver lookup time (local / Cloudflare / Google / Quad9)
  - `benchmark_tcp` — connection establishment time
  - `benchmark_tls` — TLS handshake duration
  - `benchmark_http` — TTFB, download duration, transfer speed
- **Scoring engine** computing the weighted **Seat of Pants Score** with
  configurable weights and normalization thresholds. Missing metrics (e.g.
  browser render before Playwright lands) are handled by proportional weight
  redistribution, so the score is always meaningful.
- **Configuration Discovery** via a pluggable provider interface, with a real
  **OPNsense API provider** (FQ-CoDel / traffic shaper discovery) and a **mock
  provider** for development and tests.
- **REST API** (FastAPI): `/run`, `/results`, `/history`, `/config`, `/score`,
  `/plugins`, `/experiments` (stub).
- **Web dashboard** (React + MUI, dark mode, Material design): trigger runs,
  watch SOPS over time, drill into a run, edit config.
- **SQLite** persistence with a clean storage layer (PostgreSQL/InfluxDB later).
- **Background run execution** with run status tracking.

### Deferred to later phases (scaffolded, not yet built)

Playwright browser engine, real-world test profiles, speed test, bufferbloat
test, experiment engine, autonomous closed-loop optimization, routing
intelligence / SD-WAN.

---

## Architecture

```
backend/pathbrain/
  main.py              FastAPI app + static frontend serving
  config.py            Settings (env-driven)
  database.py          SQLAlchemy engine/session (SQLite)
  models.py            ORM models: Run, BenchmarkResult, ScoreResult, ConfigSnapshot, AppConfig
  schemas.py           Pydantic request/response schemas
  runner.py            Orchestrates a benchmark run across plugins
  config_store.py      Persisted app config (targets, weights, thresholds)
  logging_config.py    Structured logging setup
  api/                 REST route modules
  plugins/             Benchmark plugins + registry (base.py)
  providers/           Config discovery providers (OPNsense, mock)
  scoring/             SOPS scoring engine

frontend/              React + Vite + MUI dashboard
```

The benchmark plugins and config providers are **independent, registered
modules** — adding a new benchmark or firewall integration means dropping in a
new class, no core changes required.

---

## Running locally

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn pathbrain.main:app --reload --host 0.0.0.0 --port 8000
```

API docs at <http://localhost:8000/docs>.

### Frontend

```bash
cd frontend
npm install
npm run dev      # dev server on :5173, proxies /api to :8000
```

### Docker (production-style: API serves the built UI)

```bash
docker compose up --build
```

Then open <http://localhost:8000>.

---

## Configuration

All runtime config is via environment variables (see `.env.example`). The most
important ones:

| Variable | Purpose |
| --- | --- |
| `PATHBRAIN_DATABASE_URL` | SQLAlchemy URL (default `sqlite:///./data/pathbrain.db`) |
| `PATHBRAIN_OPNSENSE_URL` | OPNsense base URL, e.g. `https://192.168.1.1` |
| `PATHBRAIN_OPNSENSE_API_KEY` | OPNsense API key |
| `PATHBRAIN_OPNSENSE_API_SECRET` | OPNsense API secret |
| `PATHBRAIN_OPNSENSE_VERIFY_TLS` | Verify the firewall's TLS cert (default `false`) |
| `PATHBRAIN_CONFIG_PROVIDER` | `opnsense` or `mock` (default `mock`) |

Benchmark targets, DNS providers, HTTP URLs, scoring weights, and normalization
thresholds are stored in the database and editable at runtime via `/api/config`
(and the **Config** page in the UI).

---

## Philosophy

Empirical. Never assume. Never rely on folklore. Every optimization is tested,
measured, scored, and historically tracked — with safety via configuration
snapshots and rollback as the autonomous features come online.
