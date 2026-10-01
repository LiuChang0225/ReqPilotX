"""Fail-closed isolation guards for explicitly opted-in real-service smoke."""
from .runtime_isolation import activate_test_runtime
activate_test_runtime()

import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("reqpilotx_service_smoke", ROOT / "scripts/smoke_workbench.py")
assert spec and spec.loader
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class ServiceSmokeIsolationTests(unittest.TestCase):
    def test_child_environment_excludes_existing_model_configuration(self):
        with TemporaryDirectory() as temporary, patch.dict(os.environ, {
            "AZURE_OPENAI_API_KEY": "unrelated-profile",
            "DASHSCOPE_API_KEY": "unrelated-profile",
            "REQPILOT_AGENT_RUNTIME": "provider-tool-loop",
        }):
            root = Path(temporary)
            env = smoke.isolate(root, "postgresql://reqpilot_smoke@127.0.0.1:1234/reqpilot_smoke")
            self.assertNotIn("AZURE_OPENAI_API_KEY", env)
            self.assertNotIn("DASHSCOPE_API_KEY", env)
            self.assertEqual(env["REQPILOT_AGENT_RUNTIME"], "none")
            self.assertEqual(env["REQPILOT_STORAGE_DIR"], str(root / "storage"))

    def test_network_guard_allows_only_loopback(self):
        for address in (("127.0.0.1", 1234), ("::1", 1234), ("localhost", 1234)):
            smoke.block_external_connections("socket.connect", (None, address))
        for address in (("example.com", 443), ("192.0.2.4", 443), "/tmp/unowned.sock"):
            with self.assertRaisesRegex(RuntimeError, "loopback"):
                smoke.block_external_connections("socket.connect", (None, address))

    def test_recovery_guard_denies_original_storage_and_aliases(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original"
            original.mkdir()
            alias = root / "alias"
            alias.symlink_to(original, target_is_directory=True)
            with patch.dict(os.environ, {"REQPILOT_SMOKE_FORBIDDEN_STORAGE_ROOT": str(original)}):
                for path in (original / "head.json", alias / "head.json", root / "restored/../original/head.json"):
                    with self.assertRaisesRegex(RuntimeError, "original storage"):
                        smoke.block_external_connections("open", (str(path), "r", 0))
                smoke.block_external_connections("open", (str(root / "restored/head.json"), "r", 0))
                smoke.block_external_connections("open", (123, "r", 0))

    def test_postgres_smoke_refuses_existing_cluster(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "postgres-data").mkdir()
            with self.assertRaisesRegex(RuntimeError, "Refusing to reuse"):
                smoke.start_postgres(root, root / "missing-binaries", 1234, {})


if __name__ == "__main__":
    unittest.main()
