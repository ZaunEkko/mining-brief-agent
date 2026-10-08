"""Shared command-line entry for the MCP servers."""

import argparse
import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from mining_brief.common.config import get_settings


def run_server(server: MCPServer, *, default_port: int, argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog=server.name, description=server.instructions)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args(argv)

    # stdout belongs to the JSON-RPC stream under stdio, so logs go to stderr.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per upstream request is noise

    if args.transport == "stdio":
        server.run("stdio")
        return
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=get_settings().allowed_hosts,
        allowed_origins=[],
    )
    server.run(
        "streamable-http",
        host=args.host,
        port=args.port,
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
