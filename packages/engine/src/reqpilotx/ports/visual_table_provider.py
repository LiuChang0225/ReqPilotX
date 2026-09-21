from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from reqpilotx.domain.extraction.visual_table import (
    VisualTableCropInput,
    VisualTableSnapshot,
)


class VisualTableProviderRejectionCode(str, Enum):
    UNAVAILABLE = "unavailable"
    INVALID_INPUT = "invalid_input"
    SOURCE_HASH_MISMATCH = "source_hash_mismatch"
    CROP_IMAGE_HASH_MISMATCH = "crop_image_hash_mismatch"
    PROVIDER_OUTPUT_INVALID = "provider_output_invalid"
    MULTIPLE_TABLES = "multiple_tables"
    TOPOLOGY_INVALID = "topology_invalid"
    PROVIDER_FAILED = "provider_failed"


class VisualTableProviderRejected(RuntimeError):
    """Safe, machine-readable failure at the remote table provider boundary."""

    def __init__(
        self,
        code: VisualTableProviderRejectionCode,
        message: str,
    ) -> None:
        if not isinstance(code, VisualTableProviderRejectionCode):
            raise TypeError("code must be a VisualTableProviderRejectionCode value")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must not be blank")
        self.code = code
        super().__init__(message)


@runtime_checkable
class VisualTableProviderPort(Protocol):
    """Parse one exact native-table crop without inventing page geometry."""

    provider_id: str
    provider_version: str

    def parse_table(self, crop_input: VisualTableCropInput) -> VisualTableSnapshot:
        ...


__all__ = [
    "VisualTableProviderPort",
    "VisualTableProviderRejected",
    "VisualTableProviderRejectionCode",
]
