# ==============================================================================
# Galgame2Voice Production Container Image
# Multi-stage reproducible build:
# Stage 1: Build modern frontend SPA artifacts via Node.js
# Stage 2: Minimal Python 3.11 runtime with locked dependencies via uv
# ==============================================================================

# ------------------------------------------------------------------------------
# Stage 1: Frontend Build
# ------------------------------------------------------------------------------
FROM node:20-alpine AS frontend-builder

WORKDIR /frontend

# Install dependencies (reproducible from lockfile; no fallback to unpinned install)
COPY frontend/package*.json ./
RUN npm ci --ignore-scripts

# Build production assets
COPY frontend/ ./
RUN npm run build

# ------------------------------------------------------------------------------
# Stage 2: Python Runtime
# ------------------------------------------------------------------------------
FROM python:3.11-slim

# Install system dependencies (ffmpeg for audio transcoding, curl for healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy uv binary from official Astral image for locked, reproducible dependency installation.
# Pinned tag for reproducible builds (no floating :latest); bump deliberately and re-test when upgrading.
COPY --from=ghcr.io/astral-sh/uv:0.9.0 /uv /uvx /bin/

WORKDIR /app

# Enable bytecode compilation, configure paths and environment
ENV UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    GALGAME2VOICE_PORT=8080 \
    GALGAME2VOICE_HOST=0.0.0.0 \
    GALGAME2VOICE_AUTH_DISABLED=0 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH"

# Copy pyproject.toml and uv.lock for deterministic dependency installation
COPY pyproject.toml uv.lock ./

# Install dependencies locked
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code and scripts
COPY galgame2voice/ ./galgame2voice/
COPY scripts/ ./scripts/
COPY .env.example ./

# Copy frontend build artifacts from stage 1 into /app/galgame2voice/static/
# Clean up any stale assets copied from host galgame2voice/static/assets before placing fresh artifacts
RUN rm -rf /app/galgame2voice/static/assets
COPY --from=frontend-builder /frontend/dist/ /app/galgame2voice/static/

# Create runtime directories
RUN mkdir -p /app/data /app/audio /app/logs

# Run as a dedicated non-root user. /app itself stays root-owned (read-only for the
# app); only the writable data directories (bind-mounted at runtime) are chowned.
# APP_UID/APP_GID default to 1000 -- the usual first Linux user -- so the
# docker-compose bind mounts (./data, ./audio, ./logs) remain writable. If your
# host user has a different id, rebuild with:
#   docker compose build --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g)
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} app && useradd -u ${APP_UID} -g app -d /app -M app \
    && chown -R app:app /app/data /app/audio /app/logs
USER app

EXPOSE 8080

VOLUME ["/app/data", "/app/audio", "/app/logs"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://127.0.0.1:8080/api/health || exit 1

# The listener and startup safety check share the same Settings values.
CMD ["python", "-m", "galgame2voice.main"]

