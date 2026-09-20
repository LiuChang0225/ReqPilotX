from __future__ import annotations

import hashlib
import uuid
from os import PathLike
from pathlib import Path

from app.domain.extraction.attachment import (
    AttachmentArtifactReceipt,
    stable_attachment_artifact_id,
)


_COPY_CHUNK_BYTES = 1024 * 1024


class AttachmentStoreError(RuntimeError):
    pass


class LocalAttachmentArtifactStore:
    """Content-addressed local storage for opaque customer attachments."""

    def __init__(self, root: str | PathLike[str]) -> None:
        self.root = Path(root).expanduser().resolve()

    def preserve(
        self,
        source_path: str | PathLike[str],
        *,
        expected_sha256: str,
        expected_size: int,
        media_type: str,
    ) -> AttachmentArtifactReceipt:
        source = Path(source_path).expanduser().resolve()
        storage_key = f"opaque/{expected_sha256[:2]}/{expected_sha256}"
        target = self._resolve_storage_key(storage_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            size, digest = _file_identity(target)
            if size != expected_size or digest != expected_sha256:
                raise AttachmentStoreError(
                    "A content-addressed attachment target has conflicting bytes."
                )
        else:
            part = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
            try:
                size, digest = _copy_with_identity(source, part)
                if size != expected_size or digest != expected_sha256:
                    raise AttachmentStoreError(
                        "The attachment source changed before it was preserved."
                    )
                part.replace(target)
            except BaseException:
                part.unlink(missing_ok=True)
                raise

        receipt = AttachmentArtifactReceipt(
            artifact_id=stable_attachment_artifact_id(
                source_sha256=expected_sha256,
                source_size=expected_size,
            ),
            source_sha256=expected_sha256,
            source_size=expected_size,
            media_type=media_type,
            storage_key=storage_key,
            store_verified=True,
        )
        if not self.verify(receipt):
            raise AttachmentStoreError(
                "The preserved attachment failed immediate store verification."
            )
        return receipt

    def verify(self, receipt: AttachmentArtifactReceipt) -> bool:
        try:
            target = self._resolve_storage_key(receipt.storage_key)
            size, digest = _file_identity(target)
        except (OSError, ValueError):
            return False
        return size == receipt.source_size and digest == receipt.source_sha256

    def _resolve_storage_key(self, storage_key: str) -> Path:
        target = (self.root / storage_key).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(
                "attachment storage key escapes its configured root"
            ) from exc
        return target


def _copy_with_identity(source: Path, target: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as source_handle, target.open("xb") as target_handle:
        while chunk := source_handle.read(_COPY_CHUNK_BYTES):
            target_handle.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _file_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


__all__ = ["AttachmentStoreError", "LocalAttachmentArtifactStore"]
