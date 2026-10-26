"""No network: existing-profile selection must not widen provider destinations."""

from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import hashlib
from pathlib import Path
import sys
import subprocess
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from private_web_trial import (
    provider_environment,
    verify_frontend,
    stop_owned_resources,
    start_owned_process,
    stop_owned_group,
    group_running,
)  # noqa: E402
from check_trial_installation import verify_payload  # noqa: E402


class PrivateWebTrialProfileTests(unittest.TestCase):
    def test_explicit_profile_is_read_without_rewriting_or_reusing_old_model(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            raw = json.dumps(
                {
                    "DASHSCOPE_API_KEY": "invented-test-only-key",
                    "REQPILOT_DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "REQPILOT_ALIYUN_CHAT_MODEL": "old-invented-model",
                    "UNRELATED": "must-not-copy",
                }
            )
            path.write_text(raw)
            env = provider_environment(path, "qwen3.8-max-0902")
            self.assertEqual(path.read_text(), raw)
            self.assertEqual(env["REQPILOT_AGENT_MODEL_ID"], "qwen3.8-max-0902")
            self.assertNotIn("UNRELATED", env)

    def test_foreign_credential_urls_and_unapproved_model_are_rejected(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            for origin in (
                "http://dashscope.aliyuncs.com/v1",
                "https://untrusted.invalid/v1",
                "https://user@example.invalid/v1",
                "https://dashscope.aliyuncs.com/v1?key=invented",
            ):
                path.write_text(
                    json.dumps(
                        {
                            "DASHSCOPE_API_KEY": "invented-test-only-key",
                            "REQPILOT_DASHSCOPE_BASE_URL": origin,
                        }
                    )
                )
                with self.assertRaises(ValueError):
                    provider_environment(path, "qwen3.8-max-0902")
            with self.assertRaises(ValueError):
                path.write_text(
                    json.dumps(
                        {
                            "DASHSCOPE_API_KEY": "invented-test-only-key",
                            "REQPILOT_DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                        }
                    )
                )
                provider_environment(path, "another-model")

    def test_frontend_rejects_old_dirty_and_tampered_builds(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "index.html").write_bytes(b"invented")
            identity = {
                "schema_version": "reqpilotx-web-build/v1",
                "source_commit": "a" * 40,
                "source_tree": "b" * 40,
                "source_inputs_clean": True,
                "files": [
                    {
                        "path": "index.html",
                        "bytes": 8,
                        "sha256": hashlib.sha256(b"invented").hexdigest(),
                    }
                ],
            }
            manifest = root / "reqpilotx-build.json"
            manifest.write_text(json.dumps(identity))
            self.assertEqual(
                verify_frontend(root, "a" * 40, "b" * 40)["status"], "VERIFIED"
            )
            with self.assertRaises(ValueError):
                verify_frontend(root, "c" * 40, "b" * 40)
            identity["source_inputs_clean"] = False
            manifest.write_text(json.dumps(identity))
            with self.assertRaises(ValueError):
                verify_frontend(root, "a" * 40, "b" * 40)
            identity["source_inputs_clean"] = True
            manifest.write_text(json.dumps(identity))
            (root / "index.html").write_bytes(b"swapped!")
            with self.assertRaises(ValueError):
                verify_frontend(root, "a" * 40, "b" * 40)

    def test_installation_rejects_extra_modified_and_linked_code(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "__init__.py"
            module.write_bytes(b"# invented\n")
            expected = {"__init__.py": hashlib.sha256(module.read_bytes()).hexdigest()}
            self.assertEqual(len(verify_payload(root, expected)), 64)
            foreign = root / "foreign.py"
            foreign.write_bytes(b"# extra\n")
            with self.assertRaises(ValueError):
                verify_payload(root, expected)
            foreign.unlink()
            module.write_bytes(b"# changed\n")
            with self.assertRaises(ValueError):
                verify_payload(root, expected)
            module.unlink()
            module.symlink_to(root / "missing")
            with self.assertRaises(ValueError):
                verify_payload(root, expected)

    def test_worker_timeout_still_stops_api_and_postgres(self):
        worker = SimpleNamespace(poll=lambda: -9)
        api = SimpleNamespace(poll=lambda: 0)
        with (
            TemporaryDirectory() as temporary,
            patch(
                "private_web_trial.stop_owned_group",
                side_effect=[
                    {"stopped": True, "forced": True},
                    {"stopped": True, "forced": False},
                ],
            ) as stop,
            patch("private_web_trial.shared.stop_postgres") as postgres,
        ):
            result = stop_owned_resources(Path(temporary), {}, worker, api)
            self.assertEqual(stop.call_count, 2)
            postgres.assert_called_once()
            self.assertEqual(result["forced_stop"], ["worker"])
            self.assertEqual(result["cleanup_errors"][0]["resource"], "worker")
            self.assertTrue(result["api_stopped"] and result["postgres_stopped"])

    def test_unstopped_writer_keeps_database_and_reports_failure(self):
        with (
            TemporaryDirectory() as temporary,
            patch(
                "private_web_trial.stop_owned_group",
                side_effect=RuntimeError("still alive"),
            ),
            patch("private_web_trial.shared.stop_postgres") as postgres,
        ):
            result = stop_owned_resources(
                Path(temporary), {}, SimpleNamespace(poll=lambda: None), None
            )
            postgres.assert_not_called()
            self.assertFalse(result["worker_stopped"])
            self.assertTrue(
                any(
                    row["error_type"] == "OwnedWriterStillRunning"
                    for row in result["cleanup_errors"]
                )
            )

    @unittest.skipUnless(sys.platform != "win32", "POSIX trial runner")
    def test_owned_group_stops_a_term_ignoring_portable_child_after_parent_exit(self):
        with TemporaryDirectory() as temporary:
            marker = Path(temporary) / "child-ready"
            child_code = (
                "import signal,time,sys;from pathlib import Path;"
                'signal.signal(signal.SIGTERM,signal.SIG_IGN);Path(sys.argv[1]).write_text("READY");time.sleep(60)'
            )
            parent_code = (
                "import subprocess,sys,time;"
                'subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]]);time.sleep(60)'
            )
            process = start_owned_process(
                [sys.executable, "-c", parent_code, child_code, str(marker)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 3
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(marker.exists())
                result = stop_owned_group(process, grace=0.1)
                self.assertTrue(result["stopped"])
                self.assertTrue(result["forced"])
                self.assertFalse(group_running(process.pid))
                self.assertIsNotNone(process.poll())
            finally:
                stop_owned_group(process, grace=0.1)


if __name__ == "__main__":
    unittest.main()
