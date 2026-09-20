from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from app.infrastructure.storage_lock import acquire_storage_lease
from scripts import storage_backup


class StorageBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temporary.name)
        self.storage = self.root / "运行 数据" / "storage"
        self.storage.mkdir(parents=True)
        self.connection = sqlite3.connect(self.storage / "app.db")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        self.connection.execute("INSERT INTO records(value) VALUES ('committed-in-wal')")
        self.connection.commit()
        for directory, filename, content in (
            ("uploads", "sor.pdf", b"upload"),
            ("knowledge", "reference.docx", b"knowledge"),
            ("exports", "matrix.xlsx", b"export"),
            ("table_snapshots/persisted", "table.png", b"snapshot"),
            ("ai_traces", "trace.json", b"trace"),
            ("knowledge_index", "payload.json", b"index"),
            ("previews", "preview.json", b"preview"),
            ("qdrant_db", "segment", b"qdrant"),
        ):
            target = self.storage / directory / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (self.storage / "config.json").write_text(
            json.dumps({"provider": "runtime-owned"}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_documented_direct_cli_bootstraps_the_prototype_import_path(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "storage_backup.py"
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Backup, verify and restore ReqPilotX storage", result.stdout)

    def test_backup_consolidates_wal_and_covers_complete_storage(self) -> None:
        backup = self.root / "backups" / "before-upgrade"

        report = storage_backup.create_backup(self.storage, backup)

        self.assertTrue(report["ok"])
        self.assertEqual(report["sqlite_strategy"], "sqlite_online_backup")
        with closing(sqlite3.connect(backup / "data" / "app.db")) as conn:
            row = conn.execute("SELECT value FROM records").fetchone()
        self.assertEqual(row, ("committed-in-wal",))
        self.assertFalse((backup / "data" / "app.db-wal").exists())
        self.assertFalse((backup / "data" / "app.db-shm").exists())
        for relative in (
            "uploads/sor.pdf",
            "knowledge/reference.docx",
            "exports/matrix.xlsx",
            "table_snapshots/persisted/table.png",
            "ai_traces/trace.json",
            "knowledge_index/payload.json",
            "previews/preview.json",
            "qdrant_db/segment",
            "config.json",
        ):
            self.assertTrue((backup / "data" / relative).is_file(), relative)

    def test_existing_output_and_active_storage_fail_without_overwrite(self) -> None:
        output = self.root / "existing-backup"
        output.mkdir()
        marker = output / "keep.txt"
        marker.write_text("keep", encoding="utf-8")

        with self.assertRaisesRegex(storage_backup.BackupError, "never overwritten"):
            storage_backup.create_backup(self.storage, output)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

        with acquire_storage_lease(self.storage, blocking=False):
            with self.assertRaisesRegex(storage_backup.BackupError, "Storage is active"):
                storage_backup.create_backup(self.storage, self.root / "locked-backup")

    def test_native_no_replace_rename_never_clobbers_destination(self) -> None:
        source = self.root / "rename-source"
        destination = self.root / "rename-destination"
        source.mkdir()
        (source / "source.txt").write_text("source", encoding="utf-8")
        destination.mkdir()
        marker = destination / "marker.txt"
        marker.write_text("destination", encoding="utf-8")

        with self.assertRaisesRegex(storage_backup.BackupError, "already exists"):
            storage_backup._rename_no_replace(source, destination)

        self.assertTrue(source.is_dir())
        self.assertEqual(marker.read_text(encoding="utf-8"), "destination")
        marker.unlink()
        destination.rmdir()
        storage_backup._rename_no_replace(source, destination)
        self.assertFalse(source.exists())
        self.assertEqual(
            (destination / "source.txt").read_text(encoding="utf-8"),
            "source",
        )

    def test_no_replace_api_unavailable_fails_without_plain_rename_fallback(self) -> None:
        source = self.root / "source"
        destination = self.root / "destination"
        source.mkdir()

        with mock.patch.object(storage_backup.sys, "platform", "unsupported"):
            with self.assertRaisesRegex(storage_backup.BackupError, "unavailable"):
                storage_backup._rename_no_replace(source, destination)

        self.assertTrue(source.is_dir())
        self.assertFalse(destination.exists())

    def test_linux_and_windows_no_replace_branches_use_native_exclusive_flags(self) -> None:
        class FakeFunction:
            def __init__(self, result: int = 0) -> None:
                self.calls: list[tuple[object, ...]] = []
                self.argtypes = None
                self.restype = None
                self.result = result

            def __call__(self, *args):
                self.calls.append(args)
                return self.result

        source = self.root / "native-source"
        destination = self.root / "native-destination"
        source.mkdir()

        linux_function = FakeFunction()
        linux_libc = mock.Mock(renameat2=linux_function)
        with (
            mock.patch.object(storage_backup.sys, "platform", "linux"),
            mock.patch.object(storage_backup.ctypes, "CDLL", return_value=linux_libc),
        ):
            storage_backup._rename_no_replace(source, destination)
        self.assertEqual(linux_function.calls[0][-1], 1)

        windows_function = FakeFunction(result=1)
        windows_kernel = mock.Mock(MoveFileW=windows_function)
        with (
            mock.patch.object(storage_backup.sys, "platform", "win32"),
            mock.patch.object(
                storage_backup.ctypes,
                "WinDLL",
                return_value=windows_kernel,
                create=True,
            ),
        ):
            storage_backup._rename_no_replace(source, destination)
        self.assertEqual(len(windows_function.calls), 1)

    def test_backup_restore_and_rollback_preserve_racing_destinations(self) -> None:
        real_rename = storage_backup._rename_no_replace
        racing_backup = self.root / "backup-publication-race"

        def race_backup(source: Path, destination: Path) -> None:
            destination.mkdir()
            (destination / "racer.txt").write_text("racer", encoding="utf-8")
            real_rename(source, destination)

        with mock.patch.object(
            storage_backup,
            "_rename_no_replace",
            side_effect=race_backup,
        ):
            with self.assertRaisesRegex(storage_backup.BackupError, "already exists"):
                storage_backup.create_backup(self.storage, racing_backup)
        self.assertEqual(
            (racing_backup / "racer.txt").read_text(encoding="utf-8"),
            "racer",
        )

        backup = self.root / "backup-race"
        storage_backup.create_backup(self.storage, backup)
        restore_target = self.root / "restore-race"

        def race_restore(source: Path, destination: Path) -> None:
            destination.mkdir()
            (destination / "racer.txt").write_text("racer", encoding="utf-8")
            real_rename(source, destination)

        with mock.patch.object(
            storage_backup,
            "_rename_no_replace",
            side_effect=race_restore,
        ):
            with self.assertRaisesRegex(storage_backup.BackupError, "already exists"):
                storage_backup.restore_backup(backup, restore_target)
        self.assertEqual(
            (restore_target / "racer.txt").read_text(encoding="utf-8"),
            "racer",
        )

        self.connection.close()
        preserve = self.storage.parent / "preserve-race"

        def race_preserve(source: Path, destination: Path) -> None:
            if destination.resolve(strict=False) == preserve.resolve(strict=False):
                destination.mkdir()
                (destination / "racer.txt").write_text("racer", encoding="utf-8")
            real_rename(source, destination)

        with mock.patch.object(
            storage_backup,
            "_rename_no_replace",
            side_effect=race_preserve,
        ):
            with self.assertRaisesRegex(storage_backup.BackupError, "already exists"):
                storage_backup.rollback_backup(backup, self.storage, preserve)
        self.assertTrue(self.storage.is_dir())
        self.assertEqual(
            (preserve / "racer.txt").read_text(encoding="utf-8"),
            "racer",
        )
        self.connection = sqlite3.connect(self.storage / "app.db")

    def test_tampered_backup_is_rejected(self) -> None:
        backup = self.root / "backup"
        storage_backup.create_backup(self.storage, backup)
        (backup / "data" / "uploads" / "sor.pdf").write_bytes(b"tampered")

        with self.assertRaisesRegex(storage_backup.BackupError, "hash mismatch"):
            storage_backup.verify_backup(backup)

    def test_backup_data_root_symlink_fails_closed(self) -> None:
        backup = self.root / "backup"
        storage_backup.create_backup(self.storage, backup)
        data_root = backup / "data"
        real_data = backup / "data-real"
        data_root.rename(real_data)
        try:
            data_root.symlink_to(real_data, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")

        with self.assertRaisesRegex(storage_backup.BackupError, "data directory is missing"):
            storage_backup.verify_backup(backup)

    def test_windows_or_drive_relative_manifest_paths_fail_closed(self) -> None:
        for malicious in (r"C:\escape\file", "C:drive-relative", r"\\server\share\file"):
            with self.subTest(path=malicious):
                backup = self.root / f"backup-{abs(hash(malicious))}"
                storage_backup.create_backup(self.storage, backup)
                manifest = backup / "backup-manifest.json"
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                payload["entries"][0]["path"] = malicious
                manifest.write_text(json.dumps(payload), encoding="utf-8")

                with self.assertRaisesRegex(storage_backup.BackupError, "unsafe relative path"):
                    storage_backup.verify_backup(backup)

    def test_restore_uses_first_verified_manifest_snapshot_through_copy(self) -> None:
        backup = self.root / "backup"
        storage_backup.create_backup(self.storage, backup)
        target = self.root / "restored"
        real_copytree = storage_backup.shutil.copytree
        observed: dict[str, object] = {}

        def racing_copytree(source: Path, destination: Path, *args, **kwargs):
            observed.update(kwargs)
            (Path(source) / "uploads" / "sor.pdf").write_bytes(b"changed-after-verify")
            with mock.patch.object(storage_backup.shutil, "copytree", real_copytree):
                return real_copytree(source, destination, *args, **kwargs)

        with mock.patch.object(
            storage_backup.shutil,
            "copytree",
            side_effect=racing_copytree,
        ):
            with self.assertRaisesRegex(storage_backup.BackupError, "does not match"):
                storage_backup.restore_backup(backup, target)

        self.assertIs(observed.get("symlinks"), True)
        self.assertFalse(target.exists())

    def test_restore_only_creates_a_new_storage_root(self) -> None:
        backup = self.root / "backup"
        storage_backup.create_backup(self.storage, backup)
        restored = self.root / "restored storage"

        report = storage_backup.restore_backup(backup, restored)

        self.assertTrue(report["target_created"])
        self.assertEqual((restored / "uploads" / "sor.pdf").read_bytes(), b"upload")
        with closing(sqlite3.connect(restored / "app.db")) as conn:
            self.assertEqual(
                conn.execute("SELECT value FROM records").fetchone(),
                ("committed-in-wal",),
            )
        marker = restored / "do-not-overwrite"
        marker.write_text("current", encoding="utf-8")
        with self.assertRaisesRegex(storage_backup.BackupError, "never overwritten"):
            storage_backup.restore_backup(backup, restored)
        self.assertEqual(marker.read_text(encoding="utf-8"), "current")

    def test_rollback_preserves_failed_upgrade_storage_as_a_sibling(self) -> None:
        backup = self.root / "backup"
        storage_backup.create_backup(self.storage, backup)
        self.connection.close()
        (self.storage / "uploads" / "sor.pdf").write_bytes(b"failed-upgrade")
        preserve = self.storage.parent / "storage.failed-upgrade"

        report = storage_backup.rollback_backup(
            backup,
            self.storage,
            preserve,
        )

        self.assertTrue(report["previous_storage_preserved"])
        self.assertEqual((self.storage / "uploads" / "sor.pdf").read_bytes(), b"upload")
        self.assertEqual(
            (preserve / "uploads" / "sor.pdf").read_bytes(),
            b"failed-upgrade",
        )
        self.connection = sqlite3.connect(self.storage / "app.db")

    @unittest.skipIf(not hasattr(Path, "symlink_to"), "symlinks unavailable")
    def test_storage_symlink_fails_closed(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.storage / "uploads" / "escape"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")

        with self.assertRaisesRegex(storage_backup.BackupError, "symbolic link"):
            storage_backup.create_backup(self.storage, self.root / "unsafe-backup")


if __name__ == "__main__":
    unittest.main()
