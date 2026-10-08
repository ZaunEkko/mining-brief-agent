# syntax=docker/dockerfile:1
# One image serves the three MCP servers, the agent CLI and the web UI; compose picks
# the command.

# --- UI build -------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- runtime ----------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:0.11.2 AS uv

FROM python:3.12-slim
COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" \
    MB_CACHE_DIR=/data/cache \
    MB_FIXTURES_DIR=/app/fixtures/demo \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN useradd --create-home --uid 10001 app && mkdir -p /data /out && chown app:app /data /out

# Dependencies first so source edits do not invalidate this layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY fixtures ./fixtures
RUN uv sync --locked --no-dev
COPY --from=web /web/dist ./web/dist

USER app
VOLUME ["/data"]
CMD ["mining-brief", "--help"]
