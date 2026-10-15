"""Exercise allowed metadata and refuse accidental payload/credential disclosure."""
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location("pilot_status", Path(__file__).with_name("status-readonly.py"))
status = importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


class ReadOnlyStatusTests(unittest.TestCase):
    def test_bounded_release_and_backup_metadata_without_payloads(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            base = root / "opt"; release = base / "releases/candidate-1"
            release.mkdir(parents=True); (base / "current").symlink_to(release)
            (release / "release.json").write_text(json.dumps({
                "contract": "reqpilotx-private-pilot-release-v1",
                "target": "linux-x86_64-python3.11-glibc2.28", "revision_label": "candidate-1",
                "source_snapshot_sha256": "a" * 64, "source_commit": "b" * 40,
                "engine_core_identity": "c" * 64, "native_capability_bundle_sha256": "d" * 64,
                "extra": "DO-NOT-PRINT-MANIFEST-EXTRA"}))
            state = root / "application"; state.mkdir()
            (state / "private.env").write_text("DO-NOT-PRINT-SECRET")
            backups = root / "backups"; snapshot = backups / "20261015T060000Z-123"
            snapshot.mkdir(parents=True)
            (snapshot / "database.dump").write_text("DO-NOT-PRINT-DATABASE")
            calls = []
            def runner(command, **kwargs):
                calls.append(command)
                return SimpleNamespace(returncode=0, stdout="ActiveState=active\nEnvironment=DO-NOT-PRINT-ENV\nExecMainStatus=0\n")
            result = status.collect(base=base, state=state, backups=backups, runner=runner)
            serialized = json.dumps(result)
            self.assertNotIn("DO-NOT-PRINT", serialized)
            self.assertEqual(result["release"]["source_commit"], "b" * 40)
            self.assertEqual(result["backups"]["snapshot_directories"], 1)
            self.assertFalse(result["backups"]["restore_verified"])
            self.assertEqual(result["network_requests"], 0)
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(command[0:2] == ["systemctl", "show"] for command in calls))

    def test_unknown_release_target_is_not_read(self):
        with TemporaryDirectory() as temporary:
            base = Path(temporary).resolve() / "opt"; base.mkdir()
            (base / "releases").mkdir()
            outside = Path(temporary).resolve() / "unapproved"; outside.mkdir()
            (base / "current").symlink_to(outside)
            self.assertEqual(status.release_status(base)["status"], "UNKNOWN")

    def test_raw_pointer_parent_traversal_is_not_normalized_into_another_release(self):
        with TemporaryDirectory() as temporary:
            base = Path(temporary).resolve() / "opt"; (base / "releases/good").mkdir(parents=True)
            (base / "current").symlink_to("releases/missing/../good")
            self.assertEqual(status.release_status(base)["status"], "UNKNOWN")

    def test_error_text_and_service_values_are_not_echoed(self):
        def runner(command, **kwargs):
            if command[2] == "reqpilotx-api.service":
                raise PermissionError("DO-NOT-PRINT-PRIVATE-PATH")
            return SimpleNamespace(returncode=0, stdout="ActiveState=private value with spaces\n")
        result = json.dumps(status.service_status(runner))
        self.assertNotIn("DO-NOT-PRINT", result)
        self.assertNotIn("private value", result)
        self.assertIn("PermissionError", result)

    def test_backup_root_symlink_is_refused(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); (root / "alias").symlink_to(root, target_is_directory=True)
            self.assertEqual(status.backup_status(root / "alias")["status"], "UNKNOWN")

    def test_symlink_ancestor_cannot_redirect_release_backup_or_state(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(); actual = root / "actual"; actual.mkdir()
            (actual / "backups").mkdir(); (actual / "state").mkdir()
            (actual / "opt/releases/candidate-1").mkdir(parents=True)
            (actual / "opt/current").symlink_to(actual / "opt/releases/candidate-1")
            alias = root / "alias"; alias.symlink_to(actual, target_is_directory=True)
            def runner(command, **kwargs):
                return SimpleNamespace(returncode=0, stdout="ActiveState=active\n")
            result = status.collect(base=alias / "opt", backups=alias / "backups", state=alias / "state", runner=runner)
            for key in ("release", "backups", "state_filesystem"):
                self.assertEqual(result[key]["status"], "UNKNOWN", key)


if __name__ == "__main__":
    unittest.main()
