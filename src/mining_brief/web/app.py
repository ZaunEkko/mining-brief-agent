"""Web API + static UI.

    GET  /api/meta                      LLM, offline flag, examples
    GET  /api/tools                     servers and their tools (with schemas)
    POST /api/tools/{server}/{tool}     call one MCP tool directly
    POST /api/brief   {query, use_llm}  SSE: progress events, then `done` with Markdown
    POST /api/ask     {question}        SSE: same, LLM decides the tool calls

Long operations stream Server-Sent Events over a POST response; the UI reads them with
fetch() and a stream reader. MCP connections are opened once at startup and shared;
each request gets a bound view that routes tool-call events to its own stream.
"""

import argparse
import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from mining_brief.agent.ask import ask
from mining_brief.agent.cli import DEFAULT_QUERY
from mining_brief.agent.events import Event, EventSink
from mining_brief.agent.llm import LLMClient, build_llm
from mining_brief.agent.orchestrator import generate_brief
from mining_brief.agent.toolbox import (
    SERVER_MODULES,
    ServerName,
    Target,
    Toolbox,
    ToolFailure,
    targets_from_settings,
)
from mining_brief.common.config import Settings, get_settings

logger = logging.getLogger(__name__)

TargetsFactory = Callable[[], dict[ServerName, Target]]
PRODUCER_STOP_TIMEOUT_S = 10.0
EXAMPLE_BRIEFS = [DEFAULT_QUERY, "近 14 天镍价与镍矿新闻简报", "LME 铜价走势简报"]
EXAMPLE_QUESTIONS = [
    "PLS 的 Pilgangoora 锂矿储量有多大？最近一个月碳酸锂价格走势如何？对项目有什么影响？",
    "最近一周锂行业有哪些值得关注的新闻？",
    "比较 LME 铜、镍、锌近 30 天的涨跌幅。",
]


def _sse(event: Event) -> str:
    payload = json.dumps(event.data, ensure_ascii=False, default=str)
    return f"event: {event.type}\ndata: {payload}\n\n"


async def _stream(run: Callable[[EventSink], Awaitable[dict[str, Any]]]) -> AsyncIterator[str]:
    """Run ``run`` in a task and relay its events; cancel it if the client goes away."""
    queue: asyncio.Queue[Event | None] = asyncio.Queue()

    async def sink(event: Event) -> None:
        await queue.put(event)

    async def runner() -> None:
        try:
            await queue.put(Event("done", await run(sink)))
        except Exception as exc:
            logger.exception("request failed")
            await queue.put(Event("error", {"message": f"{type(exc).__name__}: {exc}"}))
        finally:
            await queue.put(None)

    task = asyncio.create_task(runner())
    try:
        while (event := await queue.get()) is not None:
            yield _sse(event)
    finally:
        # Client gone or stream closed: stop the producer and wait until its own
        # cleanup (MCP calls, LLM requests) has finished before releasing the request.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=PRODUCER_STOP_TIMEOUT_S)


def _event_stream(run: Callable[[EventSink], Awaitable[dict[str, Any]]]) -> StreamingResponse:
    return StreamingResponse(
        _stream(run),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def create_app(
    settings: Settings | None = None,
    *,
    targets_factory: TargetsFactory | None = None,
    llm: LLMClient | None = None,
    dist: Path | None = None,
) -> Starlette:
    cfg = settings or get_settings()
    make_targets = targets_factory or (lambda: targets_from_settings(cfg))
    model = llm if llm is not None else build_llm(cfg)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with Toolbox(make_targets(), cfg.tool_timeout_s) as tb:
            app.state.toolbox = tb
            yield

    def shared(request: Request) -> Toolbox:
        toolbox: Toolbox = request.app.state.toolbox
        return toolbox

    async def meta(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "llm": model.name if model else None,
                "offline": cfg.offline,
                "transport": "http" if cfg.news_url else "stdio",
                "examples": {"brief": EXAMPLE_BRIEFS, "ask": EXAMPLE_QUESTIONS},
            }
        )

    async def tools(request: Request) -> JSONResponse:
        return JSONResponse(await shared(request).describe())

    async def call_tool(request: Request) -> JSONResponse:
        server, tool = request.path_params["server"], request.path_params["tool"]
        if server not in SERVER_MODULES:
            return JSONResponse({"error": f"unknown server {server!r}"}, status_code=404)
        body = await _json_body(request)
        arguments = body.get("arguments", {})
        if not isinstance(arguments, dict):
            return JSONResponse({"error": "arguments must be a JSON object"}, status_code=400)
        started = datetime.now()
        result = await shared(request).call(server, tool, arguments)
        ms = round((datetime.now() - started).total_seconds() * 1000)
        if isinstance(result, ToolFailure):
            return JSONResponse({"ok": False, "error": result.message, "ms": ms})
        return JSONResponse({"ok": True, "result": result, "ms": ms})

    async def brief(request: Request) -> Response:
        body = await _json_body(request)
        query = str(body.get("query") or DEFAULT_QUERY).strip()
        use_llm = bool(body.get("use_llm", True))
        toolbox = shared(request)

        async def run(sink: EventSink) -> dict[str, Any]:
            result = await generate_brief(
                query,
                {},
                llm=model if use_llm else None,
                timeout_s=cfg.tool_timeout_s,
                now=datetime.now(ZoneInfo(cfg.timezone)),
                on_event=sink,
                toolbox=toolbox,
            )
            return {"markdown": result.markdown, "generator": result.content.generator}

        return _event_stream(run)

    async def ask_route(request: Request) -> Response:
        body = await _json_body(request)
        question = str(body.get("question") or "").strip()
        if not question:
            return JSONResponse({"error": "question must not be empty"}, status_code=400)
        if model is None:
            return JSONResponse(
                {"error": "问答模式需要 LLM：在 .env 中配置 ANTHROPIC_API_KEY 或 MB_OPENAI_*"},
                status_code=409,
            )
        llm_client, toolbox = model, shared(request)

        async def run(sink: EventSink) -> dict[str, Any]:
            answer = await ask(question, toolbox.bind(sink), llm_client, sink)
            return {
                "markdown": answer.markdown,
                "model": answer.model,
                "calls": answer.calls,
                "unverified_urls": answer.unverified_urls,
                "stopped_early": answer.stopped_early,
            }

        return _event_stream(run)

    routes: list[Route | Mount] = [
        Route("/api/meta", meta),
        Route("/api/tools", tools),
        Route("/api/tools/{server}/{tool}", call_tool, methods=["POST"]),
        Route("/api/brief", brief, methods=["POST"]),
        Route("/api/ask", ask_route, methods=["POST"]),
    ]
    static = dist or cfg.web_dist
    if static.is_dir():
        routes.append(Mount("/", StaticFiles(directory=static, html=True), name="ui"))
    else:
        logger.warning("UI build not found at %s; serving the API only", static)
    return Starlette(routes=routes, lifespan=lifespan)


def main(argv: list[str] | None = None) -> None:
    cfg = get_settings()
    parser = argparse.ArgumentParser(prog="mining-brief-web", description="Web UI and API")
    parser.add_argument("--host", default=cfg.web_host)
    parser.add_argument("--port", type=int, default=cfg.web_port)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(cfg), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
