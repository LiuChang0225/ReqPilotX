from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from app.ports.object_store import ObjectStore


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RuntimeObjectFiles:
    """Resolve storage identities to verified parser-compatible local files."""

    def __init__(
        self,
        object_store: ObjectStore,
        cache_root: Path,
        *,
        allow_legacy_paths: bool = False,
    ) -> None:
        self.object_store = object_store
        self.cache_root = Path(cache_root).expanduser().resolve(strict=False)
        self.allow_legacy_paths = allow_legacy_paths
        self.cache_root.mkdir(parents=True, exist_ok=True)

    def materialize(self, *, storage_key: str, expected_sha256: str) -> Path:
        digest = _validated_sha256(expected_sha256)
        suffix = _safe_suffix(Path(storage_key).suffix)
        target = self.cache_root / digest[:2] / f"{digest}{suffix}"
        if target.is_file() and _hash_file(target) == digest:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                with self.object_store.open(storage_key) as source:
                    copied_digest = hashlib.sha256()
                    while chunk := source.read(1024 * 1024):
                        if not isinstance(chunk, bytes):
                            raise TypeError("ObjectStore sources must return bytes.")
                        output.write(chunk)
                        copied_digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            if copied_digest.hexdigest() != digest:
                raise ValueError("Materialized object hash does not match its identity.")
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def resolve_record(self, record: Any) -> Path:
        payload = record if isinstance(record, dict) else {}
        storage_key = str(payload.get("storage_key") or "").strip()
        if storage_key:
            return self.materialize(
                storage_key=storage_key,
                expected_sha256=str(payload.get("source_sha256") or ""),
            )
        file_path = str(payload.get("file_path") or "").strip()
        if self.allow_legacy_paths and file_path:
            return Path(file_path)
        raise ValueError("Record has no storage identity.")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_sha256(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError("Object materialization requires a SHA-256 identity.")
    return normalized


def _safe_suffix(value: str) -> str:
    normalized = str(value or "").lower()
    return normalized if re.fullmatch(r"\.[a-z0-9]{1,12}", normalized) else ""


__all__ = ["RuntimeObjectFiles"]
