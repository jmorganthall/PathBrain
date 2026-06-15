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
- Lower-is-better for all current SOPS metrics; thresholds define best/worst and
  are interpolated on a perception-calibrated log curve (Weber–Fechner). The
  rubric (weights+thresholds+`rubric_version`) is versioned; changing it should be
  followed by `POST /api/score/rescore` to re-grade history from stored raw
  measurements (runs keep `metric_values` + per-iteration metrics for this).
- Every action should be logged (`logging_config.get_logger`).

## Phase map

- **Phase 1 (done):** benchmark engine (ICMP/DNS/TCP/TLS/HTTP), SOPS scoring,
  history, config discovery (OPNsense/mock), REST API, dashboard.
- **Phase 2 (done):** Playwright browser engine — `benchmark_browser` emits
  `total_render_ms` (+ nav timings), captures screenshot/HAR to the artifact dir,
  served at `/artifacts`. The `render` SOPS weight (25%) activates automatically.
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
