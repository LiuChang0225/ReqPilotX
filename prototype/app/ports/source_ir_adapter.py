from __future__ import annotations

from datetime import datetime
from enum import Enum
from os import PathLike
from typing import Protocol, runtime_checkable

from app.domain.extraction.preflight import DocumentFormat, DocumentPreflightReport
from app.domain.extraction.source_ir import SourceIRSnapshot


class SourceIRAdapterRejectionCode(str, Enum):
    """Fail-closed reasons that prevent a native adapter from parsing bytes."""

    INVALID_PREFLIGHT = "invalid_preflight"
    SOURCE_CHANGED = "source_changed"
    INVALID_TIMESTAMP = "invalid_timestamp"
    CONVERSION_POLICY_REJECTED = "conversion_policy_rejected"
    CONVERTER_UNAVAILABLE = "converter_unavailable"
    CONVERTER_VERSION_MISMATCH = "converter_version_mismatch"
    CONVERSION_TIMEOUT = "conversion_timeout"
    CONVERSION_FAILED = "conversion_failed"
    DERIVED_PREFLIGHT_REJECTED = "derived_preflight_rejected"
    VISUAL_PROFILE_FAILED = "visual_profile_failed"


class SourceIRAdapterRejected(RuntimeError):
    """Raised when an adapter cannot trust its input boundary.

    Parser failures after a valid preflight belong in a ``FAILED`` SourceIR
    snapshot.  This exception is reserved for a bypassed/stale boundary where
    creating a snapshot would attach facts to an identity the adapter has not
    actually parsed.
    """

    def __init__(self, code: SourceIRAdapterRejectionCode, message: str) -> None:
        if not isinstance(code, SourceIRAdapterRejectionCode):
            raise TypeError("code must be a SourceIRAdapterRejectionCode value")
        if not message.strip():
            raise ValueError("message must not be blank")
        self.code = code
        super().__init__(message)


@runtime_checkable
class SourceIRAdapterPort(Protocol):
    """Read-only native-document adapter for the provider-neutral fact layer."""

    adapter_id: str
    adapter_version: str
    document_format: DocumentFormat

    def extract(
        self,
        source_path: str | PathLike[str],
        *,
        preflight: DocumentPreflightReport,
        created_at: datetime,
    ) -> SourceIRSnapshot:
        ...


__all__ = [
    "SourceIRAdapterPort",
    "SourceIRAdapterRejected",
    "SourceIRAdapterRejectionCode",
]
