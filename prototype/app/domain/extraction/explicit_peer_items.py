"""Compatibility alias for reqpilotx.domain.extraction.explicit_peer_items."""
from importlib import import_module as _import_module
import sys as _sys

_sys.modules[__name__] = _import_module("reqpilotx.domain.extraction.explicit_peer_items")
