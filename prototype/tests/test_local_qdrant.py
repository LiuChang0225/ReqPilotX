from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.infrastructure import local_qdrant


class LocalQdrantContractTests(unittest.TestCase):
    def test_endpoint_accepts_only_plain_loopback_http_root_urls(self) -> None:
        endpoint = local_qdrant.parse_local_qdrant_endpoint("http://localhost:6333/")

        self.assertEqual(endpoint.url, "http://127.0.0.1:6333")
        self.assertEqual(endpoint.http_port, 6333)
        self.assertEqual(endpoint.grpc_port, 6334)
        for unsafe in (
            "https://127.0.0.1:6333",
            "http://0.0.0.0:6333",
            "http://192.168.1.10:6333",
            "http://user@127.0.0.1:6333",
            "http://127.0.0.1:6333/collections",
            "http://127.0.0.1:6333/?token=secret",
        ):
            with self.subTest(url=unsafe):
                with self.assertRaises(local_qdrant.LocalQdrantConfigurationError):
                    local_qdrant.parse_local_qdrant_endpoint(unsafe)

    def test_subprocess_environment_is_allowlisted_and_loopback_bound(self) -> None:
        environment = local_qdrant.build_local_qdrant_environment(
            local_qdrant.parse_local_qdrant_endpoint("http://localhost:6433"),
            storage_path=Path("/tmp/reqpilotx-qdrant"),
            source={
                "PATH": "/safe/bin",
                "LANG": "zh_CN.UTF-8",
                "HOME": "/safe/home",
                "REQPILOT_DASHSCOPE_API_KEY": "must-not-leak",
                "REQPILOT_DATABASE_URL": "postgresql://secret@localhost/db",
                "AWS_SECRET_ACCESS_KEY": "must-not-leak",
                "UNRELATED": "also-not-needed",
            },
            owner_token="owner-token",
        )

        self.assertEqual(environment["PATH"], "/safe/bin")
        self.assertEqual(environment["QDRANT__SERVICE__HOST"], "127.0.0.1")
        self.assertEqual(environment["QDRANT__SERVICE__HTTP_PORT"], "6433")
        self.assertEqual(environment["QDRANT__SERVICE__GRPC_PORT"], "6434")
        self.assertEqual(
            environment["QDRANT__STORAGE__STORAGE_PATH"],
            "/tmp/reqpilotx-qdrant",
        )
        self.assertEqual(environment[local_qdrant.OWNER_ENV_KEY], "owner-token")
        self.assertNotIn("REQPILOT_DASHSCOPE_API_KEY", environment)
        self.assertNotIn("REQPILOT_DATABASE_URL", environment)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)
        self.assertNotIn("UNRELATED", environment)

    def test_configured_url_uses_environment_then_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            storage_dir = Path(temporary)
            (storage_dir / "config.json").write_text(
                json.dumps(
                    {
                        "REQPILOT_ALIYUN_QDRANT_URL": "http://localhost:6333",
                        "REQPILOT_DASHSCOPE_API_KEY": "must-not-be-read-out",
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                local_qdrant.configured_qdrant_url(storage_dir, environment={}),
                "http://localhost:6333",
            )
            self.assertEqual(
                local_qdrant.configured_qdrant_url(
                    storage_dir,
                    environment={"REQPILOT_QDRANT_URL": "http://127.0.0.1:6433"},
                ),
                "http://127.0.0.1:6433",
            )

    def test_ensure_skips_unconfigured_and_remote_urls_without_spawning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(local_qdrant.subprocess, "Popen") as popen:
                unconfigured = local_qdrant.ensure_local_qdrant_running(
                    Path(temporary), ""
                )
                remote = local_qdrant.ensure_local_qdrant_running(
                    Path(temporary), "https://qdrant.example.com:6333"
                )

        self.assertEqual(unconfigured.state, "not_configured")
        self.assertEqual(remote.state, "remote_configured")
        self.assertFalse(unconfigured.ready)
        self.assertFalse(remote.ready)
        popen.assert_not_called()

    def test_ensure_reports_grpc_port_conflict_before_spawning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            storage_dir = Path(temporary)
            with (
                patch.object(local_qdrant, "probe_qdrant", return_value=False),
                patch.object(
                    local_qdrant,
                    "_loopback_port_in_use",
                    side_effect=lambda port: port == 6334,
                ),
                patch.object(local_qdrant.subprocess, "Popen") as popen,
            ):
                with self.assertRaisesRegex(local_qdrant.LocalQdrantError, "6334"):
                    local_qdrant.ensure_local_qdrant_running(
                        storage_dir, "http://localhost:6333"
                    )

        popen.assert_not_called()

    def test_windows_healthy_external_qdrant_is_reused_without_spawning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(local_qdrant.sys, "platform", "win32"),
                patch.object(local_qdrant, "probe_qdrant", return_value=True),
                patch.object(
                    local_qdrant,
                    "_windows_listener_is_loopback_only",
                    return_value=True,
                ) as listener,
                patch.object(local_qdrant, "resolve_local_qdrant_binary") as resolve,
                patch.object(local_qdrant.subprocess, "Popen") as popen,
            ):
                result = local_qdrant.ensure_local_qdrant_running(
                    Path(temporary), "http://localhost:6333"
                )

        self.assertEqual(result.state, "ready_external")
        self.assertTrue(result.ready)
        self.assertEqual(listener.call_count, 2)
        resolve.assert_not_called()
        popen.assert_not_called()

    def test_windows_stopped_qdrant_refuses_auto_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(local_qdrant.sys, "platform", "win32"),
                patch.object(local_qdrant, "probe_qdrant", return_value=False),
                patch.object(local_qdrant, "_loopback_port_in_use", return_value=False),
                patch.object(local_qdrant, "resolve_local_qdrant_binary") as resolve,
                patch.object(local_qdrant.subprocess, "Popen") as popen,
            ):
                with self.assertRaisesRegex(
                    local_qdrant.LocalQdrantError, "spawn is not supported on Windows"
                ):
                    local_qdrant.ensure_local_qdrant_running(
                        Path(temporary), "http://localhost:6333"
                    )

        resolve.assert_not_called()
        popen.assert_not_called()

    def test_ensure_reuses_healthy_external_qdrant_without_claiming_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            storage_dir = Path(temporary)
            with (
                patch.object(local_qdrant, "probe_qdrant", return_value=True),
                patch.object(
                    local_qdrant,
                    "listener_is_loopback_only",
                    return_value=True,
                ),
                patch.object(local_qdrant.subprocess, "Popen") as popen,
            ):
                result = local_qdrant.ensure_local_qdrant_running(
                    storage_dir, "http://localhost:6333"
                )

        self.assertEqual(result.state, "ready_external")
        self.assertTrue(result.ready)
        self.assertFalse(result.owned)
        popen.assert_not_called()

    def test_ensure_starts_once_with_owner_record_and_sanitized_environment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            storage_dir = Path(temporary)
            binary = storage_dir / "bin" / "qdrant"
            binary.parent.mkdir()
            binary.write_bytes(b"binary")
            binary.chmod(0o700)
            process = Mock(pid=4321)
            process.poll.return_value = None
            probe_results = iter((False, True))

            with (
                patch.object(
                    local_qdrant,
                    "resolve_local_qdrant_binary",
                    return_value=binary,
                ),
                patch.object(
                    local_qdrant,
                    "probe_qdrant",
                    side_effect=lambda *_args, **_kwargs: next(probe_results),
                ),
                patch.object(
                    local_qdrant,
                    "listener_is_loopback_only",
                    return_value=True,
                ),
                patch.object(
                    local_qdrant,
                    "_loopback_port_in_use",
                    return_value=False,
                ),
                patch.object(
                    local_qdrant,
                    "_process_start_fingerprint",
                    return_value="started-now",
                ),
                patch.object(
                    local_qdrant.subprocess,
                    "Popen",
                    return_value=process,
                ) as popen,
                patch.dict(
                    os.environ,
                    {
                        "REQPILOT_DASHSCOPE_API_KEY": "must-not-leak",
                        "REQPILOT_DATABASE_URL": "postgresql://secret@localhost/db",
                    },
                ),
            ):
                result = local_qdrant.ensure_local_qdrant_running(
                    storage_dir,
                    "http://localhost:6333",
                    timeout_seconds=0.1,
                    poll_interval_seconds=0.001,
                )

            self.assertEqual(result.state, "started")
            self.assertTrue(result.ready)
            self.assertTrue(result.owned)
            self.assertEqual(result.pid, 4321)
            popen.assert_called_once()
            environment = popen.call_args.kwargs["env"]
            self.assertNotIn("REQPILOT_DASHSCOPE_API_KEY", environment)
            self.assertNotIn("REQPILOT_DATABASE_URL", environment)
            self.assertEqual(environment["QDRANT__SERVICE__HOST"], "127.0.0.1")
            record = json.loads(
                local_qdrant.owner_record_path(storage_dir).read_text(encoding="utf-8")
            )
            self.assertEqual(record["pid"], 4321)
            self.assertEqual(record["url"], "http://127.0.0.1:6333")
            self.assertNotIn("api_key", json.dumps(record).lower())

    def test_post_spawn_identity_failure_always_terminates_child(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            storage_dir = Path(temporary)
            binary = storage_dir / "bin" / "qdrant"
            binary.parent.mkdir()
            binary.write_bytes(b"binary")
            binary.chmod(0o700)
            process = Mock(pid=4321)
            process.poll.return_value = None
            process.wait.return_value = None
            with (
                patch.object(local_qdrant, "probe_qdrant", return_value=False),
                patch.object(
                    local_qdrant,
                    "_loopback_port_in_use",
                    return_value=False,
                ),
                patch.object(
                    local_qdrant,
                    "resolve_local_qdrant_binary",
                    return_value=binary,
                ),
                patch.object(
                    local_qdrant,
                    "_spawn_qdrant",
                    return_value=process,
                ),
                patch.object(
                    local_qdrant,
                    "_process_start_fingerprint",
                    side_effect=RuntimeError("identity probe failed"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "identity probe failed"):
                    local_qdrant.ensure_local_qdrant_running(
                        storage_dir, "http://localhost:6333"
                    )

            process.terminate.assert_called_once_with()
            process.wait.assert_called_once_with(timeout=3)
            self.assertFalse(local_qdrant.owner_record_path(storage_dir).exists())

    def test_empty_post_spawn_identity_never_writes_owner_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            storage_dir = Path(temporary)
            binary = storage_dir / "bin" / "qdrant"
            binary.parent.mkdir()
            binary.write_bytes(b"binary")
            binary.chmod(0o700)
            process = Mock(pid=4321)
            process.poll.return_value = None
            process.wait.return_value = None
            with (
                patch.object(local_qdrant, "probe_qdrant", return_value=False),
                patch.object(
                    local_qdrant,
                    "_loopback_port_in_use",
                    return_value=False,
                ),
                patch.object(
                    local_qdrant,
                    "resolve_local_qdrant_binary",
                    return_value=binary,
                ),
                patch.object(
                    local_qdrant,
                    "_spawn_qdrant",
                    return_value=process,
                ),
                patch.object(
                    local_qdrant,
                    "_process_start_fingerprint",
                    return_value="",
                ),
            ):
                with self.assertRaisesRegex(
                    local_qdrant.LocalQdrantError, "identity could not be proven"
                ):
                    local_qdrant.ensure_local_qdrant_running(
                        storage_dir, "http://localhost:6333"
                    )

            process.terminate.assert_called_once_with()
            self.assertFalse(local_qdrant.owner_record_path(storage_dir).exists())

    def test_ensure_returns_ready_owned_without_duplicate_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            storage_dir = Path(temporary)
            path = local_qdrant.owner_record_path(storage_dir)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pid": 4321,
                        "owner_token": "ours",
                        "process_start": "started-now",
                        "binary": "/tmp/qdrant",
                        "url": "http://127.0.0.1:6333",
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(local_qdrant, "probe_qdrant", return_value=True),
                patch.object(
                    local_qdrant,
                    "listener_is_loopback_only",
                    return_value=True,
                ),
                patch.object(
                    local_qdrant,
                    "_owned_process_matches",
                    return_value=True,
                ),
                patch.object(local_qdrant.subprocess, "Popen") as popen,
            ):
                result = local_qdrant.ensure_local_qdrant_running(
                    storage_dir, "http://localhost:6333"
                )

        self.assertEqual(result.state, "ready_owned")
        self.assertTrue(result.ready)
        self.assertTrue(result.owned)
        self.assertEqual(result.pid, 4321)
        popen.assert_not_called()

    def test_stop_owned_refuses_stale_or_forged_owner_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            storage_dir = Path(temporary)
            path = local_qdrant.owner_record_path(storage_dir)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pid": 4321,
                        "owner_token": "forged",
                        "process_start": "old",
                        "binary": "/tmp/qdrant",
                        "url": "http://127.0.0.1:6333",
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(
                    local_qdrant, "_owned_process_matches", return_value=False
                ),
                patch.object(local_qdrant.os, "kill") as kill,
            ):
                result = local_qdrant.stop_owned_local_qdrant(storage_dir)

        self.assertEqual(result.state, "stale_owner_record")
        self.assertFalse(result.owned)
        kill.assert_not_called()
        self.assertFalse(path.exists())

    def test_stop_owned_terminates_only_matching_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            storage_dir = Path(temporary)
            path = local_qdrant.owner_record_path(storage_dir)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "pid": 4321,
                        "owner_token": "ours",
                        "process_start": "started-now",
                        "binary": "/tmp/qdrant",
                        "url": "http://127.0.0.1:6333",
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(
                    local_qdrant,
                    "_owned_process_matches",
                    side_effect=(True, True, False, False),
                ),
                patch.object(local_qdrant.os, "kill") as kill,
            ):
                result = local_qdrant.stop_owned_local_qdrant(
                    storage_dir,
                    timeout_seconds=0.1,
                    poll_interval_seconds=0.001,
                )

        self.assertEqual(result.state, "stopped")
        self.assertTrue(result.owned)
        kill.assert_called_once_with(4321, signal.SIGTERM)
        self.assertFalse(path.exists())

    def test_cli_and_shell_launchers_share_the_same_lifecycle(self) -> None:
        prototype_dir = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ.copy()
            for key in (
                "REQPILOT_ALIYUN_QDRANT_URL",
                "REQPILOT_QDRANT_URL",
                "QDRANT_URL",
            ):
                environment.pop(key, None)
            result = subprocess.run(
                [
                    sys.executable,
                    str(prototype_dir / "scripts" / "local_qdrant.py"),
                    "--storage-dir",
                    temporary,
                    "status",
                ],
                cwd=prototype_dir,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "not_configured")
        start_source = (prototype_dir / "start.sh").read_text(encoding="utf-8")
        stop_source = (prototype_dir / "stop.sh").read_text(encoding="utf-8")
        status_source = (prototype_dir / "status.sh").read_text(encoding="utf-8")
        self.assertIn('"$LOCAL_QDRANT_CLI" ensure', start_source)
        self.assertIn('"$LOCAL_QDRANT_CLI" stop-owned', stop_source)
        self.assertIn('"$LOCAL_QDRANT_CLI" status', status_source)
        self.assertIn('OTHER_RUNTIME_FOUND="false"', stop_source)
        self.assertLess(
            stop_source.index('OTHER_RUNTIME_FOUND="false"'),
            stop_source.index('"$LOCAL_QDRANT_CLI" stop-owned'),
        )
        self.assertLess(
            start_source.index('"$LOCAL_QDRANT_CLI" ensure'),
            start_source.index('echo "[INFO] 启动稳定后台 API'),
        )

    def test_stop_script_preserves_qdrant_while_another_runtime_is_live(self) -> None:
        prototype_dir = Path(__file__).resolve().parents[1]
        sleeper = subprocess.Popen(["sleep", "30"])
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "tmp").mkdir()
                (root / "scripts").mkdir()
                shutil.copy2(prototype_dir / "stop.sh", root / "stop.sh")
                (root / "scripts" / "local_worker_guard.py").write_text(
                    "raise SystemExit(0)\n", encoding="utf-8"
                )
                marker = root / "qdrant-stopped"
                (root / "scripts" / "local_qdrant.py").write_text(
                    (
                        "import os, pathlib, sys\n"
                        "if 'stop-owned' in sys.argv:\n"
                        "    pathlib.Path(os.environ['QDRANT_STOP_MARKER']).touch()\n"
                    ),
                    encoding="utf-8",
                )
                (root / "tmp" / "uvicorn_8000.pid").write_text(
                    str(sleeper.pid), encoding="utf-8"
                )
                environment = os.environ.copy()
                environment["QDRANT_STOP_MARKER"] = str(marker)

                result = subprocess.run(
                    ["bash", str(root / "stop.sh"), "8010"],
                    cwd=root,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("保留共享 Qdrant", result.stdout)
                self.assertFalse(marker.exists())
                self.assertIsNone(sleeper.poll())
        finally:
            if sleeper.poll() is None:
                sleeper.terminate()
            sleeper.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
