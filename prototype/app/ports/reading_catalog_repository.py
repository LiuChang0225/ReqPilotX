"""Read-only persistence capability for the reading workspace catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.domain.reading_catalog import (
    ReadingDocumentCatalogItemV1,
    ReadingProjectCatalogItemV1,
)


@dataclass(frozen=True, slots=True)
class ReadingSourceDocumentRecord:
    """Minimum persisted identity needed to capture one immutable source."""

    id: str
    project_id: str
    source_sha256: str
    source_size: int
    source_locator: str


@runtime_checkable
class ReadingCatalogRepositoryPort(Protocol):
    """Expose only source identity fields needed to choose one reading document."""

    def list_projects(self) -> tuple[ReadingProjectCatalogItemV1, ...]: ...

    def get_project(
        self,
        project_id: str,
    ) -> ReadingProjectCatalogItemV1 | None: ...

    def list_documents(
        self,
        project_id: str,
    ) -> tuple[ReadingDocumentCatalogItemV1, ...]: ...


@runtime_checkable
class ReadingSourceIdentityRepositoryPort(Protocol):
    def get_source_document(
        self,
        *,
        project_id: str,
        sor_document_id: str,
    ) -> ReadingSourceDocumentRecord | None: ...


__all__ = [
    "ReadingCatalogRepositoryPort",
    "ReadingSourceDocumentRecord",
    "ReadingSourceIdentityRepositoryPort",
]
