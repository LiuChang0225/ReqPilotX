from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from app.application.extraction.agent_native_document import (
    AgentNativeDocumentStatus,
    AgentNativeWorkflowMode,
    extract_agent_native_document_with_provider,
    serialize_agent_native_document_result,
)
from app.application.extraction.build_llm_shadow_preview import (
    build_llm_requirement_document_preview,
)
from app.application.extraction.document_decomposition import (
    DocumentDecompositionItemReferenceV1,
    DocumentDecompositionProviderResult,
    DocumentDecompositionReferenceV1,
    validate_document_decomposition_reference,
)
from app.application.extraction.document_parent_boundaries import (
    DocumentParentBoundaryProviderResult,
)
from app.application.extraction.native_atomization_reference import (
    NativeAtomizationReferenceStatus,
)
from app.application.extraction.native_parent_boundary_reference import (
    NativeParentBoundaryReferenceV1,
    NativeParentBoundaryStatus,
    NativeParentContextDisposition,
    NativeParentContextRangeV1,
    NativeProvisionBlockV1,
    NativeProvisionDisposition,
    validate_native_parent_boundary_reference,
)
from app.domain.extraction.semantic_provider import SemanticProviderRunStatus
from app.domain.extraction.source_ir import SourceNodeKind
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationObligation,
    RequirementAtomizationRange,
)
from app.ports.semantic_provider import SemanticImageInput
from tests.test_llm_semantics import _run
from tests.test_semantics import _flat_source


def _png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (1).to_bytes(4, "big")
        + (1).to_bytes(4, "big")
    )


class _AgentNativeProvider:
    provider_id = "synthetic-qwen-provider"
    provider_version = "1.0.0"
    model_id = "qwen3.7-plus"

    def __init__(self) -> None:
        self.prepared_structure_identity = ""

    def prepare_document_session(
        self,
        *,
        source_ir_id: str,
        structure_snapshot_id: str,
        images: tuple[SemanticImageInput, ...],
    ) -> None:
        del source_ir_id, images
        self.prepared_structure_identity = structure_snapshot_id

    def close_document_session(self) -> None:
        return None

    def preflight_classification_request(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("legacy node classification must not run")

    def classify_nodes(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("legacy node classification must not run")

    def assemble_requirements(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("legacy node assembly must not run")

    def identify_parent_boundaries(self, request):  # type: ignore[no-untyped-def]
        reference = NativeParentBoundaryReferenceV1(
            source_sha256=request.source_sha256,
            scope_id=request.scope_id,
            status=NativeParentBoundaryStatus.OBSERVED,
            provisions=(
                NativeProvisionBlockV1(
                    provision_id="native-provision-" + "a" * 32,
                    explicit_label="1",
                    source_order=0,
                    disposition=NativeProvisionDisposition.REQUIREMENT,
                    body_ranges=tuple(
                        RequirementAtomizationRange(
                            segment_index=segment.segment_index,
                            start_offset=0,
                            end_offset=len(segment.source_text),
                        )
                        for segment in request.source_segments
                    ),
                    reason_codes=("complete_source_bound_parent",),
                ),
            ),
        )
        return DocumentParentBoundaryProviderResult(
            reference=reference,
            validation_receipt=validate_native_parent_boundary_reference(
                request,
                reference,
            ),
            run=_run(
                "identify_parent_boundaries",
                SemanticProviderRunStatus.SUCCESS,
                nonce=1,
            ),
        )

    def decompose_document_parents(self, request):  # type: ignore[no-untyped-def]
        reference = DocumentDecompositionReferenceV1(
            source_sha256=request.source_sha256,
            items=tuple(
                DocumentDecompositionItemReferenceV1(
                    parent_provision_id=item.parent_provision_id,
                    lineage_sha256=item.lineage_sha256,
                    status=NativeAtomizationReferenceStatus.OBSERVED,
                    obligations=(
                        RequirementAtomizationObligation(
                            source_ranges=tuple(
                                RequirementAtomizationRange(
                                    segment_index=segment.segment_index,
                                    start_offset=0,
                                    end_offset=len(segment.source_text),
                                )
                                for segment in item.source_segments
                            ),
                            atomic_statement=" ".join(
                                segment.source_text for segment in item.source_segments
                            ),
                            split_basis="single_or_descriptive",
                            reason_codes=("single_complete_obligation",),
                        ),
                    ),
                )
                for item in request.items
            ),
        )
        return DocumentDecompositionProviderResult(
            reference=reference,
            validation_receipts=validate_document_decomposition_reference(
                request,
                reference,
            ),
            run=_run(
                "decompose_document_parents",
                SemanticProviderRunStatus.SUCCESS,
                nonce=2,
            ),
        )


class _ContextAwareAgentNativeProvider(_AgentNativeProvider):
    def identify_parent_boundaries(self, request):  # type: ignore[no-untyped-def]
        segment = request.source_segments[0]
        heading_end = segment.source_text.index("\n")
        reference = NativeParentBoundaryReferenceV1(
            source_sha256=request.source_sha256,
            scope_id=request.scope_id,
            status=NativeParentBoundaryStatus.OBSERVED,
            provisions=(
                NativeProvisionBlockV1(
                    provision_id="native-provision-" + "b" * 32,
                    explicit_label="1",
                    source_order=0,
                    disposition=NativeProvisionDisposition.REQUIREMENT,
                    body_ranges=(
                        RequirementAtomizationRange(
                            segment_index=0,
                            start_offset=heading_end + 1,
                            end_offset=len(segment.source_text),
                        ),
                    ),
                    reason_codes=("source_bound_requirement",),
                ),
            ),
            context_ranges=(
                NativeParentContextRangeV1(
                    source_range=RequirementAtomizationRange(
                        segment_index=0,
                        start_offset=0,
                        end_offset=heading_end,
                    ),
                    disposition=NativeParentContextDisposition.HEADING,
                    reason_codes=("document_heading",),
                ),
            ),
        )
        return DocumentParentBoundaryProviderResult(
            reference=reference,
            validation_receipt=validate_native_parent_boundary_reference(
                request,
                reference,
            ),
            run=_run(
                "identify_parent_boundaries",
                SemanticProviderRunStatus.SUCCESS,
                nonce=3,
            ),
        )


class AgentNativeDocumentTests(unittest.TestCase):
    def test_source_baseline_keeps_context_and_requirement_in_document_order(
        self,
    ) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Scope"),
                (SourceNodeKind.PARAGRAPH, "The component shall operate."),
            )
        )
        image_bytes = _png()

        result = extract_agent_native_document_with_provider(
            source,
            _ContextAwareAgentNativeProvider(),
            images=(
                SemanticImageInput(
                    source_sha256=source.source_sha256,
                    page_number=1,
                    width_px=1,
                    height_px=1,
                    image_bytes=image_bytes,
                    image_sha256=hashlib.sha256(image_bytes).hexdigest(),
                ),
            ),
        )
        serialized = serialize_agent_native_document_result(result)

        self.assertEqual(
            [item["role"] for item in serialized["review_groups"]],
            ["CONTEXT", "PRIMARY"],
        )
        self.assertEqual(serialized["review_groups"][0]["text"], "1 Scope")
        self.assertEqual(
            serialized["review_groups"][0]["requirementness"],
            "NON_REQUIREMENT",
        )
        self.assertEqual(len(serialized["requirement_groups"]), 1)
        self.assertEqual(serialized["baseline"]["entry_count"], 2)
        self.assertEqual(serialized["baseline"]["context_entry_count"], 1)
        self.assertTrue(serialized["baseline"]["exact_cover_visible"])
        self.assertFalse(serialized["baseline"]["classification_policy_attested"])

    def test_source_span_path_has_no_structure_or_requirement_view_owner(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Scope"),
                (SourceNodeKind.PARAGRAPH, "The component shall operate."),
            )
        )
        image_bytes = _png()
        provider = _AgentNativeProvider()
        progress = []

        result = extract_agent_native_document_with_provider(
            source,
            provider,
            images=(
                SemanticImageInput(
                    source_sha256=source.source_sha256,
                    page_number=1,
                    width_px=1,
                    height_px=1,
                    image_bytes=image_bytes,
                    image_sha256=hashlib.sha256(image_bytes).hexdigest(),
                ),
            ),
            progress_callback=progress.append,
        )
        serialized = serialize_agent_native_document_result(result)
        contract = json.dumps(
            {
                "parent_requests": [
                    item.request.model_dump(mode="json") for item in result.parent_plans
                ],
                "decomposition_requests": [
                    item.model_dump(mode="json")
                    for item in result.decomposition_requests
                ],
                "read_model": serialized,
            },
            sort_keys=True,
        )

        self.assertIs(result.status, AgentNativeDocumentStatus.COMPLETE)
        self.assertEqual(result.parent_count, 1)
        self.assertIs(result.workflow_mode, AgentNativeWorkflowMode.BASELINE)
        self.assertEqual(result.obligation_count, 0)
        self.assertEqual(serialized["requirement_decomposition"], [])
        self.assertEqual(serialized["atomization"]["state"], "NOT_REQUESTED")
        self.assertEqual(len(serialized["requirement_groups"]), 1)
        self.assertNotIn("structure_node", contract)
        self.assertNotIn("requirement_view_node", contract)
        self.assertNotIn("semantic_unit", contract)
        self.assertTrue(
            provider.prepared_structure_identity.startswith("agent-source-session-")
        )
        self.assertEqual(progress[-1].total_targets, 1)
        self.assertEqual(progress[-1].progress_basis, "PAGE_EVIDENCE")
        self.assertEqual(progress[-1].completed_targets, 1)

    def test_atomization_only_runs_when_explicitly_requested(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Scope"),
                (SourceNodeKind.PARAGRAPH, "The component shall operate."),
            )
        )
        image_bytes = _png()
        result = extract_agent_native_document_with_provider(
            source,
            _AgentNativeProvider(),
            images=(
                SemanticImageInput(
                    source_sha256=source.source_sha256,
                    page_number=1,
                    width_px=1,
                    height_px=1,
                    image_bytes=image_bytes,
                    image_sha256=hashlib.sha256(image_bytes).hexdigest(),
                ),
            ),
            atomize=True,
        )
        serialized = serialize_agent_native_document_result(result)

        self.assertIs(
            result.workflow_mode,
            AgentNativeWorkflowMode.EXPLICIT_ATOMIZATION,
        )
        self.assertEqual(result.obligation_count, 1)
        self.assertEqual(len(serialized["requirement_decomposition"]), 1)
        self.assertEqual(serialized["atomization"]["state"], "APPLIED")

    def test_product_document_preview_bypasses_legacy_scope_locator(self) -> None:
        provider = _AgentNativeProvider()
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "document.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "1 Scope")
            page.insert_text((72, 96), "The component shall operate.")
            document.save(path)
            document.close()
            source_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

            with patch(
                "app.application.extraction.build_llm_shadow_preview."
                "locate_llm_document_scope",
                side_effect=AssertionError("legacy scope locator must not run"),
            ):
                result = build_llm_requirement_document_preview(
                    path,
                    expected_source_sha256=source_sha256,
                    provider=provider,
                )

        self.assertEqual(result["llm_batch"]["strategy"], "AGENT_NATIVE_DOCUMENT")
        self.assertEqual(result["llm_batch"]["workflow_mode"], "BASELINE")
        self.assertEqual(result["presentation"]["default_collection"], "review_groups")
        self.assertTrue(result["baseline"]["source_text_preserved"])
        self.assertEqual(result["atomization"]["state"], "NOT_REQUESTED")
        self.assertEqual(result["requirement_decomposition"], [])
        self.assertFalse(result["llm_batch"]["agent_contract"]["legacy_scope_used"])
        self.assertNotIn("locator", result["llm_batch"])
        self.assertEqual(result["counts"]["legacy_scope_nodes"], 0)
        self.assertFalse(result["pipeline"]["structure"]["used_as_agent_boundary"])


if __name__ == "__main__":
    unittest.main()
