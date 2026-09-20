from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
import hashlib
from datetime import datetime, timezone
from types import SimpleNamespace

from app.application.extraction.llm_provision_batch import (
    LlmProvisionExtractionOutcome,
    LlmProvisionExtractionStatus,
)
from app.application.extraction.llm_requirement_review import (
    build_llm_requirement_model_prediction,
    initial_llm_requirement_review,
)
from app.application.extraction.llm_semantics import (
    LlmRequirementExtractionResult,
    _classification_context_annotations,
    _normalize_model_continuation_parent_semantics,
    _normalize_provision_subtypes,
    _resolve_cross_window_continuation_conflicts,
    _semantic_windows,
    _split_semantic_windows_by_image_budget,
    assemble_requirements_with_provider,
    classify_content_with_provider,
    extract_requirements_with_provider,
)
from app.application.extraction.classify_content import classify_content
from app.application.extraction.locate_llm_scope import LocatedLlmExtractionScope
from app.application.extraction.project_requirements import (
    RequirementProjectionRejected,
    RequirementProjectionRejectionCode,
    project_requirements_from_assembly,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.requirement_assembly import RequirementAssemblyStatus
from app.domain.extraction.requirement_view import (
    RequirementDecisionMode,
    RequirementProjectionRole,
)
from app.domain.extraction.semantic_provider import (
    SemanticProviderRunRecord,
    SemanticProviderRunStatus,
    stable_semantic_provider_run_id,
)
from app.domain.extraction.semantic_terminal_receipts import (
    SemanticUnitTerminalReceipt,
    SemanticUnitTerminalStatus,
)
from app.domain.extraction.semantics import (
    RequirementSubtype,
    SemanticAnnotationStatus,
    SemanticCandidateSource,
    SemanticClass,
)
from app.domain.extraction.source_ir import SourceNodeKind
from app.domain.extraction.structure import (
    StructureNodeKind,
    StructureRelation,
    StructureRelationKind,
    stable_structure_relation_id,
)
from app.ports.semantic_provider import (
    RequirementAssemblyDecision,
    RequirementAssemblyProviderResult,
    SemanticClassificationProviderResult,
    SemanticDecision,
)
from tests.test_semantics import _flat_source, _hierarchical_source, _tabular_source


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


class LlmSemanticPipelineTests(unittest.TestCase):
    def test_adaptive_context_keeps_adjacent_siblings_read_only(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Scope"),
                (SourceNodeKind.HEADING, "1.1 Product"),
                (SourceNodeKind.PARAGRAPH, "Context before."),
                (SourceNodeKind.PARAGRAPH, "Target statement."),
                (SourceNodeKind.PARAGRAPH, "Context after."),
            )
        )
        tree = build_structure_tree(source)
        content_nodes = tuple(
            sorted(
                (node for node in tree.nodes if node.kind is StructureNodeKind.CONTENT),
                key=lambda node: node.source_order,
            )
        )
        self.assertEqual(len(content_nodes), 3)
        target = content_nodes[1]
        target_scaffold = classify_content(
            source,
            tree,
            scope_structure_node_ids=(target.structure_node_id,),
        )
        full_scaffold = classify_content(source, tree)
        nodes_by_id = {node.structure_node_id: node for node in tree.nodes}

        ancestors_only = _classification_context_annotations(
            target_scaffold,
            full_scaffold,
            nodes_by_id=nodes_by_id,
        )
        with_siblings = _classification_context_annotations(
            target_scaffold,
            full_scaffold,
            nodes_by_id=nodes_by_id,
            include_adjacent_siblings=True,
        )

        ancestor_ids = {
            annotation.target_structure_node_id for annotation in ancestors_only
        }
        contextual_ids = {
            annotation.target_structure_node_id for annotation in with_siblings
        }
        self.assertNotIn(content_nodes[0].structure_node_id, ancestor_ids)
        self.assertNotIn(content_nodes[2].structure_node_id, ancestor_ids)
        self.assertIn(content_nodes[0].structure_node_id, contextual_ids)
        self.assertIn(content_nodes[2].structure_node_id, contextual_ids)
        self.assertNotIn(target.structure_node_id, contextual_ids)

    def test_semantic_windows_keep_section_provisions_independent(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Scope"),
                (SourceNodeKind.HEADING, "1.1 Product"),
                (SourceNodeKind.PARAGRAPH, "The product shall operate."),
                (SourceNodeKind.HEADING, "2 Verification"),
                (SourceNodeKind.PARAGRAPH, "The test shall be recorded."),
            )
        )
        tree = build_structure_tree(source)
        scope = tuple(
            item.structure_node_id
            for item in tree.nodes
            if item.parent_structure_node_id is not None
        )

        windows = _semantic_windows(tree, scope, max_nodes_per_request=4)

        self.assertGreaterEqual(len(windows), 3)
        self.assertEqual(tuple(item for window in windows for item in window), scope)
        nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
        for window in windows:
            section_ids = {
                item.structure_node_id
                for item in (nodes_by_id[target_id] for target_id in window)
                if item.kind.value == "section"
            }
            self.assertLessEqual(len(section_ids), 1)

    def test_semantic_windows_can_pack_adjacent_complete_provisions(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Scope"),
                (SourceNodeKind.HEADING, "1.1 Product"),
                (SourceNodeKind.PARAGRAPH, "The product shall operate."),
                (SourceNodeKind.HEADING, "1.2 Verification"),
                (SourceNodeKind.PARAGRAPH, "The test shall be recorded."),
            )
        )
        tree = build_structure_tree(source)
        scope = tuple(
            item.structure_node_id
            for item in tree.nodes
            if item.parent_structure_node_id is not None
        )

        windows = _semantic_windows(
            tree,
            scope,
            max_nodes_per_request=80,
            pack_adjacent_provisions=True,
        )

        self.assertEqual(windows, (scope,))

    def test_image_budget_splits_only_at_source_bound_unit_edges(self) -> None:
        windows = _split_semantic_windows_by_image_budget(
            (("a", "b", "c", "d"),),
            image_pages_by_target={
                "a": frozenset({1}),
                "b": frozenset({1}),
                "c": frozenset({2}),
                "d": frozenset({2}),
            },
            max_images_per_request=1,
        )

        self.assertEqual(windows, (("a", "b"), ("c", "d")))

    def test_image_budget_never_splits_one_indivisible_source_group(self) -> None:
        with self.assertRaisesRegex(ValueError, "source-bound semantic unit group"):
            _split_semantic_windows_by_image_budget(
                (("a", "b"),),
                image_pages_by_target={
                    "a": frozenset({1}),
                    "b": frozenset({2}),
                },
                max_images_per_request=1,
                indivisible_group_by_target={"a": "continued", "b": "continued"},
            )

    def test_distinct_provisions_each_fit_their_own_image_budget(self) -> None:
        windows = (("a", "b"), ("c", "d"))

        result = _split_semantic_windows_by_image_budget(
            windows,
            image_pages_by_target={
                "a": frozenset({1}),
                "b": frozenset({1}),
                "c": frozenset({2}),
                "d": frozenset({2}),
            },
            max_images_per_request=1,
        )

        self.assertEqual(result, windows)

    def test_image_page_map_must_exactly_cover_window_targets(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            _split_semantic_windows_by_image_budget(
                (("a", "b"),),
                image_pages_by_target={"a": frozenset({1})},
                max_images_per_request=1,
            )

    def test_single_node_exceeding_image_budget_fails_before_provider_calls(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "page-image budget"):
            _split_semantic_windows_by_image_budget(
                (("a",),),
                image_pages_by_target={"a": frozenset({1, 2})},
                max_images_per_request=1,
            )

    def test_large_scope_is_split_on_a_hard_node_budget(self) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Synthetic measurement tool",
            leaf_paragraph="The tool shall retain its maximum reading.",
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider()

        classified = classify_content_with_provider(
            source,
            tree,
            provider,
            max_nodes_per_request=2,
        )

        self.assertEqual(len(classified.runs), 3)
        self.assertEqual(provider.classification_call_count, 3)
        self.assertTrue(
            any(request.context_nodes for request in provider.classification_requests)
        )
        self.assertTrue(
            all(
                not set(request.scope_structure_node_ids).intersection(
                    item.target_structure_node_id for item in request.context_nodes
                )
                for request in provider.classification_requests
            )
        )
        self.assertEqual(
            len(classified.semantics.annotations),
            len(classified.semantics.scope_structure_node_ids),
        )

    def test_provision_over_node_budget_is_planned_by_source_bound_units(self) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Synthetic measurement tool",
            leaf_paragraph="The tool shall retain its maximum reading.",
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider()

        classified = classify_content_with_provider(
            source,
            tree,
            provider,
            max_nodes_per_request=1,
        )

        self.assertEqual(
            len(classified.terminal_receipts),
            len(classified.semantics.scope_semantic_unit_ids),
        )
        self.assertEqual(
            tuple(
                target_id
                for request in provider.classification_requests
                for target_id in request.scope_structure_node_ids
            ),
            classified.semantics.scope_structure_node_ids,
        )
        self.assertTrue(
            all(
                request.context_nodes
                for request in provider.classification_requests
                if request.nodes[0].kind is not StructureNodeKind.SECTION
            )
        )

    def test_specific_section_subtype_is_inherited_by_numbered_provision(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Performance requirements"),
                (SourceNodeKind.LIST_ITEM, "1) The force shall be 10 N."),
                (SourceNodeKind.PARAGRAPH, "Measured at room temperature."),
            )
        )
        tree = build_structure_tree(source)
        targets = tuple(
            item for item in tree.nodes if item.parent_structure_node_id is not None
        )
        section, clause, continuation = targets
        decisions = (
            SemanticDecision(
                target_structure_node_id=section.structure_node_id,
                semantic_class=SemanticClass.REQUIREMENT,
                requirement_subtype=RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT,
                reason_codes=("synthetic_model",),
            ),
            SemanticDecision(
                target_structure_node_id=clause.structure_node_id,
                semantic_class=SemanticClass.REQUIREMENT,
                requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
                reason_codes=("synthetic_model",),
            ),
            SemanticDecision(
                target_structure_node_id=continuation.structure_node_id,
                semantic_class=SemanticClass.REQUIREMENT,
                requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
                reason_codes=("synthetic_model",),
            ),
        )

        normalized = _normalize_provision_subtypes(decisions, tree=tree)

        self.assertEqual(
            tuple(item.requirement_subtype for item in normalized),
            (
                RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT,
                RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT,
                RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT,
            ),
        )
        self.assertIn("structural_subtype_inheritance", normalized[1].reason_codes)
        self.assertIn("structural_subtype_inheritance", normalized[2].reason_codes)

        independent = _normalize_provision_subtypes(
            (
                decisions[0],
                decisions[1].model_copy(
                    update={
                        "requirement_subtype": RequirementSubtype.ACCEPTANCE_CRITERION
                    }
                ),
                decisions[2],
            ),
            tree=tree,
        )
        self.assertIs(
            independent[1].requirement_subtype,
            RequirementSubtype.ACCEPTANCE_CRITERION,
        )
        self.assertIs(
            independent[2].requirement_subtype,
            RequirementSubtype.ACCEPTANCE_CRITERION,
        )

    def test_section_only_scope_fails_before_provider_call(self) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Synthetic measurement tool",
            leaf_paragraph="The tool shall retain its maximum reading.",
        )
        tree = build_structure_tree(source)
        target = next(item for item in tree.nodes if item.explicit_label == "4.12.1")
        provider = _FakeProvider()

        with self.assertRaisesRegex(ValueError, "omits provision descendants"):
            classify_content_with_provider(
                source,
                tree,
                provider,
                scope_structure_node_ids=(target.structure_node_id,),
            )

        self.assertEqual(provider.classification_call_count, 0)

    def test_explicit_descendant_scope_retains_full_tree_ancestor_context(self) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Synthetic measurement tool",
            leaf_paragraph="The tool shall retain its maximum reading.",
        )
        tree = build_structure_tree(source)
        target = next(
            item for item in tree.nodes if item.kind is StructureNodeKind.CONTENT
        )
        provider = _FakeProvider()

        classified = classify_content_with_provider(
            source,
            tree,
            provider,
            scope_structure_node_ids=(target.structure_node_id,),
        )

        request = provider.classification_requests[0]
        context_labels = {item.explicit_label for item in request.context_nodes}
        self.assertEqual(request.scope_structure_node_ids, (target.structure_node_id,))
        self.assertTrue({"4", "4.12", "4.12.1"}.issubset(context_labels))
        self.assertTrue(
            set(request.nodes[0].ancestor_structure_node_ids).issuperset(
                item.target_structure_node_id
                for item in request.context_nodes
                if item.explicit_label in {"4", "4.12", "4.12.1"}
            )
        )
        self.assertEqual(len(classified.semantics.annotations), 1)

    def test_leaf_section_exposes_owned_provision_as_auditable_evidence(
        self,
    ) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Synthetic measurement tool",
            leaf_paragraph="The tool shall retain its maximum reading.",
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider()

        classify_content_with_provider(source, tree, provider)

        request = next(
            item
            for item in provider.classification_requests
            if any(node.explicit_label == "4.12.1" for node in item.nodes)
        )
        section = next(
            item for item in request.nodes if item.explicit_label == "4.12.1"
        )
        owned = tuple(
            item.target_structure_node_id
            for item in request.nodes
            if section.target_structure_node_id in item.ancestor_structure_node_ids
        )
        self.assertEqual(section.provision_structure_node_ids, owned)
        self.assertTrue(section.provision_structure_node_ids)
        for classification_request in provider.classification_requests:
            for node in (
                *classification_request.context_nodes,
                *classification_request.nodes,
            ):
                evidence_refs = tuple(item.source_ref for item in node.evidence)
                if node.kind is StructureNodeKind.SECTION:
                    owned_nodes = tuple(
                        item
                        for item in classification_request.nodes
                        if item.target_structure_node_id
                        in node.provision_structure_node_ids
                    )
                    expected_refs = tuple(
                        dict.fromkeys(
                            (
                                *node.target_source_refs,
                                *(
                                    source_ref
                                    for owned_node in owned_nodes
                                    for source_ref in owned_node.target_source_refs
                                ),
                            )
                        )
                    )
                    self.assertEqual(evidence_refs, expected_refs)
                else:
                    self.assertEqual(evidence_refs, node.target_source_refs)
                self.assertEqual(
                    len(node.target_source_refs),
                    len(set(node.target_source_refs)),
                )

    def test_section_provision_stops_at_nested_section_boundary(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Parent"),
                (SourceNodeKind.HEADING, "1.1 Child requirement"),
                (SourceNodeKind.PARAGRAPH, "The product shall operate."),
            )
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider()

        classify_content_with_provider(source, tree, provider)

        parent_request = next(
            item
            for item in provider.classification_requests
            if any(node.explicit_label == "1" for node in item.nodes)
        )
        child_request = next(
            item
            for item in provider.classification_requests
            if any(node.explicit_label == "1.1" for node in item.nodes)
        )
        parent = next(
            item for item in parent_request.nodes if item.explicit_label == "1"
        )
        child = next(
            item for item in child_request.nodes if item.explicit_label == "1.1"
        )
        self.assertEqual(parent.provision_structure_node_ids, ())
        self.assertTrue(child.provision_structure_node_ids)
        self.assertNotIn(
            child.target_structure_node_id,
            parent.provision_structure_node_ids,
        )

    def test_model_classification_and_assembly_project_a_grounded_shadow_view(
        self,
    ) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Force test instrument",
            leaf_paragraph="The force test instrument has a peak-reading function.",
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider()

        classified = classify_content_with_provider(source, tree, provider)
        assembled = assemble_requirements_with_provider(
            source,
            tree,
            classified.semantics,
            provider,
        )
        view = project_requirements_from_assembly(
            source,
            tree,
            classified.semantics,
            assembled.assembly,
        )

        leaf = next(
            item
            for item in classified.semantics.annotations
            if item.requirement_subtype is RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT
        )
        self.assertEqual(leaf.candidate_source, SemanticCandidateSource.MODEL)
        self.assertEqual(leaf.status, SemanticAnnotationStatus.CANDIDATE)
        self.assertIn(
            leaf.provider_run_id,
            {item.provider_run_id for item in classified.runs},
        )
        self.assertTrue(leaf.grounding_spans)
        self.assertTrue(leaf.selected_evidence_source_refs)
        leaf_node = next(
            item
            for item in tree.nodes
            if item.structure_node_id == leaf.target_structure_node_id
        )
        self.assertFalse(
            set(leaf.selected_evidence_source_refs).issubset(
                set(leaf_node.source_refs)
            ),
            "the synthetic Section decision should cite descendant substance",
        )
        self.assertEqual(
            [item.role for item in view.nodes],
            [RequirementProjectionRole.PRIMARY, RequirementProjectionRole.CONTENT],
        )
        serialized = "\n".join(
            (
                classified.semantics.model_dump_json(),
                assembled.assembly.model_dump_json(),
                view.model_dump_json(),
            )
        )
        self.assertNotIn("peak-reading", serialized)
        self.assertIsNone(view.requirement_set_id)

    def test_model_continuation_fragment_is_persisted_and_excluded(self) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Force test instrument",
            leaf_paragraph="The instrument requirement continues on this line.",
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider(mark_content_continuation=True)

        classified = classify_content_with_provider(source, tree, provider)
        continuation = next(
            item
            for item in classified.semantics.annotations
            if item.is_continuation_fragment
        )
        assembled = assemble_requirements_with_provider(
            source,
            tree,
            classified.semantics,
            provider,
        )

        self.assertEqual(continuation.candidate_source, SemanticCandidateSource.MODEL)
        assembled_item = next(
            item
            for item in assembled.assembly.items
            if item.target_structure_node_id == continuation.target_structure_node_id
        )
        self.assertFalse(assembled_item.include_in_view)
        self.assertIs(assembled_item.status, RequirementAssemblyStatus.CANDIDATE)
        view = project_requirements_from_assembly(
            source,
            tree,
            classified.semantics,
            assembled.assembly,
        )
        tree_nodes = {item.structure_node_id: item for item in tree.nodes}
        owner_target = tree_nodes[
            continuation.target_structure_node_id
        ].parent_structure_node_id
        self.assertIsNotNone(owner_target)
        owner_annotation = next(
            item
            for item in classified.semantics.annotations
            if item.target_structure_node_id == owner_target
        )
        owner_view = next(
            item for item in view.nodes if item.target_structure_node_id == owner_target
        )
        self.assertNotIn(
            continuation.target_structure_node_id,
            {item.target_structure_node_id for item in view.nodes},
        )
        self.assertEqual(
            owner_view.semantic_annotation_ids,
            (
                owner_annotation.semantic_annotation_id,
                continuation.semantic_annotation_id,
            ),
        )
        self.assertEqual(
            owner_view.source_span_ids,
            tuple(
                dict.fromkeys(
                    span.grounding_span_id
                    for annotation in (owner_annotation, continuation)
                    for span in annotation.grounding_spans
                    if span.source_ref
                    in {
                        *tree_nodes[owner_target].source_refs,
                        *tree_nodes[continuation.target_structure_node_id].source_refs,
                    }
                )
            ),
        )
        self.assertEqual(
            owner_view.source_refs,
            tuple(
                dict.fromkeys(
                    span.source_ref
                    for annotation in (owner_annotation, continuation)
                    for span in annotation.grounding_spans
                    if span.source_ref
                    in {
                        *tree_nodes[owner_target].source_refs,
                        *tree_nodes[continuation.target_structure_node_id].source_refs,
                    }
                )
            ),
        )
        continuation_relation = next(
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.CONTAINS
            and relation.source_structure_node_id == owner_target
            and relation.target_structure_node_id
            == continuation.target_structure_node_id
        )
        self.assertIn(continuation_relation.relation_id, owner_view.relation_ids)
        self.assertFalse(
            any(
                relation.kind is StructureRelationKind.CONTINUES
                and continuation.target_structure_node_id
                in {
                    relation.source_structure_node_id,
                    relation.target_structure_node_id,
                }
                for relation in tree.relations
            )
        )

        truncated_assembly = assembled.assembly.model_copy(
            update={
                "scope_structure_node_ids": tuple(
                    target_id
                    for target_id in assembled.assembly.scope_structure_node_ids
                    if target_id != continuation.target_structure_node_id
                ),
                "items": tuple(
                    item
                    for item in assembled.assembly.items
                    if item.target_structure_node_id
                    != continuation.target_structure_node_id
                ),
            }
        )
        with self.assertRaises(RequirementProjectionRejected) as caught:
            project_requirements_from_assembly(
                source,
                tree,
                classified.semantics,
                truncated_assembly,
            )
        self.assertIs(
            caught.exception.code,
            RequirementProjectionRejectionCode.ASSEMBLY_MISMATCH,
        )

    def test_rollup_projects_normative_continuation_as_direct_residual(
        self,
    ) -> None:
        source = _tabular_source(
            "4.12.1 Force requirements",
            intro_text="The assembly shall withstand the specified force.",
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider(
            mark_content_continuation=True,
            parameter_rows=True,
        )

        classified = classify_content_with_provider(source, tree, provider)
        assembled = assemble_requirements_with_provider(
            source,
            tree,
            classified.semantics,
            provider,
        )
        view = project_requirements_from_assembly(
            source,
            tree,
            classified.semantics,
            assembled.assembly,
        )

        continuation = next(
            item
            for item in classified.semantics.annotations
            if item.is_continuation_fragment
        )
        continuation_assembly = next(
            item
            for item in assembled.assembly.items
            if item.semantic_unit_id == continuation.semantic_unit_id
        )
        self.assertFalse(continuation_assembly.include_in_view)

        primary = next(
            item
            for item in view.nodes
            if item.role is RequirementProjectionRole.PRIMARY
        )
        residual = next(
            item
            for item in view.nodes
            if item.target_structure_node_id == continuation.owner_structure_node_id
        )
        parameters = tuple(
            item
            for item in view.nodes
            if item.role is RequirementProjectionRole.PARAMETER
        )

        self.assertEqual(primary.decision_mode, RequirementDecisionMode.ROLLUP)
        self.assertEqual(residual.role, RequirementProjectionRole.CHILD)
        self.assertEqual(residual.decision_mode, RequirementDecisionMode.DIRECT)
        self.assertEqual(
            residual.parent_requirement_view_node_id,
            primary.requirement_view_node_id,
        )
        self.assertTrue(parameters)
        self.assertTrue(
            all(
                item.decision_mode is RequirementDecisionMode.DIRECT
                for item in parameters
            )
        )
        self.assertEqual(
            residual.semantic_annotation_ids,
            (continuation.semantic_annotation_id,),
        )
        self.assertNotIn(
            continuation.semantic_annotation_id,
            primary.semantic_annotation_ids,
        )
        self.assertTrue(set(primary.source_refs).isdisjoint(residual.source_refs))
        self.assertEqual(
            primary.full_context_source_refs,
            residual.full_context_source_refs,
        )

        scope = classified.semantics.scope_structure_node_ids
        extraction = LlmRequirementExtractionResult(
            semantics=classified.semantics,
            assembly=assembled.assembly,
            requirement_view=view,
            provider_runs=(*classified.runs, *assembled.runs),
            semantic_terminal_receipts=classified.terminal_receipts,
        )
        outcome = LlmProvisionExtractionOutcome(
            root_section_structure_node_id=scope[0],
            scope_structure_node_ids=scope,
            source_order=0,
            status=LlmProvisionExtractionStatus.SUCCESS,
            extraction=extraction,
            error_code=None,
        )
        located_scope = LocatedLlmExtractionScope(
            locator_version="test-locator/v1",
            locator_sha256=hashlib.sha256(b"test-locator").hexdigest(),
            source_sha256=source.source_sha256,
            root_section_structure_node_ids=(scope[0],),
            scope_structure_node_ids=scope,
        )
        prediction = build_llm_requirement_model_prediction(
            source,
            tree,
            located_scope,
            SimpleNamespace(
                provisions=(outcome,),
                provider_binding=(
                    classified.runs[0].provider_id,
                    classified.runs[0].provider_version,
                    classified.runs[0].model_id,
                ),
            ),
        )
        predicted_residual = next(
            item
            for item in prediction.nodes
            if item.owner_structure_node_id == continuation.owner_structure_node_id
        )
        self.assertTrue(predicted_residual.include_in_view)
        self.assertEqual(
            predicted_residual.role,
            RequirementProjectionRole.CHILD,
        )
        self.assertEqual(
            predicted_residual.decision_mode,
            RequirementDecisionMode.DIRECT,
        )
        self.assertIn(
            "rollup_residual_projection",
            predicted_residual.reason_codes,
        )
        self.assertIsNone(predicted_residual.review_owner_semantic_unit_id)

        reviewed = initial_llm_requirement_review(prediction)
        reviewed_residual = next(
            item
            for item in reviewed.nodes
            if item.review_node_id == predicted_residual.semantic_unit_id
        )
        self.assertEqual(
            reviewed_residual.source_semantic_unit_ids,
            (predicted_residual.semantic_unit_id,),
        )
        self.assertEqual(
            reviewed_residual.parent_review_node_id,
            next(
                item.semantic_unit_id
                for item in prediction.nodes
                if item.owner_structure_node_id == primary.target_structure_node_id
            ),
        )

    def test_model_continuation_inherits_parent_subtype_with_audit_code(self) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Force test instrument",
            leaf_paragraph="The instrument requirement continues on this line.",
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider(
            mark_content_continuation=True,
            continuation_subtype=RequirementSubtype.TEST_METHOD_REQUIREMENT,
        )

        classified = classify_content_with_provider(source, tree, provider)

        content = next(
            item
            for item in classified.semantics.annotations
            if next(
                node
                for node in tree.nodes
                if node.structure_node_id == item.target_structure_node_id
            ).kind
            is StructureNodeKind.CONTENT
        )
        self.assertIs(content.semantic_class, SemanticClass.REQUIREMENT)
        self.assertIs(
            content.requirement_subtype,
            RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT,
        )
        self.assertTrue(content.is_continuation_fragment)
        self.assertIn(
            "continuation_parent_semantics_normalized",
            content.reason_codes,
        )
        assembled = assemble_requirements_with_provider(
            source,
            tree,
            classified.semantics,
            provider,
        )
        view = project_requirements_from_assembly(
            source,
            tree,
            classified.semantics,
            assembled.assembly,
        )
        self.assertTrue(view.nodes)
        self.assertEqual(provider.assembly_call_count, 1)

    def test_model_continuation_flag_is_ignored_for_non_requirement_parent(
        self,
    ) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Force test instrument",
            leaf_paragraph="The instrument requirement continues on this line.",
        )
        tree = build_structure_tree(source)
        content = next(
            node for node in tree.nodes if node.kind is StructureNodeKind.CONTENT
        )
        assert content.parent_structure_node_id is not None
        parent = SemanticDecision(
            target_structure_node_id=content.parent_structure_node_id,
            semantic_class=SemanticClass.TECHNICAL_INFORMATION,
            reason_codes=("synthetic_parent",),
        )
        child = SemanticDecision(
            target_structure_node_id=content.structure_node_id,
            semantic_class=SemanticClass.REQUIREMENT,
            requirement_subtype=RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT,
            is_continuation_fragment=True,
            reason_codes=("synthetic_child",),
        )

        normalized = _normalize_model_continuation_parent_semantics(
            [
                (
                    parent,
                    _run(
                        "classify_nodes",
                        SemanticProviderRunStatus.SUCCESS,
                        nonce=1,
                    ),
                ),
                (
                    child,
                    _run(
                        "classify_nodes",
                        SemanticProviderRunStatus.SUCCESS,
                        nonce=2,
                    ),
                ),
            ],
            tree=tree,
        )

        decisions = {
            decision.target_structure_node_id: decision for decision, _ in normalized
        }
        normalized_child = decisions[content.structure_node_id]
        self.assertIs(
            normalized_child.semantic_class,
            SemanticClass.REQUIREMENT,
        )
        self.assertFalse(normalized_child.is_continuation_fragment)
        self.assertIn(
            "model_continuation_flag_not_applicable",
            normalized_child.reason_codes,
        )

    def test_model_continuation_flag_is_ignored_for_explicit_requirement(
        self,
    ) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Force test instrument",
            leaf_paragraph="The instrument requirement continues on this line.",
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider(mark_explicit_requirement_continuation=True)

        classified = classify_content_with_provider(source, tree, provider)

        explicit_requirement = next(
            item
            for item in classified.semantics.annotations
            if next(
                node
                for node in tree.nodes
                if node.structure_node_id == item.target_structure_node_id
            ).explicit_label
            == "4.12.1"
        )
        self.assertIs(
            explicit_requirement.semantic_class,
            SemanticClass.REQUIREMENT,
        )
        self.assertFalse(explicit_requirement.is_continuation_fragment)
        self.assertIn(
            "model_continuation_flag_not_applicable",
            explicit_requirement.reason_codes,
        )

    def test_empty_table_row_placeholder_closes_model_unresolved_receipt(
        self,
    ) -> None:
        source = _tabular_source("4.12.1 Force test instrument")
        table = next(node for node in source.nodes if node.table_payload is not None)
        assert table.table_payload is not None
        blank_cells = tuple(
            cell.model_copy(update={"raw_text": ""}) if cell.row == 1 else cell
            for cell in table.table_payload.cells
        )
        blank_table = table.model_copy(
            update={
                "table_payload": table.table_payload.model_copy(
                    update={"cells": blank_cells}
                )
            }
        )
        source = source.model_copy(
            update={
                "nodes": tuple(
                    blank_table if node.source_node_id == table.source_node_id else node
                    for node in source.nodes
                )
            }
        )
        tree = build_structure_tree(source)
        blank_row = next(
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW and node.table_row_index == 1
        )

        classified = classify_content_with_provider(
            source,
            tree,
            _FakeProvider(
                classification_model_unresolved_source_orders={blank_row.source_order},
                classification_model_unresolved_above=0,
            ),
        )

        annotation = next(
            item
            for item in classified.semantics.annotations
            if item.owner_structure_node_id == blank_row.structure_node_id
        )
        receipt = next(
            item
            for item in classified.terminal_receipts
            if item.owner_structure_node_id == blank_row.structure_node_id
        )
        self.assertIs(annotation.semantic_class, SemanticClass.NOISE)
        self.assertIn("empty_table_row_placeholder", annotation.reason_codes)
        self.assertIs(receipt.status, SemanticUnitTerminalStatus.RESOLVED)

    def test_broken_reference_table_row_closes_as_project_risk(self) -> None:
        source = _tabular_source("4.12.1 Force test instrument")
        table = next(node for node in source.nodes if node.table_payload is not None)
        assert table.table_payload is not None
        broken_cells = tuple(
            cell.model_copy(update={"raw_text": "#REF!"}) if cell.row == 1 else cell
            for cell in table.table_payload.cells
        )
        broken_table = table.model_copy(
            update={
                "table_payload": table.table_payload.model_copy(
                    update={"cells": broken_cells}
                )
            }
        )
        source = source.model_copy(
            update={
                "nodes": tuple(
                    broken_table
                    if node.source_node_id == table.source_node_id
                    else node
                    for node in source.nodes
                )
            }
        )
        tree = build_structure_tree(source)
        broken_row = next(
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW and node.table_row_index == 1
        )

        classified = classify_content_with_provider(
            source,
            tree,
            _FakeProvider(
                classification_model_unresolved_source_orders={broken_row.source_order},
                classification_model_unresolved_above=0,
            ),
        )

        annotation = next(
            item
            for item in classified.semantics.annotations
            if item.owner_structure_node_id == broken_row.structure_node_id
        )
        receipt = next(
            item
            for item in classified.terminal_receipts
            if item.owner_structure_node_id == broken_row.structure_node_id
        )
        self.assertIs(annotation.semantic_class, SemanticClass.PROJECT_RISK)
        self.assertIn("broken_reference_placeholder", annotation.reason_codes)
        self.assertIs(receipt.status, SemanticUnitTerminalStatus.RESOLVED)

    def test_provider_failure_becomes_explicit_unresolved_without_rule_fallback(
        self,
    ) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Force test instrument",
            leaf_paragraph="The force test instrument has a peak-reading function.",
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider(
            classification_status=SemanticProviderRunStatus.TIMEOUT
        )

        classified = classify_content_with_provider(source, tree, provider)

        self.assertTrue(classified.semantics.annotations)
        self.assertTrue(
            all(
                item.semantic_class is SemanticClass.UNRESOLVED
                for item in classified.semantics.annotations
            )
        )
        self.assertTrue(
            all(
                item.reason_codes == ("provider_timeout",)
                for item in classified.semantics.annotations
            )
        )
        self.assertTrue(
            all(
                item.candidate_source is SemanticCandidateSource.MODEL
                for item in classified.semantics.annotations
            )
        )

    def test_schema_invalid_classification_recursively_bisects_at_unit_edges(
        self,
    ) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4.12.1 Force test instrument"),
                (SourceNodeKind.PARAGRAPH, "The instrument shall measure force."),
                (SourceNodeKind.PARAGRAPH, "The range is 10 N."),
                (SourceNodeKind.PARAGRAPH, "The result is recorded."),
            )
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider(classification_schema_invalid_above=1)

        classified = classify_content_with_provider(source, tree, provider)

        self.assertIs(
            classified.runs[0].status,
            SemanticProviderRunStatus.INVALID_OUTPUT,
        )
        self.assertGreater(len(classified.runs), len(classified.semantics.annotations))
        self.assertTrue(
            all(
                receipt.status is SemanticUnitTerminalStatus.RESOLVED
                for receipt in classified.terminal_receipts
            )
        )
        successful_run_ids = {
            run.provider_run_id
            for run in classified.runs
            if run.status is SemanticProviderRunStatus.SUCCESS
        }
        self.assertTrue(
            all(
                receipt.provider_run_id in successful_run_ids
                for receipt in classified.terminal_receipts
            )
        )

    def test_one_schema_invalid_unit_does_not_erase_successful_peers(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4.12.1 Force test instrument"),
                (SourceNodeKind.PARAGRAPH, "The instrument shall measure force."),
                (SourceNodeKind.PARAGRAPH, "The range is 10 N."),
                (SourceNodeKind.PARAGRAPH, "The result is recorded."),
            )
        )
        tree = build_structure_tree(source)
        bad_target = next(
            item
            for item in reversed(tree.nodes)
            if item.parent_structure_node_id is not None
        )
        provider = _FakeProvider(
            classification_schema_invalid_source_orders={bad_target.source_order}
        )

        extraction = extract_requirements_with_provider(source, tree, provider)

        annotations = {
            item.target_structure_node_id: item
            for item in extraction.semantics.annotations
        }
        self.assertIs(
            annotations[bad_target.structure_node_id].semantic_class,
            SemanticClass.UNRESOLVED,
        )
        self.assertTrue(
            all(
                annotation.semantic_class is not SemanticClass.UNRESOLVED
                for target_id, annotation in annotations.items()
                if target_id != bad_target.structure_node_id
            )
        )
        receipts = {
            item.owner_structure_node_id: item
            for item in extraction.semantic_terminal_receipts
        }
        self.assertIs(
            receipts[bad_target.structure_node_id].status,
            SemanticUnitTerminalStatus.UNRESOLVED,
        )
        self.assertTrue(
            all(
                receipt.status is SemanticUnitTerminalStatus.RESOLVED
                for target_id, receipt in receipts.items()
                if target_id != bad_target.structure_node_id
            )
        )
        assembly_items = {
            item.target_structure_node_id: item for item in extraction.assembly.items
        }
        self.assertIs(
            assembly_items[bad_target.structure_node_id].status,
            RequirementAssemblyStatus.UNRESOLVED,
        )
        self.assertTrue(
            any(
                item.status is RequirementAssemblyStatus.CANDIDATE
                for target_id, item in assembly_items.items()
                if target_id != bad_target.structure_node_id
            )
        )
        self.assertTrue(extraction.requirement_view.nodes)

    def test_schema_invalid_singleton_retries_without_adjacent_siblings(
        self,
    ) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4.12.1 Force test instrument"),
                (SourceNodeKind.PARAGRAPH, "Synthetic supporting statement 1."),
                (SourceNodeKind.PARAGRAPH, "Synthetic supporting statement 2."),
                (SourceNodeKind.PARAGRAPH, "Synthetic supporting statement 3."),
            )
        )
        tree = build_structure_tree(source)
        target = next(
            item
            for item in reversed(tree.nodes)
            if item.kind is StructureNodeKind.CONTENT
        )
        provider = _FakeProvider(
            classification_model_unresolved_source_orders={target.source_order},
            classification_model_unresolved_above=1,
            classification_schema_invalid_with_adjacent_context_source_orders={
                target.source_order
            },
        )

        classified = classify_content_with_provider(source, tree, provider)

        annotation = next(
            item
            for item in classified.semantics.annotations
            if item.target_structure_node_id == target.structure_node_id
        )
        self.assertIsNot(annotation.semantic_class, SemanticClass.UNRESOLVED)
        target_requests = tuple(
            request
            for request in provider.classification_requests
            if tuple(node.source_order for node in request.nodes)
            == (target.source_order,)
        )
        self.assertEqual(len(target_requests), 2)
        self.assertTrue(
            any(
                context.parent_structure_node_id == target.parent_structure_node_id
                for context in target_requests[0].context_nodes
            )
        )
        self.assertFalse(
            any(
                context.parent_structure_node_id == target.parent_structure_node_id
                for context in target_requests[1].context_nodes
            )
        )

    def test_model_unresolved_singleton_retries_without_adjacent_siblings(
        self,
    ) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4.12.1 Force test instrument"),
                (SourceNodeKind.PARAGRAPH, "Synthetic supporting statement 1."),
                (SourceNodeKind.PARAGRAPH, "Synthetic supporting statement 2."),
                (SourceNodeKind.PARAGRAPH, "Synthetic supporting statement 3."),
            )
        )
        tree = build_structure_tree(source)
        target = next(
            item
            for item in reversed(tree.nodes)
            if item.kind is StructureNodeKind.CONTENT
        )
        provider = _FakeProvider(
            classification_model_unresolved_source_orders={target.source_order},
            classification_model_unresolved_above=1,
            classification_model_unresolved_with_adjacent_context_source_orders={
                target.source_order
            },
        )

        classified = classify_content_with_provider(source, tree, provider)

        annotation = next(
            item
            for item in classified.semantics.annotations
            if item.target_structure_node_id == target.structure_node_id
        )
        self.assertIsNot(annotation.semantic_class, SemanticClass.UNRESOLVED)
        target_requests = tuple(
            request
            for request in provider.classification_requests
            if tuple(node.source_order for node in request.nodes)
            == (target.source_order,)
        )
        self.assertEqual(len(target_requests), 2)

    def test_structural_continuation_conflict_is_reconciled_after_window(
        self,
    ) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4.12.1 Force test instrument"),
                (SourceNodeKind.PARAGRAPH, "The instrument shall measure force."),
                (SourceNodeKind.PARAGRAPH, "The measurement continues here."),
            )
        )
        tree = build_structure_tree(source)
        content_nodes = tuple(
            item for item in tree.nodes if item.kind is StructureNodeKind.CONTENT
        )
        self.assertEqual(len(content_nodes), 2)
        primary, continuation = content_nodes
        relation = StructureRelation(
            relation_id=stable_structure_relation_id(
                source_ir_id=tree.source_ir_id,
                resolver_id=tree.resolver_id,
                resolver_version=tree.resolver_version,
                kind=StructureRelationKind.CONTINUES,
                source_structure_node_id=primary.structure_node_id,
                target_structure_node_id=continuation.structure_node_id,
                source_refs=(*primary.source_refs, *continuation.source_refs),
            ),
            kind=StructureRelationKind.CONTINUES,
            source_structure_node_id=primary.structure_node_id,
            target_structure_node_id=continuation.structure_node_id,
            source_refs=(*primary.source_refs, *continuation.source_refs),
        )
        continued_tree = tree.model_copy(
            update={"relations": (*tree.relations, relation)}
        )
        run = _run("classify_nodes", SemanticProviderRunStatus.SUCCESS)
        normalized = _resolve_cross_window_continuation_conflicts(
            [
                (
                    SemanticDecision(
                        target_structure_node_id=primary.structure_node_id,
                        semantic_class=SemanticClass.REQUIREMENT,
                        requirement_subtype=(
                            RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT
                        ),
                        evidence_source_refs=primary.source_refs,
                        reason_codes=("model_classification",),
                    ),
                    run,
                ),
                (
                    SemanticDecision(
                        target_structure_node_id=continuation.structure_node_id,
                        semantic_class=SemanticClass.TECHNICAL_INFORMATION,
                        evidence_source_refs=continuation.source_refs,
                        reason_codes=("model_classification",),
                    ),
                    run,
                ),
            ],
            tree=continued_tree,
        )
        annotations = {item.target_structure_node_id: item for item, _run in normalized}
        self.assertIs(
            annotations[primary.structure_node_id].semantic_class,
            SemanticClass.REQUIREMENT,
        )
        self.assertIs(
            annotations[continuation.structure_node_id].semantic_class,
            SemanticClass.REQUIREMENT,
        )
        self.assertIn(
            "structural_continuation_semantics_inherited",
            annotations[continuation.structure_node_id].reason_codes,
        )

    def test_model_unresolved_units_selectively_retry_in_smaller_scopes(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4.12.1 Force test instrument"),
                *(
                    (
                        SourceNodeKind.PARAGRAPH,
                        f"Synthetic supporting statement {index}.",
                    )
                    for index in range(1, 12)
                ),
            )
        )
        tree = build_structure_tree(source)
        scoped_nodes = tuple(
            item for item in tree.nodes if item.parent_structure_node_id is not None
        )
        unresolved_nodes = scoped_nodes[7:10]
        unresolved_source_orders = {item.source_order for item in unresolved_nodes}
        provider = _FakeProvider(
            classification_model_unresolved_source_orders=(unresolved_source_orders),
            classification_model_unresolved_above=3,
        )

        classified = classify_content_with_provider(source, tree, provider)

        self.assertEqual(len(provider.classification_requests[0].nodes), 12)
        self.assertGreater(len(provider.classification_requests), 1)
        self.assertTrue(
            all(
                unresolved_source_orders.intersection(
                    node.source_order for node in request.nodes
                )
                for request in provider.classification_requests[1:]
            )
        )
        self.assertTrue(
            all(
                receipt.status is SemanticUnitTerminalStatus.RESOLVED
                for receipt in classified.terminal_receipts
            )
        )
        first_run_id = classified.runs[0].provider_run_id
        unresolved_target_ids = {item.structure_node_id for item in unresolved_nodes}
        receipts_by_owner = {
            item.owner_structure_node_id: item for item in classified.terminal_receipts
        }
        self.assertTrue(
            all(
                receipt.provider_run_id == first_run_id
                for owner_id, receipt in receipts_by_owner.items()
                if owner_id not in unresolved_target_ids
            )
        )
        self.assertTrue(
            all(
                receipts_by_owner[target_id].provider_run_id != first_run_id
                for target_id in unresolved_target_ids
            )
        )

    def test_singleton_model_unresolved_is_terminal_without_erasing_peers(
        self,
    ) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4.12.1 Force test instrument"),
                (SourceNodeKind.PARAGRAPH, "Synthetic supporting statement 1."),
                (SourceNodeKind.PARAGRAPH, "Synthetic supporting statement 2."),
                (SourceNodeKind.PARAGRAPH, "Synthetic supporting statement 3."),
            )
        )
        tree = build_structure_tree(source)
        scoped_nodes = tuple(
            item for item in tree.nodes if item.parent_structure_node_id is not None
        )
        unresolved_node = scoped_nodes[-1]
        provider = _FakeProvider(
            classification_model_unresolved_source_orders={
                unresolved_node.source_order
            },
            classification_model_unresolved_above=0,
        )

        extraction = extract_requirements_with_provider(source, tree, provider)

        self.assertEqual(
            tuple(len(item.nodes) for item in provider.classification_requests),
            (4, 2, 1, 1),
        )
        receipts_by_owner = {
            item.owner_structure_node_id: item
            for item in extraction.semantic_terminal_receipts
        }
        unresolved_receipt = receipts_by_owner[unresolved_node.structure_node_id]
        self.assertIs(
            unresolved_receipt.status,
            SemanticUnitTerminalStatus.UNRESOLVED,
        )
        self.assertEqual(
            unresolved_receipt.reason_codes,
            ("provider_model_unresolved",),
        )
        first_run_id = extraction.provider_runs[0].provider_run_id
        self.assertTrue(
            all(
                receipt.provider_run_id == first_run_id
                for owner_id, receipt in receipts_by_owner.items()
                if owner_id != unresolved_node.structure_node_id
            )
        )
        assembly_items = {
            item.owner_structure_node_id: item for item in extraction.assembly.items
        }
        self.assertIs(
            assembly_items[unresolved_node.structure_node_id].status,
            RequirementAssemblyStatus.UNRESOLVED,
        )
        self.assertEqual(
            assembly_items[unresolved_node.structure_node_id].reason_codes,
            ("upstream_semantic_unresolved",),
        )
        self.assertTrue(extraction.requirement_view.nodes)

    def test_distributed_model_unresolved_units_retry_only_matching_branches(
        self,
    ) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4.12.1 Force test instrument"),
                *(
                    (
                        SourceNodeKind.PARAGRAPH,
                        f"Synthetic supporting statement {index}.",
                    )
                    for index in range(1, 16)
                ),
            )
        )
        tree = build_structure_tree(source)
        scoped_nodes = tuple(
            item for item in tree.nodes if item.parent_structure_node_id is not None
        )
        unresolved_nodes = tuple(scoped_nodes[index] for index in (4, 10, 15))
        unresolved_source_orders = {item.source_order for item in unresolved_nodes}
        provider = _FakeProvider(
            classification_model_unresolved_source_orders=(unresolved_source_orders),
            classification_model_unresolved_above=1,
        )

        classified = classify_content_with_provider(source, tree, provider)

        self.assertEqual(len(provider.classification_requests[0].nodes), 16)
        self.assertTrue(
            all(
                unresolved_source_orders.intersection(
                    node.source_order for node in request.nodes
                )
                for request in provider.classification_requests[1:]
            )
        )
        self.assertTrue(
            all(
                receipt.status is SemanticUnitTerminalStatus.RESOLVED
                for receipt in classified.terminal_receipts
            )
        )
        first_run_id = classified.runs[0].provider_run_id
        unresolved_target_ids = {item.structure_node_id for item in unresolved_nodes}
        receipts_by_owner = {
            item.owner_structure_node_id: item for item in classified.terminal_receipts
        }
        self.assertTrue(
            all(
                receipt.provider_run_id == first_run_id
                for owner_id, receipt in receipts_by_owner.items()
                if owner_id not in unresolved_target_ids
            )
        )
        self.assertTrue(
            all(
                receipts_by_owner[target_id].provider_run_id != first_run_id
                for target_id in unresolved_target_ids
            )
        )

    def test_schema_invalid_assembly_recursively_merges_valid_windows(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4.12.1 Force test instrument"),
                (SourceNodeKind.PARAGRAPH, "The instrument shall measure force."),
                (SourceNodeKind.PARAGRAPH, "The range is 10 N."),
                (SourceNodeKind.PARAGRAPH, "The result is recorded."),
            )
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider(assembly_schema_invalid_above=2)

        extraction = extract_requirements_with_provider(source, tree, provider)

        assembly_runs = tuple(
            item
            for item in extraction.provider_runs
            if item.operation == "assemble_requirements"
        )
        self.assertTrue(
            any(
                item.status is SemanticProviderRunStatus.INVALID_OUTPUT
                for item in assembly_runs
            )
        )
        self.assertTrue(
            any(
                item.status is SemanticProviderRunStatus.SUCCESS
                for item in assembly_runs
            )
        )
        self.assertTrue(
            all(
                item.status is RequirementAssemblyStatus.CANDIDATE
                for item in extraction.assembly.items
            )
        )
        self.assertEqual(
            len(extraction.requirement_view.nodes),
            len(extraction.assembly.items),
        )

    def test_missing_assembly_decision_is_isolated_to_that_unit(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4.12.1 Force test instrument"),
                (SourceNodeKind.PARAGRAPH, "The instrument shall measure force."),
                (SourceNodeKind.PARAGRAPH, "The range is 10 N."),
                (SourceNodeKind.PARAGRAPH, "The result is recorded."),
            )
        )
        tree = build_structure_tree(source)
        bad_target = next(
            item
            for item in reversed(tree.nodes)
            if item.parent_structure_node_id is not None
        )
        provider = _FakeProvider(
            assembly_missing_source_orders={bad_target.source_order}
        )

        extraction = extract_requirements_with_provider(source, tree, provider)

        items = {
            item.target_structure_node_id: item for item in extraction.assembly.items
        }
        self.assertIs(
            items[bad_target.structure_node_id].status,
            RequirementAssemblyStatus.UNRESOLVED,
        )
        self.assertEqual(
            items[bad_target.structure_node_id].reason_codes,
            ("provider_assembly_scope_missing",),
        )
        self.assertTrue(
            all(
                item.status is RequirementAssemblyStatus.CANDIDATE
                for target_id, item in items.items()
                if target_id != bad_target.structure_node_id
            )
        )

    def test_missing_assembly_decision_recovers_in_smaller_scope(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "4.12.1 Force test instrument"),
                (SourceNodeKind.PARAGRAPH, "The instrument shall measure force."),
                (SourceNodeKind.PARAGRAPH, "The range is 10 N."),
                (SourceNodeKind.PARAGRAPH, "The result is recorded."),
            )
        )
        tree = build_structure_tree(source)
        bad_target = next(
            item
            for item in reversed(tree.nodes)
            if item.parent_structure_node_id is not None
        )
        provider = _FakeProvider(
            assembly_missing_source_orders={bad_target.source_order},
            assembly_missing_above=2,
        )

        extraction = extract_requirements_with_provider(source, tree, provider)

        self.assertGreater(len(provider.assembly_requests), 1)
        self.assertTrue(
            all(
                item.status is RequirementAssemblyStatus.CANDIDATE
                for item in extraction.assembly.items
            )
        )
        self.assertTrue(extraction.requirement_view.nodes)

    def test_terminal_receipt_rejects_an_old_schema_identity(self) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Force test instrument",
            leaf_paragraph="The force test instrument has a peak-reading function.",
        )
        tree = build_structure_tree(source)
        classified = classify_content_with_provider(source, tree, _FakeProvider())
        payload = classified.terminal_receipts[0].model_dump(mode="json")
        payload["schema_version"] = "semantic-unit-terminal-receipt/v0"

        with self.assertRaises(ValueError):
            SemanticUnitTerminalReceipt.model_validate(payload)

    def test_unresolved_semantics_skip_remote_assembly(self) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Force test instrument",
            leaf_paragraph="The force test instrument has a peak-reading function.",
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider(
            classification_status=SemanticProviderRunStatus.TIMEOUT
        )
        classified = classify_content_with_provider(source, tree, provider)

        assembled = assemble_requirements_with_provider(
            source,
            tree,
            classified.semantics,
            provider,
        )

        self.assertEqual(provider.assembly_call_count, 0)
        self.assertEqual(assembled.run.status, SemanticProviderRunStatus.SKIPPED)
        self.assertEqual(assembled.run.request_count, 0)
        self.assertEqual(
            assembled.run.error_code,
            "upstream_semantic_unresolved",
        )
        self.assertTrue(
            all(
                item.status is RequirementAssemblyStatus.UNRESOLVED
                and item.reason_codes == ("upstream_semantic_unresolved",)
                for item in assembled.assembly.items
            )
        )

    def test_non_requirement_only_scope_is_locally_excluded(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Overview"),
                (SourceNodeKind.PARAGRAPH, "Background information only."),
            )
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider()

        extraction = extract_requirements_with_provider(source, tree, provider)

        self.assertEqual(provider.assembly_call_count, 0)
        assembly_runs = tuple(
            item
            for item in extraction.provider_runs
            if item.operation == "assemble_requirements"
        )
        self.assertEqual(len(assembly_runs), 1)
        self.assertIs(
            assembly_runs[0].status,
            SemanticProviderRunStatus.SKIPPED,
        )
        self.assertEqual(
            assembly_runs[0].error_code,
            "no_requirement_candidates",
        )
        self.assertTrue(
            all(
                item.status is RequirementAssemblyStatus.CANDIDATE
                and not item.include_in_view
                and item.reason_codes == ("semantic_non_requirement_excluded",)
                for item in extraction.assembly.items
            )
        )
        self.assertEqual(extraction.requirement_view.nodes, ())

    def test_non_requirement_window_does_not_hide_later_requirement(self) -> None:
        source, _ = _flat_source(
            (
                (SourceNodeKind.HEADING, "1 Overview"),
                (SourceNodeKind.PARAGRAPH, "Background information only."),
                (SourceNodeKind.HEADING, "4.12.1 Force test instrument"),
                (SourceNodeKind.PARAGRAPH, "The instrument shall measure force."),
            )
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider()

        extraction = extract_requirements_with_provider(source, tree, provider)

        self.assertEqual(provider.assembly_call_count, 1)
        self.assertTrue(extraction.requirement_view.nodes)
        locally_excluded = tuple(
            item
            for item in extraction.assembly.items
            if item.reason_codes == ("semantic_non_requirement_excluded",)
        )
        self.assertTrue(locally_excluded)
        self.assertTrue(
            all(
                item.status is RequirementAssemblyStatus.CANDIDATE
                and not item.include_in_view
                for item in locally_excluded
            )
        )

    def test_invalid_evidence_reference_is_rejected_per_node(self) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Force test instrument",
            leaf_paragraph="The force test instrument has a peak-reading function.",
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider(tamper_evidence=True)

        classified = classify_content_with_provider(source, tree, provider)

        self.assertTrue(
            any(
                item.reason_codes == ("provider_evidence_invalid",)
                for item in classified.semantics.annotations
            )
        )

    def test_illegal_model_parentage_fails_closed_for_the_assembly_scope(self) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.12.1 Force test instrument",
            leaf_paragraph="The force test instrument has a peak-reading function.",
        )
        tree = build_structure_tree(source)
        provider = _FakeProvider(illegal_parent=True)
        classified = classify_content_with_provider(source, tree, provider)

        assembled = assemble_requirements_with_provider(
            source,
            tree,
            classified.semantics,
            provider,
        )

        self.assertTrue(
            all(
                item.status is RequirementAssemblyStatus.UNRESOLVED
                for item in assembled.assembly.items
            )
        )
        self.assertTrue(
            all(
                item.reason_codes == ("provider_assembly_invalid_parent",)
                for item in assembled.assembly.items
            )
        )


class _FakeProvider:
    provider_id = "synthetic-qwen-provider"
    provider_version = "1.0.0"
    model_id = "qwen3.7-plus"

    def __init__(
        self,
        *,
        classification_status: SemanticProviderRunStatus = SemanticProviderRunStatus.SUCCESS,
        tamper_evidence: bool = False,
        illegal_parent: bool = False,
        mark_content_continuation: bool = False,
        mark_explicit_requirement_continuation: bool = False,
        parameter_rows: bool = False,
        continuation_subtype: RequirementSubtype = (
            RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT
        ),
        classification_schema_invalid_above: int | None = None,
        classification_schema_invalid_source_orders: set[int] | None = None,
        classification_schema_invalid_with_adjacent_context_source_orders: (
            set[int] | None
        ) = None,
        classification_model_unresolved_source_orders: set[int] | None = None,
        classification_model_unresolved_above: int = 0,
        classification_model_unresolved_with_adjacent_context_source_orders: (
            set[int] | None
        ) = None,
        assembly_schema_invalid_above: int | None = None,
        assembly_missing_source_orders: set[int] | None = None,
        assembly_missing_above: int | None = None,
    ) -> None:
        self.classification_status = classification_status
        self.tamper_evidence = tamper_evidence
        self.illegal_parent = illegal_parent
        self.mark_content_continuation = mark_content_continuation
        self.mark_explicit_requirement_continuation = (
            mark_explicit_requirement_continuation
        )
        self.parameter_rows = parameter_rows
        self.continuation_subtype = continuation_subtype
        self.classification_schema_invalid_above = classification_schema_invalid_above
        self.classification_schema_invalid_source_orders = (
            classification_schema_invalid_source_orders or set()
        )
        self.classification_schema_invalid_with_adjacent_context_source_orders = (
            classification_schema_invalid_with_adjacent_context_source_orders or set()
        )
        self.classification_model_unresolved_source_orders = (
            classification_model_unresolved_source_orders or set()
        )
        self.classification_model_unresolved_above = (
            classification_model_unresolved_above
        )
        self.classification_model_unresolved_with_adjacent_context_source_orders = (
            classification_model_unresolved_with_adjacent_context_source_orders or set()
        )
        self.assembly_schema_invalid_above = assembly_schema_invalid_above
        self.assembly_missing_source_orders = assembly_missing_source_orders or set()
        self.assembly_missing_above = assembly_missing_above
        self.classification_call_count = 0
        self.assembly_call_count = 0
        self.classification_requests = []
        self.assembly_requests = []

    def classify_nodes(self, request):  # type: ignore[no-untyped-def]
        self.classification_call_count += 1
        self.classification_requests.append(request)
        if (
            (
                self.classification_schema_invalid_above is not None
                and len(request.nodes) > self.classification_schema_invalid_above
            )
            or any(
                node.source_order in self.classification_schema_invalid_source_orders
                for node in request.nodes
            )
            or any(
                node.source_order
                in self.classification_schema_invalid_with_adjacent_context_source_orders
                and any(
                    context.parent_structure_node_id == node.parent_structure_node_id
                    for context in request.context_nodes
                )
                for node in request.nodes
            )
        ):
            return SemanticClassificationProviderResult(
                decisions=(),
                run=_schema_invalid_run(
                    "classify_nodes",
                    nonce=self.classification_call_count,
                ),
            )
        run = _run(
            "classify_nodes",
            self.classification_status,
            nonce=self.classification_call_count,
        )
        if self.classification_status is not SemanticProviderRunStatus.SUCCESS:
            return SemanticClassificationProviderResult(decisions=(), run=run)
        decisions = []
        available_nodes = (*request.context_nodes, *request.nodes)
        for node in request.nodes:
            model_unresolved = (
                node.source_order in self.classification_model_unresolved_source_orders
                and len(request.nodes) > self.classification_model_unresolved_above
            ) or (
                node.source_order
                in self.classification_model_unresolved_with_adjacent_context_source_orders
                and any(
                    context.parent_structure_node_id == node.parent_structure_node_id
                    for context in request.context_nodes
                )
            )
            if model_unresolved:
                semantic_class = SemanticClass.UNRESOLVED
                subtype = None
            elif node.explicit_label == "4.12.1":
                semantic_class = SemanticClass.REQUIREMENT
                subtype = RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT
            elif any(
                ancestor.explicit_label == "4.12.1"
                for ancestor in available_nodes
                if ancestor.target_structure_node_id in node.ancestor_structure_node_ids
            ):
                if self.parameter_rows and node.kind is StructureNodeKind.TABLE_ROW:
                    semantic_class = SemanticClass.REQUIREMENT
                    subtype = RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT
                elif (
                    self.mark_content_continuation
                    and node.kind is StructureNodeKind.CONTENT
                ):
                    semantic_class = SemanticClass.REQUIREMENT
                    subtype = self.continuation_subtype
                else:
                    semantic_class = SemanticClass.TECHNICAL_INFORMATION
                    subtype = None
            else:
                semantic_class = SemanticClass.HEADING
                subtype = None
            evidence_items = (
                node.evidence[-1:]
                if node.kind is StructureNodeKind.SECTION
                and node.provision_structure_node_ids
                else node.evidence[:1]
            )
            evidence = tuple(item.source_ref for item in evidence_items)
            if self.tamper_evidence and node is request.nodes[0]:
                from app.domain.extraction.structure import (
                    StructureSourceRef,
                    StructureSourceRefKind,
                )

                evidence = (
                    StructureSourceRef(
                        kind=StructureSourceRefKind.SOURCE_NODE,
                        source_id="node-" + "f" * 32,
                    ),
                )
            decisions.append(
                SemanticDecision(
                    target_structure_node_id=node.target_structure_node_id,
                    semantic_class=semantic_class,
                    requirement_subtype=subtype,
                    is_continuation_fragment=(
                        self.mark_content_continuation
                        and node.kind is StructureNodeKind.CONTENT
                    )
                    or (
                        self.mark_explicit_requirement_continuation
                        and node.explicit_label == "4.12.1"
                    ),
                    evidence_source_refs=evidence,
                    reason_codes=(
                        ("provider_model_unresolved",)
                        if model_unresolved
                        else ("synthetic_model",)
                    ),
                )
            )
        return SemanticClassificationProviderResult(decisions=tuple(decisions), run=run)

    def assemble_requirements(self, request):  # type: ignore[no-untyped-def]
        self.assembly_call_count += 1
        self.assembly_requests.append(request)
        if (
            self.assembly_schema_invalid_above is not None
            and len(request.nodes) > self.assembly_schema_invalid_above
        ):
            return RequirementAssemblyProviderResult(
                decisions=(),
                run=_schema_invalid_run(
                    "assemble_requirements",
                    nonce=self.assembly_call_count,
                ),
            )
        run = _run(
            "assemble_requirements",
            SemanticProviderRunStatus.SUCCESS,
            nonce=self.assembly_call_count,
        )
        primary = next(
            item
            for item in request.nodes
            if item.semantic_class is SemanticClass.REQUIREMENT
        )
        decisions = []
        for node in request.nodes:
            if node.source_order in self.assembly_missing_source_orders and (
                self.assembly_missing_above is None
                or len(request.nodes) > self.assembly_missing_above
            ):
                continue
            if node.target_structure_node_id == primary.target_structure_node_id:
                include = True
                role = RequirementProjectionRole.PRIMARY
                parent = None
            elif node.is_continuation_fragment:
                include = False
                role = None
                parent = None
            elif primary.target_structure_node_id in node.ancestor_structure_node_ids:
                include = True
                role = (
                    RequirementProjectionRole.PARAMETER
                    if (
                        self.parameter_rows
                        and node.kind is StructureNodeKind.TABLE_ROW
                        and node.semantic_class is SemanticClass.REQUIREMENT
                    )
                    else RequirementProjectionRole.CONTENT
                )
                parent = (
                    request.nodes[0].target_structure_node_id
                    if self.illegal_parent
                    else primary.target_structure_node_id
                )
            else:
                include = False
                role = None
                parent = None
            decisions.append(
                RequirementAssemblyDecision(
                    target_structure_node_id=node.target_structure_node_id,
                    include_in_view=include,
                    role=role,
                    parent_structure_node_id=parent,
                    status=RequirementAssemblyStatus.CANDIDATE,
                    reason_codes=("synthetic_assembly",),
                )
            )
        return RequirementAssemblyProviderResult(decisions=tuple(decisions), run=run)


def _schema_invalid_run(
    operation: str,
    *,
    nonce: int,
) -> SemanticProviderRunRecord:
    return _run(
        operation,
        SemanticProviderRunStatus.INVALID_OUTPUT,
        nonce=nonce,
    ).model_copy(update={"error_code": "provider_output_schema_invalid"})


def _run(
    operation: str,
    status: SemanticProviderRunStatus,
    *,
    nonce: int = 0,
) -> SemanticProviderRunRecord:
    request_sha = hashlib.sha256(f"{operation}:{nonce}".encode()).hexdigest()
    response_sha = (
        hashlib.sha256(f"response:{operation}:{nonce}".encode()).hexdigest()
        if status is SemanticProviderRunStatus.SUCCESS
        else None
    )
    error_code = (
        None if status is SemanticProviderRunStatus.SUCCESS else "provider_timeout"
    )
    run_id = stable_semantic_provider_run_id(
        operation=operation,
        provider_id=_FakeProvider.provider_id,
        model_id=_FakeProvider.model_id,
        request_sha256=request_sha,
        response_sha256=response_sha,
        status=status,
        started_at=NOW,
    )
    return SemanticProviderRunRecord(
        provider_run_id=run_id,
        operation=operation,
        provider_id=_FakeProvider.provider_id,
        provider_version=_FakeProvider.provider_version,
        model_id=_FakeProvider.model_id,
        prompt_version="synthetic-prompt/v1",
        response_schema_version="synthetic-schema/v1",
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
