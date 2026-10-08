# Task runner. Every recipe is a thin alias over a plain `uv run ...` command,
# so nothing here is required — see RUN.md for the raw commands.

set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

default:
    @just --list

# Install the locked environment (runtime + dev)
sync:
    uv sync --locked

# Format + lint + type-check + test: the gate before every commit
check: fmt-check lint type test

fmt:
    uv run ruff format .
    uv run ruff check --fix .

fmt-check:
    uv run ruff format --check .

lint:
    uv run ruff check .

type:
    uv run mypy

test:
    uv run pytest

# Tests that hit the real upstream sites (network required)
test-live:
    uv run pytest -m live

# Generate today's brief locally (stdio transport, servers spawned as subprocesses)
brief query="给我生成一份关于 Pilbara 锂矿的今日简报":
    uv run mining-brief "{{query}}"

# Full stack in Docker (servers over streamable HTTP)
up:
    docker compose up -d --build news pdf price

down:
    docker compose down
