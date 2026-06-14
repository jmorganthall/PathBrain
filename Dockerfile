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
FROM python:3.11-slim AS runtime

# icmplib needs iputils for some environments; install ca-certs for TLS probes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Browser engine: install Chromium + its OS dependencies for Playwright so the
# `browser` benchmark (total render / SOPS render metric) works out of the box.
RUN playwright install --with-deps chromium

COPY backend/ ./backend/
COPY --from=frontend /frontend/dist ./frontend/dist

ENV PATHBRAIN_DATABASE_URL=sqlite:////data/pathbrain.db \
    PATHBRAIN_ARTIFACT_DIR=/data/artifacts \
    PATHBRAIN_FRONTEND_DIST=/app/frontend/dist \
    PATHBRAIN_HOST=0.0.0.0 \
    PATHBRAIN_PORT=8000 \
    PYTHONUNBUFFERED=1

WORKDIR /app/backend
VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "pathbrain.main:app", "--host", "0.0.0.0", "--port", "8000"]
