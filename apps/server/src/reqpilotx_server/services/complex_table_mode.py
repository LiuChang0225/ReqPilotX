"""Engine compatibility alias for reqpilotx.services.complex_table_mode."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("reqpilotx.services.complex_table_mode")
