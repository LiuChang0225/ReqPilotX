"""Compatibility alias for reqpilotx.adapters.documents.pdf_reading_views."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("reqpilotx.adapters.documents.pdf_reading_views")
