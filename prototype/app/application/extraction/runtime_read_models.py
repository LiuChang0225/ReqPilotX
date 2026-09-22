"""Compatibility alias for reqpilotx_server.application.extraction.runtime_read_models."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("reqpilotx_server.application.extraction.runtime_read_models")
