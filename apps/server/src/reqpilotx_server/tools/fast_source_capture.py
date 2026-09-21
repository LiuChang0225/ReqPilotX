"""Engine compatibility alias for reqpilotx.tools.fast_source_capture."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("reqpilotx.tools.fast_source_capture")
