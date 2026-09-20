"""Compatibility alias for promoted shadow Structure contracts."""

import sys

from app.adapters.agents.codex_app_server import shadow_structure as _implementation


sys.modules[__name__] = _implementation
