"""Compatibility alias for reqpilotx.services.response_matrix."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("reqpilotx.services.response_matrix")
