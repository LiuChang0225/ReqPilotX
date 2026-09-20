from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from app.application.extraction.document_parent_boundaries import (
    DocumentParentBoundaryProviderResult,
    build_document_parent_boundary_plans,
    build_parent_boundary_target_windows,
)
from app.application.extraction.native_parent_boundary_reference import (
    NativeParentBoundaryReferenceV1,
    NativeParentBoundaryStatus,
    NativeProvisionDisposition,
    NativeProvisionBlockV1,
    validate_native_parent_boundary_reference,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.application.extraction.revise_structure import get_base_effective_structure
from app.domain.extraction.semantic_provider import SemanticProviderRunStatus
from app.domain.extraction.source_ir import SourceNodeKind
from app.domain.extraction.structure import StructureNodeKind
from app.ports.requirement_atomization_provider import RequirementAtomizationRange
from tests.test_llm_semantics import _run
from tests.test_semantics import _flat_source


class DocumentParentBoundaryPlanningTests(unittest.TestCase):
    def test_agent_parent_ranges_project_to_exact_source_order_windows(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Scope"),
                (SourceNodeKind.PARAGRAPH, "The product shall operate."),
                (SourceNodeKind.HEADING, "2 Verification"),
                (SourceNodeKind.PARAGRAPH, "The test shall be recorded."),
            )
        )
        base = build_structure_tree(source)
        tree = get_base_effective_structure(source, base)
        scope = tuple(
            node.structure_node_id
            for node in tree.nodes
            if node.kind is not StructureNodeKind.DOCUMENT
        )

        plans = build_document_parent_boundary_plans(source)
        self.assertEqual(len(plans), 1)
        request = plans[0].request
        segment = request.source_segments[0]
        reference = NativeParentBoundaryReferenceV1(
            source_sha256=request.source_sha256,
            scope_id=request.scope_id,
            status=NativeParentBoundaryStatus.OBSERVED,
            provisions=(
                NativeProvisionBlockV1(
                    provision_id="native-provision-" + "1" * 32,
                    explicit_label="1",
                    source_order=0,
                    disposition=NativeProvisionDisposition.REQUIREMENT,
                    body_ranges=(
                        RequirementAtomizationRange(
                            segment_index=0,
                            start_offset=0,
                            end_offset=len(segment.source_text),
                        ),
                    ),
                    reason_codes=("complete_parent_provision",),
                ),
            ),
        )
        receipt = validate_native_parent_boundary_reference(request, reference)
        result = DocumentParentBoundaryProviderResult(
            reference=reference,
            validation_receipt=receipt,
            run=_run(
                "identify_parent_boundaries",
                SemanticProviderRunStatus.SUCCESS,
                nonce=1,
            ),
        )

        windows = build_parent_boundary_target_windows(
            plans,
            (result,),
            tree,
            scope,
        )

        self.assertEqual(
            tuple(item for window in windows for item in window),
            scope,
        )
        self.assertEqual(len(windows), 1)
        self.assertTrue(plans[0].source_ref_spans)


if __name__ == "__main__":
    unittest.main()
