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

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

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
