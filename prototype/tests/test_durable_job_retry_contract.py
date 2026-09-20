from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.application.extraction import document_jobs
from app.domain.extraction.processing_profile import (
    ResolvedProviderBindingsV1,
    build_local_rules_profile,
    build_resolved_processing_plan,
)
from app.ports.job_queue import DurableJob, JobStatus, NonRetryableJobError
from app.worker.runtime import WorkerRuntime


def _durable_job(*, payload: dict[str, object]) -> DurableJob:
    now = datetime.now(timezone.utc)
    return DurableJob(
        job_id="job-runtime-binding",
        kind="document.extract",
        aggregate_type="sor-document",
        aggregate_id="document-a",
        idempotency_key="document-extraction-runtime-binding",
        payload=dict(payload),
        status=JobStatus.RUNNING,
        priority=10,
        attempts=1,
        max_attempts=3,
        generation=1,
        cancel_requested=False,
        lease_owner="worker-a",
        lease_expires_at=now,
        available_at=now,
        created_at=now,
        updated_at=now,
        progress={},
        result=None,
        error_code="",
        error_message="",
    )


class _RecordingQueue:
    def __init__(self, job: DurableJob) -> None:
        self.job = job
        self.claimed = False
        self.failed: dict[str, object] | None = None
        self.succeeded: dict[str, object] | None = None

    def claim(self, **_kwargs) -> DurableJob | None:
        if self.claimed:
            return None
        self.claimed = True
        return self.job

    def heartbeat(self, **_kwargs) -> DurableJob:
        return self.job

    def fail(self, **kwargs) -> DurableJob:
        self.failed = dict(kwargs)
        return self.job

    def succeed(self, **kwargs) -> DurableJob:
        self.succeeded = dict(kwargs)
        return self.job


class DurableJobRetryContractTests(unittest.TestCase):
    def test_stale_document_runtime_binding_fails_terminal_without_execution(
        self,
    ) -> None:
        source_sha256 = "a" * 64
        profile = build_local_rules_profile(project_id="project-a")
        plan = build_resolved_processing_plan(
            project_id="project-a",
            document_id="document-a",
            source_sha256=source_sha256,
            profile=profile,
            document=profile.document,
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf"
            ),
            connection_revision=1,
        )
        queue = _RecordingQueue(
            _durable_job(
                payload={
                    "project_id": "project-a",
                    "document_id": "document-a",
                    "source_sha256": source_sha256,
                    "processing_plan": plan.model_dump(mode="json"),
                    "result_runtime_sha256": "0" * 64,
                    "visual_strategy": {
                        "schema_version": "extraction-visual-strategy/v1",
                        "mode": "OFF",
                    },
                }
            )
        )
        runtime = WorkerRuntime(
            queue=queue,
            handlers={"document.extract": document_jobs.run_durable_document_extraction},
            worker_id="worker-a",
        )

        with (
            patch(
                "app.application.extraction.review_support._require_project_document",
                return_value={
                    "id": "document-a",
                    "project_id": "project-a",
                    "source_sha256": source_sha256,
                },
            ),
            patch.object(
                document_jobs,
                "_run_local_structure_document_extraction_worker",
            ) as execute_current_runtime,
        ):
            self.assertTrue(runtime.run_once())

        execute_current_runtime.assert_not_called()
        self.assertIsNone(queue.succeeded)
        self.assertEqual(
            queue.failed,
            {
                "job_id": "job-runtime-binding",
                "worker_id": "worker-a",
                "error_code": "document_extraction_runtime_binding_mismatch",
                "error_message": (
                    "Durable extraction result runtime binding is obsolete; "
                    "start a new extraction for the current runtime."
                ),
                "retry_delay_seconds": None,
            },
        )

    def test_transient_handler_failure_keeps_existing_retry_delay(self) -> None:
        queue = _RecordingQueue(
            _durable_job(payload={"document_id": "document-a"})
        )

        def fail_transiently(*_args) -> dict[str, object]:
            raise OSError("temporary object-store outage")

        runtime = WorkerRuntime(
            queue=queue,
            handlers={"document.extract": fail_transiently},
            worker_id="worker-a",
        )

        self.assertTrue(runtime.run_once())

        self.assertIsNone(queue.succeeded)
        self.assertEqual(queue.failed["error_code"], "OSError")
        self.assertEqual(queue.failed["retry_delay_seconds"], 30)

    def test_changed_structure_revision_chain_is_non_retryable(self) -> None:
        source_sha256 = "a" * 64
        profile = build_local_rules_profile(project_id="project-a")
        plan = build_resolved_processing_plan(
            project_id="project-a",
            document_id="document-a",
            source_sha256=source_sha256,
            profile=profile,
            document=profile.document,
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf"
            ),
            connection_revision=1,
        )
        visual_strategy = {
            "schema_version": "extraction-visual-strategy/v1",
            "mode": "OFF",
            "source_ir_adapter_version": document_jobs.PDF_SOURCE_IR_ADAPTER_VERSION,
            "provider_profile": None,
            "visual_text_runtime": None,
        }
        queue = _RecordingQueue(
            _durable_job(
                payload={
                    "project_id": "project-a",
                    "document_id": "document-a",
                    "source_sha256": source_sha256,
                    "processing_plan": plan.model_dump(mode="json"),
                    "result_runtime_sha256": (
                        document_jobs._document_extraction_result_runtime_sha256(plan)
                    ),
                    "visual_strategy": visual_strategy,
                    "structure_revision_chain_sha256": "b" * 64,
                }
            )
        )
        runtime = WorkerRuntime(
            queue=queue,
            handlers={"document.extract": document_jobs.run_durable_document_extraction},
            worker_id="worker-a",
        )

        with (
            patch(
                "app.application.extraction.review_support._require_project_document",
                return_value={
                    "id": "document-a",
                    "project_id": "project-a",
                    "source_sha256": source_sha256,
                },
            ),
            patch(
                "app.application.extraction.processing_pipeline.processing_plan_visual_strategy",
                return_value=visual_strategy,
            ),
            patch.object(
                document_jobs,
                "_validated_structure_revisions_for_document_rerun",
                return_value=(),
            ),
            patch.object(
                document_jobs,
                "_run_local_structure_document_extraction_worker",
            ) as execute_current_runtime,
        ):
            self.assertTrue(runtime.run_once())

        execute_current_runtime.assert_not_called()
        self.assertEqual(
            queue.failed["error_code"],
            "document_extraction_structure_revision_binding_mismatch",
        )
        self.assertIsNone(queue.failed["retry_delay_seconds"])

    def test_non_retryable_error_rejects_invalid_queue_error_code(self) -> None:
        with self.assertRaisesRegex(ValueError, "stable non-blank token"):
            NonRetryableJobError(error_code="bad code", message="invalid")


if __name__ == "__main__":
    unittest.main()
