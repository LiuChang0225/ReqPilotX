"""Compatibility entrypoint for the promoted MCP qualification harness."""

import sys

from app.adapters.agents.codex_app_server import (
    read_only_mcp_canary as _implementation,
)


if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
