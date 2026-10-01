# CPI Log Lens — production image. Build context is the repository root:
#   docker build -t cpi-log-lens .
ARG PYTHON_VERSION=3.12

# uv installs the locked dependencies (backend/uv.lock); pinned, kept current by Dependabot.
FROM ghcr.io/astral-sh/uv:0.12.21 AS uv

# ── CSS build stage: pinned standalone tools are not copied to runtime ────────
FROM debian:bookworm-slim AS css-builder
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY scripts/build-css.sh scripts/tailwind.input.css ./scripts/
COPY frontend/index.html ./frontend/index.html
COPY frontend/js/ ./frontend/js/
ENV CSS_TOOL_CACHE=/opt/tailwind
RUN chmod +x scripts/build-css.sh \
 && scripts/build-css.sh --output frontend/tailwind.css

# ── Stage 1: install dependencies into an isolated virtualenv ─────────────────
FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=uv /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=0 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /build
COPY backend/pyproject.toml backend/uv.lock ./
# --locked: fail if uv.lock does not match pyproject.toml instead of re-resolving.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim
ARG VERSION=0.0.0-dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="CPI Log Lens" \
      org.opencontainers.image.description="Self-hosted tool to fetch, store and search SAP Cloud Integration logs" \
      org.opencontainers.image.source="https://github.com/FinkeFlo/cpi-log-lens" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

# Security updates for the Debian base packages (the slim base image lags
# behind), then an unprivileged user that can only write /data.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/*
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
 && mkdir -p /data /config \
 && chown app:app /data

COPY --from=builder /opt/venv /opt/venv
COPY backend/app/     /app/backend/app/
COPY backend/mock/    /app/backend/mock/
COPY frontend/        /app/frontend/
COPY --from=css-builder /build/frontend/tailwind.css /app/frontend/tailwind.css

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # glibc creates up to 8 malloc arenas per CPU; with DuckDB's worker threads
    # the fragmented heap grew far beyond memory_limit. Two keep RSS close to it.
    MALLOC_ARENA_MAX=2 \
    APP_VERSION=${VERSION} \
    DB_PATH=/data/cpi_logs.duckdb \
    LOGS_DIR=/data/logs \
    TENANTS_CONFIG=/config/tenants.jsonc \
    FRONTEND_DIR=/app/frontend

WORKDIR /app/backend
USER app:app
VOLUME ["/data"]
EXPOSE 8080

# Liveness only (no database access). Docker marks a hanging app unhealthy;
# the in-process watchdog is what actually restarts it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

# No --reload: uvicorn itself is PID 1, so a crashed or OOM-killed server ends
# the container and the restart policy brings it back. (With the reloader as
# PID 1 the port stayed open but unanswered.) Use compose.dev.yaml to develop.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--timeout-graceful-shutdown", "10", "--timeout-keep-alive", "5", "--no-server-header"]
