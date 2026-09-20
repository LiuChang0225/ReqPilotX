from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime, active_test_storage_dir
else:
    from runtime_isolation import activate_test_runtime, active_test_storage_dir

activate_test_runtime()

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from app.runtime_paths import STORAGE_ROOT_ENV_VAR, resolve_storage_dir


class RuntimePathIsolationTests(unittest.TestCase):
    def test_resolver_accepts_an_injected_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            prototype_dir = Path(tmpdir) / "prototype"
            absolute_root = Path(tmpdir) / "absolute-storage"

            self.assertEqual(
                resolve_storage_dir(prototype_dir, environment={}),
                prototype_dir / "storage",
            )
            self.assertEqual(
                resolve_storage_dir(
                    prototype_dir,
                    environment={STORAGE_ROOT_ENV_VAR: str(absolute_root)},
                ),
                absolute_root,
            )
            self.assertEqual(
                resolve_storage_dir(
                    prototype_dir,
                    environment={STORAGE_ROOT_ENV_VAR: "relative-storage"},
                ),
                prototype_dir / "relative-storage",
            )

    def test_all_legacy_storage_consumers_bind_to_the_isolated_root(self) -> None:
        from app import db
        if __package__:
            from .application_under_test import main
        else:
            from application_under_test import main
        from app.services import ai_copilot

        isolated_root = active_test_storage_dir()

        self.assertEqual(db.STORAGE_DIR, isolated_root)
        self.assertEqual(db.DB_PATH, isolated_root / "app.db")
        self.assertEqual(main.STORAGE_DIR, isolated_root)
        self.assertEqual(main.UPLOADS_DIR, isolated_root / "uploads")
        self.assertEqual(main.KNOWLEDGE_DIR, isolated_root / "knowledge")
        self.assertEqual(main.EXPORTS_DIR, isolated_root / "exports")
        self.assertEqual(main.TABLE_SNAPSHOT_DIR, isolated_root / "table_snapshots")
        self.assertEqual(ai_copilot.STORAGE_DIR, isolated_root)
        self.assertEqual(ai_copilot.TRACE_DIR, isolated_root / "ai_traces")
        self.assertEqual(ai_copilot.INDEX_PAYLOAD_DIR, isolated_root / "knowledge_index")

    def test_every_test_module_activates_isolation_before_application_imports(self) -> None:
        tests_dir = Path(__file__).resolve().parent
        missing: list[str] = []
        late: list[str] = []
        import_markers = (
            "\nfrom app",
            "\nimport app",
            "\nfrom prototype.scripts",
        )

        for path in sorted(tests_dir.glob("test_*.py")):
            source = path.read_text(encoding="utf-8")
            activation_index = source.find("activate_test_runtime()")
            if activation_index < 0:
                missing.append(path.name)
                continue
            first_application_import = min(
                (index for marker in import_markers if (index := source.find(marker)) >= 0),
                default=-1,
            )
            if first_application_import >= 0 and activation_index > first_application_import:
                late.append(path.name)

        self.assertEqual(missing, [], f"Tests missing runtime isolation: {missing}")
        self.assertEqual(late, [], f"Tests activate isolation after app imports: {late}")

    def test_package_and_raw_discovery_entries_preserve_real_config(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        production_config = repo_root / "prototype" / "storage" / "config.json"
        original_exists = production_config.exists()
        original_bytes = production_config.read_bytes() if original_exists else b""
        test_name = (
            "test_apply_runtime_ai_provider_setup_can_switch_default_profile_to_aliyun"
        )
        commands = (
            [
                sys.executable,
                "-m",
                "unittest",
                f"prototype.tests.test_ai_copilot.AICopilotTests.{test_name}",
                "-v",
            ],
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "prototype/tests",
                "-p",
                "test_ai_copilot.py",
                "-k",
                test_name,
                "-v",
            ],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            canary_root = Path(tmpdir) / "inherited-runtime"
            canary_root.mkdir()
            canary_config = canary_root / "config.json"
            canary_bytes = b'{"DO_NOT_CHANGE": "developer-state-canary"}'
            canary_config.write_bytes(canary_bytes)

            for index, command in enumerate(commands):
                isolated_root = Path(tmpdir) / f"entry-{index}"
                env = os.environ.copy()
                env["REQPILOT_TEST_STORAGE_DIR"] = str(isolated_root)
                # Simulate an inherited live runtime without ever pointing a
                # failing regression at the developer's real storage tree.
                env[STORAGE_ROOT_ENV_VAR] = str(canary_root)
                result = subprocess.run(
                    command,
                    cwd=repo_root,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"Command failed: {' '.join(command)}\n{result.stdout}\n{result.stderr}",
                )
                isolated_config = isolated_root / "config.json"
                self.assertTrue(isolated_config.is_file())
                payload = json.loads(isolated_config.read_text(encoding="utf-8"))
                self.assertEqual(payload["REQPILOT_AI_DEFAULT_PROFILE"], "aliyun")
                self.assertEqual(canary_config.read_bytes(), canary_bytes)

        # Read-only assurance for the actual developer path. The subprocesses
        # above can never target this file, even when isolation regresses.
        self.assertEqual(production_config.exists(), original_exists)
        if original_exists:
            self.assertEqual(production_config.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
