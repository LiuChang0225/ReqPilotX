"""Capability-separated ports for immutable neutral source facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot


class SourceCaptureRepositoryError(RuntimeError):
    pass


class SourceCaptureRepositoryConflict(SourceCaptureRepositoryError):
    pass


class SourceCaptureRepositoryCorrupt(SourceCaptureRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class SourceCaptureRecord:
    capture_id: str
    project_id: str
    sor_document_id: str
    source_sha256: str
    source_ir: SourceIRSnapshot
    created_at_utc: datetime


@runtime_checkable
class SourceFactsReaderPort(Protocol):
    def get_capture(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        source_sha256: str,
    ) -> SourceCaptureRecord | None: ...


@runtime_checkable
class SourceCaptureRepositoryPort(SourceFactsReaderPort, Protocol):
    """Write capability owned only by the neutral source-capture use case."""

    def append_capture(
        self,
        record: SourceCaptureRecord,
        *,
        asset_publication_id: str | None = None,
    ) -> SourceCaptureRecord: ...

    def purge_document(
        self,
        *,
        project_id: str,
        sor_document_id: str,
    ) -> tuple[str, ...]: ...


__all__ = [
    "SourceCaptureRecord",
    "SourceCaptureRepositoryConflict",
    "SourceCaptureRepositoryCorrupt",
    "SourceCaptureRepositoryError",
    "SourceCaptureRepositoryPort",
    "SourceFactsReaderPort",
]
