"""``mining-brief``: daily brief (fixed plan) or ``ask`` (LLM-driven tool use).

mining-brief ["request"] [--offline] [--no-llm] [--out DIR]
mining-brief ask "question" [--offline]
"""

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from mining_brief.agent.ask import ask
from mining_brief.agent.events import Event
from mining_brief.agent.llm import build_llm
from mining_brief.agent.orchestrator import generate_brief
from mining_brief.agent.toolbox import Toolbox, targets_from_settings
from mining_brief.common.config import Settings, get_settings

DEFAULT_QUERY = "给我生成一份关于 Pilbara 锂矿的今日简报"


async def _print_event(event: Event) -> None:
    """Human-readable progress on stderr; stdout carries only the Markdown result."""
    d = event.data
    if event.type == "tool_call":
        print(f"  → {d['server']}.{d['tool']} {d['arguments']}", file=sys.stderr)
    elif event.type == "tool_result":
        mark = "✓" if d["ok"] else "✗"
        print(f"  {mark} {d['server']}.{d['tool']} {d['ms']}ms · {d['summary']}", file=sys.stderr)
    elif event.type == "llm":
        print(f"  ◆ LLM {d['model']} ({d['phase']})", file=sys.stderr)
    elif event.type == "llm_done":
        calls = f" → {d['calls']} 个工具调用" if d.get("calls") else ""
        print(f"  ◆ LLM {d['ms']}ms{calls}", file=sys.stderr)
    elif event.type == "text":
        print(f"  … {d['text'][:120]}", file=sys.stderr)
    elif event.type == "warning":
        print(f"  ! {d['message']}", file=sys.stderr)


def _settings(offline: bool, no_llm: bool) -> Settings:
    updates: dict[str, object] = {}
    if offline:
        updates["offline"] = True
    if no_llm:
        updates["llm_provider"] = "none"
    settings = get_settings()
    return settings.model_copy(update=updates) if updates else settings


def _save(markdown: str, out: Path, prefix: str) -> None:
    path = out / f"{prefix}-{datetime.now():%Y%m%d-%H%M%S}.md"
    try:
        out.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, "utf-8", newline="\n")
    except OSError as exc:  # e.g. a read-only bind mount; the result is already on stdout
        print(f"\n[warn] could not save {path}: {exc}", file=sys.stderr)
    else:
        print(f"\n[saved] {path}", file=sys.stderr)


async def _ask(question: str, settings: Settings) -> str:
    llm = build_llm(settings)
    if llm is None:
        raise SystemExit("ask mode needs an LLM: set ANTHROPIC_API_KEY or MB_OPENAI_* in .env")
    async with Toolbox(
        targets_from_settings(settings), settings.tool_timeout_s, _print_event
    ) as tb:
        answer = await ask(question, tb, llm, _print_event)
    return answer.markdown


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    ask_mode = bool(argv) and argv[0] == "ask"
    parser = argparse.ArgumentParser(
        prog="mining-brief ask" if ask_mode else "mining-brief",
        description="LLM-driven tool use over the MCP servers"
        if ask_mode
        else "MCP mining daily brief agent (use 'mining-brief ask \"...\"' for free questions)",
    )
    if ask_mode:
        parser.add_argument("question", help="free-form question; the LLM picks the tools")
    else:
        parser.add_argument("query", nargs="?", default=DEFAULT_QUERY, help="brief request")
        parser.add_argument("--no-llm", action="store_true", help="force deterministic mode")
    parser.add_argument("--out", type=Path, default=Path("out"), help="directory for the .md file")
    parser.add_argument("--offline", action="store_true", help="serve only from recorded fixtures")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv[1:] if ask_mode else argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    if ask_mode:
        settings = _settings(args.offline, no_llm=False)
        markdown = asyncio.run(_ask(args.question, settings))
        prefix = "answer"
    else:
        settings = _settings(args.offline, args.no_llm)
        brief = asyncio.run(
            generate_brief(
                args.query,
                targets_from_settings(settings),
                llm=build_llm(settings),
                timeout_s=settings.tool_timeout_s,
                now=datetime.now(ZoneInfo(settings.timezone)),
                on_event=_print_event,
            )
        )
        markdown, prefix = brief.markdown, "brief"
    print(markdown)
    _save(markdown, args.out, prefix)


if __name__ == "__main__":
    main()
