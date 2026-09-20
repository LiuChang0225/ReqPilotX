from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import re
import threading
import time
import unittest
from datetime import datetime, timezone

from app.application.extraction.audit_structure import (
    StructureAuditStatus,
    audit_structure_with_provider,
    plan_structure_audit_provider_requests,
)
from app.application.extraction.provider_source_policy import (
    ProviderSourceIRRejected,
    ProviderSourceIRRejectionCode,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
    stable_semantic_provider_run_id,
)
from app.domain.extraction.source_ir import SourceIRStatus, SourceNodeKind
from app.domain.extraction.structure import (
    StructureNodeKind,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.domain.extraction.structure_revision import (
    ContinuationMergeStructureOperationV1,
)
from app.ports.structure_audit_provider import (
    StructureAuditCategory,
    StructureAuditDecision,
    StructureAuditProviderPort,
    StructureAuditProviderResult,
    StructureAuditProposalEvidence,
    StructureAuditRequest,
)
from tests.test_semantics import _flat_source
from tests.test_structure_tree import _continued_pdf_table_source


NOW = datetime(2026, 7, 24, 9, 0, tzinfo=timezone.utc)


class StructureAuditTests(unittest.TestCase):
    def test_port_is_independent_and_runtime_checkable(self) -> None:
        provider = _FakeAuditProvider(lambda _request: ())

        self.assertIsInstance(provider, StructureAuditProviderPort)
        self.assertFalse(hasattr(provider, "classify_nodes"))
        self.assertFalse(hasattr(provider, "assemble_requirements"))

    def test_incomplete_source_is_rejected_before_audit_provider_call(
        self,
    ) -> None:
        source, _ = _flat_source(((SourceNodeKind.HEADING, "1 Scope"),))
        tree = build_structure_tree(source)

        for status in (SourceIRStatus.PARTIAL, SourceIRStatus.FAILED):
            with self.subTest(status=status.value):
                provider = _FakeAuditProvider(lambda _request: ())
                incomplete = source.model_copy(update={"status": status})

                with self.assertRaises(ProviderSourceIRRejected) as captured:
                    audit_structure_with_provider(
                        incomplete,
                        tree,
                        provider,
                    )

                self.assertIs(
                    captured.exception.code,
                    ProviderSourceIRRejectionCode.SOURCE_IR_NOT_COMPLETE,
                )
                self.assertIs(captured.exception.source_ir_status, status)
                self.assertEqual(
                    captured.exception.operation,
                    "structure_audit",
                )
                self.assertEqual(provider.requests, [])

    def test_p20_false_root_five_can_be_flagged_from_physical_outline_spine(
        self,
    ) -> None:
        source, source_ids = _flat_source(
            (
                (SourceNodeKind.HEADING, "2 Technical requirements"),
                (SourceNodeKind.PARAGRAPH, "5\nEddy-current rotor assembly"),
                (SourceNodeKind.HEADING, "2.12.5 Verification changes"),
                (SourceNodeKind.HEADING, "3 Quality requirements"),
                (SourceNodeKind.HEADING, "4 Deliverables"),
                (SourceNodeKind.HEADING, "5 Data requirements"),
            )
        )
        tree = build_structure_tree(source)

        def decide(request: StructureAuditRequest):
            semantic_nodes = {
                node.target_structure_node_id: node
                for node in (
                    *request.evidence_request.nodes,
                    *request.evidence_request.context_nodes,
                )
            }
            spine = [
                semantic_nodes[node_id]
                for node_id in request.outline_spine_structure_node_ids
            ]
            fives = [node for node in spine if node.explicit_label == "5"]
            self.assertEqual(len(fives), 2)
            physical = {
                item.structure_node_id: item.physical_source_orders
                for item in request.physical_order
            }
            false_root, real_root = sorted(
                fives,
                key=lambda node: physical[node.target_structure_node_id],
            )
            root_two = next(node for node in spine if node.explicit_label == "2")
            if false_root.target_structure_node_id not in {
                node.target_structure_node_id for node in request.evidence_request.nodes
            }:
                return ()
            return (
                StructureAuditDecision(
                    category=StructureAuditCategory.POSSIBLE_MISPLACED_PARENT,
                    target_structure_node_id=false_root.target_structure_node_id,
                    related_structure_node_ids=(
                        root_two.target_structure_node_id,
                        real_root.target_structure_node_id,
                    ),
                    evidence_source_refs=(
                        false_root.evidence[0].source_ref,
                        root_two.evidence[0].source_ref,
                        real_root.evidence[0].source_ref,
                    ),
                    reason_codes=("duplicate_root_label_inside_prior_section",),
                ),
            )

        provider = _FakeAuditProvider(decide)

        result = audit_structure_with_provider(source, tree, provider)

        self.assertIs(result.status, StructureAuditStatus.COMPLETE)
        self.assertEqual(len(result.findings), 1)
        finding = result.findings[0]
        self.assertIs(
            finding.category,
            StructureAuditCategory.POSSIBLE_MISPLACED_PARENT,
        )
        self.assertEqual(
            finding.evidence_source_refs[0].source_id,
            source_ids[1],
        )
        request = provider.requests[0]
        physical = {
            item.structure_node_id: item.physical_source_orders
            for item in request.physical_order
        }
        nested_2125 = next(
            node
            for node in request.evidence_request.nodes
            if node.explicit_label == "2.12.5"
        )
        false_five = next(
            node
            for node in request.evidence_request.nodes
            if node.explicit_label == "5"
            and node.evidence[0].source_ref.source_id == source_ids[1]
        )
        self.assertLess(
            physical[false_five.target_structure_node_id],
            physical[nested_2125.target_structure_node_id],
        )
        self.assertGreater(false_five.source_order, nested_2125.source_order)
        physical_windows = plan_structure_audit_provider_requests(
            source,
            tree,
            max_nodes_per_request=1,
        )
        self.assertEqual(
            tuple(
                request.evidence_request.nodes[0].evidence[0].source_ref.source_id
                for request in physical_windows
            )[:3],
            (source_ids[0], source_ids[1], source_ids[2]),
        )

    def test_collapsed_64_numbered_content_can_be_flagged_as_missed_boundary(
        self,
    ) -> None:
        source, _source_ids = _flat_source(
            (
                (SourceNodeKind.HEADING, "6.4 Test requirements"),
                (SourceNodeKind.PARAGRAPH, "6．4．1． Electrical performance"),
                (SourceNodeKind.PARAGRAPH, "6．4．1．1． Meet all performance needs"),
            )
        )
        tree = build_structure_tree(source)

        def decide(request: StructureAuditRequest):
            for node in request.evidence_request.nodes:
                if node.kind is not StructureNodeKind.CONTENT:
                    continue
                evidence = node.evidence[0]
                if re.match(
                    r"^6[.．]4[.．]\d+(?:[.．]\d+)*[.．]",
                    evidence.text,
                ):
                    return (
                        StructureAuditDecision(
                            category=(StructureAuditCategory.POSSIBLE_MISSED_BOUNDARY),
                            target_structure_node_id=node.target_structure_node_id,
                            evidence_source_refs=(evidence.source_ref,),
                            reason_codes=("numbered_content_below_section",),
                        ),
                    )
            return ()

        provider = _FakeAuditProvider(decide)

        result = audit_structure_with_provider(source, tree, provider)

        self.assertIs(result.status, StructureAuditStatus.COMPLETE)
        self.assertEqual(len(result.findings), 1)
        self.assertIs(
            result.findings[0].category,
            StructureAuditCategory.POSSIBLE_MISSED_BOUNDARY,
        )
        audited_request = provider.requests[0].evidence_request
        self.assertEqual(len(audited_request.nodes), 3)
        self.assertEqual(
            [node.kind for node in audited_request.nodes],
            [
                StructureNodeKind.SECTION,
                StructureNodeKind.CONTENT,
                StructureNodeKind.CONTENT,
            ],
        )

    def test_planner_compacts_semantic_context_and_carries_continuation_facts(
        self,
    ) -> None:
        source, first_source_table, second_source_table, _ = (
            _continued_pdf_table_source()
        )
        tree = build_structure_tree(source)
        nodes_by_source_id = {
            source_ref.source_id: node
            for node in tree.nodes
            for source_ref in node.source_refs
            if source_ref.kind is StructureSourceRefKind.SOURCE_NODE
        }
        first_table = nodes_by_source_id[first_source_table.source_node_id]
        second_table = nodes_by_source_id[second_source_table.source_node_id]

        requests = plan_structure_audit_provider_requests(
            source,
            tree,
            max_nodes_per_request=32,
        )
        request = next(
            item
            for item in requests
            if first_table.structure_node_id
            in item.evidence_request.scope_structure_node_ids
        )
        request_ids = {
            node.target_structure_node_id
            for node in (
                *request.evidence_request.nodes,
                *request.evidence_request.context_nodes,
            )
        }

        self.assertIn(second_table.structure_node_id, request_ids)
        self.assertEqual(len(request.current_continuations), 1)
        continuation = request.current_continuations[0]
        self.assertEqual(
            (
                continuation.source_structure_node_id,
                continuation.target_structure_node_id,
            ),
            (
                first_table.structure_node_id,
                second_table.structure_node_id,
            ),
        )
        for node in (
            *request.evidence_request.nodes,
            *request.evidence_request.context_nodes,
        ):
            self.assertEqual(node.provision_structure_node_ids, ())
            self.assertTrue(
                {item.source_ref for item in node.evidence}.issubset(
                    set(node.target_source_refs)
                )
            )

    def test_ungrounded_continuation_merge_is_rejected_at_audit_boundary(
        self,
    ) -> None:
        source, source_ids = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Scope"),
                (SourceNodeKind.PARAGRAPH, "First source fragment"),
                (SourceNodeKind.PARAGRAPH, "Second source fragment"),
            )
        )
        tree = build_structure_tree(source)
        nodes_by_source_id = {
            ref.source_id: node
            for node in tree.nodes
            for ref in node.source_refs
            if ref.kind is StructureSourceRefKind.SOURCE_NODE
        }
        primary = nodes_by_source_id[source_ids[1]]
        continuation = nodes_by_source_id[source_ids[2]]

        def decide(request: StructureAuditRequest):
            request_nodes = {
                node.target_structure_node_id: node
                for node in (
                    *request.evidence_request.nodes,
                    *request.evidence_request.context_nodes,
                )
            }
            if primary.structure_node_id not in {
                node.target_structure_node_id for node in request.evidence_request.nodes
            }:
                return ()
            return (
                StructureAuditDecision(
                    category=StructureAuditCategory.POSSIBLE_DUPLICATE_CONTENT,
                    target_structure_node_id=primary.structure_node_id,
                    related_structure_node_ids=(continuation.structure_node_id,),
                    evidence_source_refs=(
                        request_nodes[primary.structure_node_id].evidence[0].source_ref,
                        request_nodes[continuation.structure_node_id]
                        .evidence[0]
                        .source_ref,
                    ),
                    reason_codes=("synthetic_ungrounded_merge",),
                    proposed_operations=(
                        ContinuationMergeStructureOperationV1(
                            primary_structure_node_id=primary.structure_node_id,
                            continuation_structure_node_id=(
                                continuation.structure_node_id
                            ),
                        ),
                    ),
                    proposal_evidence=StructureAuditProposalEvidence.HIGH,
                ),
            )

        result = audit_structure_with_provider(
            source,
            tree,
            _FakeAuditProvider(decide),
        )

        self.assertIs(result.status, StructureAuditStatus.UNRESOLVED)
        self.assertEqual(result.findings, ())
        self.assertEqual(result.rejected_finding_count, 1)

    def test_successful_empty_findings_are_a_complete_audit(self) -> None:
        source, _ = _flat_source(((SourceNodeKind.HEADING, "1 Scope"),))
        tree = build_structure_tree(source)
        provider = _FakeAuditProvider(lambda _request: ())

        result = audit_structure_with_provider(source, tree, provider)

        self.assertIs(result.status, StructureAuditStatus.COMPLETE)
        self.assertEqual(result.findings, ())
        self.assertEqual(result.resolved_window_count, 1)
        self.assertEqual(result.unresolved_window_count, 0)

    def test_long_single_provision_is_split_by_audit_not_semantic_budget(
        self,
    ) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Long provision"),
                *tuple(
                    (SourceNodeKind.PARAGRAPH, f"detail line {index}")
                    for index in range(81)
                ),
            )
        )
        tree = build_structure_tree(source)

        requests = plan_structure_audit_provider_requests(
            source,
            tree,
            max_nodes_per_request=32,
        )

        self.assertGreaterEqual(len(requests), 3)
        self.assertTrue(
            all(1 <= len(item.evidence_request.nodes) <= 32 for item in requests)
        )
        self.assertEqual(
            {
                node.target_structure_node_id
                for request in requests
                for node in request.evidence_request.nodes
            },
            {
                node.structure_node_id
                for node in tree.nodes
                if node.kind is not StructureNodeKind.DOCUMENT
            },
        )

    def test_many_root_sections_do_not_expand_every_request_context(
        self,
    ) -> None:
        source, _ = _flat_source(
            tuple(
                (SourceNodeKind.HEADING, f"{index} Root section")
                for index in range(1, 101)
            )
        )
        tree = build_structure_tree(source)

        requests = plan_structure_audit_provider_requests(
            source,
            tree,
            max_nodes_per_request=32,
        )

        self.assertGreater(len(requests), 1)
        self.assertTrue(
            all(
                len(request.evidence_request.nodes)
                + len(request.evidence_request.context_nodes)
                <= 34
                for request in requests
            )
        )

    def test_unlinked_adjacent_table_is_visible_across_audit_window(
        self,
    ) -> None:
        source, first_source_table, second_source_table, _ = (
            _continued_pdf_table_source(same_section=False)
        )
        tree = build_structure_tree(source)
        self.assertFalse(
            any(
                relation.kind is StructureRelationKind.CONTINUES
                for relation in tree.relations
            )
        )
        table_nodes_by_source_id = {
            ref.source_id: node
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE
            for ref in node.source_refs
            if ref.kind is StructureSourceRefKind.SOURCE_NODE
        }
        first_table = table_nodes_by_source_id[first_source_table.source_node_id]
        second_table = table_nodes_by_source_id[second_source_table.source_node_id]

        requests = plan_structure_audit_provider_requests(
            source,
            tree,
            max_nodes_per_request=1,
        )
        request = next(
            item
            for item in requests
            if first_table.structure_node_id
            in item.evidence_request.scope_structure_node_ids
        )
        request_ids = {
            node.target_structure_node_id
            for node in (
                *request.evidence_request.nodes,
                *request.evidence_request.context_nodes,
            )
        }

        self.assertIn(second_table.structure_node_id, request_ids)
        self.assertEqual(request.current_continuations, ())

    def test_out_of_window_target_related_or_evidence_fails_closed(self) -> None:
        source, _ = _flat_source(((SourceNodeKind.HEADING, "1 Scope"),))
        tree = build_structure_tree(source)
        foreign_structure_id = "structure-node-" + "f" * 32
        foreign_source_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id="node-" + "f" * 32,
        )

        def invalid_decision(
            request: StructureAuditRequest,
            invalid_kind: str,
        ) -> tuple[StructureAuditDecision, ...]:
            target = request.evidence_request.nodes[0]
            target_id = target.target_structure_node_id
            related = ()
            evidence = (target.evidence[0].source_ref,)
            if invalid_kind == "target":
                target_id = foreign_structure_id
            elif invalid_kind == "related":
                related = (foreign_structure_id,)
            else:
                evidence = (foreign_source_ref,)
            return (
                StructureAuditDecision(
                    category=StructureAuditCategory.POSSIBLE_SOURCE_ORDER_ERROR,
                    target_structure_node_id=target_id,
                    related_structure_node_ids=related,
                    evidence_source_refs=evidence,
                    reason_codes=("synthetic_invalid_output",),
                ),
            )

        for invalid_kind in ("target", "related", "evidence"):
            with self.subTest(invalid_kind=invalid_kind):
                provider = _FakeAuditProvider(
                    lambda request, kind=invalid_kind: invalid_decision(
                        request,
                        kind,
                    )
                )

                result = audit_structure_with_provider(source, tree, provider)

                self.assertIs(result.status, StructureAuditStatus.UNRESOLVED)
                self.assertEqual(result.findings, ())
                self.assertEqual(result.resolved_window_count, 0)
                self.assertEqual(result.unresolved_window_count, 1)
                self.assertTrue(result.unresolved_scope_structure_node_ids)
                self.assertIs(
                    result.provider_runs[0].status,
                    SemanticProviderRunStatus.SUCCESS,
                )

    def test_application_rejects_only_invalid_peer_finding(self) -> None:
        source, _ = _flat_source(((SourceNodeKind.HEADING, "1 Scope"),))
        tree = build_structure_tree(source)
        foreign_structure_id = "structure-node-" + "f" * 32

        def mixed(request: StructureAuditRequest):
            target = request.evidence_request.nodes[0]
            valid = StructureAuditDecision(
                category=StructureAuditCategory.POSSIBLE_MISSED_BOUNDARY,
                target_structure_node_id=target.target_structure_node_id,
                evidence_source_refs=(target.evidence[0].source_ref,),
                reason_codes=("synthetic_valid_finding",),
            )
            invalid = valid.model_copy(
                update={"target_structure_node_id": foreign_structure_id}
            )
            return (valid, invalid)

        result = audit_structure_with_provider(
            source,
            tree,
            _FakeAuditProvider(mixed),
        )

        self.assertIs(result.status, StructureAuditStatus.UNRESOLVED)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.rejected_finding_count, 1)

    def test_non_success_provider_run_is_unresolved_without_findings(self) -> None:
        source, _ = _flat_source(((SourceNodeKind.HEADING, "1 Scope"),))
        tree = build_structure_tree(source)
        provider = _FakeAuditProvider(
            lambda _request: (),
            status_for_request=lambda _request: SemanticProviderRunStatus.TIMEOUT,
        )

        result = audit_structure_with_provider(source, tree, provider)

        self.assertIs(result.status, StructureAuditStatus.UNRESOLVED)
        self.assertEqual(result.findings, ())
        self.assertIs(
            result.provider_runs[0].status,
            SemanticProviderRunStatus.TIMEOUT,
        )

    def test_one_failed_window_yields_partial_without_inventing_findings(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Scope"),
                (SourceNodeKind.HEADING, "2 Product"),
                (SourceNodeKind.HEADING, "3 Verification"),
            )
        )
        tree = build_structure_tree(source)

        def status(request: StructureAuditRequest) -> SemanticProviderRunStatus:
            return (
                SemanticProviderRunStatus.TIMEOUT
                if request.evidence_request.nodes[0].explicit_label == "2"
                else SemanticProviderRunStatus.SUCCESS
            )

        provider = _FakeAuditProvider(
            lambda _request: (),
            status_for_request=status,
        )

        result = audit_structure_with_provider(
            source,
            tree,
            provider,
            max_nodes_per_request=1,
        )

        self.assertIs(result.status, StructureAuditStatus.PARTIAL)
        self.assertEqual(result.findings, ())
        self.assertEqual(result.resolved_window_count, 2)
        self.assertEqual(result.unresolved_window_count, 1)
        self.assertEqual(
            tuple(run.status for run in result.provider_runs),
            (
                SemanticProviderRunStatus.SUCCESS,
                SemanticProviderRunStatus.TIMEOUT,
                SemanticProviderRunStatus.SUCCESS,
            ),
        )

    def test_rejected_peer_marks_window_partial_but_keeps_valid_finding(
        self,
    ) -> None:
        source, _ = _flat_source(((SourceNodeKind.HEADING, "1 Scope"),))
        tree = build_structure_tree(source)

        def decide(request: StructureAuditRequest):
            target = request.evidence_request.nodes[0]
            return (
                StructureAuditDecision(
                    category=StructureAuditCategory.POSSIBLE_MISSED_BOUNDARY,
                    target_structure_node_id=target.target_structure_node_id,
                    evidence_source_refs=(target.evidence[0].source_ref,),
                    reason_codes=("synthetic_valid_finding",),
                ),
            )

        provider = _FakeAuditProvider(
            decide,
            rejected_for_request=lambda _request: 1,
        )

        result = audit_structure_with_provider(source, tree, provider)

        self.assertIs(result.status, StructureAuditStatus.UNRESOLVED)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.rejected_finding_count, 1)
        self.assertEqual(result.resolved_window_count, 0)
        self.assertEqual(result.unresolved_window_count, 1)

    def test_duplicate_provider_suspicions_use_one_stable_identity(self) -> None:
        source, _ = _flat_source(((SourceNodeKind.HEADING, "1 Scope"),))
        tree = build_structure_tree(source)

        def duplicate(request: StructureAuditRequest):
            target = request.evidence_request.nodes[0]
            decision = StructureAuditDecision(
                category=StructureAuditCategory.POSSIBLE_DUPLICATE_CONTENT,
                target_structure_node_id=target.target_structure_node_id,
                evidence_source_refs=(target.evidence[0].source_ref,),
                reason_codes=("synthetic_duplicate_probe",),
            )
            return (decision, decision)

        result = audit_structure_with_provider(
            source,
            tree,
            _FakeAuditProvider(duplicate),
        )

        self.assertIs(result.status, StructureAuditStatus.COMPLETE)
        self.assertEqual(len(result.findings), 1)
        self.assertTrue(
            result.findings[0].finding_id.startswith("structure-audit-finding-")
        )

    def test_one_raised_window_preserves_completed_peer_windows(self) -> None:
        source, _ = _flat_source(
            tuple((SourceNodeKind.HEADING, f"{index} Section") for index in range(1, 4))
        )
        tree = build_structure_tree(source)
        provider = _FakeAuditProvider(
            lambda _request: (),
            raise_for_request=lambda request: (
                request.evidence_request.nodes[0].source_order == 1
            ),
        )

        result = audit_structure_with_provider(
            source,
            tree,
            provider,
            max_nodes_per_request=1,
        )

        self.assertIs(result.status, StructureAuditStatus.PARTIAL)
        self.assertEqual(result.resolved_window_count, 2)
        self.assertEqual(result.unresolved_window_count, 1)
        self.assertEqual(result.failed_window_count, 1)
        self.assertEqual(len(result.provider_runs), 2)
        self.assertEqual(result.findings, ())

    def test_four_way_concurrency_preserves_physical_request_order(self) -> None:
        source, _ = _flat_source(
            tuple(
                (SourceNodeKind.HEADING, f"{index} Section {index}")
                for index in range(1, 5)
            )
        )
        tree = build_structure_tree(source)
        planned = plan_structure_audit_provider_requests(
            source,
            tree,
            max_nodes_per_request=1,
        )
        expected_targets = tuple(
            request.evidence_request.nodes[0].target_structure_node_id
            for request in planned
        )
        physical_index = {
            target_id: index for index, target_id in enumerate(expected_targets)
        }

        def delay(request: StructureAuditRequest) -> float:
            target_id = request.evidence_request.nodes[0].target_structure_node_id
            return 0.04 - (physical_index[target_id] * 0.008)

        def decide(request: StructureAuditRequest):
            target = request.evidence_request.nodes[0]
            return (
                StructureAuditDecision(
                    category=StructureAuditCategory.POSSIBLE_SOURCE_ORDER_ERROR,
                    target_structure_node_id=target.target_structure_node_id,
                    evidence_source_refs=(target.evidence[0].source_ref,),
                    reason_codes=("synthetic_order_probe",),
                ),
            )

        provider = _FakeAuditProvider(decide, delay_for_request=delay)

        result = audit_structure_with_provider(
            source,
            tree,
            provider,
            max_nodes_per_request=1,
            max_concurrency=4,
        )

        self.assertIs(result.status, StructureAuditStatus.COMPLETE)
        self.assertEqual(
            tuple(item.target_structure_node_id for item in result.findings),
            expected_targets,
        )
        self.assertEqual(provider.max_active_calls, 4)
        self.assertEqual(len(result.provider_runs), 4)


class _FakeAuditProvider:
    provider_id = "synthetic-structure-auditor"
    provider_version = "1.0.0"
    model_id = "qwen3.7-plus"

    def __init__(
        self,
        decide,
        *,
        status_for_request=None,
        delay_for_request=None,
        rejected_for_request=None,
        raise_for_request=None,
    ) -> None:
        self._decide = decide
        self._status_for_request = status_for_request or (
            lambda _request: SemanticProviderRunStatus.SUCCESS
        )
        self._delay_for_request = delay_for_request or (lambda _request: 0.0)
        self._rejected_for_request = rejected_for_request or (lambda _request: 0)
        self._raise_for_request = raise_for_request or (lambda _request: False)
        self.requests: list[StructureAuditRequest] = []
        self._lock = threading.Lock()
        self._active_calls = 0
        self.max_active_calls = 0

    def audit_structure(
        self,
        request: StructureAuditRequest,
    ) -> StructureAuditProviderResult:
        with self._lock:
            self.requests.append(request)
            self._active_calls += 1
            self.max_active_calls = max(
                self.max_active_calls,
                self._active_calls,
            )
        try:
            if self._raise_for_request(request):
                raise RuntimeError("synthetic audit window failure")
            delay = float(self._delay_for_request(request))
            if delay:
                time.sleep(delay)
            status = self._status_for_request(request)
            rejected_finding_count = int(self._rejected_for_request(request))
            if rejected_finding_count and status is SemanticProviderRunStatus.SUCCESS:
                status = SemanticProviderRunStatus.PARTIAL
            findings = (
                tuple(self._decide(request))
                if status
                in {
                    SemanticProviderRunStatus.SUCCESS,
                    SemanticProviderRunStatus.PARTIAL,
                }
                else ()
            )
            return StructureAuditProviderResult(
                findings=findings,
                run=_run(request, status),
                rejected_finding_count=rejected_finding_count,
            )
        finally:
            with self._lock:
                self._active_calls -= 1


def _run(
    request: StructureAuditRequest,
    status: SemanticProviderRunStatus,
) -> SemanticProviderRunRecord:
    request_key = "|".join(request.evidence_request.scope_structure_node_ids)
    request_sha = hashlib.sha256(request_key.encode("utf-8")).hexdigest()
    response_sha = (
        hashlib.sha256(f"response:{request_key}".encode("utf-8")).hexdigest()
        if status
        in {
            SemanticProviderRunStatus.SUCCESS,
            SemanticProviderRunStatus.PARTIAL,
        }
        else None
    )
    error_code = (
        None
        if status is SemanticProviderRunStatus.SUCCESS
        else "provider_audit_finding_binding_partial"
        if status is SemanticProviderRunStatus.PARTIAL
        else "provider_timeout"
    )
    provider_run_id = stable_semantic_provider_run_id(
        operation="audit_structure",
        provider_id=_FakeAuditProvider.provider_id,
        model_id=_FakeAuditProvider.model_id,
        request_sha256=request_sha,
        response_sha256=response_sha,
        status=status,
        started_at=NOW,
    )
    return SemanticProviderRunRecord(
        provider_run_id=provider_run_id,
        operation="audit_structure",
        provider_id=_FakeAuditProvider.provider_id,
        provider_version=_FakeAuditProvider.provider_version,
        model_id=_FakeAuditProvider.model_id,
        prompt_version="synthetic-structure-audit/v1",
        response_schema_version="structure-audit-provider/v1",
        request_sha256=request_sha,
        response_sha256=response_sha,
        status=status,
        started_at=NOW,
        finished_at=NOW,
        latency_ms=0,
        request_count=1,
        retry_count=0,
        error_code=error_code,
    )


if __name__ == "__main__":
    unittest.main()
