from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import unittest

from app.adapters.documents.pdf_visual_text_runtime import (
    PDF_VISUAL_ROUTING_POLICY_ID,
    visual_text_provider_id,
)
from app.services.llm_extraction_job_checkpoint import (
    LLM_EXTRACTION_JOB_CHECKPOINT_SCHEMA_VERSION,
    LLM_EXTRACTION_PARTIAL_SCHEMA_VERSION,
    build_llm_extraction_job_checkpoint,
    canonical_llm_extraction_payload_sha256,
    restore_llm_extraction_job_checkpoint,
)
from app.domain.extraction.processing_profile import (
    ResolvedProviderBindingsV1,
    build_local_rules_profile,
    build_resolved_processing_plan,
)
from app.services.extraction_visual_routes import (
    EXTRACTION_VISUAL_STRATEGY_SCHEMA_VERSION,
    extraction_visual_strategy_identity,
)

PROJECT_ID = "project-a"
DOCUMENT_ID = "document-a"
SOURCE_SHA256 = "a" * 64
JOB_ID = "llm-extraction-" + "b" * 32
PREVIEW_VERSION = "preview/v1"
PDF_VERSION = "pdf/v1"
REMOTE_PDF_VERSION = f"{PDF_VERSION}+visual.{'c' * 12}"
RESOLVER_VERSION = "resolver/v1"


def _job_with_partial() -> dict:
    partial = {
        "schema_version": LLM_EXTRACTION_PARTIAL_SCHEMA_VERSION,
        "mode": "READ_ONLY_LLM_PARTIAL",
        "revision": 1,
        "editable": False,
        "project_id": PROJECT_ID,
        "document_id": DOCUMENT_ID,
        "job_id": JOB_ID,
        "source_sha256": SOURCE_SHA256,
        "source_identity": {"source_sha256": SOURCE_SHA256},
        "coverage": {
            "completed_chunks": 1,
            "completed_chunk_indexes": [2],
        },
        "review_groups": [{"id": "group-1"}],
    }
    return {
        "job_id": JOB_ID,
        "project_id": PROJECT_ID,
        "document_id": DOCUMENT_ID,
        "source_sha256": SOURCE_SHA256,
        "status": "RUNNING",
        "stage": "extracting",
        "total_clauses": 3,
        "total_targets": 8,
        "total_chunks": 2,
        "completed_chunks": 1,
        "completed_targets": 4,
        "successful_chunks": 1,
        "unresolved_chunks": 0,
        "blocked_chunks": 0,
        "current_chunk_index": 2,
        "current_chunk_target_count": 4,
        "error_code": "",
        "error_stage": "",
        "error_cause_code": "",
        "result": None,
        "partial_result": partial,
        "partial_revision": 1,
        "partial_completed_chunk_indexes": (2,),
        "final_result_sha256": "",
        "created_at_utc": "2026-07-25T00:00:00+00:00",
        "started_at_utc": "2026-07-25T00:00:01+00:00",
        "finished_at_utc": "",
        "updated_at_utc": "2026-07-25T00:00:02+00:00",
    }


def _restore(payload: dict, **overrides):
    arguments = {
        "project_id": PROJECT_ID,
        "document_id": DOCUMENT_ID,
        "source_sha256": SOURCE_SHA256,
        "preview_schema_version": PREVIEW_VERSION,
        "pdf_source_ir_adapter_version": PDF_VERSION,
        "structure_resolver_version": RESOLVER_VERSION,
    }
    arguments.update(overrides)
    return restore_llm_extraction_job_checkpoint(payload, **arguments)


def _remote_visual_strategy() -> dict:
    return {
        "schema_version": EXTRACTION_VISUAL_STRATEGY_SCHEMA_VERSION,
        "mode": "REMOTE_VISUAL",
        "provider_name": "bailian",
        "provider_id": visual_text_provider_id("bailian"),
        "provider_version": "test-visual-provider/v1",
        "route_id": f"pvr-{'e' * 32}",
        "route_sha256": "e" * 64,
        "routing_policy_id": PDF_VISUAL_ROUTING_POLICY_ID,
        "authorized_page_count": 2,
        "source_ir_adapter_version": REMOTE_PDF_VERSION,
    }


class LlmExtractionJobCheckpointTests(unittest.TestCase):
    def test_v4_checkpoint_binds_the_full_processing_plan(self) -> None:
        profile = build_local_rules_profile(project_id=PROJECT_ID)
        plan = build_resolved_processing_plan(
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            source_sha256=SOURCE_SHA256,
            profile=profile,
            document=profile.document,
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf"
            ),
            connection_revision=3,
        )
        job = _job_with_partial()
        job["processing_plan"] = plan.model_dump(mode="json")

        payload = build_llm_extraction_job_checkpoint(
            job,
            preview_schema_version=PREVIEW_VERSION,
            pdf_source_ir_adapter_version=PDF_VERSION,
            structure_resolver_version=RESOLVER_VERSION,
        )

        self.assertEqual(
            payload["schema_version"],
            LLM_EXTRACTION_JOB_CHECKPOINT_SCHEMA_VERSION,
        )
        restored = _restore(payload)
        assert restored is not None
        self.assertEqual(
            restored["processing_plan"]["canonical_sha256"],
            plan.canonical_sha256,
        )
        tampered = copy.deepcopy(payload)
        tampered["job"]["processing_plan"]["connection_revision"] = 4
        self.assertIsNone(_restore(tampered))

    def test_active_partial_round_trip_preserves_exact_identity(self) -> None:
        payload = build_llm_extraction_job_checkpoint(
            _job_with_partial(),
            preview_schema_version=PREVIEW_VERSION,
            pdf_source_ir_adapter_version=PDF_VERSION,
            structure_resolver_version=RESOLVER_VERSION,
        )

        restored = _restore(payload)

        assert restored is not None
        self.assertEqual(restored["job_id"], JOB_ID)
        self.assertEqual(restored["status"], "RUNNING")
        self.assertEqual(restored["partial_revision"], 1)
        self.assertEqual(restored["partial_completed_chunk_indexes"], (2,))
        self.assertFalse(restored["partial_result"]["editable"])
        self.assertNotIn("result", payload["job"])

    def test_runtime_source_and_partial_tampering_fail_closed(self) -> None:
        payload = build_llm_extraction_job_checkpoint(
            _job_with_partial(),
            preview_schema_version=PREVIEW_VERSION,
            pdf_source_ir_adapter_version=PDF_VERSION,
            structure_resolver_version=RESOLVER_VERSION,
        )
        stale_runtime = copy.deepcopy(payload)
        stale_runtime["runtime_identity"]["structure_resolver_version"] = "stale"
        wrong_partial = copy.deepcopy(payload)
        wrong_partial["job"]["partial_result"]["job_id"] = "llm-extraction-" + "c" * 32
        duplicate_coverage = copy.deepcopy(payload)
        duplicate_coverage["job"]["partial_completed_chunk_indexes"] = [2, 2]
        duplicate_coverage["job"]["partial_revision"] = 2

        self.assertIsNone(_restore(stale_runtime))
        self.assertIsNone(_restore(wrong_partial))
        self.assertIsNone(_restore(duplicate_coverage))
        self.assertIsNone(_restore(payload, source_sha256="d" * 64))

    def test_failed_checkpoint_cannot_retain_partial_or_final_digest(self) -> None:
        job = _job_with_partial()
        job.update(
            {
                "status": "FAILED",
                "stage": "failed",
                "error_code": "synthetic_failure",
                "partial_result": None,
                "partial_revision": 0,
                "partial_completed_chunk_indexes": (),
                "current_chunk_index": None,
                "current_chunk_target_count": 0,
            }
        )
        payload = build_llm_extraction_job_checkpoint(
            job,
            preview_schema_version=PREVIEW_VERSION,
            pdf_source_ir_adapter_version=PDF_VERSION,
            structure_resolver_version=RESOLVER_VERSION,
        )
        restored = _restore(payload)
        assert restored is not None
        self.assertEqual(restored["status"], "FAILED")

        with_partial = copy.deepcopy(payload)
        with_partial["job"]["partial_revision"] = 1
        with_partial["job"]["partial_completed_chunk_indexes"] = [2]
        with_partial["job"]["partial_result"] = _job_with_partial()["partial_result"]
        self.assertIsNone(_restore(with_partial))

    def test_failed_checkpoint_preserves_sanitized_stage_and_cause(self) -> None:
        job = _job_with_partial()
        job.update(
            {
                "status": "FAILED",
                "stage": "failed",
                "error_code": "llm_shadow_preview_visual_capture_rejected",
                "error_stage": "visual_validation",
                "error_cause_code": "pdf_visual_text_provider_failed",
                "partial_result": None,
                "partial_revision": 0,
                "partial_completed_chunk_indexes": (),
                "current_chunk_index": None,
                "current_chunk_target_count": 0,
            }
        )

        payload = build_llm_extraction_job_checkpoint(
            job,
            preview_schema_version=PREVIEW_VERSION,
            pdf_source_ir_adapter_version=PDF_VERSION,
            structure_resolver_version=RESOLVER_VERSION,
        )
        restored = _restore(payload)

        assert restored is not None
        self.assertEqual(restored["error_stage"], "visual_validation")
        self.assertEqual(
            restored["error_cause_code"],
            "pdf_visual_text_provider_failed",
        )
        invalid = copy.deepcopy(payload)
        invalid["job"]["error_cause_code"] = "provider failed with customer text"
        self.assertIsNone(_restore(invalid))

    def test_legacy_v2_checkpoint_restores_without_stage_or_cause(self) -> None:
        payload = build_llm_extraction_job_checkpoint(
            _job_with_partial(),
            preview_schema_version=PREVIEW_VERSION,
            pdf_source_ir_adapter_version=PDF_VERSION,
            structure_resolver_version=RESOLVER_VERSION,
        )
        payload["schema_version"] = "llm-extraction-job-checkpoint/v2"
        payload["job"].pop("error_stage")
        payload["job"].pop("error_cause_code")

        restored = _restore(payload)

        assert restored is not None
        self.assertEqual(restored["error_stage"], "")
        self.assertEqual(restored["error_cause_code"], "")

    def test_remote_visual_strategy_round_trip_and_mismatch_fail_closed(
        self,
    ) -> None:
        visual_strategy = _remote_visual_strategy()
        job = _job_with_partial()
        job["visual_strategy"] = visual_strategy
        payload = build_llm_extraction_job_checkpoint(
            job,
            preview_schema_version=PREVIEW_VERSION,
            pdf_source_ir_adapter_version=REMOTE_PDF_VERSION,
            structure_resolver_version=RESOLVER_VERSION,
            visual_strategy=visual_strategy,
        )

        restored = restore_llm_extraction_job_checkpoint(
            payload,
            project_id=PROJECT_ID,
            document_id=DOCUMENT_ID,
            source_sha256=SOURCE_SHA256,
            preview_schema_version=PREVIEW_VERSION,
            pdf_source_ir_adapter_version=REMOTE_PDF_VERSION,
            structure_resolver_version=RESOLVER_VERSION,
            visual_strategy=visual_strategy,
        )

        assert restored is not None
        self.assertEqual(restored["visual_strategy"], visual_strategy)
        self.assertEqual(
            restored["partial_result"]["visual_strategy"],
            visual_strategy,
        )
        wrong_strategy = extraction_visual_strategy_identity(
            None,
            source_ir_adapter_version=REMOTE_PDF_VERSION,
        )
        self.assertIsNone(
            restore_llm_extraction_job_checkpoint(
                payload,
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=SOURCE_SHA256,
                preview_schema_version=PREVIEW_VERSION,
                pdf_source_ir_adapter_version=REMOTE_PDF_VERSION,
                structure_resolver_version=RESOLVER_VERSION,
                visual_strategy=wrong_strategy,
            )
        )
        tampered = copy.deepcopy(payload)
        tampered["job"]["visual_strategy"]["route_sha256"] = "d" * 64
        self.assertIsNone(
            restore_llm_extraction_job_checkpoint(
                tampered,
                project_id=PROJECT_ID,
                document_id=DOCUMENT_ID,
                source_sha256=SOURCE_SHA256,
                preview_schema_version=PREVIEW_VERSION,
                pdf_source_ir_adapter_version=REMOTE_PDF_VERSION,
                structure_resolver_version=RESOLVER_VERSION,
                visual_strategy=visual_strategy,
            )
        )

    def test_canonical_result_digest_is_order_independent(self) -> None:
        first = {"b": [2, 1], "a": {"value": "x"}}
        second = {"a": {"value": "x"}, "b": [2, 1]}

        self.assertEqual(
            canonical_llm_extraction_payload_sha256(first),
            canonical_llm_extraction_payload_sha256(second),
        )


if __name__ == "__main__":
    unittest.main()
