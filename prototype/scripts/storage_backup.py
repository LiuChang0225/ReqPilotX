from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

PROTOTYPE_DIR = Path(__file__).resolve().parents[1]
if str(PROTOTYPE_DIR) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_DIR))

from app.infrastructure.storage_lock import (  # noqa: E402
    StorageLockError,
    acquire_storage_lease,
)


BACKUP_CONTRACT = "reqpilotx-storage-backup-v1"
BACKUP_MANIFEST_NAME = "backup-manifest.json"
BACKUP_DATA_DIR = "data"
_SQLITE_NAME = "app.db"
_SQLITE_EXCLUDED_NAMES = {_SQLITE_NAME, f"{_SQLITE_NAME}-wal", f"{_SQLITE_NAME}-shm"}


class BackupError(RuntimeError):
    """A backup or restore safety contract failed."""


def _rename_no_replace(source: Path, destination: Path) -> None:
    source = Path(source)
    destination = Path(destination)
    if source.parent.resolve() != destination.parent.resolve():
        raise BackupError("Atomic no-replace rename requires sibling paths.")
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, "renamex_np", None)
        if function is None:
            raise BackupError("macOS renamex_np is unavailable; refusing unsafe rename fallback.")
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(os.fsencode(source), os.fsencode(destination), 0x00000004)
        if result != 0:
            _raise_rename_error(ctypes.get_errno())
        return
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, "renameat2", None)
        if function is None:
            raise BackupError("Linux renameat2 is unavailable; refusing unsafe rename fallback.")
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(destination),
            1,
        )
        if result != 0:
            _raise_rename_error(ctypes.get_errno())
        return
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = getattr(kernel32, "MoveFileW", None)
        if function is None:
            raise BackupError("Windows MoveFileW is unavailable; refusing unsafe rename fallback.")
        function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        function.restype = ctypes.c_int
        if not function(str(source), str(destination)):
            error_code = ctypes.get_last_error()
            if error_code in {80, 183}:
                raise BackupError("Atomic rename destination already exists.")
            raise BackupError(f"Windows atomic no-replace rename failed with error {error_code}.")
        return
    raise BackupError("Atomic no-replace rename is unavailable on this platform.")


def _raise_rename_error(error_code: int) -> None:
    if error_code in {errno.EEXIST, errno.ENOTEMPTY}:
        raise BackupError("Atomic rename destination already exists.")
    raise BackupError(f"Atomic no-replace rename failed with errno {error_code}.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def _require_safe_root(path: Path, *, must_exist: bool) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise BackupError("Storage and backup paths must be absolute.")
    if candidate.is_symlink():
        raise BackupError("Storage and backup roots must not be symbolic links.")
    resolved = candidate.resolve(strict=False)
    if must_exist and not resolved.is_dir():
        raise BackupError(f"Required directory is missing: {resolved}")
    return resolved


def _sqlite_quick_check(path: Path) -> None:
    try:
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10.0)
        ) as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise BackupError("SQLite integrity check could not be completed.") from exc
    if not result or str(result[0]).lower() != "ok":
        raise BackupError("SQLite integrity check failed.")


def _snapshot_sqlite(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_file() or source_path.is_symlink():
        raise BackupError("Storage does not contain a safe app.db database.")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(
            sqlite3.connect(
                source_path.as_uri() + "?mode=ro",
                uri=True,
                timeout=30.0,
            )
        ) as source, closing(
            sqlite3.connect(destination_path, timeout=30.0)
        ) as destination:
            source.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
            destination.commit()
    except sqlite3.DatabaseError as exc:
        raise BackupError("SQLite online backup failed.") from exc
    _sqlite_quick_check(destination_path)


def _source_inventory(storage_root: Path) -> tuple[list[str], dict[str, tuple[int, str]]]:
    directories: list[str] = []
    files: dict[str, tuple[int, str]] = {}
    for current, dirnames, filenames in os.walk(storage_root, followlinks=False):
        current_path = Path(current)
        for name in list(dirnames):
            path = current_path / name
            if path.is_symlink():
                raise BackupError(f"Storage contains a symbolic link: {path.relative_to(storage_root)}")
            directories.append(path.relative_to(storage_root).as_posix())
        for name in filenames:
            if current_path == storage_root and name in _SQLITE_EXCLUDED_NAMES:
                continue
            path = current_path / name
            if path.is_symlink():
                raise BackupError(f"Storage contains a symbolic link: {path.relative_to(storage_root)}")
            mode = path.stat(follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                raise BackupError(f"Storage contains an unsafe file: {path.relative_to(storage_root)}")
            relative = path.relative_to(storage_root).as_posix()
            files[relative] = (path.stat().st_size, _sha256(path))
    return sorted(set(directories)), files


def _copy_storage_files(
    storage_root: Path,
    data_root: Path,
    directories: list[str],
    files: dict[str, tuple[int, str]],
) -> None:
    for relative in directories:
        target = data_root / relative
        target.mkdir(parents=True, exist_ok=True)
        _restrict_permissions(target, directory=True)
    for relative, expected in sorted(files.items()):
        source = storage_root / relative
        target = data_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
        _restrict_permissions(target, directory=False)
        actual = (target.stat().st_size, _sha256(target))
        if actual != expected:
            raise BackupError(f"Storage file changed while it was copied: {relative}")


def _restrict_permissions(path: Path, *, directory: bool) -> None:
    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError:
        if os.name != "nt":
            raise


def _backup_entries(data_root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(data_root.rglob("*")):
        if path.is_symlink():
            raise BackupError("Backup data must not contain symbolic links.")
        relative = path.relative_to(data_root).as_posix()
        if path.is_dir():
            entries.append({"path": relative, "type": "directory"})
        elif path.is_file():
            entries.append(
                {
                    "path": relative,
                    "type": "file",
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
        else:
            raise BackupError(f"Backup contains an unsafe entry: {relative}")
    return entries


def create_backup(storage_root: Path, output_dir: Path) -> dict[str, Any]:
    storage_root = _require_safe_root(storage_root, must_exist=True)
    output_dir = _require_safe_root(output_dir, must_exist=False)
    if output_dir.exists():
        raise BackupError("Backup output already exists; existing backups are never overwritten.")
    if _is_within(output_dir, storage_root) or _is_within(storage_root, output_dir):
        raise BackupError("Backup output and storage root must not contain one another.")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.{uuid.uuid4().hex}.tmp"
    if staging.exists():
        raise BackupError("Backup staging path already exists.")

    try:
        with acquire_storage_lease(storage_root, blocking=False):
            staging.mkdir(mode=0o700)
            data_root = staging / BACKUP_DATA_DIR
            data_root.mkdir(mode=0o700)
            before_directories, before_files = _source_inventory(storage_root)
            wal_present = (storage_root / f"{_SQLITE_NAME}-wal").exists()
            shm_present = (storage_root / f"{_SQLITE_NAME}-shm").exists()
            _snapshot_sqlite(storage_root / _SQLITE_NAME, data_root / _SQLITE_NAME)
            _restrict_permissions(data_root / _SQLITE_NAME, directory=False)
            _copy_storage_files(
                storage_root,
                data_root,
                before_directories,
                before_files,
            )
            after_directories, after_files = _source_inventory(storage_root)
            if before_directories != after_directories or before_files != after_files:
                raise BackupError("Storage changed during backup; no backup was published.")
            payload = {
                "schema_version": 1,
                "contract": BACKUP_CONTRACT,
                "backup_id": uuid.uuid4().hex,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_storage": "redacted",
                "sqlite": {
                    "path": _SQLITE_NAME,
                    "strategy": "sqlite_online_backup",
                    "source_wal_present": wal_present,
                    "source_shm_present": shm_present,
                },
                "entries": _backup_entries(data_root),
            }
            _write_json(staging / BACKUP_MANIFEST_NAME, payload)
            _rename_no_replace(staging, output_dir)
    except StorageLockError as exc:
        raise BackupError(str(exc)) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return verify_backup(output_dir)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _restrict_permissions(temporary, directory=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_relative_path(value: object) -> str:
    raw = str(value or "")
    posix_candidate = PurePosixPath(raw)
    windows_candidate = PureWindowsPath(raw)
    if (
        not raw
        or "\x00" in raw
        or "\\" in raw
        or posix_candidate.is_absolute()
        or windows_candidate.is_absolute()
        or bool(windows_candidate.drive)
        or bool(windows_candidate.root)
        or ".." in posix_candidate.parts
        or ".." in windows_candidate.parts
        or any(":" in part for part in windows_candidate.parts)
    ):
        raise BackupError("Backup manifest contains an unsafe relative path.")
    return posix_candidate.as_posix()


def _manifest_entry_path(data_root: Path, relative: str) -> Path:
    path = data_root.joinpath(*PurePosixPath(relative).parts)
    if not _is_within(path, data_root):
        raise BackupError("Backup manifest path escapes the data root.")
    return path


def _verify_backup_snapshot(
    backup_dir: Path,
) -> tuple[dict[str, Any], Path, list[dict[str, Any]]]:
    backup_dir = _require_safe_root(backup_dir, must_exist=True)
    manifest_path = backup_dir / BACKUP_MANIFEST_NAME
    data_root = backup_dir / BACKUP_DATA_DIR
    if (
        not manifest_path.is_file()
        or manifest_path.is_symlink()
        or data_root.is_symlink()
        or not data_root.is_dir()
    ):
        raise BackupError("Backup manifest or data directory is missing.")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("Backup manifest is unreadable or invalid.") from exc
    if payload.get("schema_version") != 1 or payload.get("contract") != BACKUP_CONTRACT:
        raise BackupError("Backup contract is unsupported.")
    sqlite_payload = payload.get("sqlite") or {}
    if (
        sqlite_payload.get("path") != _SQLITE_NAME
        or sqlite_payload.get("strategy") != "sqlite_online_backup"
    ):
        raise BackupError("Backup SQLite strategy is unsupported.")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise BackupError("Backup manifest entries must be a list.")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise BackupError("Backup manifest contains an invalid entry.")
        relative = _safe_relative_path(raw.get("path"))
        if relative in seen:
            raise BackupError("Backup manifest contains duplicate paths.")
        seen.add(relative)
        kind = raw.get("type")
        path = _manifest_entry_path(data_root, relative)
        if kind == "directory":
            if not path.is_dir() or path.is_symlink():
                raise BackupError(f"Backup directory is missing or unsafe: {relative}")
            normalized.append({"path": relative, "type": "directory"})
        elif kind == "file":
            if not path.is_file() or path.is_symlink():
                raise BackupError(f"Backup file is missing or unsafe: {relative}")
            actual = {
                "path": relative,
                "type": "file",
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            if actual != raw:
                raise BackupError(f"Backup file hash mismatch: {relative}")
            normalized.append(actual)
        else:
            raise BackupError("Backup manifest entry type is unsupported.")
    expected_entries = sorted(normalized, key=lambda item: item["path"])
    if expected_entries != _backup_entries(data_root):
        raise BackupError("Backup data contains missing or unexpected entries.")
    _sqlite_quick_check(data_root / _SQLITE_NAME)
    report = {
        "ok": True,
        "contract": BACKUP_CONTRACT,
        "backup_id": str(payload.get("backup_id") or ""),
        "created_at": str(payload.get("created_at") or ""),
        "entry_count": len(normalized),
        "sqlite_strategy": str((payload.get("sqlite") or {}).get("strategy") or ""),
    }
    return report, data_root, expected_entries


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    report, _data_root, _expected_entries = _verify_backup_snapshot(backup_dir)
    return report


def _materialize_backup(backup_dir: Path, staging: Path) -> None:
    _report, source, expected_entries = _verify_backup_snapshot(backup_dir)
    if source.is_symlink():
        raise BackupError("Backup data root must not be a symbolic link.")
    shutil.copytree(source, staging, symlinks=True)
    if source.is_symlink():
        raise BackupError("Backup data root changed during restore.")
    if _backup_entries(staging) != expected_entries:
        raise BackupError("Restored staging data does not match the verified backup.")
    _sqlite_quick_check(staging / _SQLITE_NAME)


def restore_backup(backup_dir: Path, target_storage: Path) -> dict[str, Any]:
    backup_dir = _require_safe_root(backup_dir, must_exist=True)
    target_storage = _require_safe_root(target_storage, must_exist=False)
    if target_storage.exists():
        raise BackupError("Restore target already exists; existing data is never overwritten.")
    if _is_within(target_storage, backup_dir) or _is_within(backup_dir, target_storage):
        raise BackupError("Restore target and backup directory must not contain one another.")
    target_storage.parent.mkdir(parents=True, exist_ok=True)
    staging = target_storage.parent / f".{target_storage.name}.restore.{uuid.uuid4().hex}.tmp"
    try:
        with acquire_storage_lease(target_storage, blocking=False):
            _materialize_backup(backup_dir, staging)
            _rename_no_replace(staging, target_storage)
    except StorageLockError as exc:
        raise BackupError(str(exc)) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {"ok": True, "operation": "restore", "target_created": True}


def rollback_backup(
    backup_dir: Path,
    current_storage: Path,
    preserve_current: Path,
) -> dict[str, Any]:
    backup_dir = _require_safe_root(backup_dir, must_exist=True)
    current_storage = _require_safe_root(current_storage, must_exist=True)
    preserve_current = _require_safe_root(preserve_current, must_exist=False)
    if preserve_current.exists():
        raise BackupError("Rollback preservation path already exists.")
    if _is_within(backup_dir, current_storage) or _is_within(current_storage, backup_dir):
        raise BackupError("Rollback backup and current storage must not contain one another.")
    if preserve_current.parent != current_storage.parent:
        raise BackupError("Rollback preservation path must be a sibling of current storage.")
    staging = current_storage.parent / f".{current_storage.name}.rollback.{uuid.uuid4().hex}.tmp"
    moved_current = False
    try:
        with acquire_storage_lease(current_storage, blocking=False):
            _materialize_backup(backup_dir, staging)
            _rename_no_replace(current_storage, preserve_current)
            moved_current = True
            try:
                _rename_no_replace(staging, current_storage)
            except BackupError as swap_error:
                try:
                    _rename_no_replace(preserve_current, current_storage)
                except BackupError as restore_error:
                    raise BackupError(
                        "Rollback swap failed; previous storage remains preserved at the explicit preservation path."
                    ) from restore_error
                moved_current = False
                raise BackupError(
                    "Rollback swap failed; original storage was restored."
                ) from swap_error
    except StorageLockError as exc:
        raise BackupError(str(exc)) from exc
    except OSError as exc:
        raise BackupError("Atomic rollback preparation failed before storage replacement.") from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "ok": True,
        "operation": "rollback",
        "current_replaced": True,
        "previous_storage_preserved": moved_current and preserve_current.is_dir(),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backup, verify and restore ReqPilotX storage.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--storage", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--backup", type=Path, required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--target-storage", type=Path, required=True)
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--backup", type=Path, required=True)
    rollback.add_argument("--storage", type=Path, required=True)
    rollback.add_argument("--preserve-current", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "backup":
            report = create_backup(args.storage, args.output)
        elif args.command == "verify":
            report = verify_backup(args.backup)
        elif args.command == "restore":
            report = restore_backup(args.backup, args.target_storage)
        else:
            report = rollback_backup(args.backup, args.storage, args.preserve_current)
    except BackupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
