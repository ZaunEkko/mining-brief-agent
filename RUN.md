# RUN — 快速运行

## 方式 A：Docker

前提：Docker Desktop / Docker Engine（含 Compose v2）。首次构建约 1–2 分钟。

**Web 工作台**：

```bash
docker compose up -d --build web
```

打开 http://localhost:8080：

| 页面 | 说明 |
|---|---|
| 日报 | 固定流程：代码决定调用哪些工具，LLM（可选）只撰写摘要和风险 |
| 问答 | LLM 自主决定调用哪些 MCP 工具；需要配置 LLM |
| MCP 工具 | 直接查看和调用 3 个 server 的 5 个工具，显示参数 schema 和结构化返回 |

左侧的「钻孔日志」把运行过程按时间顺序画成岩芯：

- 每一段是一次 MCP 调用（颜色对应 server）或一轮 LLM 思考（灰色）；
- 段的长度对应耗时，取对数比例；
- `∥` 表示与上一个调用同时开始（并行）；
- 表头显示总耗时和 LLM 所占比例。

**命令行，一条命令**：

```bash
docker compose run --rm --build agent
docker compose run --rm agent mining-brief "近 14 天镍价与镍矿新闻简报" --out /out
docker compose run --rm agent mining-brief ask "比较 LME 铜、镍、锌近 30 天的涨跌幅" --out /out
```

- 结果打印到终端，并保存到 `./out/`。
- 3 个 server 会继续在后台运行（`127.0.0.1:8001-8003`），可供 MCP 客户端连接；用 `docker compose down` 全部停止。
- 断网或上游不可达时，改用录制快照：`MB_OFFLINE=1 docker compose run --rm agent`。

## 方式 B：本地 uv

前提：[uv](https://docs.astral.sh/uv/)，会自动准备 Python 3.12。

```bash
uv sync --locked
uv run mining-brief                      # 日报，默认问题：Pilbara 锂矿今日简报
uv run mining-brief --offline --no-llm   # 只用 fixtures/ 快照，不联网，不调 LLM
uv run mining-brief "近 7 天镍价简报"     # 自定义日报请求
uv run mining-brief ask "最近一周锂行业有哪些值得关注的新闻？"   # 问答，需要 LLM
```

运行时 stderr 会实时打印每次工具调用和 LLM 轮次，stdout 只输出 Markdown。Agent 以 stdio 子进程方式拉起 3 个 server，不需要另开终端。

Web 工作台本地运行：

```bash
cd web && npm ci && npm run build && cd ..
uv run mining-brief-web                  # http://127.0.0.1:8080
```

前端开发：先运行 `uv run mining-brief-web`，再执行 `cd web && npm run dev`（http://localhost:5173，`/api` 会代理到 8080）。

## 启用 LLM（可选）

没有 Key 时日报自动使用确定性模式（抽取式摘要 + 规则风险），问答模式不可用。要启用 LLM，复制 `.env.example` 为 `.env`，填入以下任一组：

```dotenv
ANTHROPIC_API_KEY=sk-ant-...          # 默认模型 claude-opus-5-5
# 或任意 OpenAI 兼容端点（DeepSeek / 通义千问 / GLM / 各类中转）；Base URL 通常要带 /v1
MB_OPENAI_BASE_URL=https://api.deepseek.com/v1
MB_OPENAI_API_KEY=...
MB_OPENAI_MODEL=deepseek-chat
```

简报页脚的「生成方式」会显示实际使用的模型。docker compose 会自动读取同目录下的 `.env`。

## 接入 MCP 客户端

见 README 的「在 AI 工具里使用这 3 个 MCP server」一节，以及写给 AI agent 的 [`llms-install.md`](llms-install.md)。可以用下面的命令验证配置：

```bash
uv run python scripts/check_mcp_config.py mcp-config.json        # Docker stdio，需先 docker compose build news
uv run python scripts/check_mcp_config.py mcp-config.http.json   # HTTP，需先 docker compose up -d news pdf price
claude mcp list                                                   # Claude Code（在仓库目录执行）
```

## 开发

```bash
uv sync --locked && uv run pre-commit install
just check          # 等价于下一行
uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest
uv run pytest -m live                              # 访问真实上游：6 份报告 ground truth
uv run python scripts/record_fixtures.py           # 重新录制 fixtures/
cd web && npm test && npm run build                # 前端单测 + vue-tsc + 构建
```

## 配置项

全部通过环境变量或 `.env` 设置，前缀为 `MB_`，定义见 [`src/mining_brief/common/config.py`](src/mining_brief/common/config.py)。

| 变量 | 默认 | 说明 |
|---|---|---|
| `MB_OFFLINE` | `0` | `1` 时完全不联网 |
| `MB_NEWS_URL` / `MB_PDF_URL` / `MB_PRICE_URL` | 空 | 设置后 Agent 走 HTTP，否则拉起 stdio 子进程 |
| `MB_LLM_PROVIDER` | `auto` | `anthropic` / `openai` / `none` |
| `MB_ANTHROPIC_MODEL` | `claude-opus-5-5` | |
| `MB_ANTHROPIC_EFFORT` | `medium` | `low` / `medium` / `high` / `xhigh` / `max` |
| `MB_ANTHROPIC_SERVER_FALLBACK` | `1` | 服务端拒答回退（beta）；网关不支持时设为 `0` |
| `MB_TIMEZONE` | `Asia/Shanghai` | 简报时间戳所用时区 |
| `MB_TOOL_TIMEOUT_S` | `180` | 单次工具调用超时 |
| `MB_WEB_HOST` / `MB_WEB_PORT` | `127.0.0.1` / `8080` | Web 服务监听地址 |
| `MB_ALLOWED_HOSTS` | 本机与 compose 服务名 | HTTP 传输允许的 Host 头（JSON 列表） |
