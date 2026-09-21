"""Integrity-checked file cache for whole-document LLM shadow results."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_RESULT_CACHE_DIRECTORY_NAME = "llm_extraction_shadow_results"
_RESULT_CACHE_SCHEMA_VERSION = "llm-extraction-result-cache/v1"
_JOB_CHECKPOINT_DIRECTORY_NAME = "llm_extraction_job_checkpoints"
_JOB_CHECKPOINT_CACHE_SCHEMA_VERSION = (
    "llm-extraction-job-checkpoint-cache/v1"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class LlmExtractionResultCachePathError(RuntimeError):
    """A cache path could not be proven to remain below its storage root."""


class _SourceBoundJsonFileCache:
    """Integrity-check source-bound JSON without making it serving data."""

    def __init__(
        self,
        storage_root: str | Path,
        *,
        cache_directory_name: str,
        cache_schema_version: str,
        max_file_bytes: int | None = None,
    ) -> None:
        if (
            max_file_bytes is not None
            and (
                isinstance(max_file_bytes, bool)
                or not isinstance(max_file_bytes, int)
                or max_file_bytes < 1
            )
        ):
            raise ValueError("max_file_bytes must be a positive integer or None")
        requested_root = Path(storage_root).expanduser()
        if requested_root.is_symlink():
            raise LlmExtractionResultCachePathError(
                "storage_root must not be a symbolic link"
            )
        requested_root.mkdir(parents=True, exist_ok=True)
        resolved_root = requested_root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise LlmExtractionResultCachePathError(
                "storage_root must be a directory"
            )
        self._storage_root = resolved_root
        self._cache_schema_version = cache_schema_version
        self._max_file_bytes = max_file_bytes
        self._cache_root = self._safe_child_directory(
            self._storage_root,
            cache_directory_name,
            create=True,
        )

    @property
    def storage_root(self) -> Path:
        return self._storage_root

    def cache_path(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
    ) -> Path:
        """Return the deterministic cache path without creating key directories."""

        project, document, source_digest = _validated_binding(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
        )
        project_directory = self._safe_child_directory(
            self._checked_cache_root(),
            f"project-{_identifier_digest(project)}",
            create=False,
        )
        document_directory = self._safe_child_directory(
            project_directory,
            f"document-{_identifier_digest(document)}",
            create=False,
            parent_may_be_missing=True,
        )
        return self._safe_cache_file_path(
            document_directory,
            f"{source_digest}.json",
            parent_may_be_missing=True,
        )

    def save(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
        payload: Mapping[str, Any],
    ) -> Path:
        """Atomically save one JSON-object payload and return its cache path."""

        project, document, source_digest = _validated_binding(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
        )
        normalized_payload, payload_bytes = _normalize_payload(payload)
        envelope = {
            "schema_version": self._cache_schema_version,
            "binding": {
                "project_id": project,
                "document_id": document,
                "source_sha256": source_digest,
            },
            "payload_sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "payload": normalized_payload,
        }
        serialized = _canonical_json_bytes(envelope) + b"\n"

        project_directory = self._safe_child_directory(
            self._checked_cache_root(),
            f"project-{_identifier_digest(project)}",
            create=True,
        )
        document_directory = self._safe_child_directory(
            project_directory,
            f"document-{_identifier_digest(document)}",
            create=True,
        )
        target = self._safe_cache_file_path(
            document_directory,
            f"{source_digest}.json",
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{source_digest}.",
            suffix=".tmp",
            dir=document_directory,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, target)
            _fsync_directory(document_directory)
        except Exception:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            raise
        return target

    def load(
        self,
        *,
        project_id: str,
        document_id: str,
        source_sha256: str,
    ) -> dict[str, Any] | None:
        """Load a matching, intact payload; return ``None`` for unsafe cache data."""

        project, document, source_digest = _validated_binding(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
        )
        try:
            path = self.cache_path(
                project_id=project,
                document_id=document,
                source_sha256=source_digest,
            )
            serialized = _read_regular_file_without_following_symlinks(
                path,
                max_bytes=self._max_file_bytes,
            )
            envelope = json.loads(serialized)
            if not _valid_envelope_shape(
                envelope,
                expected_schema_version=self._cache_schema_version,
            ):
                return None
            if envelope["binding"] != {
                "project_id": project,
                "document_id": document,
                "source_sha256": source_digest,
            }:
                return None
            payload = envelope["payload"]
            payload_bytes = _canonical_json_bytes(payload)
            expected_digest = envelope["payload_sha256"]
            if not hmac.compare_digest(
                hashlib.sha256(payload_bytes).hexdigest(),
                expected_digest,
            ):
                return None
            return payload
        except (
            FileNotFoundError,
            LlmExtractionResultCachePathError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            return None

    def purge_document(
        self,
        *,
        project_id: str,
        document_id: str,
    ) -> bool:
        """Remove every source-version result for one project/document binding."""

        project = _validated_identifier("project_id", project_id)
        document = _validated_identifier("document_id", document_id)
        project_directory = self._safe_child_directory(
            self._checked_cache_root(),
            f"project-{_identifier_digest(project)}",
            create=False,
        )
        document_directory = self._safe_child_directory(
            project_directory,
            f"document-{_identifier_digest(document)}",
            create=False,
            parent_may_be_missing=True,
        )
        if not document_directory.exists():
            return False
        if document_directory.is_symlink() or not document_directory.is_dir():
            raise LlmExtractionResultCachePathError(
                "document cache path is not a safe directory"
            )
        for child in document_directory.iterdir():
            if child.is_dir() and not child.is_symlink():
                raise LlmExtractionResultCachePathError(
                    "document cache contains an unexpected directory"
                )
            child.unlink()
        document_directory.rmdir()
        try:
            project_directory.rmdir()
        except OSError:
            pass
        return True

    def _checked_cache_root(self) -> Path:
        if self._cache_root.is_symlink() or not self._cache_root.is_dir():
            raise LlmExtractionResultCachePathError(
                "cache root is not a safe directory"
            )
        self._assert_contained(self._cache_root)
        return self._cache_root

    def _safe_child_directory(
        self,
        parent: Path,
        name: str,
        *,
        create: bool,
        parent_may_be_missing: bool = False,
    ) -> Path:
        self._assert_contained(parent)
        if parent.is_symlink():
            raise LlmExtractionResultCachePathError(
                "cache directory must not be a symbolic link"
            )
        if not parent.exists():
            if parent_may_be_missing and not create:
                candidate = parent / name
                self._assert_contained(candidate)
                return candidate
            raise LlmExtractionResultCachePathError(
                "cache parent directory does not exist"
            )
        if not parent.is_dir():
            raise LlmExtractionResultCachePathError(
                "cache parent path must be a directory"
            )

        candidate = parent / name
        self._assert_contained(candidate)
        if candidate.is_symlink():
            raise LlmExtractionResultCachePathError(
                "cache directory must not be a symbolic link"
            )
        if create:
            candidate.mkdir(exist_ok=True)
        if candidate.exists() and not candidate.is_dir():
            raise LlmExtractionResultCachePathError(
                "cache directory path is not a directory"
            )
        self._assert_contained(candidate)
        return candidate

    def _safe_cache_file_path(
        self,
        parent: Path,
        name: str,
        *,
        parent_may_be_missing: bool = False,
    ) -> Path:
        self._assert_contained(parent)
        if parent.is_symlink():
            raise LlmExtractionResultCachePathError(
                "cache file parent must not be a symbolic link"
            )
        if not parent.exists() and not parent_may_be_missing:
            raise LlmExtractionResultCachePathError(
                "cache file parent does not exist"
            )
        if parent.exists() and not parent.is_dir():
            raise LlmExtractionResultCachePathError(
                "cache file parent must be a directory"
            )
        candidate = parent / name
        self._assert_contained(candidate)
        if candidate.is_symlink():
            raise LlmExtractionResultCachePathError(
                "cache file must not be a symbolic link"
            )
        return candidate

    def _assert_contained(self, path: Path) -> None:
        try:
            path.resolve(strict=False).relative_to(self._storage_root)
        except (OSError, ValueError) as exc:
            raise LlmExtractionResultCachePathError(
                "cache path escapes storage_root"
            ) from exc


def _validated_binding(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
) -> tuple[str, str, str]:
    project = _validated_identifier("project_id", project_id)
    document = _validated_identifier("document_id", document_id)
    if not isinstance(source_sha256, str) or not _SHA256_PATTERN.fullmatch(
        source_sha256
    ):
        raise ValueError("source_sha256 must be 64 lowercase hexadecimal characters")
    return project, document, source_sha256


def _validated_identifier(field_name: str, value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise ValueError(f"{field_name} must be a non-empty canonical string")
    return value


def _identifier_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalize_payload(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], bytes]:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a JSON object")
    serialized = _canonical_json_bytes(dict(payload))
    normalized = json.loads(serialized)
    if not isinstance(normalized, dict):
        raise TypeError("payload must be a JSON object")
    return normalized, serialized


def _valid_envelope_shape(
    value: Any,
    *,
    expected_schema_version: str,
) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "binding",
        "payload_sha256",
        "payload",
    }:
        return False
    if value["schema_version"] != expected_schema_version:
        return False
    if not isinstance(value["binding"], dict):
        return False
    if set(value["binding"]) != {
        "project_id",
        "document_id",
        "source_sha256",
    }:
        return False
    if not isinstance(value["payload"], dict):
        return False
    payload_digest = value["payload_sha256"]
    return isinstance(payload_digest, str) and bool(
        _SHA256_PATTERN.fullmatch(payload_digest)
    )


def _read_regular_file_without_following_symlinks(
    path: Path,
    *,
    max_bytes: int | None = None,
) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            raise OSError("cache path is not a regular file")
        if max_bytes is not None and file_status.st_size > max_bytes:
            raise OSError("cache file exceeds its bounded read limit")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            serialized = handle.read(
                -1 if max_bytes is None else max_bytes + 1,
            )
            if max_bytes is not None and len(serialized.encode("utf-8")) > max_bytes:
                raise OSError("cache file exceeds its bounded read limit")
            return serialized
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class LlmExtractionResultCache(_SourceBoundJsonFileCache):
    """Store completed source-bound LLM shadow results."""

    def __init__(self, storage_root: str | Path) -> None:
        super().__init__(
            storage_root,
            cache_directory_name=_RESULT_CACHE_DIRECTORY_NAME,
            cache_schema_version=_RESULT_CACHE_SCHEMA_VERSION,
        )


class LlmExtractionJobCheckpointCache(_SourceBoundJsonFileCache):
    """Store active source-bound job/partial checkpoints."""

    def __init__(self, storage_root: str | Path) -> None:
        super().__init__(
            storage_root,
            cache_directory_name=_JOB_CHECKPOINT_DIRECTORY_NAME,
            cache_schema_version=_JOB_CHECKPOINT_CACHE_SCHEMA_VERSION,
        )


__all__ = [
    "LlmExtractionJobCheckpointCache",
    "LlmExtractionResultCache",
    "LlmExtractionResultCachePathError",
]
