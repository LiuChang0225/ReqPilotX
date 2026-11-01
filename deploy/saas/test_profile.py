"""Offline regression checks for fail-closed deployment operations."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entrypoint = load_module("pilot_entrypoint", "entrypoint.py")
verifier = load_module("pilot_verifier", "verify-release.py")


class DeploymentBoundaryTests(unittest.TestCase):
    def test_disabled_auth_is_refused_before_app_import(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                entrypoint.configure_environment()

    def test_sqlite_test_mode_is_refused(self):
        with patch.dict(os.environ, {"REQPILOT_SAAS_ENABLED": "1", "REQPILOT_TEST_MODE": "1"}, clear=True):
            with self.assertRaises(ValueError):
                entrypoint.configure_environment()

    def test_secret_reader_refuses_multiline_or_empty_values(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "value"
            for value in ("", "first\nsecond", "first\rsecond"):
                path.write_text(value)
                with self.assertRaises(ValueError):
                    entrypoint.read_secret(str(path))

    def test_nonprivate_container_db_is_refused(self):
        with patch.dict(os.environ, {"REQPILOT_SAAS_ENABLED": "1", "REQPILOT_DATABASE_HOST": "public.example"}, clear=True):
            with patch.object(entrypoint, "read_secret", return_value="synthetic-only"):
                with self.assertRaises(ValueError):
                    entrypoint.configure_environment()


class ReleaseVerificationTests(unittest.TestCase):
    def manifest(self, root: Path):
        payload = b"synthetic artifact"
        path = root / "payload"
        path.write_bytes(payload)
        import hashlib
        manifest = {
            "contract": "reqpilotx-private-pilot-release-v1",
            "target": "linux-x86_64-python3.11-glibc2.28",
            "revision_label": "synthetic-test-only",
            "source_snapshot_sha256": "0" * 64,
            "files": {"payload": {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}},
        }
        (root / "release.json").write_text(json.dumps(manifest))
        return manifest

    def test_manifest_validates_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.manifest(root)
            self.assertTrue(verifier.verify(root)["ok"])
            (root / "payload").write_bytes(b"tampered artifact!")
            with self.assertRaises(ValueError):
                verifier.verify(root)

    def test_unexpected_file_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.manifest(root)
            (root / "unexpected").touch()
            with self.assertRaises(ValueError):
                verifier.verify(root)

    def test_symlink_payload_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.manifest(root)
            (root / "payload").unlink()
            (root / "payload").symlink_to(root / "release.json")
            with self.assertRaises(ValueError):
                verifier.verify(root)

    def test_unexpected_nested_manifest_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.manifest(root)
            (root / "extra").mkdir()
            (root / "extra/release.json").write_text("{}")
            with self.assertRaises(ValueError):
                verifier.verify(root)

    def test_symlink_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.manifest(root)
            (root / "alias").symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                verifier.verify(root)

    def test_parent_traversal_path_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = self.manifest(root)
            manifest["files"]["../escape"] = manifest["files"]["payload"]
            (root / "release.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                verifier.verify(root)


if __name__ == "__main__":
    unittest.main()
