from __future__ import annotations

from .runtime_isolation import activate_test_runtime

activate_test_runtime()

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from .test_native_loop_capabilities import bundle, caps


class CapabilityInspectCliTests(unittest.TestCase):
    """Exercise the real CLI against disposable, content-verified bundles."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="reqpilotx-inspect-cli-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.registry = self.root / "registry"
        self.registry.mkdir()
        self.release = self.root / "release"
        self.sha = bundle(self.release, release=True, marker="inspect-regression")
        repo = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location(
            "reqpilotx_inspect_cli_under_test",
            repo / "prototype/scripts/prepare_native_loop_capability.py",
        )
        assert spec is not None and spec.loader is not None
        self.cli = importlib.util.module_from_spec(spec)
        # The legacy checkout CLI adds its source roots. Do not leak them to
        # unrelated tests; the loaded CLI still executes its real functions.
        with patch.object(sys, "path", list(sys.path)):
            spec.loader.exec_module(self.cli)

    def inspect(self, sha=None):
        argv = [
            "reqpilotx-capability", "inspect", "--registry-root",
            str(self.registry), "--bundle-sha256", self.sha if sha is None else sha,
        ]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", argv), redirect_stdout(out), redirect_stderr(err):
            result = self.cli.main()
        return result, out.getvalue(), err.getvalue()

    def assert_rejected(self, sha=None):
        code, out, err = self.inspect(sha)
        self.assertEqual(code, 2, (out, err))
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["status"], "CAPABILITY_PREPARATION_FAILED")
        return json.loads(err)["error"]

    def register(self):
        return caps.register_native_loop_capability_bundle(
            self.release, registry_root=self.registry, require_engine_release=True
        )

    def test_normal_registered_release_is_verified(self):
        registered = self.register()
        code, out, err = self.inspect()
        result = json.loads(out)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["bundle_sha256"], self.sha)
        self.assertEqual(Path(result["path"]), Path(registered["path"]).resolve())
        self.assertEqual(
            result["configuration"]["REQPILOT_NATIVE_CAPABILITY_BUNDLE_SHA256"],
            self.sha,
        )

    def test_leaf_symlink_to_valid_external_release_is_rejected(self):
        parent = self.registry / self.sha
        parent.mkdir()
        (parent / "reqpilot").symlink_to(self.release, target_is_directory=True)
        self.assertEqual(caps._verify_bundle(self.release, self.sha)["bundle_sha256"], self.sha)
        self.assertIn("symlink", self.assert_rejected())
        with self.assertRaisesRegex(ValueError, "symlink"):
            caps._registered_bundle_path(self.registry, self.sha)

    def test_sha_directory_symlink_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        shutil.copytree(self.release, outside / "reqpilot")
        (self.registry / self.sha).symlink_to(outside, target_is_directory=True)
        self.assertIn("symlink", self.assert_rejected())

    def test_invalid_sha_and_traversal_are_rejected(self):
        for value in ("../release", str(self.release), "g" * 64, "a" * 63, ""):
            with self.subTest(value=value):
                self.assertIn("SHA-256", self.assert_rejected(value))

    def test_missing_registered_release_is_rejected(self):
        self.assert_rejected()

    def test_tampered_registered_release_is_rejected(self):
        registered = self.register()
        (Path(registered["path"]) / "scripts/reqpilot.py").write_text("# changed\n")
        self.assertIn("immutable build", self.assert_rejected())

    def test_registered_development_bundle_is_not_a_release(self):
        dev = self.root / "development"
        sha = bundle(dev, release=False)
        shutil.copytree(dev, self.registry / sha / "reqpilot")
        self.assertIn("ENGINE_WHEEL", self.assert_rejected(sha))


if __name__ == "__main__":
    unittest.main()
