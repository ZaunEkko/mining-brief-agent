# Installation guide for AI agents

This file is written for an AI coding agent (Claude Code, Cursor, Cline, Codex, ...)
that has been asked to install **mining-brief-agent** and connect its three MCP servers
to itself or to another MCP client. Humans should read [`README.md`](README.md).

Follow the steps in order. Run every command from the repository root. Report what you
did and what you verified at the end.

## Ground rules

- The software is proprietary (all rights reserved, see `LICENSE`). Install it only
  for a user who owns it or has the copyright holder's written permission.
- Do not commit or print secrets. API keys go into `.env` (already in `.gitignore`).
- Ask the user before editing any **global** client configuration
  (`~/.claude.json`, `claude_desktop_config.json`, `~/.cursor/mcp.json`). Project-level
  files in this repository are fine to create.
- Never use `--force`, `sudo`, or global `pip install`; everything runs through `uv`.

## 1. Prerequisites

```bash
uv --version        # required; install: https://docs.astral.sh/uv/getting-started/installation/
git --version
docker --version    # optional, only for the Docker route
node --version      # optional, >= 22.12, only to rebuild the web UI
```

If `uv` is missing, tell the user and stop; do not try another Python installer.

## 2. Install

```bash
uv sync --locked
```

This creates `.venv/` with Python 3.12 and every dependency pinned in `uv.lock`.

## 3. Smoke test (no network, no API key needed)

```bash
uv run mining-brief --offline --no-llm
```

Expected: a Markdown brief titled `Pilgangoora 矿业日报` with sections 一 to 四, a
resource table whose Indicated row is `356 | 1.29 | 4.6`, and a footer line
`生成方式：deterministic (no LLM configured)`. If this fails, stop and report the error.

## 4. Optional: LLM

Only if the user provides a key. Copy `.env.example` to `.env` and set **one** of:

```dotenv
ANTHROPIC_API_KEY=...                        # Anthropic, default model claude-opus-5-5
MB_OPENAI_BASE_URL=https://.../v1            # or any OpenAI-compatible endpoint;
MB_OPENAI_API_KEY=...                        # the base URL usually ends in /v1
MB_OPENAI_MODEL=...
```

Verify with `uv run mining-brief ask "LME 铜价最近 30 天涨跌多少？"`; the answer must end
with `生成方式：<provider>:<model>`. Without a key, skip this step; the brief still works.

## 5. Connect the MCP servers

The three servers are stdio commands run from this repository:

| Name | Command (cwd = repo root) | Tools |
|---|---|---|
| `mining-news-mcp` | `uv run mining-news-mcp` | `search`, `fetch_article` |
| `mineral-pdf-mcp` | `uv run mineral-pdf-mcp` | `extract_resources` |
| `lme-price-mcp` | `uv run lme-price-mcp` | `get_price`, `get_trend` |

Use the section for the client you are configuring. `<REPO>` means the absolute path of
this repository (forward slashes work on Windows too).

### Claude Code

The repository ships a project-scoped [`.mcp.json`](.mcp.json); opening Claude Code in
the repo offers to enable the three servers. To register them explicitly instead (only
for this project):

```bash
claude mcp add --scope local mining-news-mcp -- uv run mining-news-mcp
claude mcp add --scope local mineral-pdf-mcp -- uv run mineral-pdf-mcp
claude mcp add --scope local lme-price-mcp -- uv run lme-price-mcp
claude mcp list        # all three must show ✔ Connected
```

For use outside this repository, ask the user, then use `--scope user` with
`uv run --directory <REPO> <server>`.

### Claude Desktop

Merge into `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`), then restart:

```json
{
  "mcpServers": {
    "mining-news-mcp": {"command": "uv", "args": ["run", "--directory", "<REPO>", "mining-news-mcp"]},
    "mineral-pdf-mcp": {"command": "uv", "args": ["run", "--directory", "<REPO>", "mineral-pdf-mcp"]},
    "lme-price-mcp": {"command": "uv", "args": ["run", "--directory", "<REPO>", "lme-price-mcp"]}
  }
}
```

If the user prefers Docker: run `docker compose build news` once and use
[`mcp-config.json`](mcp-config.json) as is (no paths needed).

### Cursor

Create `.cursor/mcp.json` in the repo (project scope) with the Claude Desktop JSON
above, or, while `docker compose up -d news pdf price` is running, use
[`mcp-config.http.json`](mcp-config.http.json) (Streamable HTTP on ports 8001-8003).

### Any other MCP client

Use the stdio commands from the table with the repository as working directory, or the
HTTP endpoints `http://127.0.0.1:800{1,2,3}/mcp` when the Docker servers are running.

## 6. Verify

Run the config checker; every server must list its tools:

```bash
uv run python scripts/check_mcp_config.py mcp-config.json        # needs the Docker image
uv run python scripts/check_mcp_config.py mcp-config.http.json   # needs docker compose up
```

Then, from the client you configured, call `lme-price-mcp` → `get_price` with
`{"commodity": "copper"}`. Expected: a price in `USD/t` with a `trading_date` and
`provenance.source` mentioning westmetall. If the network is blocked, set
`MB_OFFLINE=1` in the server environment; results then come from `fixtures/` and
`provenance.data_mode` is `fixture`.

## 7. Optional: web UI

```bash
docker compose up -d --build web     # http://localhost:8080
# or, without Docker:
cd web && npm ci && npm run build && cd .. && uv run mining-brief-web
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `LLM 调用失败 … returned text/html, not JSON` | `MB_OPENAI_BASE_URL` is missing its `/v1` suffix |
| Tool result `upstream unreachable and no cached copy` | Network blocked; use `MB_OFFLINE=1` or configure a proxy (`HTTPS_PROXY`) |
| `Invalid Host header` (HTTP transport) | Add the host to `MB_ALLOWED_HOSTS` (JSON list, e.g. `["myhost:*"]`) |
| Claude Code shows the servers as pending | Approve them when prompted, or use the `claude mcp add` commands above |
