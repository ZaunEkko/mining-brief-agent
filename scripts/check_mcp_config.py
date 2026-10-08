"""Connect to every server in an MCP client config and list its tools.

Usage: uv run python scripts/check_mcp_config.py [mcp-config.json | mcp-config.http.json]
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters


async def check(name: str, spec: dict[str, object]) -> bool:
    target: str | StdioServerParameters
    if "url" in spec:
        target = str(spec["url"])
    else:
        args = spec.get("args", [])
        target = StdioServerParameters(
            command=str(spec["command"]),
            args=[str(a) for a in args] if isinstance(args, list) else [],
        )
    try:
        async with Client(target, read_timeout_seconds=60) as client:
            tools = (await client.list_tools()).tools
    except Exception as exc:
        print(f"[fail] {name}: {type(exc).__name__}: {exc}")
        return False
    print(f"[ok]   {name}: " + ", ".join(t.name for t in tools))
    return True


async def main(servers: dict[str, dict[str, object]]) -> int:
    results = [await check(name, spec) for name, spec in servers.items()]
    return 0 if all(results) else 1


if __name__ == "__main__":
    config = Path(sys.argv[1] if len(sys.argv) > 1 else "mcp-config.json")
    sys.exit(asyncio.run(main(json.loads(config.read_text("utf-8"))["mcpServers"])))
