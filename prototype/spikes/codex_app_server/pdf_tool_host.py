"""Compatibility alias for the promoted qualification PDF host."""

import sys

from app.adapters.agents.codex_app_server import pdf_tool_host as _implementation


sys.modules[__name__] = _implementation
