from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import inspect
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.adapters.persistence.sql_reading_catalog import (
    SqlReadingCatalogRepository,
)
from app.api.errors import application_error_handler
from app.api.routers import reading as reading_router
from app.application.errors import ApplicationError
from app.application.source_capture import require_current_source_document


SOURCE_SHA256 = "a" * 64
OTHER_SOURCE_SHA256 = "b" * 64
CURRENT_BODY_SENTINEL = "CURRENT_DOCUMENT_BODY_MUST_NOT_LEAK"
OTHER_BODY_SENTINEL = "OTHER_DOCUMENT_BODY_MUST_NOT_LEAK"
FOREIGN_BODY_SENTINEL = "FOREIGN_PROJECT_BODY_MUST_NOT_LEAK"

_PROJECT_KEYS = {"id", "name", "customer_name"}
_DOCUMENT_KEYS = {
    "id",
    "project_id",
    "file_name",
    "file_type",
    "detected_format",
    "source_sha256",
    "source_size",
    "created_at",
}
_FORBIDDEN_KEYS = {
    "active_requirement_set_id",
    "ai_assessed_at",
    "ai_status",
    "extracted_text",
    "extraction_locked_at",
    "file_path",
    "knowledge_base_version",
    "preview_status",
    "processing_profile_json",
    "processing_profile_revision",
    "processing_profile_sha256",
    "publication_currentness_generation",
    "requirement_set_version",
    "storage_key",
    "workflow_stage",
}


class ReadingCatalogApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        db.init_db()
        self.project = db.create_project("Reading project", "Customer")
        self.other_project = db.create_project("Other project", "Other customer")
        self.document = self._add_document(
            self.project["id"],
            "current.pdf",
            CURRENT_BODY_SENTINEL,
            SOURCE_SHA256,
        )
        self.other_document = self._add_document(
            self.project["id"],
            "other.pdf",
            OTHER_BODY_SENTINEL,
            OTHER_SOURCE_SHA256,
        )
        self.foreign_document = self._add_document(
            self.other_project["id"],
            "foreign.pdf",
            FOREIGN_BODY_SENTINEL,
            "c" * 64,
        )
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE projects
                SET processing_profile_json = ?,
                    processing_profile_revision = 7,
                    processing_profile_sha256 = ?
                WHERE id = ?
                """,
                (OTHER_BODY_SENTINEL, "d" * 64, self.project["id"]),
            )
            connection.execute(
                """
                UPDATE sor_documents
                SET workflow_stage = 'EXTRACTION_LOCKED',
                    ai_status = 'READY',
                    preview_status = 'READY',
                    active_requirement_set_id = ?
                WHERE id = ?
                """,
                ("formal-set-must-not-leak", self.document["id"]),
            )
        app = FastAPI()
        app.add_exception_handler(ApplicationError, application_error_handler)
        app.include_router(reading_router.build())
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.stack.close()

    def _add_document(
        self,
        project_id: str,
        file_name: str,
        body: str,
        source_sha256: str,
    ) -> dict:
        return db.add_sor_document(
            project_id,
            file_name,
            "PDF",
            str(self.root / file_name),
            body,
            source_sha256=source_sha256,
            source_size=len(body.encode("utf-8")),
            declared_extension=".pdf",
            detected_format="pdf",
            preflight_schema_version="document-preflight/v1",
        )

    def test_catalog_responses_are_exact_identity_projections(self) -> None:
        projects_response = self.client.get("/api/reading/projects")
        documents_response = self.client.get(
            f"/api/reading/projects/{self.project['id']}/documents"
        )

        self.assertEqual(projects_response.status_code, 200)
        self.assertEqual(documents_response.status_code, 200)
        projects_payload = projects_response.json()
        documents_payload = documents_response.json()
        self.assertEqual(
            projects_payload["schema_version"],
            "reading-project-catalog/v1",
        )
        self.assertEqual(
            documents_payload["schema_version"],
            "reading-document-catalog/v1",
        )
        self.assertEqual(documents_payload["project_id"], self.project["id"])
        self.assertTrue(projects_payload["items"])
        self.assertEqual(
            {frozenset(item) for item in projects_payload["items"]},
            {frozenset(_PROJECT_KEYS)},
        )
        self.assertEqual(
            {frozenset(item) for item in documents_payload["items"]},
            {frozenset(_DOCUMENT_KEYS)},
        )

        serialized = json.dumps(
            {"projects": projects_payload, "documents": documents_payload},
            ensure_ascii=False,
            sort_keys=True,
        )
        keys = _all_keys({"projects": projects_payload, "documents": documents_payload})
        self.assertFalse(keys & _FORBIDDEN_KEYS)
        self.assertNotIn(CURRENT_BODY_SENTINEL, serialized)
        self.assertNotIn(OTHER_BODY_SENTINEL, serialized)
        self.assertNotIn(FOREIGN_BODY_SENTINEL, serialized)
        self.assertNotIn(self.foreign_document["id"], serialized)

    def test_missing_project_fails_inside_reading_namespace(self) -> None:
        response = self.client.get(
            "/api/reading/projects/project-does-not-exist/documents"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["detail"]["reason_code"],
            "reading_project_not_found",
        )

    def test_source_capture_identity_uses_narrow_projection(self) -> None:
        with (
            patch.object(
                db,
                "get_project",
                side_effect=AssertionError("generic project SELECT * is forbidden"),
            ),
            patch.object(
                db,
                "get_sor_document",
                side_effect=AssertionError("generic document SELECT * is forbidden"),
            ),
        ):
            projected = require_current_source_document(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                expected_source_sha256=SOURCE_SHA256,
            )

        self.assertEqual(
            set(projected),
            {"id", "project_id", "source_sha256", "source_size", "file_path"},
        )
        self.assertEqual(projected["source_sha256"], SOURCE_SHA256)
        self.assertNotIn(CURRENT_BODY_SENTINEL, json.dumps(projected))

    def test_reading_catalog_sql_never_uses_wide_or_formal_columns(self) -> None:
        source = inspect.getsource(SqlReadingCatalogRepository).lower()
        self.assertNotIn("select *", source)
        for forbidden in _FORBIDDEN_KEYS - {"file_path", "storage_key"}:
            self.assertNotIn(forbidden, source)


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_all_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value))
    return set()


if __name__ == "__main__":
    unittest.main()
