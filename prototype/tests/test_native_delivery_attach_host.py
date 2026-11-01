"""The Agent can attach a real unchanged export without gaining arbitrary paths."""
from __future__ import annotations

# ruff: noqa: E402
from .runtime_isolation import activate_test_runtime
activate_test_runtime()

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from reqpilotx_server.application.agent_workflow.native_loop_tools import NativeLoopToolHost


class NativeDeliveryAttachHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.plugin = root / "plugin"
        (self.plugin / "scripts").mkdir(parents=True)
        (self.plugin / "scripts/reqpilot.py").write_text("# synthetic registered entry")
        delivery = self.workspace / "delivery"
        delivery.mkdir()
        (delivery / "matrix-snapshot.json").write_text(json.dumps({"delivery_sha256": "a" * 64}))
        (delivery / "matrix.xlsx").write_bytes(b"synthetic preexisting tool output")
        self.host = NativeLoopToolHost(workspace=self.workspace, plugin_root=self.plugin,
                                      review_dir=self.workspace / "review")

    def test_attach_pins_host_review_and_only_trusted_compiled_delivery(self):
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout='{"status":"CURRENT_DELIVERY_ATTACHED"}')) as run:
            result = self.host.invoke("reqpilot_run", {"operation": "review-attach-delivery",
                "arguments": {"delivery_dir": "delivery", "expected_review_sha256": "b" * 64}})
        self.assertTrue(result["success"], result)
        command = run.call_args.args[0]
        self.assertIn("review-attach-delivery", command)
        self.assertEqual(command[command.index("--review-dir") + 1], str(self.workspace / "review"))
        self.assertEqual(command[command.index("--delivery-dir") + 1], str(self.workspace / "delivery"))

    def test_missing_pin_untrusted_file_or_foreign_path_never_executes(self):
        for args in ({"delivery_dir": "delivery"},
                     {"delivery_dir": "delivery", "expected_review_sha256": "invalid"},
                     {"delivery_dir": "../outside", "expected_review_sha256": "b" * 64},
                     {"delivery_dir": "delivery", "expected_review_sha256": "b" * 64, "review_dir": "/"}):
            with patch("subprocess.run") as run:
                result = self.host.invoke("reqpilot_run", {"operation": "review-attach-delivery", "arguments": args})
                self.assertFalse(result["success"], result)
                run.assert_not_called()
        (self.workspace / "delivery/matrix.xlsx").write_bytes(b"tampered")
        with patch("subprocess.run") as run:
            result = self.host.invoke("reqpilot_run", {"operation": "review-attach-delivery",
                "arguments": {"delivery_dir": "delivery", "expected_review_sha256": "b" * 64}})
            self.assertFalse(result["success"])
            run.assert_not_called()
