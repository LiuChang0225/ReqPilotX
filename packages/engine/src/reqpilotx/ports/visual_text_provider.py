from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from reqpilotx.domain.source_visual_text import VisualTextPageInput, VisualTextSnapshot


class VisualTextCaptureCancelled(RuntimeError):
    """Abort visual capture when the owning workflow is no longer current."""


class VisualTextProviderRejectionCode(str, Enum):
    UNAVAILABLE = "unavailable"
    AUTHENTICATION_FAILED = "authentication_failed"
    ACCESS_DENIED = "access_denied"
    QUOTA_EXHAUSTED = "quota_exhausted"
    RATE_LIMITED = "rate_limited"
    INVALID_INPUT = "invalid_input"
    SOURCE_HASH_MISMATCH = "source_hash_mismatch"
    PAGE_IMAGE_HASH_MISMATCH = "page_image_hash_mismatch"
    PROVIDER_OUTPUT_INVALID = "provider_output_invalid"
    PROVIDER_FAILED = "provider_failed"


class VisualTextProviderRejected(RuntimeError):
    """Safe, machine-readable failure at the optional visual provider boundary."""

    def __init__(
        self,
        code: VisualTextProviderRejectionCode,
        message: str,
    ) -> None:
        if not isinstance(code, VisualTextProviderRejectionCode):
            raise TypeError("code must be a VisualTextProviderRejectionCode value")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must not be blank")
        self.code = code
        super().__init__(message)


@runtime_checkable
class VisualTextProviderPort(Protocol):
    """Recognize text from an in-memory page rendering without changing SourceIR text."""

    provider_id: str
    provider_version: str

    def recognize_page(self, page_input: VisualTextPageInput) -> VisualTextSnapshot:
        ...


__all__ = [
    "VisualTextCaptureCancelled",
    "VisualTextProviderPort",
    "VisualTextProviderRejected",
    "VisualTextProviderRejectionCode",
]
