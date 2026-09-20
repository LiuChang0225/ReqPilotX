from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fitz

from app.adapters.documents.pdf_source_ir_adapter import (
    PDF_SOURCE_IR_ADAPTER_VERSION,
    PdfSourceIRAdapter,
)
from app.adapters.documents.pdf_visual_text_runtime import (
    visual_text_provider_id,
)
from app.adapters.documents.preflight import LocalDocumentPreflight
from app.application.extraction.build_llm_shadow_preview import (
    LlmShadowPreviewProgressCancelled,
    LlmShadowPreviewRejected,
    LlmShadowPreviewRejectionCode,
    _source_ir_adapter_with_visual_capture_progress,
    _VisualCaptureProgressProvider,
    build_llm_requirement_batch_preview,
    build_llm_requirement_document_preview,
)
from app.application.extraction.build_source_ir import build_source_ir
from app.application.extraction.llm_provision_batch import (
    LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION,
)
from app.application.extraction.llm_semantics import (
    MODEL_REQUIREMENT_ASSEMBLER_VERSION,
    MODEL_SEMANTIC_CLASSIFIER_VERSION,
    SEMANTIC_PLANNING_RUNTIME_VERSION,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.semantic_provider import SemanticProviderRunStatus
from app.domain.extraction.source_ir import SourceDiagnostic, SourceIRStatus
from app.domain.extraction.structure import (
    StructureFinding,
    StructureFindingSeverity,
    stable_structure_finding_id,
)
from app.domain.extraction.structure_revision import (
    ReparentStructureOperationV1,
    StructureRevisionV1,
)
from app.ports.structure_audit_provider import (
    StructureAuditCategory,
    StructureAuditDecision,
    StructureAuditProposalEvidence,
    StructureAuditProviderResult,
)
from app.services.extraction_visual_routes import AuthorizedPdfVisualRuntime
from tests.test_llm_provision_batch import (
    _DocumentProvisionProvider,
    _ProvisionProvider,
)
from tests.test_llm_semantics import _run


class LlmShadowPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "requirements.pdf"
        _write_three_provision_pdf(self.source)
        self.source_sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.section_paths = (
            ("1", "1.1"),
            ("1", "1.2"),
            ("1", "1.3"),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_complete_batch_reuses_frontend_dto_without_requirement_set(self) -> None:
        provider = _PreviewProvider()

        result = self._build(provider)

        self.assertEqual(result["schema_version"], "extraction-llm-shadow-preview/v5")
        self.assertEqual(result["mode"], "EDITABLE_LLM_REVIEW")
        self.assertEqual(result["collection"], "llm_requirement_batch")
        self.assertEqual(result["serving_impact"], "NONE")
        self.assertIsNone(result["requirement_set_id"])
        self.assertFalse(result["requirement_set_created"])
        self.assertFalse(result["requirement_set_activated"])
        self.assertEqual(result["llm_batch"]["status"], "COMPLETE")
        self.assertEqual(result["status"], "READY")
        self.assertEqual(
            [
                item["completed_provider_run_count"]
                for item in result["llm_batch"]["provisions"]
            ],
            [2, 2, 2],
        )
        self.assertEqual(result["counts"]["successful_provisions"], 3)
        self.assertEqual(result["counts"]["needs_classification"], 0)
        self.assertEqual(
            result["pipeline"]["semantic"]["planning_runtime_version"],
            SEMANTIC_PLANNING_RUNTIME_VERSION,
        )
        self.assertEqual(
            result["pipeline"]["semantic"]["classifier_runtime_version"],
            MODEL_SEMANTIC_CLASSIFIER_VERSION,
        )
        self.assertEqual(
            result["pipeline"]["semantic"]["assembler_runtime_version"],
            MODEL_REQUIREMENT_ASSEMBLER_VERSION,
        )
        self.assertEqual(
            result["pipeline"]["semantic"]["document_runtime_version"],
            LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION,
        )
        self.assertEqual(provider.classification_call_count, 3)
        self.assertEqual(provider.assembly_call_count, 3)
        self.assertTrue(result["items"])
        self.assertEqual(len(result["requirement_groups"]), 3)
        self.assertEqual(len(result["review_groups"]), 3)
        self.assertTrue(
            all(
                item["review_status"] == "REQUIREMENT"
                for item in result["review_groups"]
            )
        )
        first_point = result["requirement_groups"][0]["assessment_points"][0]
        self.assertEqual(first_point["role"], "PRIMARY")
        self.assertEqual(first_point["decision_mode"], "DIRECT")
        self.assertEqual(first_point["semantic_class"], "REQUIREMENT")
        self.assertIn("parent_id", first_point)
        self.assertTrue(first_point["source_refs"])
        first_prediction = next(
            node
            for node in result["llm_review"]["model_prediction"]["nodes"]
            if node["owner_structure_node_id"]
            == result["requirement_groups"][0]["boundary_structure_node_id"]
        )
        self.assertTrue(
            first_prediction["semantic_unit_id"].startswith("semantic-unit-")
        )
        self.assertEqual(
            first_prediction["source_semantic_unit_ids"][0],
            first_prediction["semantic_unit_id"],
        )
        self.assertEqual(
            {
                (source_ref["kind"], source_ref["source_id"])
                for source_ref in first_prediction["source_refs"]
            },
            {
                (source_ref["kind"], source_ref["source_id"])
                for source_ref in first_point["source_refs"]
            },
        )
        self.assertEqual(first_prediction["review_text"], first_point["text"])
        first_item = result["items"][0]
        self.assertTrue(first_item["source_refs"])
        self.assertTrue(first_item["context_source_refs"])
        self.assertTrue(first_item["text"])
        self.assertEqual(result["ocr"]["mode"], "OFF")
        self.assertFalse(result["ocr"]["local_fallback"])
        review = result["llm_review"]
        self.assertEqual(review["persistence"]["status"], "UNBOUND")
        self.assertIsNone(review["persistence"]["session_id"])
        self.assertEqual(review["reviewed_result"]["revision"], 0)
        self.assertEqual(review["reviewed_result"]["status"], "DRAFT")
        self.assertEqual(
            review["delta"]["metrics"]["confirmed_target_rate"],
            1.0,
        )
        self.assertEqual(
            review["model_prediction"]["prediction_id"],
            review["reviewed_result"]["prediction_id"],
        )

    def test_document_preview_analyzes_all_clauses_without_manual_paths(
        self,
    ) -> None:
        provider = _DocumentProvisionProvider()
        partial_results: list[dict[str, object]] = []
        preparation_stages: list[str] = []

        result = build_llm_requirement_document_preview(
            self.source,
            expected_source_sha256=self.source_sha256,
            provider=provider,
            max_concurrent_chunks=2,
            preparation_progress_callback=preparation_stages.append,
            partial_result_callback=partial_results.append,
        )

        self.assertEqual(
            preparation_stages,
            [
                "preflight",
                "visual_authorization",
                "source_ir",
                "visual_validation",
                "structure_build",
                "structure_audit",
                "scope_planning",
                "scoped_visual_validation",
                "semantic_image_planning",
                "batch_planning",
            ],
        )
        self.assertEqual(result["collection"], "llm_requirement_document")
        self.assertEqual(
            result["presentation"]["default_collection"], "requirement_groups"
        )
        self.assertEqual(result["llm_batch"]["strategy"], "DOCUMENT_AUTO")
        self.assertEqual(result["llm_batch"]["max_concurrent_chunks"], 2)
        self.assertEqual(result["llm_batch"]["analyzed_clause_count"], 4)
        self.assertEqual(result["llm_batch"]["semantic_chunk_count"], 1)
        self.assertEqual(result["counts"]["analyzed_clauses"], 4)
        self.assertEqual(provider.classification_call_count, 1)
        self.assertEqual(provider.assembly_call_count, 1)
        self.assertEqual(len(result["requirement_groups"]), 4)
        self.assertTrue(result["llm_review"]["model_prediction"]["nodes"])
        self.assertEqual(len(partial_results), 1)
        partial = partial_results[0]
        self.assertEqual(
            partial["schema_version"],
            "extraction-llm-document-partial/v1",
        )
        self.assertEqual(partial["mode"], "READ_ONLY_LLM_PARTIAL")
        self.assertEqual(partial["status"], "PROCESSING")
        self.assertFalse(partial["editable"])
        self.assertEqual(
            partial["coverage"],
            {
                "total_clauses": 4,
                "total_chunks": 1,
                "completed_chunks": 1,
                "pending_chunks": 0,
                "completed_chunk_indexes": [1],
                "total_targets": result["counts"]["semantic_annotations"],
                "completed_targets": result["counts"]["semantic_annotations"],
                "successful_chunks": 1,
                "unresolved_chunks": 0,
                "blocked_chunks": 0,
            },
        )
        self.assertNotIn("llm_review", partial)
        self.assertEqual(
            partial["review_groups"],
            result["review_groups"],
        )
        self.assertEqual(partial["items"], result["items"])
        self.assertEqual(partial["requirement_groups"], result["requirement_groups"])
        self.assertEqual(partial["tables"], result["tables"])
        self.assertEqual(partial["findings"], result["findings"])

    def test_document_preview_defaults_custom_provider_to_serial(self) -> None:
        result = build_llm_requirement_document_preview(
            self.source,
            expected_source_sha256=self.source_sha256,
            provider=_DocumentProvisionProvider(),
        )

        self.assertEqual(result["llm_batch"]["max_concurrent_chunks"], 1)

    def test_visual_capture_progress_preserves_provider_contract(self) -> None:
        delegate = mock.Mock()
        delegate.provider_id = "visual-provider"
        delegate.provider_version = "visual-provider/v1"
        delegate.provider_version_for_page.side_effect = (
            lambda page_number: f"visual-provider/page-{page_number}"
        )
        snapshot = object()
        delegate.recognize_page.return_value = snapshot
        observed: list[tuple[int, int, int]] = []
        wrapped = _VisualCaptureProgressProvider(
            delegate,
            authorized_upper_bound=60,
            callback=lambda completed, upper_bound, page_number: observed.append(
                (completed, upper_bound, page_number)
            ),
        )

        self.assertEqual(wrapped.provider_id, delegate.provider_id)
        self.assertEqual(wrapped.provider_version, delegate.provider_version)
        self.assertEqual(
            wrapped.provider_version_for_page(12),
            "visual-provider/page-12",
        )
        self.assertIs(
            wrapped.recognize_page(SimpleNamespace(page_number=12)),  # type: ignore[arg-type]
            snapshot,
        )
        self.assertEqual(observed, [(1, 60, 12)])

        delegate.recognize_page.side_effect = RuntimeError("provider failed")
        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            wrapped.recognize_page(  # type: ignore[arg-type]
                SimpleNamespace(page_number=13)
            )
        self.assertEqual(observed, [(1, 60, 12), (2, 60, 13)])

    def test_visual_capture_progress_callback_cannot_change_provider_result(
        self,
    ) -> None:
        delegate = mock.Mock()
        delegate.provider_id = "visual-provider"
        delegate.provider_version = "visual-provider/v1"
        snapshot = object()
        delegate.recognize_page.return_value = snapshot
        wrapped = _VisualCaptureProgressProvider(
            delegate,
            authorized_upper_bound=2,
            callback=mock.Mock(side_effect=RuntimeError("progress unavailable")),
        )

        with mock.patch(
            "app.application.extraction.build_llm_shadow_preview.logger.warning"
        ) as warning:
            result = wrapped.recognize_page(  # type: ignore[arg-type]
                SimpleNamespace(page_number=1)
            )

        self.assertIs(result, snapshot)
        warning.assert_called_once()

    def test_visual_capture_progress_preserves_cancellation_signal(self) -> None:
        delegate = mock.Mock()
        delegate.provider_id = "visual-provider"
        delegate.provider_version = "visual-provider/v1"
        delegate.recognize_page.return_value = object()
        wrapped = _VisualCaptureProgressProvider(
            delegate,
            authorized_upper_bound=2,
            callback=mock.Mock(
                side_effect=LlmShadowPreviewProgressCancelled("job replaced")
            ),
        )

        with self.assertRaisesRegex(
            LlmShadowPreviewProgressCancelled,
            "job replaced",
        ):
            wrapped.recognize_page(  # type: ignore[arg-type]
                SimpleNamespace(page_number=1)
            )

    def test_authorized_visual_progress_adapter_keeps_runtime_identity(self) -> None:
        delegate = mock.Mock()
        delegate.provider_id = "visual-provider"
        delegate.provider_version = "visual-provider/v1"
        selected_pages = frozenset({1, 2})
        original_adapter = PdfSourceIRAdapter(
            visual_text_provider=delegate,
            visual_text_page_numbers=selected_pages,
        )
        runtime = SimpleNamespace(
            pdf_adapter=original_adapter,
            visual_text_provider=delegate,
            visual_text_page_numbers=selected_pages,
            visual_table_provider=None,
            visual_table_page_numbers=frozenset(),
            route=SimpleNamespace(
                source_sha256=self.source_sha256,
                page_numbers=frozenset({1, 2, 3}),
            ),
        )
        authorized = AuthorizedPdfVisualRuntime(
            provider_name="bailian",
            route=SimpleNamespace(
                source_sha256=self.source_sha256,
                route_id=f"pvr-{'3' * 32}",
                route_sha256="3" * 64,
            ),
            runtime=runtime,
        )
        observed: list[tuple[int, int, int]] = []

        adapter = _source_ir_adapter_with_visual_capture_progress(
            authorized,
            lambda completed, upper_bound, page_number: observed.append(
                (completed, upper_bound, page_number)
            ),
        )

        self.assertIsNot(adapter, original_adapter)
        self.assertEqual(adapter.adapter_version, original_adapter.adapter_version)
        wrapped = adapter._visual_text_provider
        assert wrapped is not None
        delegate.recognize_page.return_value = object()
        wrapped.recognize_page(  # type: ignore[arg-type]
            SimpleNamespace(page_number=2)
        )
        self.assertEqual(observed, [(1, 3, 2)])

    def test_document_preview_maps_llm_structure_audit_to_finding_and_group(
        self,
    ) -> None:
        provider = _DocumentStructureAuditProvider()

        result = build_llm_requirement_document_preview(
            self.source,
            expected_source_sha256=self.source_sha256,
            provider=provider,
            structure_audit_enabled=True,
            max_concurrent_chunks=4,
        )

        self.assertEqual(provider.audit_call_count, 1)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(
            result["llm_structure_audit"]["status"],
            "COMPLETE",
        )
        self.assertEqual(
            result["llm_structure_audit"]["finding_count"],
            1,
        )
        self.assertEqual(
            result["llm_structure_audit"]["unmapped_finding_count"],
            0,
        )
        self.assertEqual(result["counts"]["structure_audit_findings"], 1)
        finding = next(
            item
            for item in result["findings"]
            if item["origin"] == "LLM_STRUCTURE_AUDIT"
        )
        expected_code = "llm_structure_audit_possible_missed_boundary"
        self.assertEqual(finding["code"], expected_code)
        self.assertEqual(finding["structure_audit_status"], "SUSPECT")
        self.assertFalse(finding["blocking"])
        self.assertEqual(
            finding["reason_codes"],
            ["synthetic_numbered_content_boundary"],
        )
        self.assertTrue(finding["source_refs"])
        self.assertTrue(finding["provider_run_id"])

        self.assertEqual(finding["mapping_basis"], "TARGET_BOUNDARY")
        review_group = next(
            item
            for item in result["review_groups"]
            if item["id"] == finding["review_group_id"]
        )
        self.assertEqual(
            review_group["boundary_structure_node_id"],
            finding["structure_node_id"],
        )
        self.assertIn(expected_code, review_group["finding_codes"])
        self.assertIn(finding["id"], review_group["finding_ids"])
        self.assertEqual(
            review_group["structure_audit_findings"],
            [
                {
                    "id": finding["id"],
                    "code": expected_code,
                    "structure_audit_status": "SUSPECT",
                    "structure_node_id": finding["structure_node_id"],
                    "related_structure_node_ids": finding["related_structure_node_ids"],
                    "page_start": finding["page_start"],
                    "page_end": finding["page_end"],
                    "source_refs": finding["source_refs"],
                    "reason_codes": finding["reason_codes"],
                    "provider_run_id": finding["provider_run_id"],
                    "review_group_id": review_group["id"],
                    "mapping_basis": "TARGET_BOUNDARY",
                    "related_review_groups": finding["related_review_groups"],
                }
            ],
        )
        self.assertEqual(len(finding["related_review_groups"]), 1)
        related_group = finding["related_review_groups"][0]
        self.assertEqual(related_group["outline_label"], "1.3")
        self.assertTrue(related_group["title"])
        self.assertEqual(related_group["mapping_basis"], "RELATED_BOUNDARY")
        self.assertTrue(
            all("structure_audit_findings" in item for item in result["review_groups"])
        )

    def test_document_preview_defaults_to_rules_only_revision_zero(self) -> None:
        provider = _DocumentStructureAuditProvider()

        result = build_llm_requirement_document_preview(
            self.source,
            expected_source_sha256=self.source_sha256,
            provider=provider,
        )

        agent = result["structure_agent"]
        self.assertEqual(provider.audit_call_count, 0)
        self.assertEqual(
            agent["effective_structure_identity"]["revision_number"],
            0,
        )
        self.assertEqual(agent["revision_chain"], [])
        self.assertEqual(agent["audit"]["status"], "NOT_RUN")
        self.assertEqual(
            result["llm_structure_audit"]["error_code"],
            "structure_audit_not_requested",
        )

    def test_document_preview_applies_valid_ai_proposal_before_semantics(
        self,
    ) -> None:
        provider = _DocumentStructureRepairProvider()

        result = build_llm_requirement_document_preview(
            self.source,
            expected_source_sha256=self.source_sha256,
            provider=provider,
            structure_audit_enabled=True,
        )

        agent = result["structure_agent"]
        base_identity = agent["base_structure_identity"]
        effective_identity = agent["effective_structure_identity"]
        self.assertEqual(effective_identity["revision_number"], 1)
        self.assertNotEqual(
            effective_identity["snapshot_id"],
            base_identity["snapshot_id"],
        )
        self.assertEqual(
            agent["proposal_summaries"][0]["status"],
            "APPLIED",
        )
        self.assertEqual(len(agent["patch_summaries"]), 1)
        self.assertEqual(len(agent["revision_summaries"]), 1)
        self.assertEqual(
            result["llm_review"]["model_prediction"]["structure_snapshot_id"],
            effective_identity["snapshot_id"],
        )
        self.assertTrue(
            all(
                provision["status"] == "SUCCESS"
                for provision in result["llm_batch"]["provisions"]
            )
        )

    def test_document_preview_replays_existing_structure_revision_before_semantics(
        self,
    ) -> None:
        initial = build_llm_requirement_document_preview(
            self.source,
            expected_source_sha256=self.source_sha256,
            provider=_DocumentStructureRepairProvider(),
            structure_audit_enabled=True,
        )
        revision = StructureRevisionV1.model_validate(
            initial["structure_agent"]["revision_chain"][0]
        )

        replay_provider = _DocumentStructureAuditProvider()
        replayed = build_llm_requirement_document_preview(
            self.source,
            expected_source_sha256=self.source_sha256,
            provider=replay_provider,
            structure_revisions=(revision,),
        )

        agent = replayed["structure_agent"]
        self.assertEqual(
            agent["effective_structure_identity"]["revision_number"],
            1,
        )
        self.assertEqual(len(agent["revision_chain"]), 1)
        self.assertEqual(
            replayed["llm_review"]["model_prediction"]["structure_snapshot_id"],
            agent["effective_structure_identity"]["snapshot_id"],
        )
        self.assertEqual(
            replayed["llm_structure_audit"]["error_code"],
            "structure_audit_not_requested",
        )
        self.assertEqual(replay_provider.audit_call_count, 0)

    def test_document_preview_uses_source_order_navigation_fallback_for_audit(
        self,
    ) -> None:
        with mock.patch(
            "app.application.extraction.build_llm_shadow_preview."
            "_review_group_mapping_for_structure_node",
            return_value=None,
        ):
            result = build_llm_requirement_document_preview(
                self.source,
                expected_source_sha256=self.source_sha256,
                provider=_DocumentStructureAuditProvider(),
                structure_audit_enabled=True,
                max_concurrent_chunks=4,
            )

        finding = next(
            item
            for item in result["findings"]
            if item["origin"] == "LLM_STRUCTURE_AUDIT"
        )
        self.assertEqual(
            finding["mapping_basis"],
            "NEAREST_SOURCE_ORDER_NAVIGATION_ONLY",
        )
        self.assertIsNotNone(finding["review_group_id"])
        self.assertEqual(
            result["llm_structure_audit"]["unmapped_finding_count"],
            0,
        )
        mapped_group = next(
            item
            for item in result["review_groups"]
            if item["id"] == finding["review_group_id"]
        )
        self.assertEqual(
            mapped_group["structure_audit_findings"][0]["id"],
            finding["id"],
        )
        self.assertEqual(
            mapped_group["structure_audit_findings"][0]["mapping_basis"],
            "NEAREST_SOURCE_ORDER_NAVIGATION_ONLY",
        )

    def test_document_preview_reports_audit_finding_if_navigation_is_unavailable(
        self,
    ) -> None:
        with (
            mock.patch(
                "app.application.extraction.build_llm_shadow_preview."
                "_review_group_mapping_for_structure_node",
                return_value=None,
            ),
            mock.patch(
                "app.application.extraction.build_llm_shadow_preview."
                "_nearest_review_group_by_source_order",
                return_value=None,
            ),
        ):
            result = build_llm_requirement_document_preview(
                self.source,
                expected_source_sha256=self.source_sha256,
                provider=_DocumentStructureAuditProvider(),
                structure_audit_enabled=True,
                max_concurrent_chunks=4,
            )

        finding = next(
            item
            for item in result["findings"]
            if item["origin"] == "LLM_STRUCTURE_AUDIT"
        )
        self.assertIsNone(finding["review_group_id"])
        self.assertEqual(finding["mapping_basis"], "UNMAPPED")
        self.assertEqual(
            result["llm_structure_audit"]["unmapped_finding_count"],
            1,
        )
        self.assertFalse(
            any(group["structure_audit_findings"] for group in result["review_groups"])
        )

    def test_document_preview_exposes_incomplete_structure_audit_without_losing_draft(
        self,
    ) -> None:
        provider = _UnresolvedDocumentStructureAuditProvider()

        result = build_llm_requirement_document_preview(
            self.source,
            expected_source_sha256=self.source_sha256,
            provider=provider,
            structure_audit_enabled=True,
            max_concurrent_chunks=4,
        )

        self.assertTrue(result["requirement_groups"])
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["pipeline"]["structure"]["status"], "REVIEW_REQUIRED")
        self.assertEqual(
            result["pipeline"]["structure"]["counts"]["llm_audit_unresolved_windows"],
            1,
        )
        self.assertEqual(result["llm_structure_audit"]["status"], "UNRESOLVED")
        self.assertEqual(
            result["llm_structure_audit"]["unresolved_window_count"],
            1,
        )
        self.assertFalse(
            any(
                item.get("origin") == "LLM_STRUCTURE_AUDIT"
                for item in result["findings"]
            )
        )

    def test_complete_batch_keeps_partial_source_ir_visible_for_review(self) -> None:
        report = LocalDocumentPreflight().inspect(self.source)
        source_ir = build_source_ir(
            self.source,
            preflight_report=report,
            adapters=(PdfSourceIRAdapter(),),
        ).model_copy(update={"status": SourceIRStatus.PARTIAL})
        provider = _PreviewProvider()

        with mock.patch(
            "app.application.extraction.build_llm_shadow_preview.build_source_ir",
            return_value=source_ir,
        ):
            result = self._build(provider)

        self.assertEqual(result["llm_batch"]["status"], "COMPLETE")
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["pipeline"]["source_ir"]["status"], "PARTIAL")
        self.assertEqual(
            result["pipeline"]["requirement_view"]["status"],
            "REVIEW_REQUIRED",
        )

    def test_complete_batch_keeps_scoped_structure_finding_visible_for_review(
        self,
    ) -> None:
        report = LocalDocumentPreflight().inspect(self.source)
        source_ir = build_source_ir(
            self.source,
            preflight_report=report,
            adapters=(PdfSourceIRAdapter(),),
        )
        tree = build_structure_tree(source_ir)
        target = next(item for item in tree.nodes if item.explicit_label == "1.1")
        finding = StructureFinding(
            finding_id=stable_structure_finding_id(
                source_ir_id=tree.source_ir_id,
                resolver_id=tree.resolver_id,
                resolver_version=tree.resolver_version,
                code="structure_number_gap",
                identity_key="gap:1.1:1.2",
            ),
            identity_key="gap:1.1:1.2",
            code="structure_number_gap",
            severity=StructureFindingSeverity.WARNING,
            blocking=False,
            message_key="structure.number_gap",
            structure_node_id=target.structure_node_id,
            source_refs=target.source_refs,
        )
        provider = _PreviewProvider()

        with mock.patch(
            "app.application.extraction.build_llm_shadow_preview._scoped_structure_findings",
            return_value=(finding,),
        ):
            result = self._build(provider)

        self.assertEqual(result["llm_batch"]["status"], "COMPLETE")
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["pipeline"]["structure"]["status"], "REVIEW_REQUIRED")
        self.assertEqual(
            result["pipeline"]["requirement_view"]["status"], "REVIEW_REQUIRED"
        )

    def test_informational_source_arbitration_is_visible_without_false_review_gate(
        self,
    ) -> None:
        report = LocalDocumentPreflight().inspect(self.source)
        source_ir = build_source_ir(
            self.source,
            preflight_report=report,
            adapters=(PdfSourceIRAdapter(),),
        )
        tree = build_structure_tree(source_ir)
        target = next(item for item in tree.nodes if item.explicit_label == "1.1")
        code = "pdf_table_text_channel_conflict"
        identity_key = "finding:" + "a" * 64
        finding = StructureFinding(
            finding_id=stable_structure_finding_id(
                source_ir_id=tree.source_ir_id,
                resolver_id=tree.resolver_id,
                resolver_version=tree.resolver_version,
                code=code,
                identity_key=identity_key,
            ),
            identity_key=identity_key,
            code=code,
            severity=StructureFindingSeverity.INFO,
            blocking=False,
            message_key=f"structure.{code}",
            structure_node_id=target.structure_node_id,
            source_refs=target.source_refs,
        )
        provider = _PreviewProvider()

        with mock.patch(
            "app.application.extraction.build_llm_shadow_preview._scoped_structure_findings",
            return_value=(finding,),
        ):
            result = self._build(provider)

        self.assertEqual(result["llm_batch"]["status"], "COMPLETE")
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["pipeline"]["structure"]["status"], "COMPLETE")
        self.assertEqual(
            result["semantic_coverage"]["total_units"],
            len(result["semantic_terminal_receipts"]),
        )
        self.assertEqual(result["semantic_coverage"]["blocked_unit_count"], 0)
        self.assertEqual(result["semantic_coverage"]["unresolved_unit_count"], 0)
        self.assertEqual(
            result["pipeline"]["requirement_view"]["status"],
            "SHADOW_READY",
        )
        self.assertIn(
            code,
            {
                finding_code
                for item in result["items"]
                for finding_code in item["finding_codes"]
            },
        )
        self.assertIn(
            code,
            {item["code"] for item in result["findings"]},
        )

    def test_partial_batch_keeps_unresolved_provision_visible_for_review(self) -> None:
        provider = _PreviewProvider(timeout_labels={"1.2"})

        result = self._build(provider)

        self.assertEqual(result["llm_batch"]["status"], "PARTIAL")
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(result["counts"]["successful_provisions"], 2)
        self.assertEqual(result["counts"]["unresolved_provisions"], 1)
        unresolved_groups = [
            item
            for item in result["review_groups"]
            if item["llm_provision_status"] == "UNRESOLVED"
        ]
        self.assertEqual(len(unresolved_groups), 2)
        unresolved = unresolved_groups[0]
        self.assertEqual(unresolved["review_status"], "NEEDS_CLASSIFICATION")
        self.assertEqual(unresolved["llm_error_code"], "provider_timeout")
        self.assertGreater(
            unresolved["llm_provision_summary"]["scope_node_count"],
            0,
        )
        self.assertEqual(
            unresolved["llm_provision_summary"]["unresolved_semantic_count"],
            2,
        )
        self.assertTrue(unresolved["context_text"])
        self.assertIn("llm_semantic_unit_unresolved", unresolved["finding_codes"])
        finding = next(
            item
            for item in result["findings"]
            if item["code"] == "llm_semantic_unit_unresolved"
        )
        self.assertTrue(finding["blocking"])
        self.assertEqual(finding["llm_error_code"], "provider_timeout")
        self.assertEqual(
            finding["llm_provision_summary"],
            unresolved["llm_provision_summary"],
        )

    def test_one_unresolved_unit_keeps_resolved_peer_and_terminal_receipts(
        self,
    ) -> None:
        provider = _PreviewProvider(schema_invalid_content_labels={"1.2"})

        result = self._build(provider)

        self.assertEqual(result["llm_batch"]["status"], "PARTIAL")
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(len(result["items"]), 5)
        self.assertEqual(len(result["requirement_groups"]), 3)
        partial_group = next(
            item
            for item in result["requirement_groups"]
            if item["outline_label"] == "1.2"
        )
        self.assertEqual(partial_group["llm_provision_status"], "PARTIAL")
        self.assertEqual(partial_group["review_status"], "REQUIREMENT")
        unresolved_groups = [
            item
            for item in result["review_groups"]
            if item["llm_provision_status"] == "UNRESOLVED"
        ]
        self.assertEqual(len(unresolved_groups), 1)
        unresolved = unresolved_groups[0]
        self.assertEqual(unresolved["kind"], "SEMANTIC_UNIT_REVIEW")
        self.assertEqual(unresolved["review_status"], "NEEDS_CLASSIFICATION")
        self.assertEqual(
            unresolved["llm_error_code"],
            "provider_output_schema_invalid",
        )
        receipts = result["semantic_terminal_receipts"]
        self.assertEqual(len(receipts), result["counts"]["semantic_annotations"])
        self.assertEqual(
            sum(item["status"] == "UNRESOLVED" for item in receipts),
            1,
        )
        self.assertEqual(result["semantic_coverage"]["total_units"], len(receipts))
        self.assertEqual(result["semantic_coverage"]["blocked_unit_count"], 0)
        self.assertEqual(result["semantic_coverage"]["unresolved_unit_count"], 1)
        failed_receipt = next(
            item for item in receipts if item["status"] == "UNRESOLVED"
        )
        self.assertEqual(
            unresolved["semantic_unit_id"],
            failed_receipt["semantic_unit_id"],
        )
        self.assertEqual(unresolved["terminal_receipt"], failed_receipt)
        partial_provision = next(
            item
            for item in result["llm_batch"]["provisions"]
            if item["status"] == "UNRESOLVED"
        )
        self.assertEqual(
            partial_provision["semantic_terminal_receipts"],
            [
                item
                for item in receipts
                if item["semantic_unit_plan_id"]
                == partial_provision["semantic_terminal_receipts"][0][
                    "semantic_unit_plan_id"
                ]
            ],
        )
        self.assertEqual(
            len(result["llm_review"]["model_prediction"]["nodes"]),
            6,
        )

    def test_provider_preflight_rejection_is_receipted_and_peers_continue(
        self,
    ) -> None:
        provider = _PreviewProvider(preflight_block_labels={"1.2"})

        result = self._build(provider)

        self.assertEqual(result["llm_batch"]["status"], "PARTIAL")
        self.assertEqual(result["counts"]["blocked_provisions"], 0)
        self.assertEqual(result["counts"]["unresolved_provisions"], 1)
        self.assertEqual(
            result["semantic_coverage"]["total_units"],
            result["semantic_coverage"]["terminal_receipt_count"],
        )
        self.assertEqual(result["semantic_coverage"]["blocked_unit_count"], 0)
        self.assertEqual(result["semantic_coverage"]["unresolved_unit_count"], 2)
        unresolved = next(
            item
            for item in result["review_groups"]
            if item["llm_provision_status"] == "UNRESOLVED"
        )
        self.assertEqual(unresolved["review_status"], "NEEDS_CLASSIFICATION")
        self.assertEqual(
            unresolved["llm_error_code"],
            "semantic_evidence_budget_exceeded",
        )
        self.assertIn(
            "llm_semantic_unit_unresolved",
            unresolved["finding_codes"],
        )
        self.assertEqual(set(provider.preflight_labels), {"1.1", "1.2", "1.3"})
        self.assertEqual(provider.classification_call_count, 2)
        self.assertEqual(provider.assembly_call_count, 2)

    def test_late_assembly_rejection_preserves_text_free_terminal_receipts(
        self,
    ) -> None:
        provider = _PreviewProvider(assembly_runtime_block_labels={"1.2"})

        result = self._build(provider)

        unresolved = next(
            item
            for item in result["llm_batch"]["provisions"]
            if item["status"] == "UNRESOLVED"
        )
        self.assertEqual(unresolved["completed_provider_run_count"], 3)
        self.assertEqual(unresolved["unresolved_semantic_count"], 0)
        self.assertEqual(unresolved["unresolved_assembly_count"], 2)
        self.assertTrue(
            all(
                receipt["status"] == "RESOLVED"
                for receipt in unresolved["semantic_terminal_receipts"]
            )
        )
        review_group = next(
            item
            for item in result["review_groups"]
            if item["llm_provision_status"] == "UNRESOLVED"
        )
        self.assertEqual(review_group["review_status"], "NEEDS_CLASSIFICATION")
        self.assertIn(
            "llm_assembly_unit_unresolved",
            review_group["finding_codes"],
        )
        self.assertEqual(provider.classification_call_count, 3)
        self.assertGreater(provider.assembly_call_count, 3)

    def test_hash_mismatch_rejects_before_provider_calls(self) -> None:
        provider = _PreviewProvider()

        with self.assertRaises(LlmShadowPreviewRejected) as caught:
            build_llm_requirement_batch_preview(
                self.source,
                expected_source_sha256="0" * 64,
                section_paths=self.section_paths,
                provider=provider,
            )

        self.assertIs(
            caught.exception.code,
            LlmShadowPreviewRejectionCode.SOURCE_HASH_MISMATCH,
        )
        self.assertEqual(caught.exception.stage, "preflight")
        self.assertEqual(provider.classification_call_count, 0)
        self.assertEqual(provider.assembly_call_count, 0)
        self.assertFalse(provider.preflight_labels)

    def test_empty_paths_are_a_stable_request_rejection(self) -> None:
        provider = _PreviewProvider()

        with self.assertRaises(LlmShadowPreviewRejected) as caught:
            build_llm_requirement_batch_preview(
                self.source,
                expected_source_sha256=self.source_sha256,
                section_paths=(),
                provider=provider,
            )

        self.assertIs(
            caught.exception.code,
            LlmShadowPreviewRejectionCode.INVALID_REQUEST,
        )
        self.assertEqual(caught.exception.stage, "request")
        self.assertEqual(caught.exception.cause_code, "empty_section_paths")
        self.assertEqual(provider.classification_call_count, 0)

    def test_response_whitelist_excludes_provider_prompts_and_raw_responses(
        self,
    ) -> None:
        provider = _PreviewProvider()
        provider.secret_prompt = "DO-NOT-RETURN-SYNTHETIC-PROMPT"
        provider.raw_response = "DO-NOT-RETURN-SYNTHETIC-RAW-RESPONSE"

        result = self._build(provider)

        keys = _recursive_keys(result)
        self.assertFalse(
            {
                "prompt",
                "system_prompt",
                "user_prompt",
                "raw_response",
                "messages",
                "request_payload",
                "response_payload",
            }
            & keys
        )
        values = _recursive_string_values(result)
        self.assertNotIn(provider.secret_prompt, values)
        self.assertNotIn(provider.raw_response, values)
        self.assertNotIn("provider_runs", keys)
        self.assertNotIn("semantics", keys)
        self.assertNotIn("assembly", keys)

    def test_scoped_native_table_page_is_rendered_as_semantic_evidence_not_ocr(
        self,
    ) -> None:
        table_source = self.root / "table-requirement.pdf"
        _write_table_provision_pdf(table_source)
        source_sha256 = hashlib.sha256(table_source.read_bytes()).hexdigest()
        provider = _PreviewProvider()

        result = build_llm_requirement_batch_preview(
            table_source,
            expected_source_sha256=source_sha256,
            section_paths=(("2", "2.1"),),
            provider=provider,
        )

        self.assertEqual(result["llm_batch"]["semantic_image_page_numbers"], [1])
        self.assertEqual(len(provider.classification_requests), 1)
        request = provider.classification_requests[0]
        self.assertEqual([item.page_number for item in request.images], [1])
        self.assertEqual(request.images[0].source_sha256, source_sha256)
        self.assertEqual(len(result["tables"]), 1)
        image_cells = [cell for cell in result["tables"][0]["cells"] if cell["images"]]
        self.assertEqual(len(image_cells), 1)
        self.assertEqual(
            image_cells[0]["images"][0]["rendering"],
            {"mode": "SOURCE_PDF_CROP", "available": True},
        )
        self.assertEqual(
            result["source_identity"]["source_sha256"],
            source_sha256,
        )
        self.assertEqual(result["ocr"]["mode"], "OFF")
        self.assertEqual(result["ocr"]["observation_count"], 0)

    def test_cross_page_table_keeps_one_grounded_continuation_chain(self) -> None:
        table_source = self.root / "continued-table-requirement.pdf"
        _write_continued_table_provision_pdf(table_source)
        source_sha256 = hashlib.sha256(table_source.read_bytes()).hexdigest()

        result = build_llm_requirement_batch_preview(
            table_source,
            expected_source_sha256=source_sha256,
            section_paths=(("4", "4.1"),),
            provider=_PreviewProvider(),
        )

        self.assertEqual(len(result["tables"]), 2)
        first, second = sorted(
            result["tables"],
            key=lambda table: table["continuation"]["segment_index"],
        )
        self.assertEqual(
            [table["continuation"]["status"] for table in (first, second)],
            ["CONTINUED", "CONTINUED"],
        )
        self.assertEqual(
            [table["continuation"]["segment_count"] for table in (first, second)],
            [2, 2],
        )
        self.assertEqual(
            first["continuation"]["logical_table_id"],
            second["continuation"]["logical_table_id"],
        )
        self.assertEqual(first["continuation"]["next_table_id"], second["id"])
        self.assertEqual(second["continuation"]["previous_table_id"], first["id"])

    def test_native_only_visual_incomplete_scope_stops_before_all_provider_work(
        self,
    ) -> None:
        report = LocalDocumentPreflight().inspect(self.source)
        source_ir = build_source_ir(
            self.source,
            preflight_report=report,
            adapters=(PdfSourceIRAdapter(),),
        )
        diagnostic = SourceDiagnostic(
            code="pdf_visual_text_page_out_of_scope",
            message="Synthetic selected-page visual scope gap.",
            native_locator=source_ir.nodes[0].native_locator,
        )
        source_ir = source_ir.model_copy(
            update={"diagnostics": (*source_ir.diagnostics, diagnostic)}
        )
        provider = _PreviewProvider()

        with (
            mock.patch(
                "app.application.extraction.build_llm_shadow_preview.build_source_ir",
                return_value=source_ir,
            ),
            self.assertRaises(LlmShadowPreviewRejected) as caught,
        ):
            self._build(provider)

        self.assertIs(
            caught.exception.code,
            LlmShadowPreviewRejectionCode.SCOPED_VISUAL_INCOMPLETE,
        )
        self.assertEqual(caught.exception.stage, "scoped_visual_gate")
        self.assertEqual(
            caught.exception.cause_code,
            "pdf_visual_text_page_out_of_scope",
        )
        self.assertFalse(provider.preflight_labels)
        self.assertFalse(provider.assembly_preflight_labels)
        self.assertEqual(provider.classification_call_count, 0)
        self.assertEqual(provider.assembly_call_count, 0)

    def test_document_preview_uses_only_authorized_visual_adapter_and_binds_identity(
        self,
    ) -> None:
        report = LocalDocumentPreflight().inspect(self.source)
        source_ir = build_source_ir(
            self.source,
            preflight_report=report,
            adapters=(PdfSourceIRAdapter(),),
        )
        authorized, visual_adapter = _authorized_visual_runtime(self.source_sha256)
        source_ir = source_ir.model_copy(
            update={"adapter_version": visual_adapter.adapter_version}
        )
        partial_results: list[dict[str, object]] = []

        with (
            mock.patch(
                "app.application.extraction.build_llm_shadow_preview.build_source_ir",
                return_value=source_ir,
            ) as source_builder,
            mock.patch(
                "app.application.extraction.build_llm_shadow_preview."
                "validate_pdf_visual_text_capture"
            ) as capture_validator,
        ):
            result = build_llm_requirement_document_preview(
                self.source,
                expected_source_sha256=self.source_sha256,
                provider=_DocumentProvisionProvider(),
                partial_result_callback=partial_results.append,
                authorized_visual_runtime=authorized,
            )

        self.assertEqual(
            source_builder.call_args.kwargs["adapters"],
            (visual_adapter,),
        )
        capture_validator.assert_called_once_with(
            source_ir,
            authorized.runtime,
        )
        self.assertEqual(result["visual_strategy"]["mode"], "REMOTE_VISUAL")
        self.assertEqual(
            result["visual_strategy"]["route_sha256"],
            "2" * 64,
        )
        self.assertEqual(
            result["pipeline"]["source_ir"]["version"],
            visual_adapter.adapter_version,
        )
        self.assertEqual(
            partial_results[0]["visual_strategy"],
            result["visual_strategy"],
        )
        self.assertNotIn("source_sha256", result["visual_strategy"])

    def test_authorized_visual_route_rejects_declared_out_of_scope_pages(
        self,
    ) -> None:
        report = LocalDocumentPreflight().inspect(self.source)
        source_ir = build_source_ir(
            self.source,
            preflight_report=report,
            adapters=(PdfSourceIRAdapter(),),
        )
        diagnostic = SourceDiagnostic(
            code="pdf_visual_text_page_out_of_scope",
            message="Synthetic native-first page outside the authorized visual ceiling.",
            native_locator=source_ir.nodes[0].native_locator,
        )
        authorized, visual_adapter = _authorized_visual_runtime(self.source_sha256)
        source_ir = source_ir.model_copy(
            update={
                "adapter_version": visual_adapter.adapter_version,
                "diagnostics": (*source_ir.diagnostics, diagnostic),
            }
        )
        provider = _DocumentProvisionProvider()

        with (
            mock.patch(
                "app.application.extraction.build_llm_shadow_preview.build_source_ir",
                return_value=source_ir,
            ),
            mock.patch(
                "app.application.extraction.build_llm_shadow_preview."
                "validate_pdf_visual_text_capture"
            ) as capture_validator,
            self.assertRaises(LlmShadowPreviewRejected) as caught,
        ):
            build_llm_requirement_document_preview(
                self.source,
                expected_source_sha256=self.source_sha256,
                provider=provider,
                authorized_visual_runtime=authorized,
            )

        capture_validator.assert_called_once_with(
            source_ir,
            authorized.runtime,
        )
        self.assertIs(
            caught.exception.code,
            LlmShadowPreviewRejectionCode.SCOPED_VISUAL_INCOMPLETE,
        )
        self.assertEqual(caught.exception.stage, "scoped_visual_gate")
        self.assertEqual(
            caught.exception.cause_code,
            "pdf_visual_text_page_out_of_scope",
        )
        self.assertFalse(provider.preflight_labels)
        self.assertFalse(provider.assembly_preflight_labels)
        self.assertEqual(provider.classification_call_count, 0)
        self.assertEqual(provider.assembly_call_count, 0)

    def test_authorized_visual_route_still_rejects_provider_failure_before_semantics(
        self,
    ) -> None:
        report = LocalDocumentPreflight().inspect(self.source)
        source_ir = build_source_ir(
            self.source,
            preflight_report=report,
            adapters=(PdfSourceIRAdapter(),),
        )
        diagnostic = SourceDiagnostic(
            code="pdf_visual_text_provider_failed",
            message="Synthetic authorized-page visual provider failure.",
            native_locator=source_ir.nodes[0].native_locator,
        )
        visual_risk = SourceDiagnostic(
            code="pdf_full_page_image_without_visible_text",
            message="Synthetic visual routing reason.",
            native_locator=source_ir.nodes[0].native_locator,
        )
        authorized, visual_adapter = _authorized_visual_runtime(self.source_sha256)
        source_ir = source_ir.model_copy(
            update={
                "adapter_version": visual_adapter.adapter_version,
                "diagnostics": (*source_ir.diagnostics, visual_risk, diagnostic),
            }
        )
        provider = _DocumentProvisionProvider()

        with (
            mock.patch(
                "app.application.extraction.build_llm_shadow_preview.build_source_ir",
                return_value=source_ir,
            ),
            self.assertRaises(LlmShadowPreviewRejected) as caught,
        ):
            build_llm_requirement_document_preview(
                self.source,
                expected_source_sha256=self.source_sha256,
                provider=provider,
                authorized_visual_runtime=authorized,
            )

        self.assertIs(
            caught.exception.code,
            LlmShadowPreviewRejectionCode.VISUAL_CAPTURE_REJECTED,
        )
        self.assertEqual(
            caught.exception.stage,
            "visual_capture_validation",
        )
        self.assertEqual(
            caught.exception.cause_code,
            "pdf_visual_text_provider_failed",
        )
        self.assertFalse(provider.preflight_labels)
        self.assertFalse(provider.assembly_preflight_labels)
        self.assertEqual(provider.classification_call_count, 0)
        self.assertEqual(provider.assembly_call_count, 0)

    def test_authorized_visual_capture_validation_fails_before_semantics(
        self,
    ) -> None:
        report = LocalDocumentPreflight().inspect(self.source)
        source_ir = build_source_ir(
            self.source,
            preflight_report=report,
            adapters=(PdfSourceIRAdapter(),),
        )
        authorized, visual_adapter = _authorized_visual_runtime(self.source_sha256)
        source_ir = source_ir.model_copy(
            update={"adapter_version": visual_adapter.adapter_version}
        )
        provider = _DocumentProvisionProvider()

        with (
            mock.patch(
                "app.application.extraction.build_llm_shadow_preview.build_source_ir",
                return_value=source_ir,
            ),
            mock.patch(
                "app.application.extraction.build_llm_shadow_preview."
                "validate_pdf_visual_text_capture",
                side_effect=RuntimeError("pdf_visual_text_capture_page_hash_mismatch"),
            ),
            self.assertRaises(LlmShadowPreviewRejected) as caught,
        ):
            build_llm_requirement_document_preview(
                self.source,
                expected_source_sha256=self.source_sha256,
                provider=provider,
                authorized_visual_runtime=authorized,
            )

        self.assertIs(
            caught.exception.code,
            LlmShadowPreviewRejectionCode.VISUAL_CAPTURE_REJECTED,
        )
        self.assertEqual(
            caught.exception.cause_code,
            "pdf_visual_text_capture_page_hash_mismatch",
        )
        self.assertEqual(provider.classification_call_count, 0)
        self.assertEqual(provider.assembly_call_count, 0)

    def test_unknown_provider_exception_is_not_disguised(self) -> None:
        provider = _PreviewProvider(raise_labels={"1.2"})

        with self.assertRaisesRegex(RuntimeError, "synthetic provider failure"):
            self._build(provider)

    def _build(self, provider: _PreviewProvider) -> dict[str, object]:
        return build_llm_requirement_batch_preview(
            self.source,
            expected_source_sha256=self.source_sha256,
            section_paths=self.section_paths,
            provider=provider,
        )


class _PreviewProvider(_ProvisionProvider):
    """The batch fake also satisfies the mandatory request-preflight port."""


def _authorized_visual_runtime(
    source_sha256: str,
) -> tuple[AuthorizedPdfVisualRuntime, object]:
    adapter = SimpleNamespace(
        adapter_version=f"{PDF_SOURCE_IR_ADAPTER_VERSION}+visual.{'1' * 12}"
    )
    provider = SimpleNamespace(
        provider_id=visual_text_provider_id("bailian"),
        provider_version="test-visual-provider/v1",
    )
    runtime = SimpleNamespace(
        pdf_adapter=adapter,
        visual_text_provider=provider,
        visual_text_page_numbers=frozenset({1}),
        route=SimpleNamespace(
            source_sha256=source_sha256,
            page_numbers=frozenset({1}),
        ),
    )
    route = SimpleNamespace(
        source_sha256=source_sha256,
        route_id=f"pvr-{'2' * 32}",
        route_sha256="2" * 64,
    )
    return (
        AuthorizedPdfVisualRuntime(
            provider_name="bailian",
            route=route,
            runtime=runtime,
        ),
        adapter,
    )


class _DocumentStructureAuditProvider(_DocumentProvisionProvider):
    def __init__(self) -> None:
        super().__init__()
        self.audit_call_count = 0

    def audit_structure(self, request):  # type: ignore[no-untyped-def]
        self.audit_call_count += 1
        target = next(
            node
            for node in request.evidence_request.nodes
            if node.explicit_label == "1.2"
        )
        related = next(
            node
            for node in request.evidence_request.nodes
            if node.explicit_label == "1.3"
        )
        run = _run(
            "audit_structure",
            SemanticProviderRunStatus.SUCCESS,
            nonce=self.audit_call_count,
        ).model_copy(update={"provider_version": self.provider_version})
        return StructureAuditProviderResult(
            findings=(
                StructureAuditDecision(
                    category=StructureAuditCategory.POSSIBLE_MISSED_BOUNDARY,
                    target_structure_node_id=target.target_structure_node_id,
                    related_structure_node_ids=(related.target_structure_node_id,),
                    evidence_source_refs=(
                        target.evidence[0].source_ref,
                        related.evidence[0].source_ref,
                    ),
                    reason_codes=("synthetic_numbered_content_boundary",),
                ),
            ),
            run=run,
        )


class _UnresolvedDocumentStructureAuditProvider(_DocumentProvisionProvider):
    def audit_structure(self, _request):  # type: ignore[no-untyped-def]
        run = _run(
            "audit_structure",
            SemanticProviderRunStatus.TIMEOUT,
            nonce=1,
        ).model_copy(update={"provider_version": self.provider_version})
        return StructureAuditProviderResult(findings=(), run=run)


class _DocumentStructureRepairProvider(_DocumentProvisionProvider):
    def audit_structure(self, request):  # type: ignore[no-untyped-def]
        target = next(
            node
            for node in request.evidence_request.nodes
            if node.explicit_label == "1.3"
        )
        new_parent = next(
            node
            for node in request.evidence_request.nodes
            if node.explicit_label == "1.2"
        )
        run = _run(
            "audit_structure",
            SemanticProviderRunStatus.SUCCESS,
            nonce=1,
        ).model_copy(update={"provider_version": self.provider_version})
        return StructureAuditProviderResult(
            findings=(
                StructureAuditDecision(
                    category=StructureAuditCategory.POSSIBLE_MISPLACED_PARENT,
                    target_structure_node_id=target.target_structure_node_id,
                    related_structure_node_ids=(new_parent.target_structure_node_id,),
                    evidence_source_refs=(
                        target.evidence[0].source_ref,
                        new_parent.evidence[0].source_ref,
                    ),
                    reason_codes=("synthetic_parent_conflict",),
                    proposed_operations=(
                        ReparentStructureOperationV1(
                            structure_node_id=target.target_structure_node_id,
                            new_parent_structure_node_id=(
                                new_parent.target_structure_node_id
                            ),
                            new_sibling_index=1,
                        ),
                    ),
                    proposal_evidence=StructureAuditProposalEvidence.HIGH,
                ),
            ),
            run=run,
        )


def _write_three_provision_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page()
    lines = (
        ("1 Product requirements", 16),
        ("1.1 First requirement", 14),
        ("The first product shall operate.", 11),
        ("1.2 Second requirement", 14),
        ("The second product shall operate.", 11),
        ("1.3 Third requirement", 14),
        ("The third product shall operate.", 11),
    )
    for index, (text, font_size) in enumerate(lines):
        page.insert_text((72, 72 + index * 35), text, fontsize=font_size)
    document.save(path)
    document.close()


def _write_table_provision_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=360, height=360)
    page.insert_text((40, 30), "2 Requirements", fontsize=14)
    page.insert_text(
        (40, 55),
        "2.1 The supplier shall meet all values in Table 1.",
        fontsize=12,
    )
    left = 40
    top = 100
    cell_width = 130
    cell_height = 50
    for row in range(3):
        page.draw_line(
            (left, top + row * cell_height),
            (left + 2 * cell_width, top + row * cell_height),
        )
    for column in range(3):
        page.draw_line(
            (left + column * cell_width, top),
            (left + column * cell_width, top + 2 * cell_height),
        )
    for x, y, text in (
        (50, 125, "Material"),
        (180, 125, "Threshold"),
        (50, 175, "Steel"),
    ):
        page.insert_text((x, y), text, fontsize=10)
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
    pixmap.clear_with(0x00A060)
    page.insert_image(
        fitz.Rect(190, 157, 220, 187),
        stream=pixmap.tobytes("png"),
    )
    document.save(path)
    document.close()


def _write_continued_table_provision_pdf(path: Path) -> None:
    document = fitz.open()
    first_page = document.new_page(width=360, height=320)
    first_page.insert_text((40, 28), "4 Requirements", fontsize=15)
    first_page.insert_text((40, 52), "4.1 Continued table requirement", fontsize=13)
    _draw_two_column_table(
        first_page,
        top=140,
        rows=(("Item", "Value"), ("A", "Page 1"), ("B", "Tail 1")),
    )
    second_page = document.new_page(width=360, height=320)
    _draw_two_column_table(
        second_page,
        top=8,
        rows=(("Item", "Value"), ("C", "Page 2"), ("D", "Tail 2")),
    )
    document.save(path)
    document.close()


def _draw_two_column_table(
    page: fitz.Page,
    *,
    top: float,
    rows: tuple[tuple[str, str], ...],
) -> None:
    left = 40
    column_width = 130
    row_height = 55
    for row in range(len(rows) + 1):
        page.draw_line(
            (left, top + row * row_height),
            (left + 2 * column_width, top + row * row_height),
        )
    for column in range(3):
        page.draw_line(
            (left + column * column_width, top),
            (left + column * column_width, top + len(rows) * row_height),
        )
    for row, values in enumerate(rows):
        for column, value in enumerate(values):
            page.insert_text(
                (
                    left + column * column_width + 6,
                    top + row * row_height + 24,
                ),
                value,
                fontsize=9,
            )


def _recursive_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {
            *(str(key) for key in value),
            *(key for child in value.values() for key in _recursive_keys(child)),
        }
    if isinstance(value, list):
        return {key for child in value for key in _recursive_keys(child)}
    return set()


def _recursive_string_values(value: object) -> set[str]:
    if isinstance(value, dict):
        return {
            item for child in value.values() for item in _recursive_string_values(child)
        }
    if isinstance(value, list):
        return {item for child in value for item in _recursive_string_values(child)}
    return {value} if isinstance(value, str) else set()


if __name__ == "__main__":
    unittest.main()
