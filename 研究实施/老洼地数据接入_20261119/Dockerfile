FROM postgres:16-bookworm AS pg-tools

# ── Stage 1: Dependency Builder ───────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.30" "pydantic>=2.0" "httpx>=0.27" \
      "pandapower>=3.0" "numpy>=1.26" "scipy>=1.11" \
      "sqlalchemy>=2.0" "alembic>=1.13" "psycopg[binary]>=3.2" "pgvector>=0.3" "pyyaml>=6.0"

# ── Stage 2: Minimal Non-Root Production Runtime ──────────────────────────────
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="laowadi-power-agent" \
      org.opencontainers.image.description="Laowadi Coal-Mine Power Distribution Enterprise Agent (FastAPI + pandapower + TimescaleDB/pgvector)" \
      org.opencontainers.image.version="2.4.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    LAOWADI_LOG_FORMAT=json \
    LAOWADI_LOG_LEVEL=INFO \
    LAOWADI_PORT=8765 \
    LAOWADI_PG_TOOL_DIR=/opt/pg-tools

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 liblz4-1 libzstd1 curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 laowadi \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash laowadi

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=pg-tools /usr/lib/postgresql/16/bin/pg_dump /usr/lib/postgresql/16/bin/pg_restore /opt/pg-tools/
COPY --chown=laowadi:laowadi pyproject.toml alembic.ini README.md ./
COPY --chown=laowadi:laowadi laowadi ./laowadi
COPY --chown=laowadi:laowadi scripts ./scripts
COPY --chown=laowadi:laowadi workbench/dist ./workbench/dist

RUN mkdir -p /app/local /app/local/backups /app/local/logs /backups \
    && chown -R laowadi:laowadi /app/local /backups \
    && chmod +x /app/scripts/*.sh

USER laowadi

EXPOSE 8765

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8765/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "laowadi.asgi:app", "--host", "0.0.0.0", "--port", "8765", "--workers", "2"]
