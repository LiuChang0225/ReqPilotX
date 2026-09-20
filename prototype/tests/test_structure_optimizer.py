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
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import Mock

from app.adapters.documents.bailian_structure_optimizer_provider import (
    BailianQwenStructureOptimizerProvider,
)
from app.application.extraction.optimize_structure import (
    StructureOptimizationProposalStatus,
    StructureOptimizationStatus,
    apply_structure_optimization_proposal,
    optimize_structure_with_provider,
    plan_structure_overview_request,
    plan_structure_overview_requests,
    plan_structure_repair_request,
    propose_structure_optimization_with_provider,
)
from app.application.extraction.provider_source_policy import (
    ProviderSourceIRRejected,
    ProviderSourceIRRejectionCode,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.application.extraction.revise_structure import (
    apply_structure_patch,
    create_structure_patch_proposal,
    get_base_effective_structure,
    validate_structure_patch_proposal,
    validate_structure_revision_chain,
)
from app.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
    stable_semantic_provider_run_id,
)
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
)
from app.domain.extraction.structure import (
    StructureNode,
    StructureRelationKind,
)
from app.domain.extraction.structure_revision import (
    AddStructureRelationOperationV1,
    BoundarySplitPartV1,
    BoundarySplitStructureOperationV1,
    ContinuationMergeStructureOperationV1,
    LogicalReorderStructureOperationV1,
    RemoveStructureRelationOperationV1,
    ReparentStructureOperationV1,
    StructurePatchOperationV1,
    StructureRevisionRejected,
    StructureRevisionRejectionCode,
    StructureSourceSpanV1,
    canonical_model_sha256,
)
from app.ports.structure_optimizer_provider import (
    StructureDocumentRegionKind,
    StructureDocumentRegionV1,
    StructureOptimizationConfidence,
    StructureOptimizationIssueCategory,
    StructureOptimizationIssueOrigin,
    StructureOptimizationIssueV1,
    StructureOverviewProviderResultV1,
    StructureOverviewRequestV1,
    StructureRepairProviderResultV1,
    StructureRepairRequestV1,
)


_SOURCE_SHA256 = "6" * 64
_ADAPTER_ID = "synthetic-structure-optimizer"
_ADAPTER_VERSION = "1.0.0"
_NOW = datetime(2026, 7, 28, 8, 0, tzinfo=UTC)
_TEXTS = (
    "1 Scope",
    "1.1 Alpha",
    "Alpha one. Alpha two.",
    "1.2 Beta",
    "Beta content.",
    "2 Other",
    "Other content.",
)


class StructureOptimizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source_ir = _source_ir_fixture()
        self.base_tree = build_structure_tree(self.source_ir)
        self.base_effective = get_base_effective_structure(
            self.source_ir,
            self.base_tree,
        )
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
        self.source_hash_before = canonical_model_sha256(self.source_ir)
        self.base_hash_before = canonical_model_sha256(self.base_tree)

    def test_incomplete_source_is_rejected_before_optimizer_provider_call(
        self,
    ) -> None:
        for status in (SourceIRStatus.PARTIAL, SourceIRStatus.FAILED):
            with self.subTest(status=status.value):
                provider = _FakeOptimizerProvider(
                    overview_builder=lambda _request: (),
                    repair_builder=lambda _request: (),
                )
                incomplete = self.source_ir.model_copy(
                    update={"status": status},
                )

                with self.assertRaises(ProviderSourceIRRejected) as captured:
                    propose_structure_optimization_with_provider(
                        incomplete,
                        self.base_tree,
                        (),
                        provider,
                    )

                self.assertIs(
                    captured.exception.code,
                    ProviderSourceIRRejectionCode.SOURCE_IR_NOT_COMPLETE,
                )
                self.assertIs(captured.exception.source_ir_status, status)
                self.assertEqual(
                    captured.exception.operation,
                    "structure_optimization",
                )
                self.assertEqual(provider.calls, [])

    def test_incomplete_source_cannot_apply_complete_source_proposal(self) -> None:
        alpha_content = self.nodes_by_text["Alpha one. Alpha two."]
        beta_section = self.sections_by_label["1.2"]
        issue = StructureOptimizationIssueV1(
            category=StructureOptimizationIssueCategory.MISPLACED_PARENT,
            target_structure_node_id=alpha_content.structure_node_id,
            related_structure_node_ids=(beta_section.structure_node_id,),
            evidence_source_refs=alpha_content.source_refs,
            origin=StructureOptimizationIssueOrigin.BASE_RESOLVER,
            confidence=StructureOptimizationConfidence.HIGH,
            reason_codes=("synthetic_parent_conflict",),
        )
        provider = _FakeOptimizerProvider(
            overview_builder=lambda _request: (issue,),
            repair_builder=lambda _request: (
                ReparentStructureOperationV1(
                    structure_node_id=alpha_content.structure_node_id,
                    new_parent_structure_node_id=beta_section.structure_node_id,
                    new_sibling_index=0,
                ),
            ),
        )
        proposal = propose_structure_optimization_with_provider(
            self.source_ir,
            self.base_tree,
            (),
            provider,
        )
        incomplete = self.source_ir.model_copy(
            update={"status": SourceIRStatus.PARTIAL},
        )

        with self.assertRaises(ProviderSourceIRRejected) as captured:
            apply_structure_optimization_proposal(
                proposal,
                incomplete,
                self.base_tree,
                (),
            )

        self.assertIs(
            captured.exception.code,
            ProviderSourceIRRejectionCode.SOURCE_IR_NOT_COMPLETE,
        )
        self.assertEqual(
            captured.exception.operation,
            "structure_optimization_apply",
        )
        self.assertEqual(
            validate_structure_revision_chain(
                incomplete,
                self.base_tree,
                (),
            ).revision_number,
            0,
        )

    def test_overview_then_repair_applies_one_aggregate_revision(self) -> None:
        alpha_section = self.sections_by_label["1.1"]
        beta_section = self.sections_by_label["1.2"]
        alpha_content = self.nodes_by_text["Alpha one. Alpha two."]
        beta_content = self.nodes_by_text["Beta content."]

        def overview(
            request: StructureOverviewRequestV1,
        ) -> tuple[StructureOptimizationIssueV1, ...]:
            self.assertEqual(
                request.effective_snapshot_id,
                self.base_effective.effective_snapshot_id,
            )
            return (
                StructureOptimizationIssueV1(
                    category=StructureOptimizationIssueCategory.MISPLACED_PARENT,
                    target_structure_node_id=alpha_section.structure_node_id,
                    related_structure_node_ids=(beta_section.structure_node_id,),
                    evidence_source_refs=alpha_section.source_refs,
                    origin=StructureOptimizationIssueOrigin.BASE_RESOLVER,
                    confidence=StructureOptimizationConfidence.HIGH,
                    reason_codes=("content_belongs_to_following_section",),
                ),
            )

        def repair(
            request: StructureRepairRequestV1,
        ) -> tuple[StructurePatchOperationV1, ...]:
            evidence_by_ref = {
                item.source_ref: item.raw_text for item in request.evidence
            }
            self.assertEqual(
                evidence_by_ref[alpha_content.source_refs[0]],
                "Alpha one. Alpha two.",
            )
            self.assertEqual(request.overview_issues, overview_result)
            self.assertEqual(
                tuple(region.kind for region in request.document_regions),
                (StructureDocumentRegionKind.MAIN_BODY,),
            )
            return (
                ReparentStructureOperationV1(
                    structure_node_id=alpha_content.structure_node_id,
                    new_parent_structure_node_id=beta_section.structure_node_id,
                    new_sibling_index=0,
                ),
                AddStructureRelationOperationV1(
                    kind=StructureRelationKind.REFERENCES,
                    source_structure_node_id=alpha_content.structure_node_id,
                    target_structure_node_id=beta_content.structure_node_id,
                    source_refs=beta_content.source_refs,
                ),
            )

        overview_result = overview(
            plan_structure_overview_request(
                self.source_ir,
                self.base_effective,
            )
        )
        provider = _FakeOptimizerProvider(
            overview_builder=overview,
            region_builder=lambda request: (
                StructureDocumentRegionV1(
                    kind=StructureDocumentRegionKind.MAIN_BODY,
                    start_structure_node_id=self.sections_by_label[
                        "1"
                    ].structure_node_id,
                    end_structure_node_id=self.sections_by_label["2"].structure_node_id,
                    evidence_source_refs=self.sections_by_label["1"].source_refs,
                    confidence=StructureOptimizationConfidence.HIGH,
                    reason_codes=("numbered_body_outline",),
                ),
            ),
            repair_builder=repair,
        )

        outcome = optimize_structure_with_provider(
            self.source_ir,
            self.base_tree,
            (),
            provider,
        )

        self.assertEqual(outcome.status, StructureOptimizationStatus.APPLIED)
        self.assertEqual(provider.calls, ["overview_structure", "repair_structure"])
        self.assertIsNotNone(outcome.apply_result)
        assert outcome.apply_result is not None
        self.assertTrue(outcome.apply_result.appended)
        self.assertEqual(outcome.effective_snapshot.revision_number, 1)
        self.assertEqual(
            len(outcome.apply_result.revision.patch.operations),  # type: ignore[union-attr]
            2,
        )
        moved = _node_by_id(
            outcome.effective_snapshot.nodes,
            alpha_content.structure_node_id,
        )
        self.assertEqual(
            moved.parent_structure_node_id,
            beta_section.structure_node_id,
        )
        self.assertEqual(
            canonical_model_sha256(self.source_ir),
            self.source_hash_before,
        )
        self.assertEqual(
            canonical_model_sha256(self.base_tree),
            self.base_hash_before,
        )

    def test_provider_proposal_has_no_write_authority_until_explicit_apply(
        self,
    ) -> None:
        alpha_content = self.nodes_by_text["Alpha one. Alpha two."]
        beta_section = self.sections_by_label["1.2"]
        issue = StructureOptimizationIssueV1(
            category=StructureOptimizationIssueCategory.MISPLACED_PARENT,
            target_structure_node_id=alpha_content.structure_node_id,
            related_structure_node_ids=(beta_section.structure_node_id,),
            evidence_source_refs=alpha_content.source_refs,
            origin=StructureOptimizationIssueOrigin.BASE_RESOLVER,
            confidence=StructureOptimizationConfidence.HIGH,
            reason_codes=("content_belongs_to_following_section",),
        )
        operation = ReparentStructureOperationV1(
            structure_node_id=alpha_content.structure_node_id,
            new_parent_structure_node_id=beta_section.structure_node_id,
            new_sibling_index=0,
        )
        provider = _FakeOptimizerProvider(
            overview_builder=lambda request: (issue,),
            repair_builder=lambda request: (operation,),
        )

        proposal = propose_structure_optimization_with_provider(
            self.source_ir,
            self.base_tree,
            (),
            provider,
        )

        self.assertEqual(
            proposal.status,
            StructureOptimizationProposalStatus.PROPOSED,
        )
        self.assertIsNotNone(proposal.proposal)
        self.assertEqual(proposal.expected_revision_number, 0)
        self.assertEqual(
            proposal.expected_effective_snapshot_id,
            self.base_effective.effective_snapshot_id,
        )
        self.assertEqual(
            validate_structure_revision_chain(
                self.source_ir,
                self.base_tree,
                (),
            ),
            self.base_effective,
        )

        applied = apply_structure_optimization_proposal(
            proposal,
            self.source_ir,
            self.base_tree,
            (),
        )

        self.assertEqual(applied.status, StructureOptimizationStatus.APPLIED)
        self.assertEqual(applied.effective_snapshot.revision_number, 1)

    def test_delayed_provider_proposal_rejects_fresh_chain_conflict(self) -> None:
        alpha_content = self.nodes_by_text["Alpha one. Alpha two."]
        beta_content = self.nodes_by_text["Beta content."]
        beta_section = self.sections_by_label["1.2"]
        issue = StructureOptimizationIssueV1(
            category=StructureOptimizationIssueCategory.MISPLACED_PARENT,
            target_structure_node_id=alpha_content.structure_node_id,
            related_structure_node_ids=(beta_section.structure_node_id,),
            evidence_source_refs=alpha_content.source_refs,
            origin=StructureOptimizationIssueOrigin.BASE_RESOLVER,
            confidence=StructureOptimizationConfidence.HIGH,
            reason_codes=("content_belongs_to_following_section",),
        )
        provider = _FakeOptimizerProvider(
            overview_builder=lambda request: (issue,),
            repair_builder=lambda request: (
                ReparentStructureOperationV1(
                    structure_node_id=alpha_content.structure_node_id,
                    new_parent_structure_node_id=beta_section.structure_node_id,
                    new_sibling_index=0,
                ),
            ),
        )
        delayed_proposal = propose_structure_optimization_with_provider(
            self.source_ir,
            self.base_tree,
            (),
            provider,
        )
        concurrent_proposal = create_structure_patch_proposal(
            self.source_ir,
            self.base_tree,
            (),
            (
                AddStructureRelationOperationV1(
                    kind=StructureRelationKind.REFERENCES,
                    source_structure_node_id=alpha_content.structure_node_id,
                    target_structure_node_id=beta_content.structure_node_id,
                    source_refs=beta_content.source_refs,
                ),
            ),
        )
        concurrent_patch = validate_structure_patch_proposal(
            concurrent_proposal,
            self.source_ir,
            self.base_tree,
            (),
        )
        concurrent_apply = apply_structure_patch(
            concurrent_patch,
            self.source_ir,
            self.base_tree,
            (),
        )
        fresh_revisions = (concurrent_apply.revision,)

        with self.assertRaises(StructureRevisionRejected) as raised:
            apply_structure_optimization_proposal(
                delayed_proposal,
                self.source_ir,
                self.base_tree,
                fresh_revisions,
            )

        self.assertEqual(
            raised.exception.code,
            StructureRevisionRejectionCode.STALE_BASE_REVISION,
        )
        self.assertEqual(
            validate_structure_revision_chain(
                self.source_ir,
                self.base_tree,
                fresh_revisions,
            ).revision_number,
            1,
        )

    def test_clean_overview_skips_repair_and_keeps_revision_zero(self) -> None:
        provider = _FakeOptimizerProvider(
            overview_builder=lambda request: (),
            repair_builder=None,
        )

        outcome = optimize_structure_with_provider(
            self.source_ir,
            self.base_tree,
            (),
            provider,
        )

        self.assertEqual(outcome.status, StructureOptimizationStatus.NO_CHANGE)
        self.assertEqual(provider.calls, ["overview_structure"])
        self.assertIsNone(outcome.repair)
        self.assertIsNone(outcome.apply_result)
        self.assertEqual(outcome.effective_snapshot, self.base_effective)

    def test_unresolved_overview_stops_without_write_authority(self) -> None:
        provider = _FakeOptimizerProvider(
            overview_builder=lambda request: (),
            repair_builder=None,
            overview_status=SemanticProviderRunStatus.TIMEOUT,
        )

        outcome = optimize_structure_with_provider(
            self.source_ir,
            self.base_tree,
            (),
            provider,
        )

        self.assertEqual(
            outcome.status,
            StructureOptimizationStatus.OVERVIEW_UNRESOLVED,
        )
        self.assertEqual(provider.calls, ["overview_structure"])
        self.assertEqual(outcome.effective_snapshot.revision_number, 0)

    def test_unconfigured_real_provider_fails_closed_without_repair(self) -> None:
        provider = BailianQwenStructureOptimizerProvider(
            api_key="",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            max_retries=0,
        )

        outcome = optimize_structure_with_provider(
            self.source_ir,
            self.base_tree,
            (),
            provider,
        )

        self.assertEqual(
            outcome.status,
            StructureOptimizationStatus.OVERVIEW_UNRESOLVED,
        )
        self.assertEqual(
            outcome.overview.run.status,
            SemanticProviderRunStatus.UNAVAILABLE,
        )
        self.assertEqual(
            outcome.overview.run.error_code,
            "provider_not_configured",
        )
        self.assertIsNone(outcome.repair)
        self.assertEqual(outcome.effective_snapshot, self.base_effective)

    def test_unconfigured_real_provider_validates_repair_schema_locally(self) -> None:
        alpha_section = self.sections_by_label["1.1"]
        beta_section = self.sections_by_label["1.2"]
        issue = StructureOptimizationIssueV1(
            category=StructureOptimizationIssueCategory.MISPLACED_PARENT,
            target_structure_node_id=alpha_section.structure_node_id,
            related_structure_node_ids=(beta_section.structure_node_id,),
            evidence_source_refs=alpha_section.source_refs,
            origin=StructureOptimizationIssueOrigin.BASE_RESOLVER,
            confidence=StructureOptimizationConfidence.HIGH,
            reason_codes=("suspected_parent_error",),
        )
        request = plan_structure_repair_request(
            self.source_ir,
            self.base_effective,
            (issue,),
        )
        provider = BailianQwenStructureOptimizerProvider(
            api_key="",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            max_retries=0,
        )

        result = provider.repair_structure(request)

        self.assertEqual(result.operations, ())
        self.assertEqual(result.run.operation, "repair_structure")
        self.assertEqual(
            result.run.status,
            SemanticProviderRunStatus.UNAVAILABLE,
        )
        self.assertEqual(result.run.error_code, "provider_not_configured")

    def test_real_provider_wire_uses_compact_aliases_and_maps_overview_back(
        self,
    ) -> None:
        request = plan_structure_overview_request(
            self.source_ir,
            self.base_effective,
        )
        captured_input: dict[str, object] = {}

        def respond(*_args: object, **kwargs: object) -> Mock:
            model_input = _model_input_from_wire(kwargs["json"])
            captured_input.update(model_input)
            nodes = _rows_by_column(model_input, "node_columns", "nodes")
            start = next(node for node in nodes if node["explicit_label"] == "1")
            end = next(node for node in nodes if node["explicit_label"] == "2")
            return _model_response(
                {
                    "document_regions": [
                        {
                            "kind": "main_body",
                            "start_node": start["node"],
                            "end_node": end["node"],
                            "evidence": [start["evidence"][0]],
                            "confidence": "HIGH",
                            "reason_codes": ["numbered_body_outline"],
                        }
                    ],
                    "issues": [],
                }
            )

        client = Mock()
        client.post.side_effect = respond
        provider = _real_optimizer_provider(client)

        result = provider.overview_structure(request)

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(len(result.document_regions), 1)
        self.assertEqual(
            result.document_regions[0].start_structure_node_id,
            self.sections_by_label["1"].structure_node_id,
        )
        self.assertEqual(
            result.document_regions[0].end_structure_node_id,
            self.sections_by_label["2"].structure_node_id,
        )
        self.assertEqual(
            captured_input["binding"]["partition_index"],  # type: ignore[index]
            0,
        )
        self.assertEqual(
            captured_input["binding"]["partition_count"],  # type: ignore[index]
            1,
        )
        self.assertEqual(
            len(captured_input["inspection_nodes"]),  # type: ignore[arg-type]
            len(request.nodes),
        )
        self.assertEqual(
            captured_input["inspection_relations"],
            [],
        )
        wire_json = json.dumps(captured_input, ensure_ascii=False)
        self.assertNotIn(self.source_ir.source_ir_id, wire_json)
        self.assertNotIn(
            self.sections_by_label["1"].structure_node_id,
            wire_json,
        )
        self.assertNotIn(
            self.sections_by_label["1"].source_refs[0].source_id,
            wire_json,
        )
        self.assertLess(
            len(wire_json.encode("utf-8")),
            len(request.model_dump_json().encode("utf-8")),
        )

    def test_real_provider_maps_all_compact_repair_operations_back(self) -> None:
        alpha_content = self.nodes_by_text["Alpha one. Alpha two."]
        beta_content = self.nodes_by_text["Beta content."]
        relation_operation = AddStructureRelationOperationV1(
            kind=StructureRelationKind.REFERENCES,
            source_structure_node_id=alpha_content.structure_node_id,
            target_structure_node_id=beta_content.structure_node_id,
            source_refs=beta_content.source_refs,
        )
        proposal = create_structure_patch_proposal(
            self.source_ir,
            self.base_tree,
            (),
            (relation_operation,),
        )
        patch = validate_structure_patch_proposal(
            proposal,
            self.source_ir,
            self.base_tree,
            (),
        )
        applied = apply_structure_patch(
            patch,
            self.source_ir,
            self.base_tree,
            (),
        )
        added_relation = next(
            relation
            for relation in applied.effective_snapshot.relations
            if relation.kind is StructureRelationKind.REFERENCES
        )
        issue = StructureOptimizationIssueV1(
            category=StructureOptimizationIssueCategory.MISSED_BOUNDARY,
            target_structure_node_id=alpha_content.structure_node_id,
            related_structure_node_ids=(beta_content.structure_node_id,),
            evidence_source_refs=alpha_content.source_refs,
            origin=StructureOptimizationIssueOrigin.BASE_RESOLVER,
            confidence=StructureOptimizationConfidence.HIGH,
            reason_codes=("compound_content",),
        )
        request = plan_structure_repair_request(
            self.source_ir,
            applied.effective_snapshot,
            (issue,),
        )

        def respond(*_args: object, **kwargs: object) -> Mock:
            model_input = _model_input_from_wire(kwargs["json"])
            nodes = _rows_by_column(model_input, "node_columns", "nodes")
            relations = _rows_by_column(
                model_input,
                "relation_columns",
                "non_parent_relations",
            )
            alpha_row = next(
                node
                for node in nodes
                if node["text_preview"] == "Alpha one. Alpha two."
            )
            beta_row = next(
                node for node in nodes if node["text_preview"] == "Beta content."
            )
            alpha_section_row = next(
                node for node in nodes if node["explicit_label"] == "1.1"
            )
            beta_section_row = next(
                node for node in nodes if node["explicit_label"] == "1.2"
            )
            alpha_children = [
                node["node"]
                for node in nodes
                if node["parent"] == alpha_section_row["node"]
            ]
            alpha_evidence = alpha_row["source_spans"][0][0]
            split_end = alpha_row["source_spans"][0][2]
            existing_reference = next(
                relation for relation in relations if relation["kind"] == "references"
            )
            return _model_response(
                {
                    "operations": [
                        {
                            "operation": "re_parent",
                            "node": beta_row["node"],
                            "new_parent": beta_section_row["node"],
                            "new_sibling_index": 0,
                        },
                        {
                            "operation": "logical_reorder",
                            "parent": alpha_section_row["node"],
                            "ordered_children": alpha_children,
                        },
                        {
                            "operation": "boundary_split",
                            "node": alpha_row["node"],
                            "parts": [
                                {
                                    "source_spans": [
                                        {
                                            "evidence": alpha_evidence,
                                            "start_offset": 0,
                                            "end_offset": 10,
                                        }
                                    ]
                                },
                                {
                                    "source_spans": [
                                        {
                                            "evidence": alpha_evidence,
                                            "start_offset": 10,
                                            "end_offset": split_end,
                                        }
                                    ]
                                },
                            ],
                        },
                        {
                            "operation": "continuation_merge",
                            "primary": alpha_row["node"],
                            "continuation": beta_row["node"],
                        },
                        {
                            "operation": "add_relation",
                            "kind": "references",
                            "source": alpha_row["node"],
                            "target": beta_row["node"],
                            "evidence": [beta_row["evidence"][0]],
                            "option_state": None,
                        },
                        {
                            "operation": "remove_relation",
                            "relation": existing_reference["relation"],
                        },
                    ]
                }
            )

        client = Mock()
        client.post.side_effect = respond
        provider = _real_optimizer_provider(client)

        result = provider.repair_structure(request)

        self.assertEqual(result.run.status, SemanticProviderRunStatus.SUCCESS)
        self.assertEqual(
            tuple(type(operation) for operation in result.operations),
            (
                ReparentStructureOperationV1,
                LogicalReorderStructureOperationV1,
                BoundarySplitStructureOperationV1,
                ContinuationMergeStructureOperationV1,
                AddStructureRelationOperationV1,
                RemoveStructureRelationOperationV1,
            ),
        )
        self.assertEqual(
            result.operations[-1].relation_id,  # type: ignore[union-attr]
            added_relation.relation_id,
        )
        self.assertEqual(
            result.operations[2].parts[0].source_spans[0].source_ref,  # type: ignore[union-attr]
            alpha_content.source_refs[0],
        )

    def test_real_provider_rejects_schema_valid_unknown_alias(self) -> None:
        request = plan_structure_overview_request(
            self.source_ir,
            self.base_effective,
        )

        def respond(*_args: object, **kwargs: object) -> Mock:
            model_input = _model_input_from_wire(kwargs["json"])
            nodes = _rows_by_column(model_input, "node_columns", "nodes")
            evidence_alias = nodes[1]["evidence"][0]
            return _model_response(
                {
                    "document_regions": [],
                    "issues": [
                        {
                            "category": "misplaced_parent",
                            "target_node": "n999999",
                            "related_nodes": [],
                            "evidence": [evidence_alias],
                            "origin": "base_resolver",
                            "confidence": "HIGH",
                            "reason_codes": ["unknown_alias_probe"],
                        }
                    ],
                }
            )

        client = Mock()
        client.post.side_effect = respond
        provider = _real_optimizer_provider(client)

        result = provider.overview_structure(request)

        self.assertEqual(result.issues, ())
        self.assertEqual(
            result.run.status,
            SemanticProviderRunStatus.INVALID_OUTPUT,
        )
        self.assertEqual(
            result.run.error_code,
            "provider_output_binding_invalid",
        )

    def test_provider_result_binding_mismatch_fails_closed(self) -> None:
        provider = _FakeOptimizerProvider(
            overview_builder=lambda request: (),
            repair_builder=None,
            mismatch_overview_binding=True,
        )

        with self.assertRaisesRegex(ValueError, "not bound"):
            optimize_structure_with_provider(
                self.source_ir,
                self.base_tree,
                (),
                provider,
            )

    def test_invalid_cycle_from_repair_is_rejected_by_existing_core(self) -> None:
        section_one = self.sections_by_label["1"]
        child_section = self.sections_by_label["1.1"]

        def overview(
            request: StructureOverviewRequestV1,
        ) -> tuple[StructureOptimizationIssueV1, ...]:
            return (
                StructureOptimizationIssueV1(
                    category=StructureOptimizationIssueCategory.MISPLACED_PARENT,
                    target_structure_node_id=section_one.structure_node_id,
                    related_structure_node_ids=(child_section.structure_node_id,),
                    evidence_source_refs=section_one.source_refs,
                    origin=StructureOptimizationIssueOrigin.BASE_RESOLVER,
                    confidence=StructureOptimizationConfidence.HIGH,
                    reason_codes=("suspected_parent_error",),
                ),
            )

        provider = _FakeOptimizerProvider(
            overview_builder=overview,
            repair_builder=lambda request: (
                ReparentStructureOperationV1(
                    structure_node_id=section_one.structure_node_id,
                    new_parent_structure_node_id=child_section.structure_node_id,
                    new_sibling_index=0,
                ),
            ),
        )

        with self.assertRaises(StructureRevisionRejected) as raised:
            optimize_structure_with_provider(
                self.source_ir,
                self.base_tree,
                (),
                provider,
            )

        self.assertEqual(
            raised.exception.code,
            StructureRevisionRejectionCode.GRAPH_CYCLE,
        )
        self.assertEqual(
            canonical_model_sha256(self.source_ir),
            self.source_hash_before,
        )
        self.assertEqual(
            canonical_model_sha256(self.base_tree),
            self.base_hash_before,
        )

    def test_repair_scope_carries_overview_and_exact_source_context(self) -> None:
        alpha_section = self.sections_by_label["1.1"]
        beta_section = self.sections_by_label["1.2"]
        alpha_content = self.nodes_by_text["Alpha one. Alpha two."]
        issue = StructureOptimizationIssueV1(
            category=StructureOptimizationIssueCategory.MISSED_BOUNDARY,
            target_structure_node_id=alpha_section.structure_node_id,
            related_structure_node_ids=(beta_section.structure_node_id,),
            evidence_source_refs=alpha_section.source_refs,
            origin=StructureOptimizationIssueOrigin.UNCERTAIN,
            confidence=StructureOptimizationConfidence.MEDIUM,
            reason_codes=("section_scope_needs_detail",),
        )

        request = plan_structure_repair_request(
            self.source_ir,
            self.base_effective,
            (issue,),
        )

        self.assertEqual(request.overview_issues, (issue,))
        self.assertEqual(
            request.effective_snapshot_id,
            self.base_effective.effective_snapshot_id,
        )
        self.assertIn(
            alpha_content.structure_node_id,
            {node.structure_node_id for node in request.nodes},
        )
        self.assertEqual(
            {item.source_ref: item.raw_text for item in request.evidence}[
                alpha_content.source_refs[0]
            ],
            "Alpha one. Alpha two.",
        )

    def test_overview_uses_current_effective_spans_after_boundary_split(self) -> None:
        target = self.nodes_by_text["Alpha one. Alpha two."]
        source_ref = target.source_refs[0]
        operation = BoundarySplitStructureOperationV1(
            structure_node_id=target.structure_node_id,
            parts=(
                BoundarySplitPartV1(
                    source_spans=(
                        StructureSourceSpanV1(
                            source_ref=source_ref,
                            start_offset=0,
                            end_offset=10,
                        ),
                    )
                ),
                BoundarySplitPartV1(
                    source_spans=(
                        StructureSourceSpanV1(
                            source_ref=source_ref,
                            start_offset=10,
                            end_offset=len("Alpha one. Alpha two."),
                        ),
                    )
                ),
            ),
        )
        proposal = create_structure_patch_proposal(
            self.source_ir,
            self.base_tree,
            (),
            (operation,),
        )
        patch = validate_structure_patch_proposal(
            proposal,
            self.source_ir,
            self.base_tree,
            (),
        )
        applied = apply_structure_patch(
            patch,
            self.source_ir,
            self.base_tree,
            (),
        )

        request = plan_structure_overview_request(
            self.source_ir,
            applied.effective_snapshot,
        )
        split_nodes = tuple(
            node for node in request.nodes if source_ref in node.source_refs
        )

        self.assertEqual(len(split_nodes), 2)
        self.assertEqual(
            tuple(node.text_preview for node in split_nodes),
            ("Alpha one.", " Alpha two."),
        )
        self.assertEqual(
            tuple(
                (node.source_spans[0].start_offset, node.source_spans[0].end_offset)
                for node in split_nodes
            ),
            ((0, 10), (10, len("Alpha one. Alpha two."))),
        )

    def test_bounded_overview_and_repair_explicitly_report_truncation(self) -> None:
        overview = plan_structure_overview_request(
            self.source_ir,
            self.base_effective,
            max_nodes=5,
        )
        self.assertTrue(overview.map_truncated)
        self.assertEqual(
            overview.total_effective_node_count,
            len(self.base_effective.nodes),
        )
        self.assertEqual(len(overview.nodes), 5)

        section_one = self.sections_by_label["1"]
        issue = StructureOptimizationIssueV1(
            category=StructureOptimizationIssueCategory.MISSED_BOUNDARY,
            target_structure_node_id=section_one.structure_node_id,
            evidence_source_refs=section_one.source_refs,
            origin=StructureOptimizationIssueOrigin.UNCERTAIN,
            confidence=StructureOptimizationConfidence.LOW,
            reason_codes=("bounded_scope_probe",),
        )
        repair = plan_structure_repair_request(
            self.source_ir,
            self.base_effective,
            (issue,),
            max_nodes=6,
        )

        self.assertTrue(repair.focus_truncated)
        self.assertEqual(
            repair.total_effective_node_count,
            len(self.base_effective.nodes),
        )
        self.assertEqual(len(repair.nodes), 6)

    def test_partitioned_overview_assigns_every_effective_node_once(self) -> None:
        requests = plan_structure_overview_requests(
            self.source_ir,
            self.base_effective,
            max_nodes=5,
        )

        self.assertGreater(len(requests), 1)
        self.assertEqual(
            tuple(request.partition_index for request in requests),
            tuple(range(len(requests))),
        )
        self.assertEqual(
            {request.partition_count for request in requests},
            {len(requests)},
        )
        self.assertEqual(
            len({request.overview_coverage_sha256 for request in requests}),
            1,
        )
        inspected = [
            node_id
            for request in requests
            for node_id in request.inspection_structure_node_ids
        ]
        self.assertEqual(len(inspected), len(set(inspected)))
        self.assertEqual(
            set(inspected),
            {node.structure_node_id for node in self.base_effective.nodes},
        )
        document_root_id = next(
            node.structure_node_id
            for node in self.base_effective.nodes
            if node.kind.value == "document"
        )
        for request in requests:
            visible_ids = {node.structure_node_id for node in request.nodes}
            self.assertLessEqual(len(visible_ids), 5)
            self.assertIn(document_root_id, visible_ids)
            self.assertTrue(
                set(request.inspection_structure_node_ids).issubset(visible_ids)
            )
            for node in request.nodes:
                if node.structure_node_id not in request.inspection_structure_node_ids:
                    continue
                if node.parent_structure_node_id is not None:
                    self.assertIn(node.parent_structure_node_id, visible_ids)

    def test_partitioned_overview_aggregates_late_issue_before_one_repair(
        self,
    ) -> None:
        target = self.nodes_by_text["Other content."]
        parent = self.sections_by_label["2"]
        source_ref = target.source_refs[0]

        def overview_builder(
            request: StructureOverviewRequestV1,
        ) -> tuple[StructureOptimizationIssueV1, ...]:
            if target.structure_node_id not in request.inspection_structure_node_ids:
                return ()
            return (
                StructureOptimizationIssueV1(
                    category=StructureOptimizationIssueCategory.MISSED_BOUNDARY,
                    target_structure_node_id=target.structure_node_id,
                    related_structure_node_ids=(parent.structure_node_id,),
                    evidence_source_refs=target.source_refs,
                    origin=StructureOptimizationIssueOrigin.BASE_RESOLVER,
                    confidence=StructureOptimizationConfidence.HIGH,
                    reason_codes=("late_partition_boundary",),
                ),
            )

        provider = _FakeOptimizerProvider(
            overview_builder=overview_builder,
            repair_builder=lambda request: (
                BoundarySplitStructureOperationV1(
                    structure_node_id=target.structure_node_id,
                    parts=(
                        BoundarySplitPartV1(
                            source_spans=(
                                StructureSourceSpanV1(
                                    source_ref=source_ref,
                                    start_offset=0,
                                    end_offset=6,
                                ),
                            )
                        ),
                        BoundarySplitPartV1(
                            source_spans=(
                                StructureSourceSpanV1(
                                    source_ref=source_ref,
                                    start_offset=6,
                                    end_offset=len("Other content."),
                                ),
                            )
                        ),
                    ),
                ),
            ),
        )
        expected_partitions = plan_structure_overview_requests(
            self.source_ir,
            self.base_effective,
            max_nodes=5,
        )

        outcome = optimize_structure_with_provider(
            self.source_ir,
            self.base_tree,
            (),
            provider,
            overview_max_nodes=5,
        )

        self.assertEqual(outcome.status, StructureOptimizationStatus.APPLIED)
        self.assertEqual(
            provider.calls,
            [
                *(["overview_structure"] * len(expected_partitions)),
                "repair_structure",
            ],
        )
        self.assertEqual(
            len(outcome.provider_runs),
            len(expected_partitions) + 1,
        )
        self.assertEqual(
            len(set(run.provider_run_id for run in outcome.provider_runs)),
            len(outcome.provider_runs),
        )
        self.assertEqual(len(outcome.overview.issues), 1)
        self.assertEqual(outcome.effective_snapshot.revision_number, 1)

    def test_partitioned_overview_assigns_cross_partition_relation_once(
        self,
    ) -> None:
        source = self.nodes_by_text["Alpha one. Alpha two."]
        target = self.nodes_by_text["Other content."]
        proposal = create_structure_patch_proposal(
            self.source_ir,
            self.base_tree,
            (),
            (
                AddStructureRelationOperationV1(
                    kind=StructureRelationKind.REFERENCES,
                    source_structure_node_id=source.structure_node_id,
                    target_structure_node_id=target.structure_node_id,
                    source_refs=target.source_refs,
                ),
            ),
        )
        patch = validate_structure_patch_proposal(
            proposal,
            self.source_ir,
            self.base_tree,
            (),
        )
        applied = apply_structure_patch(
            patch,
            self.source_ir,
            self.base_tree,
            (),
        )
        added_relation_id = next(
            relation.relation_id
            for relation in applied.effective_snapshot.relations
            if relation.kind is StructureRelationKind.REFERENCES
        )

        requests = plan_structure_overview_requests(
            self.source_ir,
            applied.effective_snapshot,
            max_nodes=6,
        )

        inspected_relation_ids = [
            relation_id
            for request in requests
            for relation_id in request.inspection_structure_relation_ids
        ]
        self.assertEqual(
            inspected_relation_ids.count(added_relation_id),
            1,
        )
        relation_request = next(
            request
            for request in requests
            if added_relation_id in request.inspection_structure_relation_ids
        )
        visible_ids = {node.structure_node_id for node in relation_request.nodes}
        self.assertTrue(
            {
                source.structure_node_id,
                target.structure_node_id,
            }.issubset(visible_ids)
        )

    def test_unresolved_overview_partition_stops_before_repair(self) -> None:
        provider = _FakeOptimizerProvider(
            overview_builder=lambda request: (),
            repair_builder=None,
            overview_status_builder=lambda request: (
                SemanticProviderRunStatus.TIMEOUT
                if request.partition_index == 1
                else SemanticProviderRunStatus.SUCCESS
            ),
        )

        outcome = optimize_structure_with_provider(
            self.source_ir,
            self.base_tree,
            (),
            provider,
            overview_max_nodes=5,
        )

        self.assertEqual(
            outcome.status,
            StructureOptimizationStatus.OVERVIEW_UNRESOLVED,
        )
        self.assertEqual(
            provider.calls,
            ["overview_structure", "overview_structure"],
        )
        self.assertEqual(len(outcome.provider_runs), 2)
        self.assertEqual(outcome.overview.issues, ())
        self.assertEqual(outcome.effective_snapshot.revision_number, 0)


class _FakeOptimizerProvider:
    provider_id = "fake-structure-optimizer"
    provider_version = "1.0.0"
    model_id = "fake-model"

    def __init__(
        self,
        *,
        overview_builder: Callable[
            [StructureOverviewRequestV1],
            tuple[StructureOptimizationIssueV1, ...],
        ],
        region_builder: Callable[
            [StructureOverviewRequestV1],
            tuple[StructureDocumentRegionV1, ...],
        ]
        | None = None,
        repair_builder: Callable[
            [StructureRepairRequestV1],
            tuple[StructurePatchOperationV1, ...],
        ]
        | None,
        overview_status: SemanticProviderRunStatus = (
            SemanticProviderRunStatus.SUCCESS
        ),
        repair_status: SemanticProviderRunStatus = (SemanticProviderRunStatus.SUCCESS),
        overview_status_builder: Callable[
            [StructureOverviewRequestV1],
            SemanticProviderRunStatus,
        ]
        | None = None,
        mismatch_overview_binding: bool = False,
    ) -> None:
        self._overview_builder = overview_builder
        self._region_builder = region_builder
        self._repair_builder = repair_builder
        self._overview_status = overview_status
        self._overview_status_builder = overview_status_builder
        self._repair_status = repair_status
        self._mismatch_overview_binding = mismatch_overview_binding
        self.calls: list[str] = []

    def overview_structure(
        self,
        request: StructureOverviewRequestV1,
    ) -> StructureOverviewProviderResultV1:
        self.calls.append("overview_structure")
        overview_status = (
            self._overview_status_builder(request)
            if self._overview_status_builder is not None
            else self._overview_status
        )
        issues = (
            self._overview_builder(request)
            if overview_status is SemanticProviderRunStatus.SUCCESS
            else ()
        )
        regions = (
            self._region_builder(request)
            if overview_status is SemanticProviderRunStatus.SUCCESS
            and self._region_builder is not None
            else ()
        )
        return StructureOverviewProviderResultV1(
            source_ir_id=(
                "wrong-source-ir"
                if self._mismatch_overview_binding
                else request.source_ir_id
            ),
            effective_snapshot_id=request.effective_snapshot_id,
            document_regions=regions,
            issues=issues,
            run=_provider_run(
                "overview_structure",
                overview_status,
                request_key=f"partition:{request.partition_index}",
            ),
        )

    def repair_structure(
        self,
        request: StructureRepairRequestV1,
    ) -> StructureRepairProviderResultV1:
        self.calls.append("repair_structure")
        if self._repair_builder is None:
            raise AssertionError("repair_structure should not have been called")
        operations = (
            self._repair_builder(request)
            if self._repair_status is SemanticProviderRunStatus.SUCCESS
            else ()
        )
        return StructureRepairProviderResultV1(
            source_ir_id=request.source_ir_id,
            effective_snapshot_id=request.effective_snapshot_id,
            operations=operations,
            run=_provider_run(
                "repair_structure",
                self._repair_status,
                request_key=request.effective_snapshot_id,
            ),
        )


def _real_optimizer_provider(client: Mock) -> BailianQwenStructureOptimizerProvider:
    return BailianQwenStructureOptimizerProvider(
        api_key="test-api-key",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        max_retries=0,
        http_client=client,
        clock=lambda: _NOW,
        monotonic=lambda: 1.0,
        sleep=lambda _seconds: None,
    )


def _model_input_from_wire(wire_payload: object) -> dict[str, object]:
    if not isinstance(wire_payload, dict):
        raise AssertionError("provider wire payload must be an object")
    messages = wire_payload["messages"]
    if not isinstance(messages, list):
        raise AssertionError("provider messages must be a list")
    user_text = messages[1]["content"][0]["text"]
    decoded = json.loads(user_text)
    return decoded["input"]


def _rows_by_column(
    model_input: dict[str, object],
    columns_key: str,
    rows_key: str,
) -> list[dict[str, object]]:
    columns = model_input[columns_key]
    rows = model_input[rows_key]
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise AssertionError("compact provider tables must use JSON arrays")
    return [dict(zip(columns, row, strict=True)) for row in rows]


def _model_response(content: dict[str, object]) -> Mock:
    response = Mock()
    response.status_code = 200
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(content, ensure_ascii=False),
                }
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
        },
    }
    response.json.return_value = payload
    response.content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return response


def _provider_run(
    operation: str,
    status: SemanticProviderRunStatus,
    *,
    request_key: str = "",
) -> SemanticProviderRunRecord:
    request_sha256 = hashlib.sha256(
        f"request:{operation}:{request_key}".encode()
    ).hexdigest()
    response_sha256 = (
        hashlib.sha256(f"response:{operation}:{request_key}".encode()).hexdigest()
        if status is SemanticProviderRunStatus.SUCCESS
        else None
    )
    error_code = (
        None if status is SemanticProviderRunStatus.SUCCESS else "provider_timeout"
    )
    provider_run_id = stable_semantic_provider_run_id(
        operation=operation,
        provider_id=_FakeOptimizerProvider.provider_id,
        model_id=_FakeOptimizerProvider.model_id,
        request_sha256=request_sha256,
        response_sha256=response_sha256,
        status=status,
        started_at=_NOW,
    )
    return SemanticProviderRunRecord(
        provider_run_id=provider_run_id,
        operation=operation,
        provider_id=_FakeOptimizerProvider.provider_id,
        provider_version=_FakeOptimizerProvider.provider_version,
        model_id=_FakeOptimizerProvider.model_id,
        prompt_version=f"synthetic-{operation}/v1",
        response_schema_version=f"synthetic-{operation}-response/v1",
        request_sha256=request_sha256,
        response_sha256=response_sha256,
        status=status,
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=0,
        request_count=1,
        retry_count=0,
        error_code=error_code,
    )


def _source_ir_fixture() -> SourceIRSnapshot:
    nodes: list[SourceNode] = []
    for source_order, raw_text in enumerate(_TEXTS):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=source_order,
            bbox=(
                80.0,
                80.0 + source_order * 25.0,
                520.0,
                98.0 + source_order * 25.0,
            ),
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=_SOURCE_SHA256,
                    adapter_id=_ADAPTER_ID,
                    adapter_version=_ADAPTER_VERSION,
                    kind=SourceNodeKind.PARAGRAPH,
                    native_locator=locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
        )
    document_id = stable_document_id(_SOURCE_SHA256)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=_SOURCE_SHA256,
            adapter_id=_ADAPTER_ID,
            adapter_version=_ADAPTER_VERSION,
        ),
        document_id=document_id,
        source_sha256=_SOURCE_SHA256,
        adapter_id=_ADAPTER_ID,
        adapter_version=_ADAPTER_VERSION,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(nodes),
        created_at=_NOW,
    )


def _structure_node_for_source(
    nodes: tuple[StructureNode, ...],
    source_node_id: str,
) -> StructureNode:
    return next(
        node for node in nodes if node.identity_key == f"source:{source_node_id}"
    )


def _node_by_id(
    nodes: tuple[StructureNode, ...],
    structure_node_id: str,
) -> StructureNode:
    return next(node for node in nodes if node.structure_node_id == structure_node_id)


if __name__ == "__main__":
    unittest.main()
