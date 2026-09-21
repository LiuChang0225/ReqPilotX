"""Persistence capabilities for neutral, append-only visual source facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from reqpilotx_server.domain.source_visual_capture import (
    VisualCaptureGenerationV1,
    VisualPageCaptureV1,
)


class SourceVisualCaptureRepositoryError(RuntimeError):
    pass


class SourceVisualCaptureRepositoryConflict(SourceVisualCaptureRepositoryError):
    pass


class SourceVisualCaptureRepositoryCorrupt(SourceVisualCaptureRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class VisualPageFailureRecord:
    attempt_id: str
    generation_id: str
    project_id: str
    sor_document_id: str
    source_sha256: str
    page_number: int
    failure_code: str
    created_at_utc: datetime


@runtime_checkable
class SourceVisualFactsReaderPort(Protocol):
    def get_active_generation(
        self,
        *,
        project_id: str,
        sor_document_id: str,
        source_sha256: str,
    ) -> VisualCaptureGenerationV1 | None: ...

    def get_page(
        self,
        *,
        generation_id: str,
        page_number: int,
    ) -> VisualPageCaptureV1 | None: ...

    def list_pages(self, generation_id: str) -> tuple[VisualPageCaptureV1, ...]: ...

    def latest_page_failure(
        self,
        *,
        generation_id: str,
        page_number: int,
    ) -> VisualPageFailureRecord | None: ...


@runtime_checkable
class SourceVisualCaptureRepositoryPort(SourceVisualFactsReaderPort, Protocol):
    def append_generation(
        self, generation: VisualCaptureGenerationV1
    ) -> VisualCaptureGenerationV1: ...

    def append_page(self, page: VisualPageCaptureV1) -> VisualPageCaptureV1: ...

    def append_page_failure(
        self, failure: VisualPageFailureRecord
    ) -> VisualPageFailureRecord: ...

    def purge_document(self, *, project_id: str, sor_document_id: str) -> None: ...


__all__ = [
    "SourceVisualCaptureRepositoryConflict",
    "SourceVisualCaptureRepositoryCorrupt",
    "SourceVisualCaptureRepositoryError",
    "SourceVisualCaptureRepositoryPort",
    "SourceVisualFactsReaderPort",
    "VisualPageFailureRecord",
]
