from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import local_worker_guard
from scripts.local_worker_guard import (
    drain_workers,
    observe_worker,
    pool_status,
    stop_workers,
    workers_for_port,
)
from scripts.local_runtime_identity import calculate_source_revision


class LocalWorkerGuardTests(unittest.TestCase):
    def test_screen_wrapper_command_is_not_counted_as_a_python_worker(self) -> None:
        with patch.object(
            local_worker_guard,
            "_is_python_process",
            return_value=False,
        ):
            observed = observe_worker(
                123,
                root=Path("/tmp/reqpilotx"),
                command=(
                    "SCREEN -dmS reqpilotx_worker_8000 bash -lc "
                    "exec .venv/bin/python -m reqpilotx_server.worker"
                ),
            )

        self.assertIsNone(observed)

    def _prepare_worker_module(self, root: Path) -> None:
        package = root / "reqpilotx_server" / "worker"
        package.mkdir(parents=True)
        (root / "reqpilotx_server" / "__init__.py").write_text("", encoding="utf-8")
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "__main__.py").write_text(
            "import time\nwhile True:\n    time.sleep(1)\n",
            encoding="utf-8",
        )

    def _spawn_worker(
        self,
        root: Path,
        *,
        port: int | None,
        slot: int | None = None,
        instance_id: str = "",
        source_revision: str = "",
        worker_count: int | None = None,
        lane_args: tuple[str, ...] | None = None,
    ) -> subprocess.Popen[bytes]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(root)
        if port is None:
            environment.pop("REQPILOT_LOCAL_RUNTIME_PORT", None)
        else:
            environment["REQPILOT_LOCAL_RUNTIME_PORT"] = str(port)
        if slot is None:
            environment.pop("REQPILOT_LOCAL_WORKER_SLOT", None)
        else:
            environment["REQPILOT_LOCAL_WORKER_SLOT"] = str(slot)
        if instance_id:
            environment["REQPILOT_LOCAL_RUNTIME_INSTANCE_ID"] = instance_id
        else:
            environment.pop("REQPILOT_LOCAL_RUNTIME_INSTANCE_ID", None)
        if source_revision:
            environment["REQPILOT_RUNTIME_SOURCE_REVISION"] = source_revision
        else:
            environment.pop("REQPILOT_RUNTIME_SOURCE_REVISION", None)
        if worker_count is not None:
            environment["REQPILOT_LOCAL_WORKER_COUNT"] = str(worker_count)
        elif slot is not None and instance_id and source_revision:
            environment["REQPILOT_LOCAL_WORKER_COUNT"] = "2"
        else:
            environment.pop("REQPILOT_LOCAL_WORKER_COUNT", None)
        if lane_args is None:
            lane_args = (
                (
                    "--accepted-kind",
                    "document.extract",
                    "--claim-profile",
                    "local-extraction-no-llm",
                )
                if slot == 2
                else ()
            )
        process = subprocess.Popen(
            [sys.executable, "-m", "reqpilotx_server.worker", *lane_args],
            cwd=root,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return process

    def _wait_until_observed(self, root: Path, expected: set[int]) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            target, protected = workers_for_port(root, 8000)
            observed = {worker.pid for worker in target + protected}
            if expected <= observed:
                return
            time.sleep(0.05)
        self.fail(f"workers were not observed: {expected}")

    def test_cleanup_stops_target_and_legacy_orphan_only(self) -> None:
        processes: list[subprocess.Popen[bytes]] = []
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "reqpilotx"
            foreign_root = base / "other-repository"
            self._prepare_worker_module(root)
            self._prepare_worker_module(foreign_root)
            (root / "tmp").mkdir()

            target = self._spawn_worker(root, port=8000)
            orphan = self._spawn_worker(root, port=None)
            other_port = self._spawn_worker(root, port=8010)
            tracked_other_port = self._spawn_worker(root, port=None)
            foreign = self._spawn_worker(foreign_root, port=8000)
            (root / "tmp" / "worker_8011.pid").write_text(
                str(tracked_other_port.pid),
                encoding="utf-8",
            )
            processes.extend((target, orphan, other_port, tracked_other_port, foreign))
            try:
                self._wait_until_observed(
                    root,
                    {
                        target.pid,
                        orphan.pid,
                        other_port.pid,
                        tracked_other_port.pid,
                    },
                )
                stopped = set(stop_workers(root, 8000, timeout_seconds=2))

                self.assertEqual(stopped, {target.pid, orphan.pid})
                target.wait(timeout=2)
                orphan.wait(timeout=2)
                self.assertIsNone(other_port.poll())
                self.assertIsNone(tracked_other_port.poll())
                self.assertIsNone(foreign.poll())
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)

    def test_cleanup_can_keep_the_valid_tracked_worker(self) -> None:
        processes: list[subprocess.Popen[bytes]] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "reqpilotx"
            self._prepare_worker_module(root)
            (root / "tmp").mkdir()
            tracked = self._spawn_worker(root, port=8000)
            duplicate = self._spawn_worker(root, port=8000)
            processes.extend((tracked, duplicate))
            try:
                self._wait_until_observed(root, {tracked.pid, duplicate.pid})
                stopped = stop_workers(
                    root,
                    8000,
                    keep_pid=tracked.pid,
                    timeout_seconds=2,
                )

                self.assertEqual(stopped, [duplicate.pid])
                self.assertIsNone(tracked.poll())
                duplicate.wait(timeout=2)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)

    def test_bounded_pool_requires_unique_slots_and_matching_runtime_identity(
        self,
    ) -> None:
        processes: list[subprocess.Popen[bytes]] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "reqpilotx"
            self._prepare_worker_module(root)
            (root / "tmp").mkdir()
            revision = "a" * 64
            instance_id = "runtime-123"
            first = self._spawn_worker(
                root,
                port=8000,
                slot=1,
                instance_id=instance_id,
                source_revision=revision,
            )
            second = self._spawn_worker(
                root,
                port=8000,
                slot=2,
                instance_id=instance_id,
                source_revision=revision,
            )
            processes.extend((first, second))
            try:
                self._wait_until_observed(root, {first.pid, second.pid})
                (root / "tmp" / "worker_8000_1.pid").write_text(
                    str(first.pid), encoding="utf-8"
                )
                (root / "tmp" / "worker_8000_2.pid").write_text(
                    str(second.pid), encoding="utf-8"
                )

                status = pool_status(
                    root,
                    8000,
                    count=2,
                    instance_id=instance_id,
                    source_revision=revision,
                )

                self.assertTrue(status.compatible)
                self.assertEqual(status.slot_pids, {1: first.pid, 2: second.pid})
                self.assertEqual(status.missing_slots, ())
                self.assertEqual(status.extra_pids, ())
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                for process in processes:
                    process.wait(timeout=2)

    def test_pool_rejects_legacy_or_wrong_revision_before_filling_a_slot(self) -> None:
        processes: list[subprocess.Popen[bytes]] = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "reqpilotx"
            self._prepare_worker_module(root)
            (root / "tmp").mkdir()
            legacy = self._spawn_worker(root, port=8000)
            wrong = self._spawn_worker(
                root,
                port=8000,
                slot=2,
                instance_id="runtime-123",
                source_revision="b" * 64,
            )
            processes.extend((legacy, wrong))
            try:
                self._wait_until_observed(root, {legacy.pid, wrong.pid})

                status = pool_status(
                    root,
                    8000,
                    count=2,
                    instance_id="runtime-123",
                    source_revision="a" * 64,
                )

                self.assertFalse(status.compatible)
                self.assertEqual(set(status.extra_pids), {legacy.pid, wrong.pid})
                self.assertEqual(status.missing_slots, (1, 2))
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                for process in processes:
                    process.wait(timeout=2)

    def test_pool_rejects_slot_two_when_its_claim_lane_is_general(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "reqpilotx"
            self._prepare_worker_module(root)
            (root / "tmp").mkdir()
            worker = self._spawn_worker(
                root,
                port=8000,
                slot=2,
                instance_id="runtime-123",
                source_revision="a" * 64,
                lane_args=(),
            )
            try:
                self._wait_until_observed(root, {worker.pid})
                (root / "tmp" / "worker_8000_2.pid").write_text(
                    str(worker.pid),
                    encoding="utf-8",
                )

                status = pool_status(
                    root,
                    8000,
                    count=2,
                    instance_id="runtime-123",
                    source_revision="a" * 64,
                )

                self.assertFalse(status.compatible)
                self.assertEqual(status.missing_slots, (1, 2))
                self.assertEqual(status.extra_pids, (worker.pid,))
            finally:
                if worker.poll() is None:
                    worker.terminate()
                worker.wait(timeout=2)

    def test_drain_only_sends_sigterm_and_never_escalates_to_sigkill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "reqpilotx"
            self._prepare_worker_module(root)
            (root / "tmp").mkdir()
            worker = self._spawn_worker(root, port=8000, slot=1)
            try:
                self._wait_until_observed(root, {worker.pid})
                with patch.object(local_worker_guard.os, "kill") as send_signal:
                    drained = drain_workers(root, 8000)

                self.assertEqual(drained, [worker.pid])
                send_signal.assert_called_once_with(worker.pid, local_worker_guard.signal.SIGTERM)
                self.assertNotIn(
                    local_worker_guard.signal.SIGKILL,
                    [call.args[1] for call in send_signal.call_args_list],
                )
            finally:
                if worker.poll() is None:
                    worker.terminate()
                worker.wait(timeout=2)

    def test_source_revision_changes_when_runtime_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app").mkdir()
            source = root / "app" / "main.py"
            source.write_text("VALUE = 1\n", encoding="utf-8")
            first = calculate_source_revision(root)

            source.write_text("VALUE = 2\n", encoding="utf-8")
            second = calculate_source_revision(root)

        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(first, second)

    def test_source_revision_ignores_generated_python_bytecode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "app" / "__pycache__"
            cache.mkdir(parents=True)
            (root / "app" / "main.py").write_text(
                "VALUE = 1\n",
                encoding="utf-8",
            )
            first = calculate_source_revision(root)

            (cache / "main.cpython-313.pyc").write_bytes(b"generated-v1")
            second = calculate_source_revision(root)
            (cache / "main.cpython-313.pyc").write_bytes(b"generated-v2")
            third = calculate_source_revision(root)

        self.assertEqual(first, second)
        self.assertEqual(second, third)


if __name__ == "__main__":
    unittest.main()
