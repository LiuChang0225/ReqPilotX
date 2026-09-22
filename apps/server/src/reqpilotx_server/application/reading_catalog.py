"""Reading-only project and document selection use cases."""

from __future__ import annotations

from reqpilotx_server.adapters.persistence.sql_reading_catalog import (
    SqlReadingCatalogRepository,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.domain.reading_catalog import (
    ReadingDocumentCatalogV1,
    ReadingProjectCatalogV1,
)


def list_reading_projects() -> ReadingProjectCatalogV1:
    return ReadingProjectCatalogV1(
        items=SqlReadingCatalogRepository().list_projects()
    )


def list_reading_documents(
    project_id: str,
) -> ReadingDocumentCatalogV1:
    store = SqlReadingCatalogRepository()
    if store.get_project(project_id) is None:
        raise ApplicationError(
            status_code=404,
            code="reading_project_not_found",
            detail={
                "reason_code": "reading_project_not_found",
                "message": "Project not found.",
            },
        )
    return ReadingDocumentCatalogV1(
        project_id=project_id,
        items=store.list_documents(project_id),
    )


__all__ = ["list_reading_documents", "list_reading_projects"]
