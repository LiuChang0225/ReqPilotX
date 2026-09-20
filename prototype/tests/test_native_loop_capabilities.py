from __future__ import annotations

# ruff: noqa: E402 -- isolate application paths before importing host modules.
from .runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.application.agent_workflow import native_loop_capabilities as caps
from app.adapters.agents.codex_app_server.native_loop_driver import _digest, _write_json


def bundle(root):
    root.mkdir(parents=True)
    script = root / "scripts/reqpilot.py"
    script.parent.mkdir()
    script.write_text("# Synthetic frozen entry; never executed in this test.\n")
    receipt = {
        "source_repository": "ReqPilotX",
        "plugin_files": [
            {
                "path": "scripts/reqpilot.py",
                "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            }
        ],
        "runtime_files": [],
    }
    receipt["bundle_sha256"] = _digest(receipt)
    _write_json(root / "build-receipt.json", receipt)
    return receipt["bundle_sha256"]


class NativeLoopCapabilityTests(unittest.TestCase):
    def test_upgrade_keeps_original_source_package_and_thread_until_resume(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            sha = bundle(base / "release")
            root = base / "task"
            (root / "runtime").mkdir(parents=True)
            (root / "plugin").mkdir()
            (root / "plugin/original.txt").write_text("unchanged original bundle")
            meta = {
                "task_id": "task",
                "project_id": "project",
                "document_id": "document",
                "source_sha256": "a" * 64,
                "plugin_bundle_sha256": "b" * 64,
                "latest_job_id": "job",
            }
            _write_json(root / "task.json", meta)
            _write_json(
                root / "runtime/run-state.json",
                {
                    "status": "INTERRUPTED",
                    "thread_id": "same-thread",
                    "identity": {"binding_sha256": "source-bound"},
                    "terminal_observation": {"verified_terminal": True},
                    "pid": None,
                    "app_server_pid": None,
                },
            )
            job = SimpleNamespace(status=SimpleNamespace(value="FAILED"))
            container = SimpleNamespace(jobs=SimpleNamespace(get=lambda _: job))
            with patch.object(caps, "REGISTRY", base / "registered"):
                caps.register_native_loop_capability_bundle(base / "release")
                with patch.object(
                    caps, "_existing", return_value=(container, root, meta)
                ):
                    payload = caps.NativeLoopCapabilityUpgradeV1(
                        command_id="upgrade-once",
                        bundle_sha256=sha,
                        origin="AGENT_SELF_REVIEW",
                        reason="Enable the verified missing Map transition compiler",
                    )
                    result = caps.upgrade_native_loop_capability(
                        "project",
                        "document",
                        "task",
                        payload,
                        SimpleNamespace(headers={}),
                    )
                    replay = caps.upgrade_native_loop_capability(
                        "project",
                        "document",
                        "task",
                        payload,
                        SimpleNamespace(headers={}),
                    )
            self.assertEqual(result, replay)
            self.assertEqual(result["status"], "STAGED")
            self.assertEqual(result["thread_id"], "same-thread")
            self.assertFalse(result["new_model_turn_started"])
            self.assertEqual(
                (root / "plugin/original.txt").read_text(), "unchanged original bundle"
            )
            self.assertEqual(
                json.loads((root / "runtime/run-state.json").read_text())["thread_id"],
                "same-thread",
            )
            updated = json.loads((root / "task.json").read_text())
            self.assertEqual(updated["active_plugin_bundle_sha256"], sha)
            self.assertEqual(updated["source_sha256"], "a" * 64)
            self.assertTrue(
                (root / updated["capability_epoch"]["authorization_reference"]).exists()
            )

    def test_registered_bundle_rejects_changed_bytes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "bundle"
            sha = bundle(root)
            (root / "scripts/reqpilot.py").write_text("# altered")
            with self.assertRaisesRegex(ValueError, "immutable build"):
                caps._verify_bundle(root, sha)
