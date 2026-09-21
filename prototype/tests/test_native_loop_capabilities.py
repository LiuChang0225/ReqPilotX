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
from concurrent.futures import ThreadPoolExecutor

from app.application.agent_workflow import native_loop_capabilities as caps
from app.adapters.agents.codex_app_server.native_loop_driver import _digest, _write_json


def bundle(root, *, release=False, marker=""):
    root.mkdir(parents=True)
    script = root / "scripts/reqpilot.py"
    script.parent.mkdir()
    script.write_text(
        "# Synthetic frozen entry; never executed in this test.\n" + marker
    )
    receipt = {
        "source_repository": "ReqPilotX",
        "release_mode": "ENGINE_WHEEL" if release else "DEVELOPMENT_SOURCE_COPY",
        "delivery_included": True,
        "decomposition_included": True,
        "source_checkout_required_at_runtime": False,
        "plugin_files": [
            {
                "path": "scripts/reqpilot.py",
                "sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
            }
        ],
        "runtime_files": [],
    }
    if release:
        receipt["engine_release"] = {
            "core_identity": hashlib.sha256(marker.encode()).hexdigest(),
            "wheel_sha256": "e" * 64,
        }
    receipt["bundle_sha256"] = _digest(receipt)
    _write_json(root / "build-receipt.json", receipt)
    return receipt["bundle_sha256"]


class NativeLoopCapabilityTests(unittest.TestCase):
    def test_registered_release_is_copied_reverified_and_pinned_by_full_bundle(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            sha_a = bundle(base / "a", release=True, marker="A")
            sha_b = bundle(base / "b", release=True, marker="B")
            registry = base / "registry"
            caps.register_native_loop_capability_bundle(
                base / "a", registry_root=registry
            )
            caps.register_native_loop_capability_bundle(
                base / "b", registry_root=registry
            )
            settings = SimpleNamespace(
                native_capability_mode="registered-release",
                native_capability_registry_root=registry,
                native_capability_bundle_sha256=sha_a,
            )
            task_a = base / "task-a"
            task_a.mkdir()
            with patch.object(caps.subprocess, "run") as build:
                prepared_a = caps.prepare_native_loop_capability(task_a, settings)
            build.assert_not_called()
            self.assertEqual(prepared_a["bundle_sha256"], sha_a)
            settings.native_capability_bundle_sha256 = sha_b
            task_b = base / "task-b"
            task_b.mkdir()
            prepared_b = caps.prepare_native_loop_capability(task_b, settings)
            self.assertEqual(prepared_b["bundle_sha256"], sha_b)
            _write_json(
                task_a / "task.json",
                {"plugin_bundle_sha256": sha_a},
            )
            self.assertEqual(
                caps.active_native_loop_plugin(task_a),
                (task_a / "plugin").resolve(),
            )
            self.assertEqual(
                _read_receipt(task_a / "plugin")["bundle_sha256"], sha_a
            )
            with self.assertRaisesRegex(ValueError, "Queued native job capability"):
                caps.native_loop_plugin_for_job(
                    task_a,
                    {"plugin_bundle_sha256": sha_a},
                    {"plugin_bundle_sha256": sha_b},
                )
            self.assertEqual(
                caps.native_loop_plugin_for_job(
                    task_a, {"plugin_bundle_sha256": sha_a}, {}
                ),
                (task_a / "plugin").resolve(),
            )
            (task_a / "plugin/scripts/reqpilot.py").write_text("# tampered\n")
            with self.assertRaisesRegex(ValueError, "immutable build"):
                caps.active_native_loop_plugin(task_a)

    def test_manifest_rejects_undeclared_missing_traversal_and_symlink(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            root = base / "bundle"
            sha = bundle(root, release=True)
            (root / "extra.sh").write_text("#!/bin/sh\n")
            with self.assertRaisesRegex(ValueError, "manifest is incomplete"):
                caps._verify_bundle(root, sha)
            (root / "extra.sh").unlink()
            (root / "scripts/reqpilot.py").unlink()
            with self.assertRaises((FileNotFoundError, ValueError)):
                caps._verify_bundle(root, sha)

            root = base / "unsafe"
            sha = bundle(root, release=True)
            receipt = _read_receipt(root)
            receipt["plugin_files"][0]["path"] = "../escape.py"
            receipt["bundle_sha256"] = _digest(
                {key: value for key, value in receipt.items() if key != "bundle_sha256"}
            )
            _write_json(root / "build-receipt.json", receipt)
            with self.assertRaisesRegex(ValueError, "unsafe plugin_files path"):
                caps._verify_bundle(root, receipt["bundle_sha256"])

            root = base / "linked"
            sha = bundle(root, release=True)
            target = root / "scripts/target.py"
            target.write_text("# target\n")
            link = root / "scripts/link.py"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symlink"):
                caps._verify_bundle(root, sha)

    def test_registration_is_idempotent_under_concurrency(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            sha = bundle(base / "release", release=True)
            registry = base / "registry"
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(
                    executor.map(
                        lambda _: caps.register_native_loop_capability_bundle(
                            base / "release", registry_root=registry
                        ),
                        range(2),
                    )
                )
            self.assertEqual({result["bundle_sha256"] for result in results}, {sha})
            self.assertFalse(list(registry.rglob(".reqpilot-registering-*")))

    def test_copy_failure_leaves_no_runnable_bundle_or_staging(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            sha = bundle(base / "release", release=True)
            registry = base / "registry"
            caps.register_native_loop_capability_bundle(
                base / "release", registry_root=registry
            )
            task = base / "task"
            task.mkdir()
            settings = SimpleNamespace(
                native_capability_mode="registered-release",
                native_capability_registry_root=registry,
                native_capability_bundle_sha256=sha,
            )
            with patch.object(caps, "_verify_bundle", side_effect=[_read_receipt(base / "release"), ValueError("copy changed")]):
                with self.assertRaisesRegex(ValueError, "copy changed"):
                    caps.prepare_native_loop_capability(task, settings)
            self.assertFalse((task / "plugin").exists())
            self.assertFalse(list(task.glob(".plugin-staging-*")))

    def test_development_builder_runs_only_when_explicit(self):
        with tempfile.TemporaryDirectory() as raw:
            task = Path(raw) / "task"
            task.mkdir()

            def build(command, **_):
                output = Path(command[command.index("--output") + 1])
                bundle(output)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            settings = SimpleNamespace(
                native_capability_mode="development-source",
                deployment_mode="local",
            )
            with patch.object(caps.subprocess, "run", side_effect=build) as process:
                result = caps.prepare_native_loop_capability(task, settings)
            self.assertEqual(process.call_count, 1)
            self.assertEqual(result["capability_source"], "development-source")

    def test_legacy_task_bundle_resumes_but_missing_bundle_never_rebuilds(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            task = base / "legacy-task"
            sha = bundle(task / "plugin")
            _write_json(task / "task.json", {"plugin_bundle_sha256": sha})
            self.assertEqual(
                caps.active_native_loop_plugin(task),
                (task / "plugin").resolve(),
            )
            missing = base / "missing-task"
            missing.mkdir()
            _write_json(missing / "task.json", {"plugin_bundle_sha256": sha})
            with patch.object(caps.subprocess, "run") as builder:
                with self.assertRaises(FileNotFoundError):
                    caps.active_native_loop_plugin(missing)
            builder.assert_not_called()

    def test_registered_release_rejects_dev_bundle_wrong_identity_and_escape(self):
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            dev_sha = bundle(base / "dev")
            registry = base / "registry"
            caps.register_native_loop_capability_bundle(
                base / "dev", registry_root=registry
            )
            task = base / "task"
            task.mkdir()
            settings = SimpleNamespace(
                native_capability_mode="registered-release",
                native_capability_registry_root=registry,
                native_capability_bundle_sha256=dev_sha,
            )
            with self.assertRaisesRegex(ValueError, "ENGINE_WHEEL"):
                caps.prepare_native_loop_capability(task, settings)
            settings.native_capability_bundle_sha256 = "f" * 64
            with self.assertRaisesRegex(ValueError, "not registered"):
                caps.prepare_native_loop_capability(task, settings)
            escape = registry / ("c" * 64)
            escape.mkdir(parents=True)
            (escape / "reqpilot").symlink_to(base / "dev", target_is_directory=True)
            settings.native_capability_bundle_sha256 = "c" * 64
            with self.assertRaisesRegex(ValueError, "symlink"):
                caps.prepare_native_loop_capability(task, settings)

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


def _read_receipt(root: Path):
    return json.loads((root / "build-receipt.json").read_text())
