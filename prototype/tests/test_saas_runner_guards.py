"""A green process is insufficient evidence when mandatory work did not run."""
from __future__ import annotations

# ruff: noqa: E402
from .runtime_isolation import activate_test_runtime
activate_test_runtime()

import importlib.util
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("saas_runner_under_test", ROOT / "scripts/test_saas_postgres.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

GOOD = "test_complete_sdk_agent_worker_review_export_without_snapshot_seeding (tests.Native) ... ok\nRan 23 tests in 1.2s\nOK\n"


class SaaSRunnerGuardTests(unittest.TestCase):
    def test_zero_skipped_missing_sdk_or_failed_process_cannot_pass(self):
        for text, code in (("Ran 0 tests in 0.0s\nOK\n", 0),
                           (GOOD + "OK (skipped=23)\n", 0),
                           (GOOD.replace("test_complete_sdk_agent_worker_review_export_without_snapshot_seeding", "ordinary_test"), 0),
                           (GOOD, 1)):
            self.assertEqual(runner.assess_result(text, code, worktree_clean=True)["status"], "FAIL")
        self.assertEqual(runner.assess_result(GOOD, 0, worktree_clean=True)["status"], "PASS")

    def test_dirty_source_never_signs_formal_candidate(self):
        self.assertEqual(runner.assess_result(GOOD, 0, worktree_clean=False)["status"], "FAIL")
        value = runner.assess_result(GOOD, 0, worktree_clean=False, allow_dirty=True)
        self.assertEqual(value["status"], "DEVELOPMENT_PASS")
        self.assertFalse(value["formal_candidate"])

    def test_partial_start_failure_stops_only_its_owned_cluster(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            data = output / "owned-database"
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                if command[-1] == "start":
                    raise subprocess.CalledProcessError(1, command)
                return SimpleNamespace(returncode=0)
            with patch.object(runner.subprocess, "run", side_effect=run):
                with self.assertRaises(subprocess.CalledProcessError):
                    with runner.managed_postgres(Path("/official/bin"), Path("/official/share"), data, output, 12345):
                        self.fail("A failed start must not enter the test body")
            stopped = [command for command in calls if command[-1] == "stop"]
            self.assertEqual(len(stopped), 1)
            self.assertEqual(stopped[0][stopped[0].index("-D") + 1], str(data))

    def test_inactive_cluster_after_failed_init_is_not_started_or_stopped(self):
        with TemporaryDirectory() as directory:
            output = Path(directory)
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                if command[0].endswith("initdb"):
                    raise subprocess.CalledProcessError(1, command)
                return SimpleNamespace(returncode=3)
            with patch.object(runner.subprocess, "run", side_effect=run):
                with self.assertRaises(subprocess.CalledProcessError):
                    with runner.managed_postgres(Path("/official/bin"), Path("/official/share"), output / "owned", output, 12345):
                        self.fail("Failed initialization cannot execute acceptance")
            self.assertFalse(any(command[-1] in {"start", "stop"} for command in calls))
