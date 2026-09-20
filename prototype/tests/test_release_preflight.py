from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import build_desktop
from scripts import release_preflight


class ReleasePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.project_dir = Path(self.temp_dir.name) / "prototype"
        frontend_dir = self.project_dir / "frontend" / "dist"
        assets_dir = frontend_dir / "assets"
        manifest_dir = frontend_dir / ".vite"
        assets_dir.mkdir(parents=True)
        manifest_dir.mkdir(parents=True)
        (frontend_dir / "index.html").write_text(
            '<script type="module" src="/assets/index-test.js"></script>'
            '<link rel="stylesheet" href="/assets/index-test.css">',
            encoding="utf-8",
        )
        (manifest_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "index.html": {
                        "file": "assets/index-test.js",
                        "isEntry": True,
                        "css": ["assets/index-test.css"],
                    }
                }
            ),
            encoding="utf-8",
        )
        (assets_dir / "index-test.js").write_text(
            'const worker = "/assets/pdf.worker-test.mjs";',
            encoding="utf-8",
        )
        (assets_dir / "index-test.css").write_text(
            ":root{color:#18211c}", encoding="utf-8"
        )
        (assets_dir / "pdf.worker-test.mjs").write_text(
            "// worker", encoding="utf-8"
        )
        (self.project_dir / "app").mkdir(parents=True)
        (self.project_dir / "app" / "module.py").write_text(
            "VALUE = 'safe'\n", encoding="utf-8"
        )
        (self.project_dir / "app_desktop.py").write_text(
            "def main():\n    return 0\n", encoding="utf-8"
        )

    def _write_manifest(
        self,
        *,
        platform: str,
        arch: str,
        binary: Path,
        digest: str | None = None,
        version: str = "1.15.4",
        size_bytes: int | None = None,
        commit: bool = True,
    ) -> Path:
        manifest = self.project_dir / "release" / "qdrant-artifacts.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        relative = binary.relative_to(self.project_dir).as_posix()
        payload = {
            "schema_version": 1,
            "artifacts": [
                {
                    "platform": platform,
                    "arch": arch,
                    "path": relative,
                    "version": version,
                    "size_bytes": binary.stat().st_size if size_bytes is None else size_bytes,
                    "sha256": digest or hashlib.sha256(binary.read_bytes()).hexdigest(),
                }
            ],
        }
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        if commit:
            self._commit_path(manifest)
        return manifest

    def _commit_path(self, path: Path) -> None:
        repo_dir = self.project_dir.parent
        if not (repo_dir / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
            subprocess.run(
                ["git", "config", "user.email", "release-preflight@example.invalid"],
                cwd=repo_dir,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Release Preflight Test"],
                cwd=repo_dir,
                check=True,
            )
        relative = path.relative_to(repo_dir)
        subprocess.run(["git", "add", "--", str(relative)], cwd=repo_dir, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "test trust anchor"],
            cwd=repo_dir,
            check=True,
        )

    def _write_empty_manifest(self) -> Path:
        manifest = self.project_dir / "release" / "qdrant-artifacts.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"schema_version": 1, "artifacts": []}),
            encoding="utf-8",
        )
        self._commit_path(manifest)
        return manifest

    def _fake_bundled_artifact(self) -> release_preflight.QdrantArtifact:
        binary = self._write_macho("arm64")
        return release_preflight.QdrantArtifact(
            path=binary,
            platform="darwin",
            arch="arm64",
            version="1.15.4",
            size_bytes=binary.stat().st_size,
            sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            bundle_destination="bin/mac",
        )

    def _copy_bundled_artifact(
        self,
        artifact: release_preflight.QdrantArtifact,
        relative: str = "ReqPilotX.app/Contents/Frameworks/bin/mac/qdrant",
    ) -> Path:
        bundled = self.project_dir / "dist" / relative
        bundled.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artifact.path, bundled)
        return bundled

    def _write_macho(self, arch: str = "arm64") -> Path:
        cpu_type = {"arm64": 0x0100000C, "x86_64": 0x01000007}[arch]
        binary = self.project_dir / "bin" / "vendor" / "qdrant"
        binary.parent.mkdir(parents=True, exist_ok=True)
        header = b"\xcf\xfa\xed\xfe" + struct.pack("<I", cpu_type) + (b"\x00" * 24)
        binary.write_bytes(
            header + (b"\x00" * (release_preflight._MIN_QDRANT_BINARY_BYTES - len(header)))
        )
        binary.chmod(0o755)
        return binary

    def _write_pe(self, arch: str = "x86_64") -> Path:
        machine = {"arm64": 0xAA64, "x86_64": 0x8664}[arch]
        binary = self.project_dir / "bin" / "vendor" / "qdrant.exe"
        binary.parent.mkdir(parents=True, exist_ok=True)
        header = bytearray(release_preflight._MIN_QDRANT_BINARY_BYTES)
        header[:2] = b"MZ"
        header[0x3C:0x40] = (0x80).to_bytes(4, "little")
        header[0x80:0x84] = b"PE\x00\x00"
        header[0x84:0x86] = machine.to_bytes(2, "little")
        binary.write_bytes(header)
        return binary

    def test_default_preflight_does_not_require_bin_directory(self) -> None:
        report = release_preflight.run_preflight(self.project_dir)

        self.assertTrue(report["ok"])
        self.assertFalse(report["qdrant_embedded"])
        self.assertFalse((self.project_dir / "bin").exists())

    def test_missing_referenced_static_asset_fails(self) -> None:
        (self.project_dir / "frontend" / "dist" / "assets" / "index-test.js").write_text(
            'const missing = "/assets/not-present.js";', encoding="utf-8"
        )

        with self.assertRaisesRegex(release_preflight.PreflightError, "missing static asset"):
            release_preflight.run_preflight(self.project_dir)

    def test_vite_entry_and_manifest_are_required(self) -> None:
        frontend_dir = self.project_dir / "frontend" / "dist"
        for required in release_preflight._REQUIRED_FRONTEND_FILES:
            with self.subTest(required=required):
                page = frontend_dir / required
                original = page.read_text(encoding="utf-8")
                page.unlink()
                with self.assertRaisesRegex(
                    release_preflight.PreflightError,
                    "required frontend build file is missing",
                ):
                    release_preflight.run_preflight(self.project_dir)
                page.write_text(original, encoding="utf-8")

    def test_unknown_vite_asset_type_does_not_create_a_release_gate(self) -> None:
        (self.project_dir / "frontend" / "dist" / "assets" / "font.woff2").write_bytes(
            b"opaque"
        )
        self.assertTrue(release_preflight.run_preflight(self.project_dir)["ok"])

    def test_explicit_static_png_binary_is_allowed(self) -> None:
        (self.project_dir / "frontend" / "dist" / "assets" / "logo.png").write_bytes(
            b"\x89PNG\r\n"
        )

        report = release_preflight.run_preflight(self.project_dir)

        self.assertTrue(report["ok"])

    def test_development_absolute_path_fails(self) -> None:
        (self.project_dir / "app" / "module.py").write_text(
            'CACHE = "/Users/alice/Documents/ReqPilotX/cache"\n', encoding="utf-8"
        )

        with self.assertRaisesRegex(release_preflight.PreflightError, "development absolute path"):
            release_preflight.run_preflight(self.project_dir)

    def test_probable_embedded_secret_fails(self) -> None:
        (self.project_dir / "app" / "module.py").write_text(
            'API_KEY = "sk-live-AbCdEf0123456789-XyZ"\n', encoding="utf-8"
        )

        with self.assertRaisesRegex(release_preflight.PreflightError, "probable credential"):
            release_preflight.run_preflight(self.project_dir)

    def test_prefixed_secret_identifiers_fail(self) -> None:
        for name in (
            "OPENAI_API_KEY",
            "ALIYUN_ACCESS_TOKEN",
            "DATABASE_PASSWORD",
            "SERVICE_CLIENT_SECRET",
        ):
            with self.subTest(name=name):
                (self.project_dir / "app" / "module.py").write_text(
                    f'{name} = "sk-live-AbCdEf0123456789-XyZ"\n',
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    release_preflight.PreflightError,
                    "probable credential",
                ):
                    release_preflight.run_preflight(self.project_dir)

    def test_static_config_formats_and_bare_values_are_scanned(self) -> None:
        static_dir = self.project_dir / "frontend" / "dist" / "assets"
        for filename in (
            ".env",
            "settings.cfg",
            "settings.env",
            "settings.ini",
            "settings.toml",
            "settings.yaml",
            "settings.yml",
        ):
            with self.subTest(filename=filename):
                config = static_dir / filename
                config.write_text(
                    "OPENAI_API_KEY = sk-live-AbCdEf0123456789-XyZ\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    release_preflight.PreflightError,
                    "probable credential",
                ):
                    release_preflight.run_preflight(self.project_dir)
                config.unlink()

    def test_javascript_backtick_and_bare_secret_values_fail(self) -> None:
        app_js = self.project_dir / "frontend" / "dist" / "assets" / "index-test.js"
        for source in (
            "const OPENAI_API_KEY = `sk-live-AbCdEf0123456789-XyZ`;",
            "const OPENAI_API_KEY = sk-live-AbCdEf0123456789-XyZ;",
        ):
            with self.subTest(source_kind="backtick" if "`" in source else "bare"):
                app_js.write_text(source, encoding="utf-8")
                with self.assertRaisesRegex(
                    release_preflight.PreflightError,
                    "probable credential",
                ):
                    release_preflight.run_preflight(self.project_dir)

    def test_code_expression_assigned_to_secret_field_is_not_a_literal(self) -> None:
        (self.project_dir / "app" / "module.py").write_text(
            'config.openai_api_key = str(getattr(config, "openai_api_key", ""))\n',
            encoding="utf-8",
        )
        (self.project_dir / "frontend" / "dist" / "assets" / "index-test.js").write_text(
            'const setupAliyunApiKey = document.getElementById("aliyun-api-key");',
            encoding="utf-8",
        )

        report = release_preflight.run_preflight(self.project_dir)

        self.assertTrue(report["ok"])

    def test_known_secret_prefix_cannot_be_hidden_by_placeholder_substring(self) -> None:
        (self.project_dir / "app" / "module.py").write_text(
            'OPENAI_API_KEY = "sk-live-dummy-AbCdEf0123456789-XyZ"\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(release_preflight.PreflightError, "probable credential"):
            release_preflight.run_preflight(self.project_dir)

    def test_high_value_secret_key_names_and_hex_values_fail(self) -> None:
        for name, value in (
            ("AWS_SECRET_ACCESS_KEY", "AbCdEf0123456789AbCdEf0123456789AbCdEf01"),
            ("SECRET_ACCESS_KEY", "0123456789abcdef" * 4),
            ("SECRET_KEY", "abcdef0123456789" * 4),
        ):
            with self.subTest(name=name):
                (self.project_dir / "app" / "module.py").write_text(
                    f'{name} = "{value}"\n',
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    release_preflight.PreflightError,
                    "probable credential",
                ):
                    release_preflight.run_preflight(self.project_dir)

    def test_humanized_secret_field_labels_do_not_fail(self) -> None:
        (self.project_dir / "frontend" / "dist" / "assets" / "index-test.js").write_text(
            """const labels = {
  AZURE_OPENAI_API_KEY: "Azure OpenAI API Key",
  "AZURE_AI_SEARCH_API_KEY": "Azure AI Search API Key",
  DATABASE_PASSWORD: "Database Password",
};
""",
            encoding="utf-8",
        )

        report = release_preflight.run_preflight(self.project_dir)

        self.assertTrue(report["ok"])

    def test_strict_python_version_mismatch_fails(self) -> None:
        with self.assertRaisesRegex(release_preflight.PreflightError, "required"):
            release_preflight.run_preflight(self.project_dir, require_python="0.1")

    def test_strict_clean_tree_rejects_dirty_worktree(self) -> None:
        dirty_status = release_preflight.subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=0,
            stdout=" M prototype/app/main.py\n",
            stderr="",
        )
        with mock.patch.object(
            release_preflight.subprocess, "run", return_value=dirty_status
        ):
            with self.assertRaisesRegex(release_preflight.PreflightError, "not clean"):
                release_preflight.run_preflight(
                    self.project_dir, require_clean_tree=True
                )

    def test_with_qdrant_requires_manifest(self) -> None:
        with self.assertRaisesRegex(release_preflight.PreflightError, "manifest is missing"):
            release_preflight.run_preflight(
                self.project_dir,
                with_qdrant=True,
                target_platform="darwin",
                target_arch="arm64",
            )

    def test_qdrant_manifest_must_be_tracked_in_git_head(self) -> None:
        anchor = self.project_dir / "README.md"
        anchor.write_text("test repository", encoding="utf-8")
        self._commit_path(anchor)
        binary = self._write_macho("arm64")
        manifest = self._write_manifest(
            platform="darwin",
            arch="arm64",
            binary=binary,
            commit=False,
        )

        with self.assertRaisesRegex(release_preflight.PreflightError, "tracked in Git HEAD"):
            release_preflight.validate_qdrant_artifact(
                self.project_dir,
                manifest,
                target_platform="darwin",
                target_arch="arm64",
                version_probe=lambda _path: "1.15.4",
            )

    def test_modified_qdrant_manifest_fails_before_executable_probe(self) -> None:
        binary = self._write_macho("arm64")
        manifest = self._write_manifest(
            platform="darwin",
            arch="arm64",
            binary=binary,
        )
        manifest.write_text(
            json.dumps({"schema_version": 1, "artifacts": []}),
            encoding="utf-8",
        )
        probe = mock.Mock(return_value="1.15.4")

        with self.assertRaisesRegex(release_preflight.PreflightError, "Git HEAD trust anchor"):
            release_preflight.validate_qdrant_artifact(
                self.project_dir,
                manifest,
                target_platform="darwin",
                target_arch="arm64",
                version_probe=probe,
            )

        probe.assert_not_called()

    def test_empty_committed_qdrant_policy_fails_closed_for_opt_in(self) -> None:
        self._write_empty_manifest()

        with self.assertRaisesRegex(release_preflight.PreflightError, "found 0"):
            release_preflight.run_preflight(
                self.project_dir,
                with_qdrant=True,
                target_platform="darwin",
                target_arch="arm64",
                qdrant_version_probe=lambda _path: "1.15.4",
            )

    def test_valid_darwin_qdrant_artifact_succeeds(self) -> None:
        binary = self._write_macho("arm64")
        manifest = self._write_manifest(
            platform="darwin", arch="arm64", binary=binary
        )

        report = release_preflight.run_preflight(
            self.project_dir,
            with_qdrant=True,
            qdrant_manifest=manifest,
            target_platform="darwin",
            target_arch="aarch64",
            qdrant_version_probe=lambda _path: "1.15.4",
        )

        self.assertTrue(report["qdrant_embedded"])
        self.assertEqual(report["qdrant_artifact"], str(binary.resolve()))
        self.assertEqual(report["qdrant_version"], "1.15.4")

    def test_qdrant_hash_mismatch_fails(self) -> None:
        binary = self._write_macho("arm64")
        manifest = self._write_manifest(
            platform="darwin", arch="arm64", binary=binary, digest="0" * 64
        )

        with self.assertRaisesRegex(release_preflight.PreflightError, "SHA-256 mismatch"):
            release_preflight.validate_qdrant_artifact(
                self.project_dir,
                manifest,
                target_platform="darwin",
                target_arch="arm64",
                version_probe=lambda _path: "1.15.4",
            )

    def test_qdrant_binary_architecture_mismatch_fails(self) -> None:
        binary = self._write_macho("x86_64")
        manifest = self._write_manifest(
            platform="darwin", arch="arm64", binary=binary
        )

        with self.assertRaisesRegex(release_preflight.PreflightError, "architecture mismatch"):
            release_preflight.validate_qdrant_artifact(
                self.project_dir,
                manifest,
                target_platform="darwin",
                target_arch="arm64",
                version_probe=lambda _path: "1.15.4",
            )

    def test_darwin_qdrant_must_be_executable(self) -> None:
        binary = self._write_macho("arm64")
        binary.chmod(0o644)
        manifest = self._write_manifest(
            platform="darwin", arch="arm64", binary=binary
        )

        with self.assertRaisesRegex(release_preflight.PreflightError, "not executable"):
            release_preflight.validate_qdrant_artifact(
                self.project_dir,
                manifest,
                target_platform="darwin",
                target_arch="arm64",
            )

    def test_valid_windows_pe_artifact_succeeds(self) -> None:
        binary = self._write_pe("x86_64")
        manifest = self._write_manifest(
            platform="windows", arch="amd64", binary=binary
        )

        artifact = release_preflight.validate_qdrant_artifact(
            self.project_dir,
            manifest,
            target_platform="win32",
            target_arch="x86_64",
            version_probe=lambda _path: "1.15.4",
        )

        self.assertEqual(artifact.bundle_destination, "bin/win")

    def test_truncated_qdrant_binary_fails_even_when_manifest_hash_matches(self) -> None:
        binary = self.project_dir / "bin" / "vendor" / "qdrant"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"\xcf\xfa\xed\xfe" + struct.pack("<I", 0x0100000C))
        binary.chmod(0o755)
        manifest = self._write_manifest(
            platform="darwin",
            arch="arm64",
            binary=binary,
        )

        with self.assertRaisesRegex(release_preflight.PreflightError, "implausibly small"):
            release_preflight.validate_qdrant_artifact(
                self.project_dir,
                manifest,
                target_platform="darwin",
                target_arch="arm64",
                version_probe=lambda _path: "1.15.4",
            )

    def test_qdrant_version_probe_must_match_manifest(self) -> None:
        binary = self._write_macho("arm64")
        manifest = self._write_manifest(
            platform="darwin",
            arch="arm64",
            binary=binary,
            version="1.15.4",
        )

        with self.assertRaisesRegex(release_preflight.PreflightError, "version mismatch"):
            release_preflight.validate_qdrant_artifact(
                self.project_dir,
                manifest,
                target_platform="darwin",
                target_arch="arm64",
                version_probe=lambda _path: "1.15.3",
            )

    def test_bundled_qdrant_is_verified_after_packaging(self) -> None:
        artifact = self._fake_bundled_artifact()
        bundled = self._copy_bundled_artifact(artifact)

        validated = release_preflight.validate_bundled_qdrant(
            self.project_dir / "dist",
            artifact,
            version_probe=lambda _path: "1.15.4",
        )

        self.assertEqual(validated, bundled.resolve())

    def test_missing_or_duplicate_bundled_qdrant_fails(self) -> None:
        artifact = self._fake_bundled_artifact()
        with self.assertRaisesRegex(release_preflight.PreflightError, "canonical runtime path"):
            release_preflight.validate_bundled_qdrant(
                self.project_dir / "dist",
                artifact,
                version_probe=lambda _path: "1.15.4",
            )

        self._copy_bundled_artifact(artifact)
        self._copy_bundled_artifact(
            artifact,
            "ReqPilotX.app/Contents/Frameworks/duplicate/qdrant",
        )
        with self.assertRaisesRegex(release_preflight.PreflightError, "exactly one"):
            release_preflight.validate_bundled_qdrant(
                self.project_dir / "dist",
                artifact,
                version_probe=lambda _path: "1.15.4",
            )

    def test_bundled_qdrant_wrong_destination_fails(self) -> None:
        artifact = self._fake_bundled_artifact()
        self._copy_bundled_artifact(artifact, "ReqPilotX/qdrant")

        with self.assertRaisesRegex(release_preflight.PreflightError, "canonical runtime path"):
            release_preflight.validate_bundled_qdrant(
                self.project_dir / "dist",
                artifact,
                version_probe=lambda _path: "1.15.4",
            )

    def test_rewritten_or_non_executable_bundled_qdrant_fails(self) -> None:
        artifact = self._fake_bundled_artifact()
        bundled = self._copy_bundled_artifact(artifact)
        bundled.write_bytes(bundled.read_bytes()[:-1] + b"x")
        with self.assertRaisesRegex(release_preflight.PreflightError, "SHA-256 mismatch"):
            release_preflight.validate_bundled_qdrant(
                self.project_dir / "dist",
                artifact,
                version_probe=lambda _path: "1.15.4",
            )

        shutil.copy2(artifact.path, bundled)
        bundled.chmod(0o644)
        with self.assertRaisesRegex(release_preflight.PreflightError, "not executable"):
            release_preflight.validate_bundled_qdrant(
                self.project_dir / "dist",
                artifact,
                version_probe=lambda _path: "1.15.4",
            )

    def test_bundled_qdrant_version_must_match(self) -> None:
        artifact = self._fake_bundled_artifact()
        self._copy_bundled_artifact(artifact)

        with self.assertRaisesRegex(release_preflight.PreflightError, "version mismatch"):
            release_preflight.validate_bundled_qdrant(
                self.project_dir / "dist",
                artifact,
                version_probe=lambda _path: "1.15.3",
            )

    def test_build_command_invokes_only_canonical_spec(self) -> None:
        command = build_desktop.build_pyinstaller_command(
            Path("/tmp/venv/pyinstaller"),
            Path("/tmp/project/ReqPilotX.spec"),
        )

        self.assertEqual(command[-1], "/tmp/project/ReqPilotX.spec")
        self.assertIn("--clean", command)
        self.assertNotIn("app_desktop.py", command)
        self.assertNotIn("--add-data", command)

    def test_full_build_requires_clean_source_even_without_optional_flag(self) -> None:
        self.assertTrue(
            build_desktop.require_clean_source_for_build(
                preflight_only=False,
                explicitly_required=False,
            )
        )
        self.assertFalse(
            build_desktop.require_clean_source_for_build(
                preflight_only=True,
                explicitly_required=False,
            )
        )
        self.assertTrue(
            build_desktop.require_clean_source_for_build(
                preflight_only=True,
                explicitly_required=True,
            )
        )

    def test_macos_build_removes_only_complete_transient_collect(self) -> None:
        dist = self.project_dir / "dist"
        bundle_entrypoint = dist / "ReqPilotX.app" / "Contents" / "MacOS" / "ReqPilotX"
        transient_entrypoint = dist / "ReqPilotX" / "ReqPilotX"
        transient_runtime = dist / "ReqPilotX" / "_internal"
        bundle_entrypoint.parent.mkdir(parents=True)
        bundle_entrypoint.write_bytes(b"bundle")
        transient_runtime.mkdir(parents=True)
        transient_entrypoint.write_bytes(b"collect")

        removed = build_desktop.finalize_pyinstaller_output(
            dist,
            platform_name="darwin",
        )

        self.assertEqual(removed, dist.resolve() / "ReqPilotX")
        self.assertFalse((dist / "ReqPilotX").exists())
        self.assertTrue((dist / "ReqPilotX.app").is_dir())

    def test_macos_transient_cleanup_fails_closed_on_incomplete_output(self) -> None:
        dist = self.project_dir / "dist"
        bundle_entrypoint = dist / "ReqPilotX.app" / "Contents" / "MacOS" / "ReqPilotX"
        transient = dist / "ReqPilotX"
        bundle_entrypoint.parent.mkdir(parents=True)
        bundle_entrypoint.write_bytes(b"bundle")
        transient.mkdir(parents=True)

        with self.assertRaisesRegex(
            release_preflight.PreflightError,
            "incomplete; refusing cleanup",
        ):
            build_desktop.finalize_pyinstaller_output(
                dist,
                platform_name="darwin",
            )

        self.assertTrue(transient.is_dir())

    def test_windows_output_is_never_treated_as_macos_transient(self) -> None:
        dist = self.project_dir / "dist"
        canonical = dist / "ReqPilotX"
        canonical.mkdir(parents=True)

        removed = build_desktop.finalize_pyinstaller_output(
            dist,
            platform_name="win32",
        )

        self.assertIsNone(removed)
        self.assertTrue(canonical.is_dir())

    def test_qdrant_build_arguments_are_forwarded_after_separator(self) -> None:
        manifest = Path("/tmp/project/release/qdrant-artifacts.json")
        command = build_desktop.build_pyinstaller_command(
            Path("/tmp/venv/pyinstaller"),
            Path("/tmp/project/ReqPilotX.spec"),
            with_qdrant=True,
            qdrant_manifest=manifest,
        )

        self.assertEqual(
            command[-4:],
            ["--", "--with-qdrant", "--qdrant-manifest", str(manifest)],
        )

    def test_missing_local_pyinstaller_has_no_global_fallback(self) -> None:
        with self.assertRaisesRegex(
            release_preflight.PreflightError, "global fallback is disabled"
        ):
            build_desktop.resolve_venv_pyinstaller(
                self.project_dir, platform_name="darwin"
            )

    def test_spec_has_no_unconditional_bin_data_bundle(self) -> None:
        spec = Path(build_desktop.__file__).with_name("ReqPilotX.spec").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("('bin', 'bin')", spec)
        self.assertIn("validate_qdrant_artifact", spec)
        self.assertIn('"release" / "qdrant-artifacts.json"', spec)
        self.assertNotIn("upx=True", spec)
        self.assertGreaterEqual(spec.count("upx=False"), 2)


if __name__ == "__main__":
    unittest.main()
