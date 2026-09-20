from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import BinaryIO

from app.ports.object_store import StoredObject


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LocalObjectStore:
    """Content-verified local object storage with atomic publication."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(
        self,
        *,
        key: str,
        source: BinaryIO,
        expected_sha256: str,
        media_type: str,
    ) -> StoredObject:
        digest_expected = _validated_sha256(expected_sha256)
        target = self._path_for_key(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise TypeError("ObjectStore sources must return bytes.")
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            actual_sha256 = digest.hexdigest()
            if actual_sha256 != digest_expected:
                raise ValueError("Object content hash does not match expected_sha256.")
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return StoredObject(
            key=_validated_key(key),
            size=size,
            sha256=digest_expected,
            media_type=str(media_type or "application/octet-stream"),
            etag=digest_expected,
        )

    def open(self, key: str) -> BinaryIO:
        return self._path_for_key(key).open("rb")

    def stat(self, key: str) -> StoredObject | None:
        target = self._path_for_key(key)
        if not target.is_file():
            return None
        digest = hashlib.sha256()
        size = 0
        with target.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        return StoredObject(
            key=_validated_key(key),
            size=size,
            sha256=digest.hexdigest(),
            media_type="application/octet-stream",
            etag=digest.hexdigest(),
        )

    def delete(self, key: str) -> bool:
        target = self._path_for_key(key)
        if not target.exists():
            return False
        if not target.is_file() or target.is_symlink():
            raise ValueError("Object key does not resolve to a regular file.")
        target.unlink()
        return True

    def _path_for_key(self, key: str) -> Path:
        normalized = _validated_key(key)
        candidate = (self.root / normalized).resolve(strict=False)
        if candidate == self.root or self.root not in candidate.parents:
            raise ValueError("Object key escapes the configured object root.")
        if candidate.is_symlink():
            raise ValueError("Object keys cannot resolve to symbolic links.")
        return candidate


def _validated_key(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/").strip("/")
    parts = normalized.split("/") if normalized else []
    if (
        not parts
        or any(part in {"", ".", ".."} for part in parts)
        or normalized.startswith("/")
    ):
        raise ValueError("Object key must be a safe relative path.")
    return "/".join(parts)


def _validated_sha256(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError("expected_sha256 must be 64 lowercase hexadecimal characters.")
    return normalized


__all__ = ["LocalObjectStore"]
