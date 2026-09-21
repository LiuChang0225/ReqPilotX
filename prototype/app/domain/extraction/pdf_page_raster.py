"""Compatibility alias for reqpilotx.domain.extraction.pdf_page_raster."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("reqpilotx.domain.extraction.pdf_page_raster")
