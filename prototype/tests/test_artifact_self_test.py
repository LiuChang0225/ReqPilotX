from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import os
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import artifact_self_test
from scripts.release_preflight import PreflightError, _REQUIRED_FRONTEND_FILES
from app.infrastructure.runtime_settings import (
    AgentRuntimeBackend,
    DatabaseBackend,
    DeploymentMode,
    ObjectStoreBackend,
    RuntimeSettings,
)


class ArtifactSelfTestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temporary.name)
        self.project_dir = self.root / "prototype"
        self.project_dir.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _make_dist(self, platform_name: str = "darwin") -> Path:
        dist = self.project_dir / "dist"
        if platform_name == "darwin":
            artifact = dist / "ReqPilotX.app"
            entrypoint = artifact / "Contents" / "MacOS" / "ReqPilotX"
            runtime = artifact / "Contents" / "Frameworks"
        else:
            artifact = dist / "ReqPilotX"
            entrypoint = artifact / "ReqPilotX.exe"
            runtime = artifact / "_internal"
        entrypoint.parent.mkdir(parents=True)
        entrypoint.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        entrypoint.chmod(0o755)
        for relative in _REQUIRED_FRONTEND_FILES:
            path = runtime / "frontend" / "dist" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            body = "ReqPilotX" if path.suffix == ".html" else "{}"
            path.write_text(body, encoding="utf-8")
        entry_asset = runtime / "frontend" / "dist" / "assets" / "index-test.js"
        entry_asset.parent.mkdir(parents=True, exist_ok=True)
        entry_asset.write_text("console.log('safe');", encoding="utf-8")
        return dist

    def _write_manifest(self, dist: Path, platform_name: str = "darwin") -> Path:
        with mock.patch.object(
            artifact_self_test,
            "_git_source",
            return_value={"commit": "a" * 40, "clean": True},
        ):
            return artifact_self_test.write_artifact_manifest(
                dist,
                project_dir=self.project_dir,
                platform_name=platform_name,
                arch="arm64" if platform_name == "darwin" else "x86_64",
            )

    def test_macos_and_windows_canonical_layouts_are_manifested(self) -> None:
        for platform_name in ("darwin", "win32"):
            with self.subTest(platform_name=platform_name):
                dist = self._make_dist(platform_name)
                manifest = self._write_manifest(dist, platform_name)

                report = artifact_self_test.run_artifact_self_test(
                    dist,
                    manifest_path=manifest,
                    launch=False,
                )

                self.assertTrue(report["structure_verified"])
                self.assertFalse(report["runtime_verified"])
                self.temporary.cleanup()
                self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
                self.root = Path(self.temporary.name)
                self.project_dir = self.root / "prototype"
                self.project_dir.mkdir()

    def test_missing_frontend_file_and_stale_sibling_fail_closed(self) -> None:
        dist = self._make_dist("darwin")
        missing = dist / "ReqPilotX.app" / "Contents" / "Frameworks" / "frontend" / "dist" / "index.html"
        missing.unlink()
        with self.assertRaisesRegex(PreflightError, "frontend file is missing"):
            self._write_manifest(dist)

        self.temporary.cleanup()
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temporary.name)
        self.project_dir = self.root / "prototype"
        self.project_dir.mkdir()
        dist = self._make_dist("darwin")
        (dist / "ReqPilotX").mkdir()
        with self.assertRaisesRegex(PreflightError, "ambiguous stale"):
            self._write_manifest(dist)

    def test_canonical_artifact_root_symlink_fails_closed(self) -> None:
        dist = self._make_dist("darwin")
        artifact = dist / "ReqPilotX.app"
        outside = self.root / "outside-app"
        artifact.rename(outside)
        try:
            artifact.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")

        with self.assertRaisesRegex(PreflightError, "root must not be a symlink"):
            self._write_manifest(dist)

    def test_artifact_source_requires_known_clean_git_commit(self) -> None:
        repo = self.root / "source-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "artifact-test@example.invalid"],
            cwd=repo,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Artifact Test"],
            cwd=repo,
            check=True,
        )
        tracked = repo / "tracked.txt"
        tracked.write_text("clean", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "clean source"], cwd=repo, check=True)

        identity = artifact_self_test._git_source(repo)

        self.assertRegex(identity["commit"], r"^[0-9a-f]{40}$")
        self.assertTrue(identity["clean"])
        tracked.write_text("dirty", encoding="utf-8")
        with self.assertRaisesRegex(PreflightError, "must be clean"):
            artifact_self_test._git_source(repo)

        short_commit = subprocess.CompletedProcess(
            args=["git", "rev-parse"],
            returncode=0,
            stdout="abc123\n",
            stderr="",
        )
        clean_status = subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=0,
            stdout="",
            stderr="",
        )
        with mock.patch.object(
            artifact_self_test.subprocess,
            "run",
            side_effect=[short_commit, clean_status],
        ):
            with self.assertRaisesRegex(PreflightError, "40-character"):
                artifact_self_test._git_source(repo)

    def test_manifest_detects_changed_and_extra_files(self) -> None:
        dist = self._make_dist("darwin")
        manifest = self._write_manifest(dist)
        app_js = dist / "ReqPilotX.app" / "Contents" / "Frameworks" / "frontend" / "dist" / "assets" / "index-test.js"
        app_js.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(PreflightError, "differ from"):
            artifact_self_test.validate_artifact_manifest(dist, manifest)

        manifest = self._write_manifest(dist)
        (dist / "ReqPilotX.app" / "Contents" / "Frameworks" / "extra.txt").write_text(
            "extra",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PreflightError, "differ from"):
            artifact_self_test.validate_artifact_manifest(dist, manifest)

    def test_default_artifact_rejects_qdrant_and_sensitive_runtime_data(self) -> None:
        dist = self._make_dist("darwin")
        runtime = dist / "ReqPilotX.app" / "Contents" / "Frameworks"
        qdrant = runtime / "bin" / "mac" / "qdrant"
        qdrant.parent.mkdir(parents=True)
        qdrant.write_bytes(b"qdrant")
        with self.assertRaisesRegex(PreflightError, "unexpected Qdrant"):
            self._write_manifest(dist)

        qdrant.unlink()
        config = runtime / "config.json"
        config.write_text("{}", encoding="utf-8")
        manifest = self._write_manifest(dist)
        with self.assertRaisesRegex(PreflightError, "forbidden runtime data"):
            artifact_self_test.validate_artifact_manifest(dist, manifest)

    def test_frontend_probable_secret_is_rejected_from_final_artifact(self) -> None:
        dist = self._make_dist("darwin")
        app_js = dist / "ReqPilotX.app" / "Contents" / "Frameworks" / "frontend" / "dist" / "assets" / "index-test.js"
        app_js.write_text(
            'const OPENAI_API_KEY = "sk-live-AbCdEf0123456789-XyZ";',
            encoding="utf-8",
        )
        manifest = self._write_manifest(dist)

        with self.assertRaisesRegex(PreflightError, "probable credential"):
            artifact_self_test.validate_artifact_manifest(dist, manifest)

    def test_binary_token_scan_requires_a_real_token_boundary(self) -> None:
        candidate = self.root / "dependency.dylib"
        candidate.write_bytes(b"prefix-ask-" + b"A" * 128 + b" suffix")

        artifact_self_test._scan_binary_file(candidate, {})

        candidate.write_bytes(b"\0sk-live-" + b"A" * 64 + b"\0")
        with self.assertRaisesRegex(PreflightError, "probable secret material"):
            artifact_self_test._scan_binary_file(candidate, {})

    def test_internal_symlink_alias_is_recorded_but_escape_is_rejected(self) -> None:
        dist = self._make_dist("darwin")
        artifact = dist / "ReqPilotX.app"
        alias = artifact / "Contents" / "Frameworks" / "frontend" / "dist" / "assets" / "index-test.js"
        target = artifact / "Contents" / "Resources" / "frontend" / "dist" / "assets" / "index-test.js"
        target.parent.mkdir(parents=True)
        target.write_text(alias.read_text(encoding="utf-8"), encoding="utf-8")
        alias.unlink()
        try:
            alias.symlink_to(os.path.relpath(target, alias.parent))
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        manifest = self._write_manifest(dist)
        report = artifact_self_test.run_artifact_self_test(
            dist,
            manifest_path=manifest,
            launch=False,
        )
        self.assertTrue(report["structure_verified"])

        escape = artifact / "Contents" / "Frameworks" / "escape"
        escape.symlink_to(self.root / "outside")
        with self.assertRaisesRegex(PreflightError, "symlink escapes"):
            self._write_manifest(dist)

    def test_headless_artifact_runtime_probes_readiness_and_http_boundary(self) -> None:
        dist = self._make_dist("darwin")
        manifest = self._write_manifest(dist)

        runtime_payload = {
            "status": "ok",
            "ready": True,
            "runtime_ready": True,
            "storage_root": "redacted",
            "db_path": "redacted",
            "checks": {
                name: {"status": "ok"}
                for name in ("startup", "storage", "sqlite")
            },
        }
        process = mock.Mock()
        process.pid = 4321
        process.stdout = StringIO()
        process.poll.return_value = None
        nonce = "b" * 64
        handshake = {
            "pid": process.pid,
            "port": 43123,
            "nonce": nonce,
            "host": "127.0.0.1",
        }
        with (
            mock.patch.object(artifact_self_test, "_normalize_platform", return_value="darwin"),
            mock.patch.object(artifact_self_test.secrets, "token_hex", return_value=nonce),
            mock.patch.object(artifact_self_test.subprocess, "Popen", return_value=process) as popen,
            mock.patch.object(
                artifact_self_test,
                "_wait_for_handshake",
                return_value=handshake,
            ),
            mock.patch.object(
                artifact_self_test,
                "_wait_for_identity",
                return_value=handshake,
            ),
            mock.patch.object(
                artifact_self_test,
                "_read_json",
                side_effect=[
                    (404, {"detail": "Not found."}),
                    (200, runtime_payload),
                    (200, runtime_payload),
                    (400, {"detail": "Invalid local request host."}),
                    (403, {"detail": "Cross-origin request blocked."}),
                    (200, {"shutting_down": True, "pid": process.pid}),
                ],
            ),
            mock.patch.object(
                artifact_self_test,
                "_read_bytes",
                side_effect=[(200, b"ReqPilotX"), (200, b"console.log(1)")],
            ),
            mock.patch.object(artifact_self_test, "_port_accepts_connection", return_value=False),
            mock.patch.object(artifact_self_test, "_assert_storage_lease_held") as lease_held,
            mock.patch.object(artifact_self_test, "_wait_for_process_exit", return_value=0),
            mock.patch.object(artifact_self_test, "_wait_for_port_closed") as port_closed,
            mock.patch.object(artifact_self_test, "_assert_storage_lease_released") as lease_released,
            mock.patch.object(artifact_self_test, "_force_stop_process") as force_stop,
        ):
            report = artifact_self_test.run_artifact_self_test(
                dist,
                manifest_path=manifest,
                timeout_seconds=5.0,
            )

        self.assertTrue(report["runtime_verified"])
        self.assertTrue(report["runtime_readiness"])
        self.assertTrue(report["full_readiness"])
        self.assertEqual(report["listener_host"], "127.0.0.1")
        command = popen.call_args.args[0]
        self.assertIn("--artifact-smoke-server", command)
        self.assertIn("--artifact-smoke-handshake", command)
        self.assertIn("--artifact-smoke-nonce", command)
        self.assertNotIn(str(handshake["port"]), command)
        self.assertTrue(Path(popen.call_args.kwargs["env"]["REQPILOT_STORAGE_DIR"]).is_absolute())
        lease_held.assert_called_once()
        port_closed.assert_called_once_with("127.0.0.1", 43123, timeout_seconds=5.0)
        lease_released.assert_called_once()
        force_stop.assert_not_called()

    def test_artifact_smoke_environment_resolves_to_disposable_test_runtime(self) -> None:
        storage_root = self.root / "artifact-smoke-storage"
        inherited_runtime = {
            "REQPILOT_ENV": "cloud",
            "REQPILOT_DATABASE_URL": "postgresql://remote.example/production",
            "REQPILOT_OBJECT_STORE": "oss",
            "REQPILOT_OBJECT_ROOT": "/outside/objects",
            "REQPILOT_SCRATCH_ROOT": "/outside/scratch",
            "REQPILOT_ALLOWED_HOSTS": "remote.example",
            "REQPILOT_ALLOWED_ORIGINS": "https://remote.example",
            "REQPILOT_OSS_REGION": "outside-region",
            "REQPILOT_OSS_BUCKET": "outside-bucket",
            "REQPILOT_AGENT_RUNTIME": "codex-app-server",
            "REQPILOT_CODEX_BIN": "/outside/codex",
            "REQPILOT_CODEX_HOME": "/outside/codex-home",
        }

        with mock.patch.dict(os.environ, inherited_runtime, clear=False):
            environment = artifact_self_test._smoke_environment(storage_root)

        settings = RuntimeSettings.from_environment(
            environment=environment,
            prototype_dir=self.project_dir,
        )
        self.assertEqual(settings.deployment_mode, DeploymentMode.TEST)
        self.assertEqual(settings.database_backend, DatabaseBackend.SQLITE_TEST)
        self.assertEqual(settings.database_url, "")
        self.assertEqual(settings.object_store_backend, ObjectStoreBackend.LOCAL)
        self.assertEqual(settings.object_root, storage_root.resolve() / "objects")
        self.assertEqual(settings.scratch_root, (storage_root / "scratch").resolve())
        self.assertEqual(settings.agent_runtime_backend, AgentRuntimeBackend.NONE)
        self.assertIn("127.0.0.1", settings.allowed_hosts)
        self.assertIn("testserver", settings.allowed_hosts)
        self.assertNotIn("remote.example", settings.allowed_hosts)
        self.assertIn("http://127.0.0.1", settings.allowed_origins)
        self.assertNotIn("https://remote.example", settings.allowed_origins)
        self.assertNotIn("REQPILOT_DATABASE_URL", environment)
        self.assertNotIn("REQPILOT_OSS_REGION", environment)
        self.assertNotIn("REQPILOT_OSS_BUCKET", environment)
        self.assertNotIn("REQPILOT_CODEX_BIN", environment)
        self.assertNotIn("REQPILOT_CODEX_HOME", environment)

    def test_artifact_handshake_rejects_spoofed_identity_and_fields(self) -> None:
        nonce = "c" * 64
        valid = {
            "pid": 1234,
            "port": 45678,
            "nonce": nonce,
            "host": "127.0.0.1",
        }
        self.assertEqual(
            artifact_self_test._validate_handshake_payload(
                valid,
                process_pid=1234,
                nonce=nonce,
            ),
            valid,
        )
        invalid_payloads = (
            {**valid, "extra": True},
            {**valid, "pid": 9999},
            {**valid, "pid": True},
            {**valid, "port": True},
            {**valid, "port": 0},
            {**valid, "host": "0.0.0.0"},
            {**valid, "nonce": "d" * 64},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(PreflightError):
                    artifact_self_test._validate_handshake_payload(
                        payload,
                        process_pid=1234,
                        nonce=nonce,
                    )

    def test_artifact_handshake_file_rejects_symlink_and_partial_json(self) -> None:
        process = mock.Mock(pid=1234)
        process.poll.return_value = None
        nonce = "e" * 64
        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        handshake = self.root / "handshake.json"
        try:
            handshake.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        with self.assertRaisesRegex(PreflightError, "regular file"):
            artifact_self_test._wait_for_handshake(
                handshake,
                process,
                nonce=nonce,
                timeout_seconds=0.1,
            )

        handshake.unlink()
        handshake.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(PreflightError, "invalid JSON"):
            artifact_self_test._wait_for_handshake(
                handshake,
                process,
                nonce=nonce,
                timeout_seconds=0.1,
            )


if __name__ == "__main__":
    unittest.main()
