"""Checkout-only compatibility exports for the installed server package."""

from pathlib import Path as _Path
import sys as _sys


_ROOT = _Path(__file__).resolve().parents[2]
for _source in (_ROOT / "apps/server/src", _ROOT / "packages/engine/src"):
    if str(_source) not in _sys.path:
        _sys.path.insert(0, str(_source))

from reqpilotx_server import *  # noqa: F401,F403,E402
