# PathBrain — multi-stage build: compile the React frontend, then serve it from
# the FastAPI backend in a single container.

# --- Stage 1: build frontend ---------------------------------------------
FROM node:20-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# --- Stage 2: backend runtime --------------------------------------------
# Use the official Playwright image: Chromium + all of its OS dependencies and
# matching browser binaries are preinstalled (browser version matches the
# playwright==1.44.0 pin in requirements.txt). This avoids the Debian-Bookworm
# `playwright install --with-deps` font-package breakage.
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy AS runtime

WORKDIR /app

# tini as PID 1. uvicorn does not reap orphans, so without an init every Chrome child
# re-parented to PID 1 by a killed browser stays a zombie forever — 388 of them were
# observed on one host. Baked into the image rather than left to `init: true` in
# compose, so a plain `docker run` (an Unraid template, say) gets reaping too.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY --from=frontend /frontend/dist ./frontend/dist

# Stamp the commit this image was built from (CI passes --build-arg GIT_SHA=$github.sha)
# so the running app can tell when a newer build is available to pull.
ARG GIT_SHA=""
ENV PATHBRAIN_GIT_SHA=$GIT_SHA

ENV PATHBRAIN_DATABASE_URL=sqlite:////data/pathbrain.db \
    PATHBRAIN_ARTIFACT_DIR=/data/artifacts \
    PATHBRAIN_FRONTEND_DIST=/app/frontend/dist \
    PATHBRAIN_HOST=0.0.0.0 \
    PATHBRAIN_PORT=8000 \
    PYTHONUNBUFFERED=1

WORKDIR /app/backend
VOLUME ["/data"]
EXPOSE 8000

# Single worker, deliberately: the scheduler, the coordinator lock and the probe worker
# all assume one process. `scheduler.is_leader` guards against a second worker, but the
# right number of schedulers is one and the right place to get it is here.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "pathbrain.main:app", "--host", "0.0.0.0", "--port", "8000"]
