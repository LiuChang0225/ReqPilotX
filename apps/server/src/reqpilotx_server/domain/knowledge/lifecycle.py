"""Engine compatibility alias for reqpilotx.domain.knowledge.lifecycle."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("reqpilotx.domain.knowledge.lifecycle")
