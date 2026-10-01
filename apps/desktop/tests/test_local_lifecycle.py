from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from build_desktop import build_pyinstaller_command, validate_postgres_distribution
import frozen_python
from scripts.release_preflight import PreflightError
from local_workers import LocalWorkerError, LocalWorkerPool
import local_worker_entry
from reqpilotx_server.infrastructure.local_postgres import (
    LocalPostgresError,
    ManagedPostgres,
    resolve_postgres_bin,
)


class ManagedPostgresContractTests(unittest.TestCase):
    def test_unknown_nonempty_root_is_not_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed = root / "managed-postgres"
            managed.mkdir()
            (managed / "unrelated.txt").write_text("keep", encoding="utf-8")
            service = ManagedPostgres(root, Path("/unused"))
            with self.assertRaisesRegex(LocalPostgresError, "not adopted"):
                service._prepare_root()
            self.assertEqual((managed / "unrelated.txt").read_text(), "keep")

    def test_foreign_owner_is_not_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed = root / "managed-postgres"
            managed.mkdir()
            owner = managed / "owner.json"
            owner.write_text(json.dumps({"application": "other", "version": 1}))
            with self.assertRaisesRegex(LocalPostgresError, "does not match"):
                ManagedPostgres(root, Path("/unused"))._prepare_root()
            self.assertIn("other", owner.read_text())

    def test_live_unreceipted_cluster_is_refused_without_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            service = ManagedPostgres(Path(temporary), Path("/unused"))
            with (
                patch.object(service, "_prepare_root", return_value="a" * 64),
                patch("reqpilotx_server.infrastructure.local_postgres.subprocess.run") as run,
                patch.object(service, "_command") as command,
            ):
                run.return_value.returncode = 0
                with self.assertRaisesRegex(LocalPostgresError, "no trusted desktop receipt"):
                    service.start()
                command.assert_not_called()

    def test_mac_real_cluster_restart_when_enabled(self) -> None:
        if os.environ.get("REQPILOT_TEST_LOCAL_POSTGRES") != "1":
            self.skipTest("Opt-in host PostgreSQL test")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ManagedPostgres(root, resolve_postgres_bin())
            service.start()
            self.assertTrue(service.alive())
            import psycopg

            with psycopg.connect(service.database_url) as connection:
                connection.execute("CREATE TABLE desktop_restart_probe (value INTEGER)")
                connection.execute("INSERT INTO desktop_restart_probe VALUES (17)")
            service.stop()
            self.assertFalse(service.alive())
            service.start()
            self.assertTrue(service.alive())
            with psycopg.connect(service.database_url) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM desktop_restart_probe").fetchone()[0],
                    17,
                )
            service.stop()
            self.assertFalse((service.root / "runtime.json").exists())

    def test_port_conflict_keeps_unrelated_listener_alive(self) -> None:
        if os.environ.get("REQPILOT_TEST_LOCAL_POSTGRES") != "1":
            self.skipTest("Opt-in host PostgreSQL test")
        with tempfile.TemporaryDirectory() as temporary:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                port = listener.getsockname()[1]
                service = ManagedPostgres(Path(temporary), resolve_postgres_bin())
                with patch(
                    "reqpilotx_server.infrastructure.local_postgres._free_loopback_port",
                    return_value=port,
                ):
                    with self.assertRaisesRegex(LocalPostgresError, "pg_ctl failed"):
                        service.start()
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    pass
                self.assertFalse(service.alive())

    def test_abrupt_launcher_exit_recovers_only_its_owned_cluster(self) -> None:
        if os.environ.get("REQPILOT_TEST_DESKTOP_RUNTIME") != "1":
            self.skipTest("Opt-in full local desktop process test")
        desktop = Path(__file__).resolve().parents[1] / "app_desktop.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage = root / "storage"
            env = {
                "HOME": os.environ.get("HOME", str(root)),
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": os.environ["PYTHONPATH"],
                "REQPILOT_STORAGE_DIR": str(storage),
                "REQPILOT_DESKTOP_POSTGRES_BIN": str(resolve_postgres_bin()),
                "REQPILOT_FRONTEND_DIR": str(Path(__file__).resolve().parents[3] / "apps/web/dist"),
            }
            command = [sys.executable, str(desktop), "--runtime-smoke-seconds", "45"]
            first = subprocess.Popen(
                command, env=env, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            manager = ManagedPostgres(storage, resolve_postgres_bin())
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if (manager.root / "runtime.json").exists() and (
                        storage / "desktop-worker-heartbeat.json"
                    ).exists():
                        break
                    if first.poll() is not None:
                        self.fail("First desktop stopped before workers started")
                    time.sleep(0.2)
                else:
                    self.fail("First desktop did not start its owned cluster and workers")
                duplicate = subprocess.run(
                    [sys.executable, str(desktop), "--runtime-smoke-seconds", "1"],
                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=10, check=False,
                )
                self.assertEqual(duplicate.returncode, 2)
                first.send_signal(signal.SIGKILL)
                first.wait(timeout=5)
                recovered = subprocess.run(
                    [sys.executable, str(desktop), "--runtime-smoke-seconds", "1"],
                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=45, check=False,
                )
                self.assertEqual(recovered.returncode, 0)
                self.assertFalse((manager.root / "runtime.json").exists())
            finally:
                if first.poll() is None:
                    first.kill()
                    first.wait(timeout=5)
                if (manager.data / "postmaster.pid").exists():
                    subprocess.run(
                        [str(resolve_postgres_bin() / "pg_ctl"), "-D", str(manager.data),
                         "-m", "fast", "-w", "stop"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=20, check=False,
                    )


class WorkerPoolContractTests(unittest.TestCase):
    def test_slots_use_exact_server_lanes_and_private_owner_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pool = LocalWorkerPool(
                storage_root=Path(temporary), api_port=41891,
                count=2, instance_id="runtime-one",
            )
            with patch("local_workers.subprocess.Popen", return_value=Mock()) as popen:
                pool._spawn(1)
                pool._spawn(2)
            first, second = (call.args[0] for call in popen.call_args_list)
            self.assertEqual(first[1:], ["-m", "local_worker_entry"])
            self.assertEqual(second[-4:], [
                "--accepted-kind", "document.extract",
                "--claim-profile", "local-extraction-no-llm",
            ])
            for call in popen.call_args_list:
                env = call.kwargs["env"]
                self.assertEqual(env["REQPILOT_LOCAL_RUNTIME_INSTANCE_ID"], "runtime-one")
                self.assertEqual(env["REQPILOT_LOCAL_WORKER_COUNT"], "2")
                self.assertEqual(env["REQPILOT_DESKTOP_HEARTBEAT_PATH"], str(pool.heartbeat_path))

    def test_heartbeat_uses_current_instance_and_removes_only_own_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pool = LocalWorkerPool(
                storage_root=Path(temporary), api_port=41891,
                count=1, instance_id="runtime-one",
            )
            pool._write_heartbeat()
            self.assertEqual(json.loads(pool.heartbeat_path.read_text())["instance_id"], "runtime-one")
            pool.heartbeat_path.write_text(json.dumps({"instance_id": "other"}))
            pool.stop()
            self.assertTrue(pool.heartbeat_path.exists())

    def test_stop_requests_worker_drain_before_releasing_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pool = LocalWorkerPool(
                storage_root=Path(temporary), api_port=41891,
                count=1, instance_id="runtime-one",
            )
            pool._write_heartbeat()
            process = Mock()
            process.poll.return_value = None
            pool.processes[1] = process
            with patch.object(pool, "_write_heartbeat", wraps=pool._write_heartbeat) as write:
                pool.stop()
            write.assert_called_once_with(stopping=True)
            process.wait.assert_called_once_with(timeout=15)
            self.assertFalse(pool.heartbeat_path.exists())

    def test_worker_child_translates_stop_marker_to_signal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "heartbeat.json"
            marker.write_text(json.dumps({
                "instance_id": "runtime-one", "updated_at": time.time(),
                "stopping": True,
            }))
            with (
                patch.dict(os.environ, {
                    "REQPILOT_DESKTOP_PARENT_INSTANCE_ID": "runtime-one",
                    "REQPILOT_DESKTOP_HEARTBEAT_PATH": str(marker),
                }),
                patch.object(local_worker_entry.signal, "raise_signal") as notify,
            ):
                local_worker_entry._watch_parent()
            notify.assert_called_once_with(signal.SIGTERM)

    def test_exited_worker_restarts_with_bounded_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pool = LocalWorkerPool(
                storage_root=Path(temporary), api_port=41891,
                count=1, instance_id="runtime-one",
            )
            exited = Mock()
            exited.poll.return_value = 1
            pool.processes[1] = exited
            with patch.object(pool, "_spawn", side_effect=lambda slot: pool.processes.update({slot: exited})) as spawn:
                self.assertEqual(pool.restart_exited(), (1,))
                self.assertEqual(pool.restart_exited(), (1,))
                self.assertEqual(pool.restart_exited(), (1,))
                with self.assertRaises(LocalWorkerError):
                    pool.restart_exited()
            self.assertEqual(spawn.call_count, 3)


class DesktopPackageInputTests(unittest.TestCase):
    def test_frozen_dispatch_allows_only_owned_worker_entry(self) -> None:
        with (
            patch.object(sys, "argv", ["ReqPilotX", "-m", "local_worker_entry"]),
            patch.object(frozen_python.runpy, "run_module") as run,
        ):
            self.assertEqual(frozen_python.run_frozen_python_child(), 0)
            run.assert_called_once_with("local_worker_entry", run_name="__main__")
        with (
            patch.object(sys, "argv", ["ReqPilotX", "-m", "arbitrary_module"]),
            self.assertRaises(SystemExit),
        ):
            frozen_python.run_frozen_python_child()

    def test_postgres_distribution_is_explicit_and_rejects_cluster_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("bin", "lib", "share"):
                (root / name).mkdir()
            suffix = ".exe" if sys.platform == "win32" else ""
            for name in ("initdb", "pg_ctl", "postgres", "psql", "createdb"):
                (root / "bin" / f"{name}{suffix}").write_bytes(b"synthetic executable")
            self.assertEqual(validate_postgres_distribution(root), root.resolve())
            command = build_pyinstaller_command(
                Path("/tmp/pyinstaller"), Path("/tmp/ReqPilotX.spec"),
                postgres_root=root,
            )
            self.assertEqual(command[-3:], ["--", "--postgres-root", str(root)])
            (root / "share" / "PG_VERSION").write_text("unexpected cluster")
            with self.assertRaisesRegex(PreflightError, "unsafe runtime data"):
                validate_postgres_distribution(root)


if __name__ == "__main__":
    unittest.main()
