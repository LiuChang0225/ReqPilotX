"""Compatibility entry for the canonical desktop artifact test."""
import importlib.util
from pathlib import Path
import sys

_path = Path(__file__).resolve().parents[2] / "apps/desktop/scripts/artifact_self_test.py"
_spec = importlib.util.spec_from_file_location("reqpilotx_desktop_artifact_test", _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
if __name__ == "__main__":
    raise SystemExit(_module.main())
sys.modules[__name__] = _module
