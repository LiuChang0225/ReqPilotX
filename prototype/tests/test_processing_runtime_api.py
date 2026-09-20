from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import asyncio
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from fastapi import Response
from starlette.datastructures import Headers, UploadFile

from app import db
from app.application import knowledge
from app.application.errors import ApplicationError
from app.application.extraction import document_jobs, provider_support
from app.application.extraction.contracts import (
    DocumentExtractionStartRequest,
)


class ProcessingRuntimeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db_patch = patch.object(db, "DB_PATH", self.root / "app.db")
        self.storage_patch = patch.object(db, "STORAGE_DIR", self.root)
        self.db_patch.start()
        self.storage_patch.start()
        db.init_db()
        self.project = db.create_project("Runtime plan", "Customer")
        self.source = self.root / "source.pdf"
        pdf = fitz.open()
        page = pdf.new_page()
        page.insert_text((72, 72), "Local source")
        pdf.save(self.source)
        pdf.close()
        source_bytes = self.source.read_bytes()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        self.document = db.add_sor_document(
            project_id=self.project["id"],
            file_name=self.source.name,
            file_type="pdf",
            file_path=str(self.source),
            extracted_text="",
            source_sha256=source_sha256,
            source_size=len(source_bytes),
            declared_extension=".pdf",
            detected_format="pdf",
            preflight_schema_version="document-preflight/v1",
            media_type="application/pdf",
        )
        with document_jobs._LLM_EXTRACTION_JOB_LOCK:
            document_jobs._LLM_EXTRACTION_JOBS.clear()

    def tearDown(self) -> None:
        with document_jobs._LLM_EXTRACTION_JOB_LOCK:
            document_jobs._LLM_EXTRACTION_JOBS.clear()
        self.storage_patch.stop()
        self.db_patch.stop()
        self.temporary.cleanup()

    def test_local_start_is_plan_bound_duplicate_safe_and_constructs_no_llm(
        self,
    ) -> None:
        with (
            patch.object(document_jobs, "Thread") as thread_type,
            patch.object(
                provider_support,
                "_build_llm_shadow_semantic_provider",
                side_effect=AssertionError("local mode constructed an LLM"),
            ) as llm_builder,
        ):
            first = document_jobs.start_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
                DocumentExtractionStartRequest(),
            )
            duplicate = document_jobs.start_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
                DocumentExtractionStartRequest(),
            )

        llm_builder.assert_not_called()
        thread_type.assert_called_once()
        thread_type.return_value.start.assert_called_once_with()
        self.assertEqual(first["job"]["job_id"], duplicate["job"]["job_id"])
        self.assertEqual(
            first["job"]["processing_plan"]["llm_provider"],
            "none",
        )
        self.assertEqual(
            first["job"]["processing_plan"]["source_sha256"],
            self.document["source_sha256"],
        )
        self.assertEqual(first["job"]["status"], "PENDING")

    def test_bailian_unavailable_receipt_marks_layout_capability_auth_failed(
        self,
    ) -> None:
        result = {
            "processing_plan": {
                "document": {
                    "enhancement_chain": ["bailian_layout"],
                }
            },
            "page_processing_receipts": [
                {
                    "degraded_reason_codes": [
                        "aliyun-bailian-qwen-ocr:unavailable",
                    ]
                }
            ],
        }

        with patch.object(
            document_jobs,
            "record_runtime_capability_failure",
        ) as record_failure:
            document_jobs._record_document_runtime_capability_failures(result)

        record_failure.assert_called_once()
        call = record_failure.call_args.kwargs
        self.assertEqual(
            call["capability_id"],
            document_jobs.CAPABILITY_BAILIAN_LAYOUT,
        )
        self.assertEqual(call["state"].value, "AUTH_FAILED")
        self.assertEqual(
            call["reason_code"],
            "bailian_layout_auth_failed",
        )

    def test_active_job_from_an_incompatible_result_runtime_fails_closed(
        self,
    ) -> None:
        with patch.object(document_jobs, "Thread"):
            first = document_jobs.start_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
                DocumentExtractionStartRequest(),
            )
        with document_jobs._LLM_EXTRACTION_JOB_LOCK:
            active = document_jobs._LLM_EXTRACTION_JOBS[self.document["id"]]
            active["result_runtime_sha256"] = "0" * 64

        with self.assertRaises(ApplicationError) as caught:
            document_jobs.start_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
                DocumentExtractionStartRequest(),
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["reason_code"],
            "document_extraction_runtime_conflict",
        )
        self.assertEqual(first["job"]["job_id"], active["job_id"])

    def test_knowledge_upload_saves_before_sync_without_selected_backend(
        self,
    ) -> None:
        upload = UploadFile(
            filename="reference.txt",
            file=io.BytesIO(b"saved before retrieval sync"),
            headers=Headers({"content-type": "text/plain"}),
        )
        staging = self.root / "knowledge-staging"
        staging.mkdir()
        with patch(
            "app.application.ingestion._upload_staging_dir",
            return_value=staging,
        ):
            created = asyncio.run(
                knowledge.upload_knowledge_document(
                    self.project["id"],
                    file=upload,
                    doc_type="REFERENCE",
                    doc_version="",
                    status="ACTIVE",
                    effective_from="",
                    effective_to="",
                )
            )

        self.assertTrue(created["retrieval_sync_deferred"])
        self.assertEqual(created["retrieval_sync_status"], "PENDING")
        self.assertEqual(len(db.list_knowledge_documents(self.project["id"])), 1)
        self.assertTrue(any(staging.iterdir()))


if __name__ == "__main__":
    unittest.main()
