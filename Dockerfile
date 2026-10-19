# syntax=docker/dockerfile:1

# ── фронт: Vite+ на Bun ──────────────────────────────────────────
FROM oven/bun:1.4.3 AS web
WORKDIR /web
COPY web/package.json web/bun.lock ./
RUN bun install --frozen-lockfile
COPY web/ ./
RUN bun run build

# ── зависимости Python (netschoolpy ставится из git) ─────────────
FROM python:3.12-slim AS deps
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# ── итоговый образ: только venv, сервер и собранный фронт ───────
FROM python:3.12-slim
RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app /data
WORKDIR /app
COPY --from=deps /app/.venv ./.venv
COPY server ./server
COPY --from=web /web/dist ./web/dist
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    SESSIONS_FILE=/data/sessions.json
USER app
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/regions')"
CMD ["python", "-m", "server"]
