"""Narrow SQL projections for the isolated reading workspace catalog."""

from __future__ import annotations

import re
from typing import Any

from reqpilotx_server import db
from reqpilotx_server.domain.reading_catalog import (
    ReadingDocumentCatalogItemV1,
    ReadingProjectCatalogItemV1,
)
from reqpilotx_server.ports.reading_catalog_repository import ReadingSourceDocumentRecord


class SqlReadingCatalogRepository:
    """Read identity facts without hydrating generic project/document rows."""

    def list_projects(self) -> tuple[ReadingProjectCatalogItemV1, ...]:
        from reqpilotx_server.saas.context import current_scope
        from reqpilotx_server.saas.config import SaaSSettings
        owner = current_scope()
        if SaaSSettings.from_environment().enabled and owner is None:
            raise PermissionError("saas_catalog_requires_trusted_owner")
        with db.get_conn() as connection:
            if owner is not None:
                rows = connection.execute("""SELECT p.id,p.name,p.customer_name FROM projects p
                    JOIN saas_project_owners o ON o.project_id=p.id WHERE o.account_id=?
                    ORDER BY p.created_at DESC""", (owner.account_id,)).fetchall()
            else:
                rows = connection.execute(
                """
                SELECT id, name, customer_name
                FROM projects
                ORDER BY created_at DESC
                """
                ).fetchall()
        return tuple(_project_item(row) for row in rows)

    def get_project(
        self,
        project_id: str,
    ) -> ReadingProjectCatalogItemV1 | None:
        with db.get_conn() as connection:
            row = connection.execute(
                """
                SELECT id, name, customer_name
                FROM projects
                WHERE id = ?
                """,
                (project_id,),
            ).fetchone()
        return _project_item(row) if row is not None else None

    def list_documents(
        self,
        project_id: str,
    ) -> tuple[ReadingDocumentCatalogItemV1, ...]:
        with db.get_conn() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    project_id,
                    file_name,
                    file_type,
                    detected_format,
                    source_sha256,
                    source_size,
                    created_at
                FROM sor_documents
                WHERE project_id = ?
                ORDER BY created_at DESC
                """,
                (project_id,),
            ).fetchall()
        return tuple(_document_item(row) for row in rows)

    def get_source_document(
        self,
        *,
        project_id: str,
        sor_document_id: str,
    ) -> ReadingSourceDocumentRecord | None:
        with db.get_conn() as connection:
            row = connection.execute(
                """
                SELECT
                    id,
                    project_id,
                    source_sha256,
                    source_size,
                    file_path AS source_locator
                FROM sor_documents
                WHERE id = ? AND project_id = ?
                """,
                (sor_document_id, project_id),
            ).fetchone()
        if row is None:
            return None
        return ReadingSourceDocumentRecord(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            source_sha256=str(row["source_sha256"] or "").strip().lower(),
            source_size=int(row["source_size"] or 0),
            source_locator=str(row["source_locator"] or "").strip(),
        )


def _project_item(row: Any) -> ReadingProjectCatalogItemV1:
    return ReadingProjectCatalogItemV1(
        id=str(row["id"]),
        name=str(row["name"]),
        customer_name=str(row["customer_name"]),
    )


def _document_item(row: Any) -> ReadingDocumentCatalogItemV1:
    source_sha256 = str(row["source_sha256"] or "").strip().lower()
    return ReadingDocumentCatalogItemV1(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        file_name=str(row["file_name"]),
        file_type=str(row["file_type"] or ""),
        detected_format=str(row["detected_format"] or ""),
        source_sha256=(
            source_sha256
            if re.fullmatch(r"[0-9a-f]{64}", source_sha256)
            else None
        ),
        source_size=int(row["source_size"] or 0),
        created_at=str(row["created_at"]),
    )


__all__ = ["SqlReadingCatalogRepository"]
