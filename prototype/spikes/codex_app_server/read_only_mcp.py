"""Compatibility entrypoint for the promoted read-only MCP server."""

import sys

from app.adapters.agents.codex_app_server import read_only_mcp as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
