from __future__ import annotations

from os import PathLike
from typing import Protocol

from reqpilotx_server.domain.extraction.attachment import AttachmentArtifactReceipt


class AttachmentArtifactStorePort(Protocol):
    def preserve(
        self,
        source_path: str | PathLike[str],
        *,
        expected_sha256: str,
        expected_size: int,
        media_type: str,
    ) -> AttachmentArtifactReceipt:
        """Persist exact source bytes and return a verified, path-safe receipt."""

    def verify(self, receipt: AttachmentArtifactReceipt) -> bool:
        """Return whether the stored artifact still matches its receipt."""


__all__ = ["AttachmentArtifactStorePort"]
