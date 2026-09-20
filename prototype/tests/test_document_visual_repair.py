from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.application import runtime as application_runtime
from app.application.extraction import document_jobs
from app.application.extraction.contracts import (
    DocumentExtractionVisualRepairRequest,
)
from app.domain.extraction.processing_profile import (
    DocumentProcessingProfileV1,
    ResolvedProviderBindingsV1,
    build_project_processing_profile,
    build_resolved_processing_plan,
)
from app.infrastructure.runtime_settings import DatabaseBackend
from app.ports.job_queue import DurableJob, JobStatus, NonRetryableJobError


class DocumentVisualRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project_id = "project-a"
        self.document_id = "document-a"
        self.source_sha256 = "a" * 64
        document_profile = DocumentProcessingProfileV1(
            primary_parser="pymupdf",
            enhancement_chain=("bailian_ocr",),
            complex_vision=None,
            auto_escalation=False,
        )
        self.profile = build_project_processing_profile(
            project_id=self.project_id,
            document=document_profile,
            llm_provider="none",
            knowledge_backend="aliyun_qdrant",
            revision=8,
        )
        self.plan = build_resolved_processing_plan(
            project_id=self.project_id,
            document_id=self.document_id,
            source_sha256=self.source_sha256,
            profile=self.profile,
            document=document_profile,
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf",
                enhancement_providers=("aliyun:bailian-ocr:qwen3.5-ocr",),
            ),
            connection_revision=26,
        )
        self.base_result = self._result(failed_pages=(2,))
        self.artifact_sha256 = "b" * 64
        now = datetime.now(timezone.utc)
        self.base_job = DurableJob(
            job_id="job-base",
            kind="document.extract",
            aggregate_type="sor-document",
            aggregate_id=self.document_id,
            idempotency_key="base-key",
            payload={
                "project_id": self.project_id,
                "document_id": self.document_id,
                "source_sha256": self.source_sha256,
                "processing_plan": self.plan.model_dump(mode="json"),
                "result_runtime_sha256": self.base_result[
                    "result_runtime_sha256"
                ],
            },
            status=JobStatus.SUCCEEDED,
            priority=10,
            attempts=1,
            max_attempts=3,
            generation=1,
            cancel_requested=False,
            lease_owner="",
            lease_expires_at=None,
            available_at=now,
            created_at=now,
            updated_at=now,
            progress={},
            result={
                "schema_version": "document-extraction-result-artifact/v1",
                "project_id": self.project_id,
                "document_id": self.document_id,
                "source_sha256": self.source_sha256,
                "sha256": self.artifact_sha256,
                "storage_key": "runtime/extraction-results/base.json",
                "size": 100,
                "media_type": "application/json",
            },
            error_code="",
            error_message="",
        )

    def _result(self, *, failed_pages: tuple[int, ...]) -> dict[str, object]:
        receipts = []
        for page_number in (1, 2):
            failed = page_number in failed_pages
            receipts.append(
                {
                    "schema_version": "page-processing-receipt/v1",
                    "page_number": page_number,
                    "page_image_sha256": str(page_number) * 64,
                    "attempted_provider_ids": ["aliyun-bailian-qwen-ocr"],
                    "adopted_provider_id": (
                        "upstream:primary-parser"
                        if failed
                        else "aliyun-bailian-qwen-ocr"
                    ),
                    "state": "DEGRADED" if failed else "READY",
                    "degraded_reason_codes": (
                        ["aliyun-bailian-qwen-ocr:provider_failed"] if failed else []
                    ),
                }
            )
        return {
            "project_id": self.project_id,
            "document_id": self.document_id,
            "source_sha256": self.source_sha256,
            "processing_plan": self.plan.model_dump(mode="json"),
            "result_runtime_sha256": (
                document_jobs._document_extraction_result_runtime_sha256(self.plan)
            ),
            "page_processing_receipts": receipts,
            "degraded_page_numbers": list(failed_pages),
        }

    def test_status_exposes_server_derived_visual_repair_identity(self) -> None:
        snapshot = {
            "job_id": self.base_job.job_id,
            "project_id": self.project_id,
            "document_id": self.document_id,
            "source_sha256": self.source_sha256,
            "processing_plan": self.plan.model_dump(mode="json"),
            "result_runtime_sha256": self.base_result["result_runtime_sha256"],
            "status": "COMPLETED",
            "result": self.base_result,
            "_durable_job_generation": 1,
            document_jobs._DURABLE_RESULT_CAS_TOKEN_KEY: dict(self.base_job.result),
        }

        repair = document_jobs._public_document_extraction_visual_repair(snapshot)

        assert repair is not None
        self.assertEqual(repair["state"], "AVAILABLE")
        self.assertEqual(repair["base_job_id"], "job-base")
        self.assertEqual(repair["base_job_generation"], 1)
        self.assertEqual(repair["base_artifact_sha256"], self.artifact_sha256)
        self.assertEqual(repair["ready_page_count"], 1)
        self.assertEqual(repair["failed_page_numbers"], [2])
        self.assertEqual(repair["remote_request_ceiling"], 1)

    def test_incompatible_durable_success_keeps_only_verified_repair_identity(
        self,
    ) -> None:
        status = self._status_from_durable(
            durable=self.base_job,
            restored_result=self.base_result,
        )

        self.assertEqual(status["job"]["status"], "FAILED")
        self.assertEqual(status["job"]["stage"], "incompatible_result")
        self.assertIsNone(status["result"])
        repair = status["visual_repair"]
        assert isinstance(repair, dict)
        self.assertEqual(repair["state"], "AVAILABLE")
        self.assertEqual(repair["base_job_id"], self.base_job.job_id)
        self.assertEqual(repair["base_artifact_sha256"], self.artifact_sha256)
        self.assertEqual(repair["failed_page_numbers"], [2])

    def test_incompatible_durable_success_rejects_wrong_source_or_artifact(
        self,
    ) -> None:
        wrong_source_result = {
            **self.base_result,
            "source_sha256": "c" * 64,
        }
        wrong_artifact = replace(
            self.base_job,
            result={
                **dict(self.base_job.result or {}),
                "sha256": "not-an-artifact-digest",
            },
        )
        for name, durable, restored_result in (
            ("wrong_source", self.base_job, wrong_source_result),
            ("wrong_artifact", wrong_artifact, self.base_result),
        ):
            with self.subTest(name=name):
                status = self._status_from_durable(
                    durable=durable,
                    restored_result=restored_result,
                )

                self.assertEqual(status["job"]["status"], "FAILED")
                self.assertIsNone(status["result"])
                self.assertIsNone(status["visual_repair"])

    def test_endpoint_derives_page_scope_and_never_accepts_client_page_ids(self) -> None:
        class Queue:
            def get_latest_succeeded(_self, **_kwargs):
                return self.base_job

        captured: dict[str, object] = {}

        def enqueue(**kwargs):
            captured.update(kwargs)
            return {
                "job_id": "job-repair",
                "status": "PENDING",
                "source_sha256": self.source_sha256,
                "visual_repair": kwargs["visual_repair"],
            }

        request = DocumentExtractionVisualRepairRequest(
            expected_base_job_id=self.base_job.job_id,
            expected_base_job_generation=1,
            expected_base_artifact_sha256=self.artifact_sha256,
        )
        response = SimpleNamespace(headers={})
        settings = SimpleNamespace(database_backend=DatabaseBackend.POSTGRESQL)
        with (
            patch.object(application_runtime, "RUNTIME_SETTINGS", settings),
            patch.object(application_runtime, "_durable_jobs", return_value=Queue()),
            patch(
                "app.application.extraction.review_support._require_project_document",
                return_value={
                    "id": self.document_id,
                    "source_sha256": self.source_sha256,
                },
            ),
            patch.object(
                document_jobs,
                "_llm_extraction_snapshot_from_durable",
                return_value={"result": self.base_result},
            ),
            patch.object(
                document_jobs,
                "get_project_processing_profile",
                return_value=self.profile,
            ),
            patch.object(
                document_jobs,
                "get_runtime_connection_revision",
                return_value=26,
            ),
            patch.object(
                document_jobs,
                "_validated_structure_revisions_for_document_rerun",
                return_value=(),
            ),
            patch.object(
                document_jobs,
                "_enqueue_durable_llm_document_extraction",
                side_effect=enqueue,
            ),
            patch.object(
                document_jobs,
                "_build_llm_extraction_job_response",
                side_effect=lambda *_args, job, **_kwargs: {"job": job},
            ),
        ):
            result = document_jobs.repair_document_extraction_visual_pages(
                self.project_id,
                self.document_id,
                response,
                request,
            )

        self.assertEqual(result["job"]["job_id"], "job-repair")
        repair = captured["visual_repair"]
        assert isinstance(repair, dict)
        self.assertEqual(repair["cache_required_page_numbers"], [1])
        self.assertEqual(repair["remote_allowed_page_numbers"], [2])
        self.assertEqual(repair["remote_request_ceiling"], 1)
        self.assertNotIn("page_numbers", request.model_dump())

    def test_repair_result_must_strictly_reduce_failures_without_regression(self) -> None:
        baseline = document_jobs._visual_repair_baseline(self.base_result)
        assert baseline is not None
        repair = {
            "cache_required_page_numbers": list(
                baseline["cache_required_page_numbers"]
            ),
            "remote_allowed_page_numbers": list(
                baseline["remote_allowed_page_numbers"]
            ),
            "page_image_sha256_by_page": baseline["page_image_sha256_by_page"],
        }

        document_jobs._validate_document_extraction_visual_repair_result(
            payload={"visual_repair": repair},
            result=self._result(failed_pages=()),
        )

        with self.assertRaisesRegex(NonRetryableJobError, "did not reduce"):
            document_jobs._validate_document_extraction_visual_repair_result(
                payload={"visual_repair": repair},
                result=self._result(failed_pages=(2,)),
            )

    def test_repair_identity_is_stable_and_binds_receipts(self) -> None:
        baseline = document_jobs._visual_repair_baseline(self.base_result)
        assert baseline is not None
        serialized = json.dumps(
            baseline,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=list,
        ).encode("utf-8")
        first = hashlib.sha256(serialized).hexdigest()
        second = hashlib.sha256(serialized).hexdigest()
        self.assertEqual(first, second)

    def _status_from_durable(
        self,
        *,
        durable: DurableJob,
        restored_result: dict[str, object],
    ) -> dict[str, object]:
        class Queue:
            def get_latest(_self, **_kwargs):
                return durable

        settings = SimpleNamespace(database_backend=DatabaseBackend.POSTGRESQL)
        with (
            patch.object(application_runtime, "RUNTIME_SETTINGS", settings),
            patch.object(application_runtime, "_durable_jobs", return_value=Queue()),
            patch(
                "app.application.extraction.review_support._require_project_document",
                return_value={
                    "id": self.document_id,
                    "project_id": self.project_id,
                    "source_sha256": self.source_sha256,
                },
            ),
            patch.object(
                document_jobs,
                "_load_document_extraction_result_artifact",
                return_value=restored_result,
            ),
            patch.object(
                document_jobs,
                "_restored_llm_extraction_job",
                return_value=None,
            ),
            patch(
                "app.application.extraction.source_page_facts."
                "public_document_extraction_page_staging",
                return_value={"ready_page_count": 0},
            ),
        ):
            return document_jobs.get_llm_document_extraction_status(
                self.project_id,
                self.document_id,
                SimpleNamespace(headers={}),
            )


if __name__ == "__main__":
    unittest.main()
