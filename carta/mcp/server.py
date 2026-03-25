"""Carta MCP server — stdio JSON-RPC transport.

Wire-protocol discipline:
- stdout is RESERVED for JSON-RPC framing. Never call print() in this module.
- All log output goes to stderr via the logging module.
- Never call sys.exit() — return structured errors instead.
- Tool handlers (Phase 2) must catch all exceptions and return error dicts.
"""
import logging
import sys

from mcp.server.fastmcp import FastMCP

# Direct ALL log output to stderr — stdout is reserved for JSON-RPC framing
logging.basicConfig(
    stream=sys.stderr,
    level=logging.WARNING,
    format="%(levelname)s [carta-mcp] %(message)s",
)

mcp_server = FastMCP("carta")


def main() -> None:
    """Entry point for carta-mcp command."""
    mcp_server.run()


if __name__ == "__main__":
    main()
