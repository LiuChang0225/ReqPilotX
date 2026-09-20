from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import unittest

from app.application.extraction.native_semantic_consensus import (
    NativeSemanticConsensusStatus,
    build_native_semantic_consensus,
    validate_native_semantic_decisions,
)
from app.domain.extraction.semantics import RequirementSubtype, SemanticClass
from app.domain.extraction.structure import (
    StructureNodeKind,
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.ports.semantic_provider import (
    SemanticClassificationRequest,
    SemanticDecision,
    SemanticEvidenceInput,
    SemanticNodeInput,
)


TARGET_ID = "structure-node-" + "1" * 32
SOURCE_REF = StructureSourceRef(
    kind=StructureSourceRefKind.SOURCE_NODE,
    source_id="node-" + "2" * 32,
)


def _request() -> SemanticClassificationRequest:
    text = "The component shall satisfy the limit."
    evidence = SemanticEvidenceInput(
        source_ref=SOURCE_REF,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
    )
    return SemanticClassificationRequest(
        source_ir_id="source-ir-test",
        source_sha256="3" * 64,
        structure_snapshot_id="structure-test",
        scope_structure_node_ids=(TARGET_ID,),
        nodes=(
            SemanticNodeInput(
                target_structure_node_id=TARGET_ID,
                source_order=0,
                kind=StructureNodeKind.CLAUSE,
                target_source_refs=(SOURCE_REF,),
                evidence=(evidence,),
            ),
        ),
    )


def _decision(
    semantic_class: SemanticClass,
) -> SemanticDecision:
    return SemanticDecision(
        target_structure_node_id=TARGET_ID,
        semantic_class=semantic_class,
        requirement_subtype=(
            RequirementSubtype.PRODUCT_REQUIREMENT
            if semantic_class is SemanticClass.REQUIREMENT
            else None
        ),
        evidence_source_refs=(SOURCE_REF,),
        reason_codes=("source_bound_decision",),
    )


class NativeSemanticConsensusTests(unittest.TestCase):
    def test_one_validator_safe_run_is_a_complete_primary_observation(self) -> None:
        decisions, report = build_native_semantic_consensus(
            _request(),
            ((_decision(SemanticClass.REQUIREMENT),),),
        )

        self.assertEqual(decisions[0].semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(report.run_count, 1)
        self.assertEqual(report.consensus_threshold, 1)
        self.assertEqual(report.targets[0].consensus_count, 1)

    def test_strict_majority_produces_requirement_decision(self) -> None:
        request = _request()

        decisions, report = build_native_semantic_consensus(
            request,
            (
                (_decision(SemanticClass.REQUIREMENT),),
                (_decision(SemanticClass.HEADING),),
                (_decision(SemanticClass.REQUIREMENT),),
            ),
        )

        self.assertEqual(decisions[0].semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(report.status, NativeSemanticConsensusStatus.COMPLETE)
        self.assertEqual(report.targets[0].consensus_count, 2)

    def test_no_majority_becomes_source_bound_unresolved(self) -> None:
        request = _request()

        decisions, report = build_native_semantic_consensus(
            request,
            (
                (_decision(SemanticClass.REQUIREMENT),),
                (_decision(SemanticClass.HEADING),),
                (_decision(SemanticClass.BACKGROUND),),
            ),
        )

        self.assertEqual(decisions[0].semantic_class, SemanticClass.UNRESOLVED)
        self.assertEqual(decisions[0].evidence_source_refs, (SOURCE_REF,))
        self.assertEqual(report.status, NativeSemanticConsensusStatus.PARTIAL)
        self.assertEqual(report.unresolved_target_count, 1)

    def test_out_of_scope_evidence_is_rejected(self) -> None:
        invalid_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id="node-" + "4" * 32,
        )
        invalid = _decision(SemanticClass.REQUIREMENT).model_copy(
            update={"evidence_source_refs": (invalid_ref,)}
        )

        with self.assertRaisesRegex(ValueError, "outside its target"):
            validate_native_semantic_decisions(_request(), (invalid,))

    def test_grounded_non_requirement_cannot_omit_available_evidence(self) -> None:
        invalid = SemanticDecision(
            target_structure_node_id=TARGET_ID,
            semantic_class=SemanticClass.HEADING,
            reason_codes=("heading_context",),
        )

        with self.assertRaisesRegex(ValueError, "terminal semantic decisions"):
            validate_native_semantic_decisions(_request(), (invalid,))


if __name__ == "__main__":
    unittest.main()
