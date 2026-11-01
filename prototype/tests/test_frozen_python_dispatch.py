from __future__ import annotations

from .runtime_isolation import activate_test_runtime

activate_test_runtime()

import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from apps.desktop.frozen_python import (
    CHILD_MARKER,
    SCRIPT_ROOT,
    is_frozen_python_child,
    run_frozen_python_child,
)


class FrozenPythonDispatchTests(unittest.TestCase):
    def test_desktop_entry_dispatches_before_loading_gui_or_server(self) -> None:
        desktop = Path(__file__).resolve().parents[2] / "apps/desktop"
        with tempfile.TemporaryDirectory() as raw:
            plugin = Path(raw) / "registered"
            script = plugin / "scripts" / "tool.py"
            script.parent.mkdir(parents=True)
            script.write_text("print('early-child-ok')\n", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import runpy,sys; sys.frozen=True; "
                    "sys.argv=['ReqPilotX.exe',sys.argv[1]]; "
                    "runpy.run_path('apps/desktop/app_desktop.py',run_name='__main__')",
                    str(script),
                ],
                cwd=desktop.parents[1],
                env={
                    **os.environ,
                    CHILD_MARKER: "1",
                    SCRIPT_ROOT: str(plugin),
                    "PYTHONPATH": str(desktop),
                },
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "early-child-ok")

    def test_registered_script_runs_without_desktop_startup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            plugin = Path(raw) / "registered"
            script = plugin / "scripts" / "tool.py"
            script.parent.mkdir(parents=True)
            script.write_text("print('tool-child-ok')\n", encoding="utf-8")
            output = io.StringIO()
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "argv", ["ReqPilotX.exe", str(script)]),
                patch.object(sys, "path", list(sys.path)),
                patch.dict(os.environ, {CHILD_MARKER: "1", SCRIPT_ROOT: str(plugin)}),
                redirect_stdout(output),
            ):
                self.assertTrue(is_frozen_python_child())
                self.assertEqual(run_frozen_python_child(), 0)
            self.assertEqual(output.getvalue().strip(), "tool-child-ok")

    def test_unregistered_script_and_module_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            plugin = Path(raw) / "registered"
            plugin.mkdir()
            outside = Path(raw) / "outside.py"
            outside.write_text("raise RuntimeError('must not run')\n", encoding="utf-8")
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.dict(os.environ, {CHILD_MARKER: "1", SCRIPT_ROOT: str(plugin)}),
            ):
                with patch.object(sys, "argv", ["ReqPilotX.exe", str(outside)]):
                    with self.assertRaisesRegex(SystemExit, "unregistered script"):
                        run_frozen_python_child()
                with patch.object(sys, "argv", ["ReqPilotX.exe", "-m", "evil.module"]):
                    with self.assertRaisesRegex(SystemExit, "Unsupported packaged Python module"):
                        run_frozen_python_child()

    def test_environment_marker_does_not_affect_source_python(self) -> None:
        with patch.dict(os.environ, {CHILD_MARKER: "1"}), patch.object(sys, "frozen", False, create=True):
            self.assertFalse(is_frozen_python_child())


if __name__ == "__main__":
    unittest.main()
