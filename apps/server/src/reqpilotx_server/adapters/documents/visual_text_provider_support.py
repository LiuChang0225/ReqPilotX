"""Engine compatibility alias for reqpilotx.adapters.documents.visual_text_provider_support."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("reqpilotx.adapters.documents.visual_text_provider_support")
