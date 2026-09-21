"""Compatibility alias for reqpilotx.ports.semantic_run_repository."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("reqpilotx.ports.semantic_run_repository")
