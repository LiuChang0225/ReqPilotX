"""Compatibility entrypoint for the promoted App Server lifecycle harness."""

import sys

from app.adapters.agents.codex_app_server import lifecycle as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
