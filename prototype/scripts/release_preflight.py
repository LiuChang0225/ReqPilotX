"""Compatibility entry for the canonical desktop preflight."""
import importlib.util
from pathlib import Path
import sys

_path = Path(__file__).resolve().parents[2] / "apps/desktop/scripts/release_preflight.py"
_spec = importlib.util.spec_from_file_location("reqpilotx_desktop_preflight", _path)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
if __name__ == "__main__":
    raise SystemExit(_module.main())
sys.modules[__name__] = _module
