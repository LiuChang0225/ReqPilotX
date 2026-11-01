"""Page derivative locking must import only the current platform's API."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import portable_lock


class PortableLockTests(TestCase):
    def test_windows_branch_locks_and_unlocks_one_initialized_byte(self):
        calls = []
        fake = SimpleNamespace(LK_LOCK=1, LK_UNLCK=2,
            locking=lambda descriptor, mode, width: calls.append((descriptor, mode, width)))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "reading" / ".page-preparation.lock"
            with patch.object(portable_lock.os, "name", "nt"), patch.dict(sys.modules, {"msvcrt": fake}):
                with portable_lock.exclusive_file(path):
                    self.assertEqual(path.read_bytes(), b"0")
            self.assertEqual([call[1:] for call in calls], [(1, 1), (2, 1)])

    def test_posix_branch_releases_after_exception(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "reading" / ".page-preparation.lock"
            with self.assertRaisesRegex(ValueError, "synthetic failure"):
                with portable_lock.exclusive_file(path):
                    raise ValueError("synthetic failure")
            with portable_lock.exclusive_file(path):
                self.assertTrue(path.is_file())
