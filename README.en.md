# mining-brief-agent

[中文](README.md) | **English**

A mining research agent built on the **Model Context Protocol (MCP)**. Three independent MCP servers provide mining news, NI 43-101 / JORC mineral resources and metal prices; an agent combines them into a cited Markdown daily brief or answers free-form questions. A Vue workbench shows every tool call as it happens.

```text
 "Give me today's brief on Pilbara lithium" / "How do Pilgangoora's resources and lithium prices affect the project?"
                │
     ┌──────────┴───────────────────────────────────────────┐
     │  Agent                                                │
     │   brief: fixed plan (code decides the calls)          │
     │   ask:   the LLM decides which tools to call, how often│
     └──────┬────────────────────┬────────────────────┬──────┘
            │ MCP (stdio / Streamable HTTP)            │
   mining-news-mcp        mineral-pdf-mcp        lme-price-mcp
   search                 extract_resources      get_price
   fetch_article          (self-check + abstain) get_trend
   mining.com · Google    ASX filings · any PDF  LME (westmetall) · GFEX lithium carbonate
```

The UI and briefs are in Chinese; code, tool schemas and agent install docs are in English.

![Brief: fixed plan, the drill-core log on the left shows every MCP call by duration](assets/workbench-brief.png)

<details>
<summary>Ask and MCP tools screenshots</summary>

![Ask: the LLM decides which tools to call; failed calls are hatched red](assets/workbench-ask.png)

![MCP tools: inspect schemas and call any tool directly](assets/workbench-tools.png)

</details>

## Quick start

**Web workbench (recommended):**

```bash
docker compose up -d --build web
```

Open http://localhost:8080. The brief, ask and MCP-tools pages draw each run as a drill core: every segment is one MCP call or one LLM round, its length is the time it took.

**CLI, one command:**

```bash
docker compose run --rm --build agent
```

**Local with uv:**

```bash
uv sync --locked
uv run mining-brief                      # daily brief (default: Pilbara lithium)
uv run mining-brief --offline --no-llm   # no network, no API key
uv run mining-brief ask "比较 LME 铜、镍、锌近 30 天的涨跌幅"   # ask mode (needs an LLM)
```

The LLM is optional: set `ANTHROPIC_API_KEY` or any OpenAI-compatible endpoint (`MB_OPENAI_*`) in `.env`. Without a key the brief runs in deterministic mode. See [RUN.md](RUN.md) for everything else.

## Using the three MCP servers from your AI tools

**Easiest: let your agent install it.** Open this repository in Claude Code, Cursor or Cline and say:

> Install and configure this project following `llms-install.md`, connect its three MCP servers to yourself, then verify by fetching the copper price with lme-price-mcp.

[`llms-install.md`](llms-install.md) is an install guide written for AI agents: prerequisites, install, smoke test, per-client configuration and verification.

**By hand:**

| Client | How |
|---|---|
| Claude Code | The repo ships a project-scoped [`.mcp.json`](.mcp.json); Claude Code offers to enable it when opened here. Or run `claude mcp add --scope local mining-news-mcp -- uv run mining-news-mcp` (once per server), then `claude mcp list`. |
| Claude Desktop | Merge [`mcp-config.json`](mcp-config.json) (Docker stdio, path-independent) into `claude_desktop_config.json`; the non-Docker form is in `llms-install.md`. |
| Cursor | Use [`mcp-config.http.json`](mcp-config.http.json) after `docker compose up -d news pdf price`, or the stdio form above. |

Check any config with `uv run python scripts/check_mcp_config.py <file>`: it connects to every server and lists its tools.

## MCP tools

| Server | Tool | What it does |
|---|---|---|
| `mining-news-mcp` | `search(query, days, limit)` | Merges mining.com search / commodity / front-page RSS with Google News, dedupes, ranks by IDF-weighted relevance |
| | `fetch_article(url, max_chars)` | Main-text extraction with an SSRF guard |
| `mineral-pdf-mcp` | `extract_resources(pdf_url, max_pages)` | Measured / Indicated / Inferred tonnage, grade and contained metal, with consistency checks |
| `lme-price-mcp` | `get_price(commodity, date)` | Price on a date (or the closest earlier trading day) |
| | `get_trend(commodity, days)` | Series, change, high / low, mean and direction over a window |

Every tool returns structured output (`outputSchema` / `structuredContent`), raises `ToolError` for expected failures, and stamps each result with `source_url`, `retrieved_at` and `data_mode`.

## Design highlights

- **No invented numbers.** Resource and price tables are rendered from tool output only. The LLM writes summaries and risks; claims citing unknown sources or nothing at all are dropped, and ask mode flags any link that did not appear in a tool result.
- **Resource extraction that abstains.** Tables are read via their units header and must reconcile (tonnage x grade ≈ contained, categories ≈ total, Measured + Indicated ≈ M&I); otherwise `abstain=true` and only a warning is shown. Ground truth covers six real reports (3 JORC, 3 NI 43-101): [`tests/data/resource_ground_truth.json`](tests/data/resource_ground_truth.json).
- **Two agent styles.** The brief is a fixed plan, predictable and easy to evaluate; ask mode is LLM-driven with step and call budgets and tool errors fed back to the model.
- **Graceful degradation.** Fresh cache → live → stale cache → recorded fixture, with the source of every result labelled. A down server only blanks its own section; no LLM means deterministic mode.
- **Runs out of the box.** `fixtures/demo/` is script-generated synthetic data (same structure as the real upstreams, no third-party text); `--offline` / `MB_OFFLINE=1` never touches the network. `scripts/record_fixtures.py` records real snapshots into the local cache when needed.

## Engineering

| Area | Practice |
|---|---|
| Dependencies | uv + `uv.lock` (Python 3.12); npm + `package-lock.json` (Node 22) |
| Quality gate | `just check` = ruff format + ruff lint + mypy `--strict` + pytest (122 offline tests, 6 live); UI: vue-tsc strict + vitest |
| Pre-commit | Same locked toolchain as CI |
| CI | GitHub Actions: Python gate, UI build, Docker smoke test |
| Testing | In-memory MCP contract tests via `mcp.Client(server)`, respx for HTTP, injected LLM stubs; no test touches the network |


## Known limitations

- Prices come from public re-publishers (westmetall, Sina Finance), not exchange-licensed feeds; iron ore (Mysteel) needs an account and is not supported.
- Google News links are JavaScript redirects: headline and publisher only, no article text.
- Resource extraction needs a PDF text layer; scanned reports fail cleanly (no OCR).
- The project catalogue (`agent/catalog.py`) currently contains Pilgangoora only. Other requests run the generic commodity flow without a resource section; ask mode accepts any report URL.

## License

**All rights reserved**; this is not open-source software. The repository is public for viewing only: no use, copying, modification, distribution or running as a service without the copyright holder's written permission. To request permission, contact the owner via their GitHub profile. See [`LICENSE`](LICENSE); the bundled fonts keep their own license, see [`NOTICE`](NOTICE).
