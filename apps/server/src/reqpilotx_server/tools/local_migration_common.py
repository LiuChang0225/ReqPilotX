"""Shared fail-closed helpers for ReqPilotX local migration migration and backup tooling."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import uuid
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable


class LocalMigrationError(RuntimeError):
    """A migration or backup safety contract failed."""


LOCAL_METADATA_DIR = "local-migration"
LOCAL_RECEIPT_FILE = "receipt.json"
LOCAL_KNOWLEDGE_MAPPING_FILE = "knowledge-mapping.json"
LOCAL_OWNERSHIP_FILE = "project-ownership.json"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SQLITE_NAMES = {"app.db", "app.db-wal", "app.db-shm", "app.db-journal"}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def require_sha256(value: Any, *, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(normalized) is None:
        raise LocalMigrationError(f"{field} must be a lowercase SHA-256 identity.")
    return normalized


def value_digest(values: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for value in values:
        encoded = str(value).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def primary_key_digest(rows: Iterable[dict[str, Any]], columns: tuple[str, ...]) -> str:
    return value_digest(
        sorted(canonical_json([row.get(column) for column in columns]) for row in rows)
    )


def records_digest(rows: Iterable[dict[str, Any]]) -> str:
    return value_digest(sorted(canonical_json(row) for row in rows))


def safe_relative_key(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/").strip("/")
    path = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (
        not raw
        or "\x00" in raw
        or path.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or bool(windows.root)
        or ".." in path.parts
        or ".." in windows.parts
        or any(part in {"", "."} for part in path.parts)
    ):
        raise LocalMigrationError("Object key must be a safe relative path.")
    return path.as_posix()


def safe_relative_file(value: Any) -> str:
    raw = str(value or "")
    path = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (
        not raw
        or "\x00" in raw
        or "\\" in raw
        or path.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or bool(windows.root)
        or ".." in path.parts
        or ".." in windows.parts
    ):
        raise LocalMigrationError("Manifest path must be a safe relative POSIX path.")
    return path.as_posix()


def json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise LocalMigrationError("Binary database values require an explicit export contract.")
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return isoformat()
    if hasattr(value, "as_integer_ratio"):
        numerator, denominator = value.as_integer_ratio()
        return numerator if denominator == 1 else float(value)
    raise LocalMigrationError(f"Unsupported database value type: {type(value).__name__}")


def write_json(path: Path, payload: Any) -> None:
    target = Path(path)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def write_new_json(path: Path, payload: Any) -> None:
    target = require_absolute(path, field="JSON output")
    if target.exists():
        raise LocalMigrationError("JSON output already exists; existing files are never overwritten.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        secure_file(temporary)
        with temporary.open("rb+") as stream:
            os.fsync(stream.fileno())
        rename_no_replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalMigrationError(f"JSON file is unreadable: {Path(path).name}") from exc


def require_absolute(path: Path, *, field: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise LocalMigrationError(f"{field} must be absolute.")
    if candidate.is_symlink():
        raise LocalMigrationError(f"{field} must not be a symbolic link.")
    return candidate.resolve(strict=False)


def is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve(strict=False).relative_to(Path(root).resolve(strict=False))
    except ValueError:
        return False
    return True


def rename_no_replace(source: Path, destination: Path) -> None:
    source = Path(source)
    destination = Path(destination)
    if source.parent.resolve() != destination.parent.resolve():
        raise LocalMigrationError("Atomic no-replace rename requires sibling paths.")
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        function = getattr(libc, "renamex_np", None)
        if function is None:
            raise LocalMigrationError("macOS renamex_np is unavailable; refusing unsafe rename fallback.")
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
            raise LocalMigrationError("Linux renameat2 is unavailable; refusing unsafe rename fallback.")
        function.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
        if result != 0:
            _raise_rename_error(ctypes.get_errno())
        return
    if sys.platform == "win32":  # pragma: no cover - Windows branch
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = getattr(kernel32, "MoveFileW", None)
        if function is None:
            raise LocalMigrationError("Windows MoveFileW is unavailable; refusing unsafe rename fallback.")
        function.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        function.restype = ctypes.c_int
        if not function(str(source), str(destination)):
            error_code = ctypes.get_last_error()
            if error_code in {80, 183}:
                raise LocalMigrationError("Atomic rename destination already exists.")
            raise LocalMigrationError(f"Windows atomic no-replace rename failed with error {error_code}.")
        return
    raise LocalMigrationError("Atomic no-replace rename is unavailable on this platform.")


def _raise_rename_error(error_code: int) -> None:
    if error_code in {errno.EEXIST, errno.ENOTEMPTY}:
        raise LocalMigrationError("Atomic rename destination already exists.")
    raise LocalMigrationError(f"Atomic no-replace rename failed with errno {error_code}.")


def secure_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        if os.name != "nt":
            raise


def secure_directory(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        if os.name != "nt":
            raise


def iter_files(root: Path, *, exclude_sqlite_sidecars: bool = True) -> tuple[list[str], dict[str, tuple[int, str]]]:
    directories: list[str] = []
    files: dict[str, tuple[int, str]] = {}
    root = Path(root)
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(dirnames):
            path = current_path / name
            if path.is_symlink():
                raise LocalMigrationError(f"Tree contains a symbolic link: {path.relative_to(root)}")
            directories.append(path.relative_to(root).as_posix())
        for name in filenames:
            if exclude_sqlite_sidecars and current_path == root and name in _SQLITE_NAMES:
                continue
            path = current_path / name
            if path.is_symlink():
                raise LocalMigrationError(f"Tree contains a symbolic link: {path.relative_to(root)}")
            mode = path.stat(follow_symlinks=False).st_mode
            if not stat.S_ISREG(mode):
                raise LocalMigrationError(f"Tree contains an unsafe file: {path.relative_to(root)}")
            files[path.relative_to(root).as_posix()] = (path.stat().st_size, sha256_file(path))
    return sorted(set(directories)), files


def copy_tree_files(
    source_root: Path,
    target_root: Path,
    directories: Iterable[str],
    files: dict[str, tuple[int, str]],
) -> None:
    for relative in directories:
        target = target_root / relative
        target.mkdir(parents=True, exist_ok=True)
        secure_directory(target)
    for relative, expected in sorted(files.items()):
        source = source_root / relative
        target = target_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as reader, target.open("wb") as writer:
            while True:
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        secure_file(target)
        actual = (target.stat().st_size, sha256_file(target))
        if actual != expected:
            raise LocalMigrationError(f"Tree file changed while it was copied: {relative}")


def sqlite_quick_check(path: Path) -> None:
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)) as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.DatabaseError as exc:
        raise LocalMigrationError("SQLite integrity check could not be completed.") from exc
    if not result or str(result[0]).lower() != "ok":
        raise LocalMigrationError("SQLite integrity check failed.")


def sqlite_snapshot(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_file() or source_path.is_symlink():
        raise LocalMigrationError("Storage does not contain a safe app.db database.")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(source_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30.0)) as source, closing(sqlite3.connect(destination_path, timeout=30.0)) as destination:
            source.backup(destination)
            destination.execute("PRAGMA journal_mode=DELETE")
            destination.commit()
    except sqlite3.DatabaseError as exc:
        raise LocalMigrationError("SQLite online backup failed.") from exc
    sqlite_quick_check(destination_path)


def database_logical_digest(path: Path) -> str:
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        parts: list[str] = []
        for table in tables:
            rows = [
                canonical_json({key: row[key] for key in row.keys()})
                for row in conn.execute(f'SELECT * FROM "{table}"').fetchall()
            ]
            parts.append(canonical_json({"table": table, "rows": sorted(rows)}))
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    finally:
        conn.close()


@contextmanager
def secure_open_object(root: Path, key: str):
    root_path = Path(root).expanduser()
    if not root_path.is_absolute() or root_path.is_symlink():
        raise LocalMigrationError("Object root must be an absolute non-symlink directory.")
    root_path = root_path.resolve(strict=False)
    if not root_path.is_dir():
        raise LocalMigrationError("Object root is missing.")
    if (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
    ):
        raise LocalMigrationError("Secure object opening is unavailable on this platform.")
    relative = safe_relative_key(key)
    parts = relative.split("/")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = -1
    try:
        descriptor = os.open(str(root_path), directory_flags)
        root_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise LocalMigrationError("Object root is not a directory.")
        for part in parts[:-1]:
            next_descriptor = os.open(
                part,
                directory_flags | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
            component = os.fstat(descriptor)
            if not stat.S_ISDIR(component.st_mode):
                raise LocalMigrationError(f"Object path component is not a directory: {part}")
        file_descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=descriptor,
        )
        os.close(descriptor)
        descriptor = file_descriptor
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise LocalMigrationError("Object reference is not a regular file.")
        stream = os.fdopen(descriptor, "rb")
        descriptor = -1
        with stream:
            yield stream, file_stat
    except OSError as exc:
        raise LocalMigrationError(f"Object path is missing or unsafe: {relative}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def secure_file_identity(root: Path, absolute_path: Any) -> tuple[int, str]:
    root_path = Path(root).expanduser().resolve(strict=False)
    candidate = Path(str(absolute_path or "")).expanduser()
    if not candidate.is_absolute():
        raise LocalMigrationError("Referenced local path must be absolute.")
    resolved = candidate.resolve(strict=False)
    if not is_within(resolved, root_path):
        raise LocalMigrationError("Referenced local path escapes the application root.")
    relative = resolved.relative_to(root_path).as_posix()
    with secure_open_object(root_path, relative) as (stream, file_stat):
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        if size != int(file_stat.st_size):
            raise LocalMigrationError("Referenced file changed while it was read.")
        return size, digest.hexdigest()


def _check_referenced_file(root: Path, value: Any, *, label: str, expected_sha256: str = "", expected_size: int | None = None) -> None:
    raw = str(value or "").strip()
    if not raw:
        raise LocalMigrationError(f"{label} is blank.")
    size, digest = secure_file_identity(root, raw)
    if expected_sha256 and digest != expected_sha256.lower():
        raise LocalMigrationError(f"{label} hash mismatch.")
    if expected_size is not None and size != int(expected_size):
        raise LocalMigrationError(f"{label} size mismatch.")



def verify_runtime_references(database_path: Path, root: Path, *, stored_root: Path | None = None) -> dict[str, int]:
    root = Path(root).resolve(strict=False)
    stored_root = Path(stored_root or root).resolve(strict=False)

    def mapped(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw:
            return raw
        candidate = Path(raw)
        if candidate.is_absolute() and stored_root != root:
            try:
                relative = candidate.resolve(strict=False).relative_to(stored_root)
            except ValueError:
                return raw
            return str(root / relative)
        return raw
    conn = sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    counts = {"stored_objects": 0, "documents": 0, "knowledge_documents": 0, "exports": 0, "knowledge_syncs": 0, "requirement_trees": 0, "deliveries": 0}
    try:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "stored_objects" in tables:
            for row in conn.execute("SELECT storage_key, sha256, size_bytes FROM stored_objects").fetchall():
                key = safe_relative_key(row["storage_key"])
                _check_referenced_file(root, mapped(str(root / "objects" / key)), label=f"stored object {key}", expected_sha256=str(row["sha256"]), expected_size=int(row["size_bytes"]))
                counts["stored_objects"] += 1
        for table, label in (("sor_documents", "documents"), ("knowledge_documents", "knowledge_documents")):
            if table not in tables:
                continue
            for row in conn.execute(f'SELECT id, file_path, source_sha256, source_size FROM "{table}"').fetchall():
                source_sha = str(row["source_sha256"] or "")
                source_size = int(row["source_size"] or 0)
                _check_referenced_file(
                    root,
                    mapped(row["file_path"]),
                    label=f"{table}.{row['id']}",
                    expected_sha256=source_sha,
                    expected_size=source_size if source_sha or source_size else None,
                )
                counts[label] += 1
        if "export_jobs" in tables:
            for row in conn.execute("SELECT id, file_path FROM export_jobs").fetchall():
                _check_referenced_file(root, mapped(row["file_path"]), label=f"export_jobs.{row['id']}")
                counts["exports"] += 1
        if "knowledge_document_syncs" in tables:
            for row in conn.execute("SELECT id, payload_path FROM knowledge_document_syncs WHERE COALESCE(payload_path, '') <> ''").fetchall():
                _check_referenced_file(root, mapped(row["payload_path"]), label=f"knowledge_document_syncs.{row['id']}")
                counts["knowledge_syncs"] += 1
        if "requirement_sets" in tables:
            for row in conn.execute("SELECT id, requirement_tree_object_key, requirement_tree_sha256 FROM requirement_sets WHERE COALESCE(requirement_tree_object_key, '') <> ''").fetchall():
                key = safe_relative_key(row["requirement_tree_object_key"])
                _check_referenced_file(root, mapped(str(root / "objects" / key)), label=f"requirement tree {row['id']}", expected_sha256=str(row["requirement_tree_sha256"] or ""))
                counts["requirement_trees"] += 1
        if "source_item_delivery_versions" in tables:
            for row in conn.execute("SELECT project_id, sor_document_id, revision, artifact_json FROM source_item_delivery_versions").fetchall():
                try:
                    artifact = json.loads(str(row["artifact_json"]))
                except json.JSONDecodeError as exc:
                    raise LocalMigrationError(f"source_item_delivery_versions.{row['sor_document_id']}#{row['revision']} is not valid JSON.") from exc
                key = safe_relative_key(artifact.get("export_key"))
                _check_referenced_file(root, mapped(str(root / "objects" / key)), label=f"delivery {row['sor_document_id']}#{row['revision']}", expected_sha256=require_sha256(artifact.get("export_sha256"), field="delivery export_sha256"))
                counts["deliveries"] += 1
    finally:
        conn.close()
    return counts


__all__ = [
    "LocalMigrationError",
    "canonical_json",
    "copy_tree_files",
    "database_logical_digest",
    "is_within",
    "iter_files",
    "json_safe",
    "primary_key_digest",
    "read_json",
    "records_digest",
    "rename_no_replace",
    "require_absolute",
    "require_sha256",
    "safe_relative_file",
    "safe_relative_key",
    "secure_directory",
    "secure_file",
    "sha256_bytes",
    "sha256_file",
    "sha256_text",
    "sqlite_quick_check",
    "sqlite_snapshot",
    "value_digest",
    "verify_runtime_references",
    "write_json",
    "write_new_json",
    "secure_open_object",
    "secure_file_identity",
    "LOCAL_METADATA_DIR",
    "LOCAL_RECEIPT_FILE",
    "LOCAL_KNOWLEDGE_MAPPING_FILE",
    "LOCAL_OWNERSHIP_FILE",
]
