"""Restricted Python child entry for the frozen desktop executable.

PyInstaller makes ``sys.executable`` point back to ReqPilotX.exe. Portable
tools may reenter it only for the registered plugin scripts or the source
capture module; ordinary desktop launches never dispatch Python code.
"""

from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys


CHILD_MARKER = "REQPILOT_FROZEN_PYTHON_CHILD"
SCRIPT_ROOT = "REQPILOT_FROZEN_PYTHON_SCRIPT_ROOT"
_ALLOWED_MODULES = frozenset({
    "reqpilotx.tools.fast_source_capture",
    "reqpilotx_server.worker",
    "local_worker_entry",
})


def is_frozen_python_child() -> bool:
    return bool(getattr(sys, "frozen", False)) and os.environ.get(CHILD_MARKER) == "1"


def run_frozen_python_child() -> int:
    arguments = sys.argv[1:]
    if len(arguments) >= 2 and arguments[0] == "-m":
        if arguments[1] not in _ALLOWED_MODULES:
            raise SystemExit("Unsupported packaged Python module")
        sys.argv = [arguments[1], *arguments[2:]]
        runpy.run_module(arguments[1], run_name="__main__")
        return 0

    if not arguments or not os.environ.get(SCRIPT_ROOT):
        raise SystemExit("Packaged Python child has no registered script root")
    root = Path(os.environ[SCRIPT_ROOT]).expanduser().resolve(strict=True)
    script = Path(arguments[0]).expanduser().resolve(strict=True)
    if not script.is_file() or script.suffix != ".py" or not script.is_relative_to(root / "scripts"):
        raise SystemExit("Packaged Python child rejected an unregistered script")
    sys.path.insert(0, str(script.parent))
    sys.argv = [str(script), *arguments[1:]]
    runpy.run_path(str(script), run_name="__main__")
    return 0
