from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from app.application.extraction.classify_content import classify_content
from app.application.extraction.orchestrate_structure_agent import (
    StructureProposalDisposition,
    apply_manual_structure_operations,
    derive_structure_derivation_scope,
    run_structure_repair_with_provider,
    undo_current_structure_revision,
)
from app.application.extraction.audit_structure import StructureAuditStatus
from app.application.extraction.project_requirements import project_requirements
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.source_ir import SourceIRStatus, SourceNodeKind
from app.domain.extraction.structure import StructureRelationKind
from app.domain.extraction.structure_revision import (
    AddStructureRelationOperationV1,
    BoundarySplitPartV1,
    BoundarySplitStructureOperationV1,
    ContinuationMergeStructureOperationV1,
    ReparentStructureOperationV1,
    StructureRevisionRejected,
    StructureRevisionRejectionCode,
    StructureSourceSpanV1,
)
from app.ports.structure_audit_provider import (
    StructureAuditCategory,
    StructureAuditDecision,
    StructureAuditProposalEvidence,
)
from tests.test_structure_audit import _FakeAuditProvider
from tests.test_structure_revision import (
    _source_ir_fixture,
    _structure_node_for_source,
)
from tests.test_semantics import _flat_source


class StructureAgentOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_ir = _source_ir_fixture()
        self.base_tree = build_structure_tree(self.source_ir)
        self.nodes_by_text = {
            source_node.raw_text: _structure_node_for_source(
                self.base_tree.nodes,
                source_node.source_node_id,
            )
            for source_node in self.source_ir.nodes
        }
        self.sections_by_label = {
            node.explicit_label: node
            for node in self.base_tree.nodes
            if node.explicit_label is not None
        }

    def test_incomplete_source_leaves_old_provider_repair_unresolved(self) -> None:
        provider = _FakeAuditProvider(lambda _request: ())
        incomplete = self.source_ir.model_copy(
            update={"status": SourceIRStatus.PARTIAL},
        )

        result = run_structure_repair_with_provider(
            incomplete,
            self.base_tree,
            provider,
        )

        self.assertEqual(result.audit_error_code, "source_ir_not_complete")
        self.assertIsNone(result.audit)
        self.assertEqual(result.status.value, "UNRESOLVED")
        self.assertEqual(result.revisions, ())
        self.assertEqual(result.effective_structure.revision_number, 0)
        self.assertEqual(provider.requests, [])

    def test_legal_ai_proposal_is_validated_and_auto_applied(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        new_parent = self.sections_by_label["1.2"]
        provider = _FakeAuditProvider(
            lambda request: (
                _reparent_decision(
                    request,
                    target.structure_node_id,
                    new_parent.structure_node_id,
                ),
            )
        )

        result = run_structure_repair_with_provider(
            self.source_ir,
            self.base_tree,
            provider,
        )

        self.assertEqual(result.effective_structure.revision_number, 1)
        self.assertEqual(len(result.revisions), 1)
        self.assertEqual(
            result.proposal_outcomes[0].disposition,
            StructureProposalDisposition.APPLIED,
        )
        effective_target = next(
            node
            for node in result.effective_structure.nodes
            if node.structure_node_id == target.structure_node_id
        )
        self.assertEqual(
            effective_target.parent_structure_node_id,
            new_parent.structure_node_id,
        )

    def test_illegal_ai_proposal_retains_finding_and_current_revision(self) -> None:
        target = self.sections_by_label["1"]
        descendant = self.sections_by_label["1.2"]
        provider = _FakeAuditProvider(
            lambda request: (
                _reparent_decision(
                    request,
                    target.structure_node_id,
                    descendant.structure_node_id,
                ),
            )
        )

        result = run_structure_repair_with_provider(
            self.source_ir,
            self.base_tree,
            provider,
        )

        self.assertEqual(len(result.audit.findings), 1)  # type: ignore[union-attr]
        self.assertEqual(result.effective_structure.revision_number, 0)
        self.assertEqual(result.revisions, ())
        self.assertEqual(
            result.proposal_outcomes[0].disposition,
            StructureProposalDisposition.REJECTED,
        )
        self.assertEqual(
            result.proposal_outcomes[0].rejection_code,
            StructureRevisionRejectionCode.GRAPH_CYCLE.value,
        )

    def test_mixed_valid_and_invalid_ai_proposals_are_isolated(self) -> None:
        valid_target = self.nodes_by_text["Alpha one. Alpha two."]
        new_parent = self.sections_by_label["1.2"]
        invalid_target = self.sections_by_label["1"]
        descendant = self.sections_by_label["1.2"]
        provider = _FakeAuditProvider(
            lambda request: (
                _reparent_decision(
                    request,
                    valid_target.structure_node_id,
                    new_parent.structure_node_id,
                ),
                _reparent_decision(
                    request,
                    invalid_target.structure_node_id,
                    descendant.structure_node_id,
                ),
            )
        )

        result = run_structure_repair_with_provider(
            self.source_ir,
            self.base_tree,
            provider,
        )

        self.assertEqual(result.effective_structure.revision_number, 1)
        self.assertEqual(
            tuple(item.disposition for item in result.proposal_outcomes),
            (
                StructureProposalDisposition.APPLIED,
                StructureProposalDisposition.REJECTED,
            ),
        )
        self.assertEqual(len(result.revisions), 1)

    def test_low_evidence_proposal_never_changes_current_revision(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        new_parent = self.sections_by_label["1.2"]
        provider = _FakeAuditProvider(
            lambda request: (
                _reparent_decision(
                    request,
                    target.structure_node_id,
                    new_parent.structure_node_id,
                    evidence=StructureAuditProposalEvidence.LOW,
                ),
            )
        )

        result = run_structure_repair_with_provider(
            self.source_ir,
            self.base_tree,
            provider,
        )

        self.assertEqual(result.effective_structure.revision_number, 0)
        self.assertEqual(
            result.proposal_outcomes[0].disposition,
            StructureProposalDisposition.LOW_EVIDENCE,
        )
        self.assertIsNotNone(result.proposal_outcomes[0].proposal)
        self.assertIsNone(result.proposal_outcomes[0].patch)

    def test_high_evidence_boundary_split_applies_and_replays_stably(self) -> None:
        source_ir, source_ids = _flat_source(
            (
                (SourceNodeKind.HEADING, "6.4 Test requirements"),
                (
                    SourceNodeKind.PARAGRAPH,
                    "6．4．1． First explicit item\n6．4．2． Second explicit item",
                ),
            )
        )
        base_tree = build_structure_tree(source_ir)
        target = _structure_node_for_source(base_tree.nodes, source_ids[1])
        text = source_ir.nodes[1].raw_text
        split_offset = text.index("6．4．2")
        provider = _FakeAuditProvider(
            lambda request: _boundary_split_decision(
                request,
                target.structure_node_id,
                split_offset=split_offset,
                text_length=len(text),
            )
        )

        result = run_structure_repair_with_provider(
            source_ir,
            base_tree,
            provider,
        )

        self.assertEqual(result.effective_structure.revision_number, 1)
        self.assertEqual(
            result.proposal_outcomes[0].disposition,
            StructureProposalDisposition.APPLIED,
        )
        self.assertNotIn(
            target.structure_node_id,
            {node.structure_node_id for node in result.effective_structure.nodes},
        )
        replayed = run_structure_repair_with_provider(
            source_ir,
            base_tree,
            provider,
            revisions=result.revisions,
        )
        self.assertEqual(replayed.revisions, result.revisions)
        self.assertEqual(
            replayed.effective_structure,
            result.effective_structure,
        )

    def test_low_evidence_boundary_split_remains_warning_only(self) -> None:
        source_ir, source_ids = _flat_source(
            (
                (SourceNodeKind.HEADING, "6.4 Test requirements"),
                (
                    SourceNodeKind.PARAGRAPH,
                    "6．4．1． First explicit item\n6．4．2． Second explicit item",
                ),
            )
        )
        base_tree = build_structure_tree(source_ir)
        target = _structure_node_for_source(base_tree.nodes, source_ids[1])
        text = source_ir.nodes[1].raw_text
        provider = _FakeAuditProvider(
            lambda request: _boundary_split_decision(
                request,
                target.structure_node_id,
                split_offset=text.index("6．4．2"),
                text_length=len(text),
                evidence=StructureAuditProposalEvidence.LOW,
            )
        )

        result = run_structure_repair_with_provider(
            source_ir,
            base_tree,
            provider,
        )

        self.assertEqual(result.effective_structure.revision_number, 0)
        self.assertEqual(
            result.proposal_outcomes[0].disposition,
            StructureProposalDisposition.LOW_EVIDENCE,
        )
        self.assertIsNone(result.proposal_outcomes[0].patch)

    def test_grounded_continuation_merge_applies_and_replays_stably(self) -> None:
        primary = self.nodes_by_text["Alpha one. Alpha two."]
        continuation = self.nodes_by_text["Second continuation."]
        base = run_structure_repair_with_provider(
            self.source_ir,
            self.base_tree,
            _FakeAuditProvider(lambda _request: ()),
        ).effective_structure
        linked = apply_manual_structure_operations(
            self.source_ir,
            self.base_tree,
            (),
            (
                AddStructureRelationOperationV1(
                    kind=StructureRelationKind.CONTINUES,
                    source_structure_node_id=primary.structure_node_id,
                    target_structure_node_id=continuation.structure_node_id,
                    source_refs=continuation.source_refs,
                ),
            ),
            expected_revision_number=base.revision_number,
            expected_effective_snapshot_id=base.effective_snapshot_id,
        )
        provider = _FakeAuditProvider(
            lambda request: _continuation_merge_decision(
                request,
                primary.structure_node_id,
                continuation.structure_node_id,
            )
        )

        result = run_structure_repair_with_provider(
            self.source_ir,
            self.base_tree,
            provider,
            revisions=linked.revisions,
        )

        self.assertEqual(result.effective_structure.revision_number, 2)
        self.assertEqual(
            result.proposal_outcomes[0].disposition,
            StructureProposalDisposition.APPLIED,
        )
        self.assertNotIn(
            continuation.structure_node_id,
            {node.structure_node_id for node in result.effective_structure.nodes},
        )
        replayed = run_structure_repair_with_provider(
            self.source_ir,
            self.base_tree,
            provider,
            revisions=result.revisions,
        )
        self.assertEqual(replayed.revisions, result.revisions)
        self.assertEqual(
            replayed.effective_structure,
            result.effective_structure,
        )

    def test_manual_patch_uses_optimistic_identity_and_undo_appends(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        new_parent = self.sections_by_label["1.2"]
        base = run_structure_repair_with_provider(
            self.source_ir,
            self.base_tree,
            _FakeAuditProvider(lambda _request: ()),
        ).effective_structure
        operation = ReparentStructureOperationV1(
            structure_node_id=target.structure_node_id,
            new_parent_structure_node_id=new_parent.structure_node_id,
            new_sibling_index=0,
        )
        applied = apply_manual_structure_operations(
            self.source_ir,
            self.base_tree,
            (),
            (operation,),
            expected_revision_number=base.revision_number,
            expected_effective_snapshot_id=base.effective_snapshot_id,
        )

        with self.assertRaises(StructureRevisionRejected) as caught:
            apply_manual_structure_operations(
                self.source_ir,
                self.base_tree,
                applied.revisions,
                (operation,),
                expected_revision_number=base.revision_number,
                expected_effective_snapshot_id=base.effective_snapshot_id,
            )
        self.assertEqual(
            caught.exception.code,
            StructureRevisionRejectionCode.STALE_BASE_REVISION,
        )

        undone = undo_current_structure_revision(
            self.source_ir,
            self.base_tree,
            applied.revisions,
            expected_revision_number=(
                applied.apply_result.effective_snapshot.revision_number
            ),
            expected_effective_snapshot_id=(
                applied.apply_result.effective_snapshot.effective_snapshot_id
            ),
        )
        self.assertEqual(len(undone.revisions), 2)
        self.assertEqual(undone.apply_result.effective_snapshot.revision_number, 2)
        restored_target = next(
            node
            for node in undone.apply_result.effective_snapshot.nodes
            if node.structure_node_id == target.structure_node_id
        )
        self.assertEqual(
            restored_target.parent_structure_node_id,
            target.parent_structure_node_id,
        )

    def test_effective_identity_flows_into_semantics_and_requirement_view(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        new_parent = self.sections_by_label["1.2"]
        provider = _FakeAuditProvider(
            lambda request: (
                _reparent_decision(
                    request,
                    target.structure_node_id,
                    new_parent.structure_node_id,
                ),
            )
        )
        repair = run_structure_repair_with_provider(
            self.source_ir,
            self.base_tree,
            provider,
        )

        semantics = classify_content(self.source_ir, repair.effective_structure)
        view = project_requirements(
            self.source_ir,
            repair.effective_structure,
            semantics,
        )

        self.assertEqual(
            semantics.structure_snapshot_id,
            repair.effective_structure.effective_snapshot_id,
        )
        self.assertEqual(
            view.structure_snapshot_id,
            repair.effective_structure.effective_snapshot_id,
        )
        self.assertNotEqual(
            view.structure_snapshot_id,
            self.base_tree.structure_snapshot_id,
        )

    def test_impact_invalidates_only_affected_requirement_owners(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        new_parent = self.sections_by_label["1.2"]
        unaffected = self.nodes_by_text["Other content."]
        provider = _FakeAuditProvider(
            lambda request: (
                _reparent_decision(
                    request,
                    target.structure_node_id,
                    new_parent.structure_node_id,
                ),
            )
        )
        repair = run_structure_repair_with_provider(
            self.source_ir,
            self.base_tree,
            provider,
        )

        scope = derive_structure_derivation_scope(
            effective_snapshot_id=repair.effective_structure.effective_snapshot_id,
            requirement_structure_node_ids=(
                target.structure_node_id,
                unaffected.structure_node_id,
            ),
            impact=repair.new_impact,
        )

        self.assertEqual(
            scope.stale_structure_node_ids,
            (target.structure_node_id,),
        )
        self.assertEqual(
            scope.reusable_structure_node_ids,
            (unaffected.structure_node_id,),
        )

    def test_audit_failure_preserves_previously_applied_safe_revision(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        new_parent = self.sections_by_label["1.2"]
        base = run_structure_repair_with_provider(
            self.source_ir,
            self.base_tree,
            _FakeAuditProvider(lambda _request: ()),
        ).effective_structure
        applied = apply_manual_structure_operations(
            self.source_ir,
            self.base_tree,
            (),
            (
                ReparentStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    new_parent_structure_node_id=new_parent.structure_node_id,
                    new_sibling_index=0,
                ),
            ),
            expected_revision_number=base.revision_number,
            expected_effective_snapshot_id=base.effective_snapshot_id,
        )
        failing = _FakeAuditProvider(
            lambda _request: (),
            raise_for_request=lambda _request: True,
        )

        result = run_structure_repair_with_provider(
            self.source_ir,
            self.base_tree,
            failing,
            revisions=applied.revisions,
        )

        self.assertIsNone(result.audit_error_code)
        self.assertIs(result.audit.status, StructureAuditStatus.UNRESOLVED)  # type: ignore[union-attr]
        self.assertEqual(result.revisions, applied.revisions)
        self.assertEqual(
            result.effective_structure,
            applied.apply_result.effective_snapshot,
        )


def _reparent_decision(
    request,
    target_id: str,
    new_parent_id: str,
    *,
    evidence: StructureAuditProposalEvidence = StructureAuditProposalEvidence.HIGH,
) -> StructureAuditDecision:
    nodes = {
        node.target_structure_node_id: node
        for node in (
            *request.evidence_request.nodes,
            *request.evidence_request.context_nodes,
        )
    }
    target = nodes[target_id]
    new_parent = nodes[new_parent_id]
    return StructureAuditDecision(
        category=StructureAuditCategory.POSSIBLE_MISPLACED_PARENT,
        target_structure_node_id=target_id,
        related_structure_node_ids=(new_parent_id,),
        evidence_source_refs=(
            target.evidence[0].source_ref,
            new_parent.evidence[0].source_ref,
        ),
        reason_codes=("synthetic_misplaced_parent",),
        proposed_operations=(
            ReparentStructureOperationV1(
                structure_node_id=target_id,
                new_parent_structure_node_id=new_parent_id,
                new_sibling_index=0,
            ),
        ),
        proposal_evidence=evidence,
    )


def _boundary_split_decision(
    request,
    target_id: str,
    *,
    split_offset: int,
    text_length: int,
    evidence: StructureAuditProposalEvidence = StructureAuditProposalEvidence.HIGH,
) -> tuple[StructureAuditDecision, ...]:
    nodes = {
        node.target_structure_node_id: node
        for node in (
            *request.evidence_request.nodes,
            *request.evidence_request.context_nodes,
        )
    }
    if target_id not in {
        node.target_structure_node_id for node in request.evidence_request.nodes
    }:
        return ()
    target = nodes[target_id]
    source_ref = target.evidence[0].source_ref
    return (
        StructureAuditDecision(
            category=StructureAuditCategory.POSSIBLE_MISSED_BOUNDARY,
            target_structure_node_id=target_id,
            evidence_source_refs=(source_ref,),
            reason_codes=("synthetic_explicit_boundary_split",),
            proposed_operations=(
                BoundarySplitStructureOperationV1(
                    structure_node_id=target_id,
                    parts=(
                        BoundarySplitPartV1(
                            source_spans=(
                                StructureSourceSpanV1(
                                    source_ref=source_ref,
                                    start_offset=0,
                                    end_offset=split_offset,
                                ),
                            )
                        ),
                        BoundarySplitPartV1(
                            source_spans=(
                                StructureSourceSpanV1(
                                    source_ref=source_ref,
                                    start_offset=split_offset,
                                    end_offset=text_length,
                                ),
                            )
                        ),
                    ),
                ),
            ),
            proposal_evidence=evidence,
        ),
    )


def _continuation_merge_decision(
    request,
    primary_id: str,
    continuation_id: str,
) -> tuple[StructureAuditDecision, ...]:
    nodes = {
        node.target_structure_node_id: node
        for node in (
            *request.evidence_request.nodes,
            *request.evidence_request.context_nodes,
        )
    }
    if (
        primary_id
        not in {
            node.target_structure_node_id for node in request.evidence_request.nodes
        }
        or continuation_id not in nodes
    ):
        return ()
    primary = nodes[primary_id]
    continuation = nodes[continuation_id]
    return (
        StructureAuditDecision(
            category=StructureAuditCategory.POSSIBLE_DUPLICATE_CONTENT,
            target_structure_node_id=primary_id,
            related_structure_node_ids=(continuation_id,),
            evidence_source_refs=(
                primary.evidence[0].source_ref,
                continuation.evidence[0].source_ref,
            ),
            reason_codes=("synthetic_grounded_continuation_merge",),
            proposed_operations=(
                ContinuationMergeStructureOperationV1(
                    primary_structure_node_id=primary_id,
                    continuation_structure_node_id=continuation_id,
                ),
            ),
            proposal_evidence=StructureAuditProposalEvidence.HIGH,
        ),
    )


if __name__ == "__main__":
    unittest.main()
