# CLAUDE.md — PathBrain developer guide

PathBrain is an AI-driven network optimization platform that maximizes a
**Seat of Pants Score (SOPS)** — a measure of *human-perceived responsiveness*,
not raw ping/throughput. See `README.md` for the product overview.

## Layout

- `backend/pathbrain/` — FastAPI app (the core). Key modules:
  - `plugins/` — independent benchmark plugins; `base.py` defines the contract +
    registry. Add a benchmark by subclassing `BenchmarkPlugin` and `@register`.
  - `providers/` — firewall config discovery (`opnsense.py`, `mock.py`); pick via
    `PATHBRAIN_CONFIG_PROVIDER`.
  - `scoring/engine.py` — SOPS computation (weighted, normalized, redistributes
    missing-metric weight). Metric→plugin mapping is `METRIC_SOURCES`.
  - `config_store.py` — persisted runtime config + defaults (targets, weights,
    thresholds).
  - `runner.py` — orchestrates a run across plugins, stores results + score.
  - `api/` — REST routers mounted at `/api`.
- `frontend/` — React + TS + Vite + MUI dashboard (dark mode).
- `Dockerfile` / `docker-compose.yml` — single-container deploy (API serves UI).

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
- Lower-is-better for all current SOPS metrics; thresholds define best/worst.
- Every action should be logged (`logging_config.get_logger`).

## Phase map

- **Phase 1 (done):** benchmark engine (ICMP/DNS/TCP/TLS/HTTP), SOPS scoring,
  history, config discovery (OPNsense/mock), REST API, dashboard.
- **Next:** Playwright browser engine (`render` metric is already wired into
  scoring), real-world profiles, speed test, bufferbloat, experiment engine,
  autonomous closed-loop optimization, routing intelligence / SD-WAN.

When adding the browser engine, emit metrics under a `browser` plugin with key
`total_render_ms` and the `render` SOPS weight activates automatically.
