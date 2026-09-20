"""Compatibility alias for the promoted synthetic PDF fixture."""

import sys

from app.adapters.agents.codex_app_server import synthetic_pdf_fixture as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
