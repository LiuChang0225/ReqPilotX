"""Compatibility entry for apps/desktop/build_desktop.py."""
import importlib.util
from pathlib import Path
import sys

_path = Path(__file__).resolve().parents[1] / "apps/desktop/build_desktop.py"
_spec = importlib.util.spec_from_file_location("reqpilotx_desktop_build", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
if __name__ == "__main__":
    raise SystemExit(_module.main())
sys.modules[__name__] = _module
