"""Local SQLite backup, verification and restore for one application root."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from reqpilotx_server.infrastructure.storage_lock import StorageLockError, acquire_storage_lease
from reqpilotx_server.tools.local_migration_common import (
    LocalMigrationError,
    copy_tree_files,
    database_logical_digest,
    is_within,
    iter_files,
    primary_key_digest,
    read_json,
    records_digest,
    rename_no_replace,
    require_absolute,
    safe_relative_file,
    secure_file,
    sha256_file,
    sqlite_quick_check,
    sqlite_snapshot,
    verify_runtime_references,
    write_json,
    LOCAL_METADATA_DIR,
    LOCAL_RECEIPT_FILE,
)

BACKUP_CONTRACT = "reqpilotx-local-storage-backup/v1"
BACKUP_MANIFEST = "backup-manifest.json"
BACKUP_DATA = "data"
_SQLITE_NAME = "app.db"


def _verify_retained_metadata(data_root: Path) -> None:
    receipt_path = data_root / LOCAL_METADATA_DIR / LOCAL_RECEIPT_FILE
    if not receipt_path.is_file() or receipt_path.is_symlink():
        return
    receipt = read_json(receipt_path)
    if not isinstance(receipt, dict):
        raise LocalMigrationError("Retained migration receipt is invalid.")
    mapping = (receipt.get("knowledge") or {}).get("mapping") or {}
    if mapping.get("provided"):
        path = data_root / LOCAL_METADATA_DIR / safe_relative_file(mapping.get("path"))
        if not path.is_file() or path.is_symlink() or sha256_file(path) != mapping.get("sha256"):
            raise LocalMigrationError("Retained knowledge mapping is missing or changed.")
    ownership = receipt.get("ownership") or {}
    if ownership.get("provided"):
        path = data_root / LOCAL_METADATA_DIR / safe_relative_file(ownership.get("path"))
        if not path.is_file() or path.is_symlink() or sha256_file(path) != ownership.get("sha256"):
            raise LocalMigrationError("Retained project ownership evidence is missing or changed.")


def _validate_backup_manifest(backup_dir: Path) -> tuple[dict[str, Any], Path]:
    backup_dir = require_absolute(backup_dir, field="backup directory")
    manifest_path = backup_dir / BACKUP_MANIFEST
    data_root = backup_dir / BACKUP_DATA
    if not manifest_path.is_file() or manifest_path.is_symlink() or not data_root.is_dir() or data_root.is_symlink():
        raise LocalMigrationError("Backup manifest or data directory is missing.")
    payload = read_json(manifest_path)
    if not isinstance(payload, dict) or payload.get("contract") != BACKUP_CONTRACT or payload.get("schema_version") != 1:
        raise LocalMigrationError("Backup contract is unsupported.")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise LocalMigrationError("Backup manifest entries are invalid.")
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise LocalMigrationError("Backup manifest contains an invalid entry.")
        relative = safe_relative_file(entry.get("path"))
        if relative in seen:
            raise LocalMigrationError("Backup manifest contains duplicate paths.")
        seen.add(relative)
        path = data_root.joinpath(*Path(relative).parts)
        if entry.get("type") == "directory":
            if not path.is_dir() or path.is_symlink():
                raise LocalMigrationError(f"Backup directory is missing or unsafe: {relative}")
        elif entry.get("type") == "file":
            if not path.is_file() or path.is_symlink():
                raise LocalMigrationError(f"Backup file is missing or unsafe: {relative}")
            if path.stat().st_size != int(entry.get("size_bytes", -1)) or sha256_file(path) != entry.get("sha256"):
                raise LocalMigrationError(f"Backup file hash mismatch: {relative}")
        else:
            raise LocalMigrationError("Backup manifest entry type is unsupported.")
    directories, files = iter_files(data_root, exclude_sqlite_sidecars=False)
    expected_directories = sorted(entry["path"] for entry in entries if entry.get("type") == "directory")
    expected_files = {
        entry["path"]: (int(entry["size_bytes"]), str(entry["sha256"]))
        for entry in entries
        if entry.get("type") == "file"
    }
    if directories != expected_directories or files != expected_files:
        raise LocalMigrationError("Backup data contains missing or unexpected entries.")
    database_path = data_root / _SQLITE_NAME
    sqlite_quick_check(database_path)
    if database_logical_digest(database_path) != payload.get("database_logical_sha256"):
        raise LocalMigrationError("Backup logical database digest does not match.")
    source_root = Path(str(payload.get("source_root") or "")).expanduser()
    if not source_root.is_absolute():
        raise LocalMigrationError("Backup source root identity is invalid.")
    verify_runtime_references(database_path, data_root, stored_root=source_root)
    _verify_retained_metadata(data_root)
    return payload, data_root


def create_backup(root: Path, output_dir: Path) -> dict[str, Any]:
    root = require_absolute(root, field="application root")
    output = require_absolute(output_dir, field="backup output")
    if not root.is_dir() or root.is_symlink():
        raise LocalMigrationError("Application root is missing or unsafe.")
    if not (root / _SQLITE_NAME).is_file() or (root / _SQLITE_NAME).is_symlink():
        raise LocalMigrationError("Application root has no safe app.db.")
    for marker in (".LOCAL-MIGRATION-INCOMPLETE", ".LOCAL-MIGRATION-VERIFY-FAILED"):
        if (root / marker).exists():
            raise LocalMigrationError("Application root is marked incomplete and cannot be backed up.")
    if output.exists():
        raise LocalMigrationError("Backup output already exists; existing backups are never overwritten.")
    if is_within(output, root) or is_within(root, output):
        raise LocalMigrationError("Backup output and application root must not contain one another.")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.{uuid.uuid4().hex}.tmp"
    try:
        with acquire_storage_lease(root, blocking=False):
            staging.mkdir(mode=0o700)
            data_root = staging / BACKUP_DATA
            data_root.mkdir(mode=0o700)
            before_directories, before_files = iter_files(root)
            sqlite_snapshot(root / _SQLITE_NAME, data_root / _SQLITE_NAME)
            secure_file(data_root / _SQLITE_NAME)
            copy_tree_files(root, data_root, before_directories, before_files)
            after_directories, after_files = iter_files(root)
            if before_directories != after_directories or before_files != after_files:
                raise LocalMigrationError("Application root changed during backup; no backup was published.")
            directories, files = iter_files(data_root, exclude_sqlite_sidecars=False)
            payload = {
                "schema_version": 1,
                "contract": BACKUP_CONTRACT,
                "backup_id": f"local-backup-{uuid.uuid4().hex}",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_root": str(root),
                "sqlite": {"path": _SQLITE_NAME, "strategy": "sqlite_online_backup"},
                "database_logical_sha256": database_logical_digest(data_root / _SQLITE_NAME),
                "entries": [
                    *({"path": path, "type": "directory"} for path in directories),
                    *(
                        {"path": path, "type": "file", "size_bytes": size, "sha256": digest}
                        for path, (size, digest) in sorted(files.items())
                    ),
                ],
            }
            write_json(staging / BACKUP_MANIFEST, payload)
            verified = verify_backup(staging)
            rename_no_replace(staging, output)
    except StorageLockError as exc:
        raise LocalMigrationError(str(exc)) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {**verified, "data_root": str(output / BACKUP_DATA)}


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    payload, data_root = _validate_backup_manifest(backup_dir)
    return {
        "ok": True,
        "contract": BACKUP_CONTRACT,
        "backup_id": payload.get("backup_id"),
        "created_at": payload.get("created_at"),
        "entry_count": len(payload.get("entries", [])),
        "database_logical_sha256": payload.get("database_logical_sha256"),
        "data_root": str(data_root),
    }


def _relocate_database_paths(database_path: Path, source_root: Path, target_root: Path) -> None:
    source_root = source_root.resolve(strict=False)
    target_root = target_root.resolve(strict=False)
    replacements = {
        "sor_documents": ("file_path",),
        "knowledge_documents": ("file_path",),
        "export_jobs": ("file_path",),
        "knowledge_document_syncs": ("payload_path",),
    }
    conn = sqlite3.connect(database_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        for table, columns in replacements.items():
            if table not in tables:
                continue
            for row in conn.execute(f'SELECT rowid, * FROM "{table}"').fetchall():
                updates: dict[str, str] = {}
                for column in columns:
                    raw = str(row[column] or "").strip()
                    if not raw:
                        continue
                    candidate = Path(raw)
                    if not candidate.is_absolute():
                        continue
                    try:
                        relative = candidate.resolve(strict=False).relative_to(source_root)
                    except ValueError:
                        continue
                    updates[column] = str(target_root / relative)
                if updates:
                    assignments = ", ".join(f'"{column}" = ?' for column in updates)
                    conn.execute(
                        f'UPDATE "{table}" SET {assignments} WHERE rowid = ?',
                        (*updates.values(), row["rowid"]),
                    )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _refresh_restored_receipt(staging: Path, target: Path, payload: dict[str, Any]) -> None:
    receipt_path = staging / LOCAL_METADATA_DIR / LOCAL_RECEIPT_FILE
    if not receipt_path.is_file() or receipt_path.is_symlink():
        return
    receipt = read_json(receipt_path)
    if not isinstance(receipt, dict):
        return
    conn = sqlite3.connect(staging / _SQLITE_NAME, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        for name, entry in (receipt.get("tables") or {}).items():
            if not name or not name.replace("_", "").isalnum():
                continue
            primary_key = tuple(entry.get("primary_key") or ())
            if not primary_key:
                continue
            rows = [{key: row[key] for key in row.keys()} for row in conn.execute(f'SELECT * FROM "{name}"').fetchall()]
            entry["row_count"] = len(rows)
            entry["records_sha256"] = records_digest(rows)
            entry["primary_key_sha256"] = primary_key_digest(rows, primary_key)
    finally:
        conn.close()
    receipt["target_root_name"] = target.name
    receipt["restored_at"] = datetime.now(timezone.utc).isoformat()
    receipt["restored_from_backup"] = payload.get("backup_id")
    write_json(receipt_path, receipt)


def restore_backup(backup_dir: Path, target_root: Path) -> dict[str, Any]:
    backup = require_absolute(backup_dir, field="backup directory")
    target = require_absolute(target_root, field="restore target")
    if target.exists():
        raise LocalMigrationError("Restore target already exists; existing roots are never overwritten.")
    if (target.parent / "app.db").is_file() and (target.parent / "objects").is_dir():
        raise LocalMigrationError("Restore target must not be created inside an existing application root.")
    payload, data_root = _validate_backup_manifest(backup)
    if is_within(target, backup) or is_within(backup, target):
        raise LocalMigrationError("Restore target and backup directory must not contain one another.")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.restore.{uuid.uuid4().hex}.tmp"
    try:
        staging.mkdir(mode=0o700)
        copy_tree_files(data_root, staging, *iter_files(data_root, exclude_sqlite_sidecars=False))
        database_path = staging / _SQLITE_NAME
        sqlite_quick_check(database_path)
        source_root = Path(str(payload.get("source_root") or "")).expanduser()
        _relocate_database_paths(database_path, source_root, target)
        verify_runtime_references(database_path, staging, stored_root=target)
        _refresh_restored_receipt(staging, target, payload)
        restored_digest = database_logical_digest(database_path)
        rename_no_replace(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "ok": True,
        "operation": "restore",
        "target_created": True,
        "backup_id": payload.get("backup_id"),
        "database_logical_sha256": restored_digest,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local SQLite backup, verification and restore.")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--root", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--backup", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--target-root", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "backup":
            report = create_backup(args.root, args.output)
        elif args.command == "verify":
            report = verify_backup(args.backup)
        else:
            report = restore_backup(args.backup, args.target_root)
    except (LocalMigrationError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"Local backup failed: {type(exc).__name__}"}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
