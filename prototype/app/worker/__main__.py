"""Compatibility alias for reqpilotx_server.worker.__main__."""
from importlib import import_module as _import_module
import sys as _sys

_entry = _import_module("reqpilotx_server.worker.__main__")

if __name__ == "__main__":
    raise SystemExit(_entry.main())
else:
    _sys.modules[__name__] = _entry
