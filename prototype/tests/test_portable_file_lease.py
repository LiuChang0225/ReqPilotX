from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import os
import subprocess
import sys

from reqpilotx_server.infrastructure.storage_lock import (
    StorageLockError,
    acquire_file_lease,
)


class PortableFileLeaseTests(unittest.TestCase):
    def test_native_loop_modules_import_without_fcntl(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.modules['fcntl'] = None; "
                "from reqpilotx_server.application.agent_workflow import "
                "source_item_loop_jobs, source_document_loop_jobs, "
                "source_document_stages, activity",
            ],
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    [
                        str(repository / "apps/server/src"),
                        str(repository / "packages/engine/src"),
                    ]
                ),
            },
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_exclusive_lease_reopens_after_release(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "native" / "task.lock"
            with acquire_file_lease(path):
                self.assertEqual(path.read_bytes(), b"\0")
                with self.assertRaises(StorageLockError):
                    acquire_file_lease(path, blocking=False)
            with acquire_file_lease(path, blocking=False):
                self.assertEqual(path.read_bytes(), b"\0")

    def test_symlink_lock_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            target = Path(raw) / "target"
            target.write_bytes(b"")
            link = Path(raw) / "task.lock"
            try:
                link.symlink_to(target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(StorageLockError):
                acquire_file_lease(link)


if __name__ == "__main__":
    unittest.main()
