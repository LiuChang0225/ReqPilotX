"""Compatibility alias for reqpilotx.ports.agent_structure_handoff_executor."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("reqpilotx.ports.agent_structure_handoff_executor")
