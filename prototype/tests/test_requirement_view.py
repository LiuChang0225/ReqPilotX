from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from app.application.extraction.classify_content import classify_content
from app.application.extraction.project_requirements import (
    RequirementProjectionRejected,
    RequirementProjectionRejectionCode,
    _ancestor_structure_node_ids,
    _continuation_components,
    project_requirements,
    validate_requirement_view_against_inputs,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.requirement_view import (
    REQUIREMENT_VIEW_SCHEMA_VERSION,
    RequirementDecisionMode,
    RequirementProjectionPolicy,
    RequirementProjectionRole,
    RequirementViewNode,
    RequirementViewSnapshot,
)
from app.domain.extraction.semantics import (
    RequirementSubtype,
    SemanticAnnotationStatus,
    SemanticClass,
    SemanticClassificationSnapshot,
    stable_semantic_grounding_span_id,
)
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
    TableIR,
    stable_cell_id,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
    stable_table_id,
)
from app.domain.extraction.structure import (
    StructureNodeKind,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
    StructureTreeSnapshot,
)
from app.domain.extraction.visual_text import (
    VisualTextObservation,
    VisualTextSnapshot,
    stable_visual_text_observation_id,
    stable_visual_text_snapshot_id,
)


SOURCE_HASH = "a" * 64
ADAPTER_ID = "synthetic-pdf-adapter"
ADAPTER_VERSION = "1.0.0"
RESOLVER_ID = "synthetic-structure-resolver"
RESOLVER_VERSION = "1.0.0"
CLASSIFIER_ID = "synthetic-semantic-classifier"
CLASSIFIER_VERSION = "1.0.0"


class RequirementProjectionBehaviorTests(unittest.TestCase):
    def test_primary_with_only_content_stays_direct_and_retains_full_context(
        self,
    ) -> None:
        fixture = _fixture(include_content=True)

        view = project_requirements(fixture.source_ir, fixture.tree, fixture.semantics)

        self.assertEqual(view.schema_version, REQUIREMENT_VIEW_SCHEMA_VERSION)
        self.assertEqual(
            view.projection_policy, RequirementProjectionPolicy.SHADOW_CANDIDATES
        )
        self.assertIsNone(view.requirement_set_id)
        self.assertEqual(
            [node.role for node in view.nodes],
            [RequirementProjectionRole.PRIMARY, RequirementProjectionRole.CONTENT],
        )
        primary, content = view.nodes
        self.assertEqual(primary.decision_mode, RequirementDecisionMode.DIRECT)
        self.assertEqual(content.decision_mode, RequirementDecisionMode.NONE)
        self.assertEqual(
            primary.ancestor_structure_node_ids,
            (
                fixture.ids["root"],
                fixture.ids["chapter"],
                fixture.ids["section"],
            ),
        )
        self.assertEqual(
            content.parent_requirement_view_node_id,
            primary.requirement_view_node_id,
        )
        self.assertEqual(primary.source_refs, (fixture.refs["primary"],))
        self.assertTrue(
            {fixture.refs["primary"], fixture.refs["content"]}.issubset(
                primary.full_context_source_refs
            )
        )
        self.assertEqual(
            primary.full_context_source_refs, content.full_context_source_refs
        )

    def test_determinable_child_and_parameter_make_parent_rollup(self) -> None:
        fixture = _fixture(
            include_content=True, include_child=True, include_parameter=True
        )

        view = project_requirements(fixture.source_ir, fixture.tree, fixture.semantics)

        by_role = {node.role: node for node in view.nodes}
        primary = by_role[RequirementProjectionRole.PRIMARY]
        child = by_role[RequirementProjectionRole.CHILD]
        parameter = by_role[RequirementProjectionRole.PARAMETER]
        content = by_role[RequirementProjectionRole.CONTENT]
        self.assertEqual(primary.decision_mode, RequirementDecisionMode.ROLLUP)
        self.assertEqual(child.decision_mode, RequirementDecisionMode.DIRECT)
        self.assertEqual(parameter.decision_mode, RequirementDecisionMode.DIRECT)
        self.assertEqual(content.decision_mode, RequirementDecisionMode.NONE)
        self.assertEqual(
            parameter.requirement_subtype,
            RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT,
        )
        self.assertEqual(
            child.parent_requirement_view_node_id,
            primary.requirement_view_node_id,
        )
        self.assertEqual(
            parameter.parent_requirement_view_node_id,
            primary.requirement_view_node_id,
        )
        self.assertIn(fixture.ids["table"], parameter.ancestor_structure_node_ids)

    def test_cross_page_table_projects_as_one_content_without_row_list_items(
        self,
    ) -> None:
        source, table_source_ids = _continued_tabular_requirement_source()
        tree = build_structure_tree(source)
        semantics = classify_content(source, tree)

        view = project_requirements(source, tree, semantics)
        structure_by_id = {node.structure_node_id: node for node in tree.nodes}
        table_content = [
            node
            for node in view.nodes
            if structure_by_id[node.target_structure_node_id].kind
            is StructureNodeKind.TABLE
        ]
        row_items = [
            node
            for node in view.nodes
            if structure_by_id[node.target_structure_node_id].kind
            is StructureNodeKind.TABLE_ROW
        ]

        self.assertEqual(
            [node.role for node in view.nodes],
            [RequirementProjectionRole.PRIMARY, RequirementProjectionRole.CONTENT],
        )
        self.assertEqual(len(table_content), 1)
        self.assertEqual(row_items, [])
        self.assertEqual(
            set(table_content[0].source_refs),
            {
                StructureSourceRef(
                    kind=StructureSourceRefKind.SOURCE_NODE,
                    source_id=source_id,
                )
                for source_id in table_source_ids
            },
        )
        self.assertTrue(
            any(
                relation.kind is StructureRelationKind.CONTINUES
                and relation.relation_id in table_content[0].relation_ids
                for relation in tree.relations
            )
        )

    def test_later_continued_table_rows_include_the_logical_table_ancestor(
        self,
    ) -> None:
        source, _ = _continued_tabular_requirement_source()
        tree = build_structure_tree(source)
        structure_by_id = {
            node.structure_node_id: node for node in tree.nodes
        }
        table_nodes = sorted(
            (
                node
                for node in tree.nodes
                if node.kind is StructureNodeKind.TABLE
            ),
            key=lambda node: (node.source_order, node.structure_node_id),
        )
        self.assertEqual(len(table_nodes), 2)
        later_table = table_nodes[1]
        later_row = next(
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
            and node.parent_structure_node_id == later_table.structure_node_id
            and node.table_row_index == 1
        )
        component_for, _ = _continuation_components(tree)

        ancestors = _ancestor_structure_node_ids(
            later_row.structure_node_id,
            nodes_by_id=structure_by_id,
            component_for=component_for,
        )

        self.assertEqual(
            component_for[later_table.structure_node_id],
            table_nodes[0].structure_node_id,
        )
        self.assertIn(
            table_nodes[0].structure_node_id,
            ancestors,
        )

    def test_cross_page_continuation_is_folded_into_one_primary(self) -> None:
        fixture = _fixture(include_continuation=True)

        view = project_requirements(fixture.source_ir, fixture.tree, fixture.semantics)

        self.assertEqual(len(view.nodes), 1)
        primary = view.nodes[0]
        self.assertEqual(primary.role, RequirementProjectionRole.PRIMARY)
        self.assertEqual(primary.decision_mode, RequirementDecisionMode.DIRECT)
        self.assertEqual(len(primary.semantic_annotation_ids), 2)
        self.assertEqual(
            set(primary.source_refs),
            {fixture.refs["content"], fixture.refs["continuation"]},
        )
        continues_relation = next(
            relation
            for relation in fixture.tree.relations
            if relation.kind is StructureRelationKind.CONTINUES
        )
        self.assertIn(continues_relation.relation_id, primary.relation_ids)
        self.assertFalse(
            any(
                node.target_structure_node_id == fixture.ids["continuation"]
                for node in view.nodes
            )
        )

    def test_reference_is_retained_as_non_determinable_context(self) -> None:
        fixture = _fixture(include_reference=True)

        view = project_requirements(fixture.source_ir, fixture.tree, fixture.semantics)

        reference = next(
            node
            for node in view.nodes
            if node.role is RequirementProjectionRole.REFERENCE
        )
        primary = next(
            node
            for node in view.nodes
            if node.role is RequirementProjectionRole.PRIMARY
        )
        self.assertEqual(reference.semantic_class, SemanticClass.REFERENCE)
        self.assertEqual(reference.decision_mode, RequirementDecisionMode.NONE)
        self.assertEqual(primary.decision_mode, RequirementDecisionMode.DIRECT)

    def test_normative_table_projects_one_rule_and_twenty_nine_references(self) -> None:
        source_ir = _normative_reference_table_source()
        tree = build_structure_tree(source_ir)
        semantics = classify_content(source_ir, tree)

        view = project_requirements(source_ir, tree, semantics)

        primary = next(
            node
            for node in view.nodes
            if node.role is RequirementProjectionRole.PRIMARY
        )
        references = [
            node
            for node in view.nodes
            if node.role is RequirementProjectionRole.REFERENCE
        ]
        self.assertEqual(
            primary.requirement_subtype,
            RequirementSubtype.REGULATION_REQUIREMENT,
        )
        self.assertEqual(len(references), 29)
        self.assertTrue(
            all(
                node.parent_requirement_view_node_id == primary.requirement_view_node_id
                for node in references
            )
        )
        rows_by_id = {
            node.structure_node_id: node
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
        }
        self.assertEqual(
            {
                rows_by_id[node.target_structure_node_id].table_row_index
                for node in references
            },
            set(range(1, 30)),
        )

    def test_only_checked_option_values_are_projected_under_regulatory_rule(
        self,
    ) -> None:
        source_ir, option_source_ids = _option_projection_source()
        tree = build_structure_tree(source_ir)
        semantics = classify_content(source_ir, tree)

        view = project_requirements(source_ir, tree, semantics)

        primary = next(
            node
            for node in view.nodes
            if node.role is RequirementProjectionRole.PRIMARY
        )
        contents = tuple(
            node
            for node in view.nodes
            if node.role is RequirementProjectionRole.CONTENT
        )
        structure_target_by_source = {
            ref.source_id: node.structure_node_id
            for node in tree.nodes
            for ref in node.source_refs
            if ref.kind is StructureSourceRefKind.SOURCE_NODE
        }
        self.assertEqual(
            primary.requirement_subtype,
            RequirementSubtype.REGULATION_REQUIREMENT,
        )
        self.assertEqual(primary.decision_mode, RequirementDecisionMode.DIRECT)
        self.assertEqual(
            {node.target_structure_node_id for node in contents},
            {
                structure_target_by_source[option_source_ids["checked_first"]],
                structure_target_by_source[option_source_ids["checked_second"]],
            },
        )
        self.assertNotIn(
            structure_target_by_source[option_source_ids["unchecked"]],
            {node.target_structure_node_id for node in view.nodes},
        )
        self.assertNotIn(
            structure_target_by_source[option_source_ids["uncertain"]],
            {node.target_structure_node_id for node in view.nodes},
        )
        self.assertTrue(any(finding.blocking for finding in tree.findings))

    def test_visual_ocr_grounding_projects_and_binds_observation_catalog(self) -> None:
        source_ir = _visual_source_fixture(
            ("4 Synthetic validation", "4.1 synthetic load test equipment")
        )
        tree = build_structure_tree(
            source_ir,
            resolver_id=RESOLVER_ID,
            resolver_version=RESOLVER_VERSION,
        )
        semantics = classify_content(
            source_ir,
            tree,
            classifier_id=CLASSIFIER_ID,
            classifier_version=CLASSIFIER_VERSION,
        )

        view = project_requirements(source_ir, tree, semantics)

        self.assertEqual(
            view.visual_text_observation_ids,
            tree.visual_text_observation_ids,
        )
        self.assertEqual(len(view.nodes), 1)
        primary = view.nodes[0]
        self.assertEqual(primary.role, RequirementProjectionRole.PRIMARY)
        self.assertTrue(
            all(
                source_ref.kind is StructureSourceRefKind.VISUAL_TEXT_OBSERVATION
                for source_ref in primary.source_refs
            )
        )
        self.assertNotIn("synthetic load test equipment", view.model_dump_json())

        payload = view.model_dump(mode="python")
        payload["visual_text_observation_ids"] = tuple(
            sorted(
                (
                    *payload["visual_text_observation_ids"],
                    f"vto-{'f' * 32}",
                )
            )
        )
        fabricated_catalog = RequirementViewSnapshot.model_validate(payload)
        with self.assertRaises(RequirementProjectionRejected) as caught:
            validate_requirement_view_against_inputs(
                fabricated_catalog,
                source_ir,
                tree,
                semantics,
            )
        self.assertEqual(
            caught.exception.code,
            RequirementProjectionRejectionCode.TAMPERED_VIEW,
        )

    def test_numbered_method_container_keeps_six_clauses_as_content(self) -> None:
        fixture = _container_fixture(
            reason_code="numbered_test_method_container_signal",
            clause_count=6,
            include_clause_continuation=True,
        )

        view = project_requirements(
            fixture.source_ir,
            fixture.tree,
            fixture.semantics,
        )

        primary = [
            node
            for node in view.nodes
            if node.role is RequirementProjectionRole.PRIMARY
        ]
        contents = [
            node
            for node in view.nodes
            if node.role is RequirementProjectionRole.CONTENT
        ]
        self.assertEqual(len(primary), 1)
        self.assertEqual(primary[0].decision_mode, RequirementDecisionMode.DIRECT)
        self.assertEqual(len(contents), 6)
        self.assertEqual(
            {node.target_structure_node_id for node in contents},
            set(fixture.clause_ids),
        )
        self.assertTrue(
            all(
                node.semantic_class is SemanticClass.REQUIREMENT
                and node.decision_mode is RequirementDecisionMode.NONE
                for node in contents
            )
        )
        self.assertNotIn(
            fixture.intro_id, {node.target_structure_node_id for node in view.nodes}
        )
        self.assertNotIn(
            fixture.clause_continuation_id,
            {node.target_structure_node_id for node in view.nodes},
        )
        clause_by_id = {
            node.structure_node_id: node
            for node in fixture.tree.nodes
            if node.kind is StructureNodeKind.CLAUSE
        }
        self.assertTrue(
            any(
                relation.kind is StructureRelationKind.CONTINUES
                and clause_by_id.get(relation.source_structure_node_id) is not None
                and clause_by_id.get(relation.target_structure_node_id) is not None
                and clause_by_id[relation.source_structure_node_id].list_ordinal == 4
                and clause_by_id[relation.target_structure_node_id].list_ordinal == 5
                for relation in fixture.tree.relations
            )
        )

    def test_numbered_requirement_container_keeps_five_clauses_as_content(self) -> None:
        fixture = _container_fixture(
            reason_code="numbered_requirement_container_signal",
            clause_count=5,
        )

        view = project_requirements(
            fixture.source_ir,
            fixture.tree,
            fixture.semantics,
        )

        self.assertEqual(
            [node.role for node in view.nodes].count(RequirementProjectionRole.PRIMARY),
            1,
        )
        self.assertEqual(
            [node.role for node in view.nodes].count(RequirementProjectionRole.CONTENT),
            5,
        )
        self.assertFalse(
            any(node.role is RequirementProjectionRole.CHILD for node in view.nodes)
        )
        self.assertEqual(view.nodes[0].decision_mode, RequirementDecisionMode.DIRECT)

    def test_equipment_section_absorbs_classification_paragraph(self) -> None:
        fixture = _container_fixture(
            reason_code="test_equipment_signal",
            clause_count=0,
        )

        view = project_requirements(
            fixture.source_ir,
            fixture.tree,
            fixture.semantics,
        )

        self.assertEqual(len(view.nodes), 1)
        self.assertEqual(view.nodes[0].role, RequirementProjectionRole.PRIMARY)
        self.assertEqual(view.nodes[0].decision_mode, RequirementDecisionMode.DIRECT)
        self.assertEqual(
            view.nodes[0].requirement_subtype,
            RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT,
        )

    def test_explicit_section_absorbs_all_physical_pdf_body_fragments(self) -> None:
        source = _physical_clause_source()
        tree = build_structure_tree(source)
        semantics = classify_content(source, tree)

        view = project_requirements(source, tree, semantics)

        self.assertEqual(len(view.nodes), 1)
        requirement = view.nodes[0]
        self.assertEqual(requirement.role, RequirementProjectionRole.PRIMARY)
        self.assertEqual(requirement.decision_mode, RequirementDecisionMode.DIRECT)
        self.assertEqual(len(requirement.source_refs), 4)
        owner = next(
            node
            for node in tree.nodes
            if node.structure_node_id == requirement.owner_structure_node_id
        )
        self.assertEqual(owner.explicit_label, "6.2")
        self.assertEqual(len(requirement.source_semantic_unit_ids), 4)


class RequirementProjectionPolicyTests(unittest.TestCase):
    def test_shadow_candidates_cannot_bind_a_formal_requirement_set(self) -> None:
        fixture = _fixture()

        with self.assertRaises(RequirementProjectionRejected) as caught:
            project_requirements(
                fixture.source_ir,
                fixture.tree,
                fixture.semantics,
                requirement_set_id="draft-set-1",
            )

        self.assertEqual(
            caught.exception.code,
            RequirementProjectionRejectionCode.SHADOW_REQUIREMENT_SET_FORBIDDEN,
        )

    def test_non_requirement_artifact_policy_preserves_catalogs_but_projects_nothing(
        self,
    ) -> None:
        fixture = _fixture(include_content=True, include_child=True)

        view = project_requirements(
            fixture.source_ir,
            fixture.tree,
            fixture.semantics,
            projection_policy=(RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT),
        )

        self.assertIs(
            view.projection_policy,
            RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT,
        )
        self.assertEqual(view.nodes, ())
        self.assertIsNone(view.requirement_set_id)
        self.assertEqual(view.source_node_ids, fixture.tree.source_node_ids)
        self.assertEqual(
            view.semantic_annotation_ids,
            tuple(
                sorted(
                    annotation.semantic_annotation_id
                    for annotation in fixture.semantics.annotations
                )
            ),
        )
        validate_requirement_view_against_inputs(
            view,
            fixture.source_ir,
            fixture.tree,
            fixture.semantics,
        )

    def test_non_requirement_artifact_policy_cannot_bind_a_requirement_set(
        self,
    ) -> None:
        fixture = _fixture()

        with self.assertRaises(RequirementProjectionRejected) as caught:
            project_requirements(
                fixture.source_ir,
                fixture.tree,
                fixture.semantics,
                projection_policy=(
                    RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT
                ),
                requirement_set_id="forbidden-set",
            )

        self.assertEqual(
            caught.exception.code,
            RequirementProjectionRejectionCode.NON_REQUIREMENT_ARTIFACT_SET_FORBIDDEN,
        )

    def test_formal_policy_rejects_candidate_semantics(self) -> None:
        fixture = _fixture()

        with self.assertRaises(RequirementProjectionRejected) as caught:
            project_requirements(
                fixture.source_ir,
                fixture.tree,
                fixture.semantics,
                projection_policy=RequirementProjectionPolicy.FORMAL_CONFIRMED,
                requirement_set_id="draft-set-1",
            )

        self.assertEqual(
            caught.exception.code,
            RequirementProjectionRejectionCode.FORMAL_SEMANTICS_UNCONFIRMED,
        )

    def test_confirmed_semantics_can_build_a_separate_formal_projection(self) -> None:
        fixture = _fixture(include_content=True)
        confirmed = _with_status(fixture.semantics, SemanticAnnotationStatus.CONFIRMED)

        shadow = project_requirements(fixture.source_ir, fixture.tree, confirmed)
        formal = project_requirements(
            fixture.source_ir,
            fixture.tree,
            confirmed,
            projection_policy=RequirementProjectionPolicy.FORMAL_CONFIRMED,
            requirement_set_id="draft-set-1",
        )

        self.assertEqual(
            formal.projection_policy,
            RequirementProjectionPolicy.FORMAL_CONFIRMED,
        )
        self.assertEqual(formal.requirement_set_id, "draft-set-1")
        self.assertNotEqual(
            formal.requirement_view_snapshot_id,
            shadow.requirement_view_snapshot_id,
        )
        self.assertNotEqual(
            formal.nodes[0].requirement_view_node_id,
            shadow.nodes[0].requirement_view_node_id,
        )


class RequirementViewIntegrityTests(unittest.TestCase):
    def test_contract_is_strict_frozen_text_free_and_deterministic(self) -> None:
        fixture = _fixture(include_content=True)
        first = project_requirements(fixture.source_ir, fixture.tree, fixture.semantics)
        second = project_requirements(
            fixture.source_ir, fixture.tree, fixture.semantics
        )

        self.assertEqual(first, second)
        with self.assertRaises(ValidationError):
            first.nodes[0].source_order = 99  # type: ignore[misc]
        unknown = first.nodes[0].model_dump(mode="python")
        unknown["raw_text"] = "synthetic private payload"
        with self.assertRaisesRegex(ValidationError, "raw_text"):
            RequirementViewNode.model_validate(unknown)
        self.assertNotIn("raw_text", first.model_dump_json())

    def test_stable_ids_reject_local_identity_tamper(self) -> None:
        fixture = _fixture()
        view = project_requirements(fixture.source_ir, fixture.tree, fixture.semantics)
        payload = view.model_dump(mode="python")
        payload["nodes"][0]["role"] = RequirementProjectionRole.CHILD
        payload["nodes"][0]["parent_requirement_view_node_id"] = view.nodes[
            0
        ].requirement_view_node_id

        with self.assertRaises(ValidationError):
            RequirementViewSnapshot.model_validate(payload)

    def test_cross_validation_rejects_relation_or_catalog_tamper(self) -> None:
        fixture = _fixture(include_continuation=True)
        view = project_requirements(fixture.source_ir, fixture.tree, fixture.semantics)
        payload = view.model_dump(mode="python")
        payload["nodes"][0]["relation_ids"] = ()
        tampered_relations = RequirementViewSnapshot.model_validate(payload)

        with self.assertRaises(RequirementProjectionRejected) as caught:
            validate_requirement_view_against_inputs(
                tampered_relations,
                fixture.source_ir,
                fixture.tree,
                fixture.semantics,
            )
        self.assertEqual(
            caught.exception.code,
            RequirementProjectionRejectionCode.TAMPERED_VIEW,
        )

        payload = view.model_dump(mode="python")
        payload["structure_relation_ids"] = tuple(
            sorted(
                (
                    *payload["structure_relation_ids"],
                    f"structure-relation-{'f' * 32}",
                )
            )
        )
        fabricated_catalog = RequirementViewSnapshot.model_validate(payload)
        with self.assertRaises(RequirementProjectionRejected):
            validate_requirement_view_against_inputs(
                fabricated_catalog,
                fixture.source_ir,
                fixture.tree,
                fixture.semantics,
            )

    def test_conflicting_continuation_semantics_fail_closed(self) -> None:
        fixture = _fixture(include_continuation=True)
        payload = fixture.semantics.model_dump(mode="python")
        payload["annotations"][1]["semantic_class"] = SemanticClass.BACKGROUND
        payload["annotations"][1]["requirement_subtype"] = None
        conflicting = SemanticClassificationSnapshot.model_validate(payload)

        with self.assertRaises(RequirementProjectionRejected) as caught:
            project_requirements(fixture.source_ir, fixture.tree, conflicting)

        self.assertEqual(
            caught.exception.code,
            RequirementProjectionRejectionCode.CONFLICTING_CONTINUATION_SEMANTICS,
        )

    def test_semantic_span_tamper_is_rejected_against_source_ir(self) -> None:
        fixture = _fixture(include_content=True)
        payload = fixture.semantics.model_dump(mode="python")
        span = payload["annotations"][0]["grounding_spans"][0]
        span["end_offset"] -= 1
        span["grounding_span_id"] = stable_semantic_grounding_span_id(
            source_ref=StructureSourceRef.model_validate(span["source_ref"]),
            start_offset=span["start_offset"],
            end_offset=span["end_offset"],
        )
        tampered_semantics = SemanticClassificationSnapshot.model_validate(payload)

        with self.assertRaises(RequirementProjectionRejected) as caught:
            project_requirements(
                fixture.source_ir,
                fixture.tree,
                tampered_semantics,
            )

        self.assertEqual(
            caught.exception.code,
            RequirementProjectionRejectionCode.INPUT_SNAPSHOT_MISMATCH,
        )


class _Fixture:
    def __init__(
        self,
        *,
        source_ir: SourceIRSnapshot,
        tree: StructureTreeSnapshot,
        semantics: SemanticClassificationSnapshot,
        ids: dict[str, str],
        refs: dict[str, StructureSourceRef],
    ) -> None:
        self.source_ir = source_ir
        self.tree = tree
        self.semantics = semantics
        self.ids = ids
        self.refs = refs


class _ContainerFixture:
    def __init__(
        self,
        *,
        source_ir: SourceIRSnapshot,
        tree: StructureTreeSnapshot,
        semantics: SemanticClassificationSnapshot,
        container_id: str,
        intro_id: str,
        clause_ids: tuple[str, ...],
        clause_continuation_id: str | None,
    ) -> None:
        self.source_ir = source_ir
        self.tree = tree
        self.semantics = semantics
        self.container_id = container_id
        self.intro_id = intro_id
        self.clause_ids = clause_ids
        self.clause_continuation_id = clause_continuation_id


def _normative_reference_table_source() -> SourceIRSnapshot:
    node_specs = (
        (SourceNodeKind.HEADING, "1 General", 80.0),
        (SourceNodeKind.HEADING, "1.9 Normative references", 110.0),
        (
            SourceNodeKind.PARAGRAPH,
            "The following standards shall apply.",
            145.0,
        ),
    )
    nodes: list[SourceNode] = []
    for source_order, (kind, raw_text, y0) in enumerate(node_specs):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=source_order,
            bbox=(70.0, y0, 530.0, y0 + 18.0),
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=SOURCE_HASH,
                    adapter_id=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    kind=kind,
                    native_locator=locator,
                ),
                kind=kind,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
        )

    table_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        table_index=0,
        bbox=(50.0, 190.0, 550.0, 700.0),
    )
    table_id = stable_table_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=table_locator,
    )
    cells: list[TableCellIR] = []
    for row in range(30):
        values = (
            ("No", "Standard", "Title")
            if row == 0
            else (str(row), f"STD-{row:03d}", f"Reference {row}")
        )
        for column, raw_text in enumerate(values):
            x0 = 50.0 + column * (500.0 / 3.0)
            y0 = 190.0 + row * 17.0
            cells.append(
                TableCellIR(
                    cell_id=stable_cell_id(table_id, row, column),
                    row=row,
                    column=column,
                    raw_text=raw_text,
                    native_locator=NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        table_index=0,
                        row_index=row,
                        column_index=column,
                        bbox=(x0, y0, x0 + 500.0 / 3.0, y0 + 17.0),
                    ),
                )
            )
    nodes.append(
        SourceNode(
            source_node_id=stable_node_id(
                source_sha256=SOURCE_HASH,
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                kind=SourceNodeKind.TABLE,
                native_locator=table_locator,
            ),
            kind=SourceNodeKind.TABLE,
            source_order=len(nodes),
            raw_text="",
            native_locator=table_locator,
            table_payload=TableIR(
                table_id=table_id,
                row_count=30,
                column_count=3,
                cells=tuple(cells),
            ),
        )
    )
    document_id = stable_document_id(SOURCE_HASH)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
        ),
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(nodes),
        created_at=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
    )


def _option_projection_source() -> tuple[SourceIRSnapshot, dict[str, str]]:
    specs = (
        ("heading", SourceNodeKind.HEADING, "4.1 法规与认证"),
        ("prompt", SourceNodeKind.PARAGRAPH, "请选择适用认证区域"),
        ("checked_first", SourceNodeKind.PARAGRAPH, "[ √ ] 区域甲"),
        ("unchecked", SourceNodeKind.PARAGRAPH, "[   ] 区域乙"),
        ("checked_second", SourceNodeKind.PARAGRAPH, "[ √ ] 区域丙"),
        ("uncertain", SourceNodeKind.PARAGRAPH, "[ ? ] 区域丁"),
    )
    nodes: list[SourceNode] = []
    source_ids: dict[str, str] = {}
    for source_order, (name, kind, raw_text) in enumerate(specs):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=source_order,
            bbox=(
                70.0,
                100.0 + source_order * 20.0,
                530.0,
                118.0 + source_order * 20.0,
            ),
        )
        source_node_id = stable_node_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=kind,
            native_locator=locator,
        )
        source_ids[name] = source_node_id
        nodes.append(
            SourceNode(
                source_node_id=source_node_id,
                kind=kind,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
        )
    document_id = stable_document_id(SOURCE_HASH)
    return (
        SourceIRSnapshot(
            source_ir_id=stable_source_ir_id(
                document_id=document_id,
                source_sha256=SOURCE_HASH,
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
            ),
            document_id=document_id,
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            status=SourceIRStatus.COMPLETE,
            nodes=tuple(nodes),
            created_at=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
        ),
        source_ids,
    )


def _continued_tabular_requirement_source() -> tuple[SourceIRSnapshot, tuple[str, str]]:
    heading_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        block_index=0,
        bbox=(70.0, 100.0, 520.0, 118.0),
    )
    heading = SourceNode(
        source_node_id=stable_node_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=SourceNodeKind.PARAGRAPH,
            native_locator=heading_locator,
        ),
        kind=SourceNodeKind.PARAGRAPH,
        source_order=0,
        raw_text="8.3.1 接口要求",
        native_locator=heading_locator,
    )

    def table_node(
        *,
        page_number: int,
        source_order: int,
        bbox: tuple[float, float, float, float],
        data: tuple[str, str],
    ) -> SourceNode:
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            table_index=0,
            bbox=bbox,
        )
        table_id = stable_table_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            native_locator=locator,
        )
        row_height = (bbox[3] - bbox[1]) / 2.0
        column_width = (bbox[2] - bbox[0]) / 2.0
        values = (("字段", "要求"), data)
        cells = tuple(
            TableCellIR(
                cell_id=stable_cell_id(table_id, row, column),
                row=row,
                column=column,
                raw_text=values[row][column],
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=page_number,
                    table_index=0,
                    row_index=row,
                    column_index=column,
                    bbox=(
                        bbox[0] + column * column_width,
                        bbox[1] + row * row_height,
                        bbox[0] + (column + 1) * column_width,
                        bbox[1] + (row + 1) * row_height,
                    ),
                ),
            )
            for row in range(2)
            for column in range(2)
        )
        return SourceNode(
            source_node_id=stable_node_id(
                source_sha256=SOURCE_HASH,
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                kind=SourceNodeKind.TABLE,
                native_locator=locator,
            ),
            kind=SourceNodeKind.TABLE,
            source_order=source_order,
            raw_text="",
            native_locator=locator,
            table_payload=TableIR(
                table_id=table_id,
                row_count=2,
                column_count=2,
                cells=cells,
            ),
        )

    first_table = table_node(
        page_number=1,
        source_order=1,
        bbox=(50.0, 600.0, 550.0, 780.0),
        data=("第一项", "值一"),
    )
    second_table = table_node(
        page_number=2,
        source_order=2,
        bbox=(50.0, 90.0, 550.0, 350.0),
        data=("第二项", "值二"),
    )
    document_id = stable_document_id(SOURCE_HASH)
    source_ir = SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
        ),
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        status=SourceIRStatus.COMPLETE,
        nodes=(heading, first_table, second_table),
        created_at=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
    )
    return source_ir, (first_table.source_node_id, second_table.source_node_id)


def _source_fixture(
    source_node_names: list[str],
) -> tuple[SourceIRSnapshot, dict[str, str], str]:
    text_by_name = {
        "chapter": "1 synthetic chapter",
        "section": "1.2 synthetic section",
        "primary": "1.2.1 synthetic topic",
        "content": "synthetic technical information",
        "child": "1) synthetic acceptance condition",
        "continuation": "synthetic cross-page continuation",
        "reference": "synthetic reference document",
    }
    kind_by_name = {
        "chapter": SourceNodeKind.HEADING,
        "section": SourceNodeKind.HEADING,
        "primary": SourceNodeKind.HEADING,
        "content": SourceNodeKind.PARAGRAPH,
        "child": SourceNodeKind.LIST_ITEM,
        "continuation": SourceNodeKind.PARAGRAPH,
        "table": SourceNodeKind.TABLE,
        "reference": SourceNodeKind.PARAGRAPH,
    }
    source_ids: dict[str, str] = {}
    source_nodes: list[SourceNode] = []
    row_cell_id = ""
    for source_order, name in enumerate(source_node_names):
        kind = kind_by_name[name]
        if name == "table":
            locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=1,
                table_index=source_order,
            )
            table_id = stable_table_id(
                source_sha256=SOURCE_HASH,
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                native_locator=locator,
            )
            cell_locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=1,
                table_index=source_order,
                row_index=0,
                column_index=0,
            )
            row_cell_id = stable_cell_id(table_id, 0, 0)
            table_payload = TableIR(
                table_id=table_id,
                row_count=1,
                column_count=1,
                cells=(
                    TableCellIR(
                        cell_id=row_cell_id,
                        row=0,
                        column=0,
                        raw_text="synthetic parameter row",
                        native_locator=cell_locator,
                    ),
                ),
            )
            raw_text = ""
        else:
            locator = NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=1 if name != "continuation" else 2,
                block_index=source_order,
            )
            table_payload = None
            raw_text = text_by_name[name]
        source_node_id = stable_node_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=kind,
            native_locator=locator,
        )
        source_ids[name] = source_node_id
        source_nodes.append(
            SourceNode(
                source_node_id=source_node_id,
                kind=kind,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
                table_payload=table_payload,
            )
        )

    document_id = stable_document_id(SOURCE_HASH)
    source_ir_id = stable_source_ir_id(
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
    )
    return (
        SourceIRSnapshot(
            source_ir_id=source_ir_id,
            document_id=document_id,
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            status=SourceIRStatus.COMPLETE,
            nodes=tuple(source_nodes),
            created_at=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
        ),
        source_ids,
        row_cell_id,
    )


def _physical_clause_source() -> SourceIRSnapshot:
    texts = (
        "6.2 质量问题处理",
        "对于开发及量产阶段的质量问题，供应商应在确认问题后 48 小时内",
        "提供初步问题分析报告，并在问题最终完成解析、整改和验证后",
        "提交完整问题分析报告。",
    )
    nodes: list[SourceNode] = []
    for source_order, raw_text in enumerate(texts):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=17 if source_order < 3 else 18,
            block_index=source_order,
            bbox=(
                72.0,
                80.0 + source_order * 22.0,
                520.0,
                96.0 + source_order * 22.0,
            ),
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=SOURCE_HASH,
                    adapter_id=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    kind=SourceNodeKind.PARAGRAPH,
                    native_locator=locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
            )
        )
    document_id = stable_document_id(SOURCE_HASH)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
        ),
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(nodes),
        created_at=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
    )


def _visual_source_fixture(lines: tuple[str, ...]) -> SourceIRSnapshot:
    provider_id = "synthetic-visual-provider"
    provider_version = "1.0.0"
    page_image_sha256 = "8" * 64
    page_bbox = (0.0, 0.0, 600.0, 800.0)
    observations: list[VisualTextObservation] = []
    for index, raw_text in enumerate(lines):
        bbox = (60.0, 100.0 + index * 24.0, 520.0, 118.0 + index * 24.0)
        observations.append(
            VisualTextObservation(
                observation_id=stable_visual_text_observation_id(
                    source_sha256=SOURCE_HASH,
                    page_image_sha256=page_image_sha256,
                    provider_id=provider_id,
                    provider_version=provider_version,
                    page_number=1,
                    observation_index=index,
                    bbox=bbox,
                ),
                observation_index=index,
                source_sha256=SOURCE_HASH,
                page_number=1,
                bbox=bbox,
                raw_text=raw_text,
                confidence=0.95,
                provider_id=provider_id,
                provider_version=provider_version,
                page_image_sha256=page_image_sha256,
            )
        )
    visual_snapshot = VisualTextSnapshot(
        visual_text_snapshot_id=stable_visual_text_snapshot_id(
            source_sha256=SOURCE_HASH,
            page_image_sha256=page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=1,
            page_bbox=page_bbox,
            page_image_width_px=1200,
            page_image_height_px=1600,
        ),
        source_sha256=SOURCE_HASH,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=1,
        page_bbox=page_bbox,
        page_image_sha256=page_image_sha256,
        page_image_width_px=1200,
        page_image_height_px=1600,
        observations=tuple(observations),
    )
    document_id = stable_document_id(SOURCE_HASH)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
        ),
        document_id=document_id,
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        status=SourceIRStatus.PARTIAL,
        visual_text_snapshots=(visual_snapshot,),
        created_at=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
    )


def _container_fixture(
    *,
    reason_code: str,
    clause_count: int,
    include_clause_continuation: bool = False,
) -> _ContainerFixture:
    source_hash = "c" * 64
    if reason_code == "test_equipment_signal":
        container_text = "4.1.1 synthetic load test equipment"
        subtype = RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT
    elif reason_code == "numbered_test_method_container_signal":
        container_text = "4.1.1 synthetic numbered test method"
        subtype = RequirementSubtype.TEST_METHOD_REQUIREMENT
    else:
        container_text = "4.1.1 synthetic numbered requirement"
        subtype = RequirementSubtype.PRODUCT_REQUIREMENT

    specs: list[tuple[str, SourceNodeKind, str, int, str | None]] = [
        ("chapter", SourceNodeKind.HEADING, "4 Synthetic validation", 1, None),
        ("section", SourceNodeKind.HEADING, "4.1 Synthetic section", 1, None),
        ("container", SourceNodeKind.HEADING, container_text, 1, None),
        (
            "intro",
            SourceNodeKind.PARAGRAPH,
            "synthetic classification paragraph",
            1,
            None,
        ),
    ]
    for ordinal in range(1, clause_count + 1):
        clause_name = f"clause_{ordinal}"
        specs.append(
            (
                clause_name,
                SourceNodeKind.LIST_ITEM,
                f"{ordinal}) synthetic numbered content",
                1 if ordinal <= 4 else 2,
                None,
            )
        )
        if ordinal == 1 and include_clause_continuation:
            specs.append(
                (
                    "clause_1_continuation",
                    SourceNodeKind.PARAGRAPH,
                    "synthetic continuation of numbered content",
                    1,
                    clause_name,
                )
            )

    source_nodes: list[SourceNode] = []
    source_ids: dict[str, str] = {}
    for source_order, (name, kind, raw_text, page_number, parent_name) in enumerate(
        specs
    ):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            block_index=source_order,
        )
        source_node_id = stable_node_id(
            source_sha256=source_hash,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=kind,
            native_locator=locator,
        )
        source_ids[name] = source_node_id
        source_nodes.append(
            SourceNode(
                source_node_id=source_node_id,
                kind=kind,
                source_order=source_order,
                raw_text=raw_text,
                native_locator=locator,
                parent_source_node_id=(
                    source_ids[parent_name] if parent_name is not None else None
                ),
            )
        )

    document_id = stable_document_id(source_hash)
    source_ir = SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=source_hash,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
        ),
        document_id=document_id,
        source_sha256=source_hash,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(source_nodes),
        created_at=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
    )
    tree = build_structure_tree(
        source_ir,
        resolver_id=RESOLVER_ID,
        resolver_version=RESOLVER_VERSION,
    )

    def structure_id(name: str) -> str:
        return next(
            node.structure_node_id
            for node in tree.nodes
            if node.identity_key == f"source:{source_ids[name]}"
        )

    container_id = structure_id("container")
    intro_id = structure_id("intro")
    clause_ids = tuple(
        structure_id(f"clause_{ordinal}") for ordinal in range(1, clause_count + 1)
    )
    continuation_id = (
        structure_id("clause_1_continuation") if include_clause_continuation else None
    )
    baseline = classify_content(
        source_ir,
        tree,
        classifier_id=CLASSIFIER_ID,
        classifier_version=CLASSIFIER_VERSION,
    )
    projected_semantic_targets = {
        container_id,
        intro_id,
        *clause_ids,
    }
    if continuation_id is not None:
        projected_semantic_targets.add(continuation_id)
    semantic_payload = baseline.model_dump(mode="python")
    for annotation in semantic_payload["annotations"]:
        target_id = annotation["owner_structure_node_id"]
        if target_id not in projected_semantic_targets:
            continue
        annotation["semantic_class"] = SemanticClass.REQUIREMENT
        annotation["requirement_subtype"] = subtype
        annotation["status"] = SemanticAnnotationStatus.CANDIDATE
        annotation["reason_codes"] = (
            reason_code if target_id == container_id else "synthetic_fixture",
        )
    semantics = SemanticClassificationSnapshot.model_validate(semantic_payload)
    return _ContainerFixture(
        source_ir=source_ir,
        tree=tree,
        semantics=semantics,
        container_id=container_id,
        intro_id=intro_id,
        clause_ids=clause_ids,
        clause_continuation_id=continuation_id,
    )


def _fixture(
    *,
    include_content: bool = False,
    include_child: bool = False,
    include_continuation: bool = False,
    include_parameter: bool = False,
    include_reference: bool = False,
) -> _Fixture:
    node_names = ["chapter", "section", "primary"]
    if include_content or include_continuation:
        node_names.append("content")
    if include_parameter:
        node_names.extend(("table", "row"))
    if include_child:
        node_names.append("child")
    if include_continuation:
        node_names.append("continuation")
    if include_reference:
        node_names.append("reference")

    source_node_names = [name for name in node_names if name != "row"]
    source_ir, source_ids, row_cell_id = _source_fixture(source_node_names)
    refs = {
        name: StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id=source_ids[name],
        )
        for name in source_node_names
    }
    if include_parameter:
        refs["row"] = StructureSourceRef(
            kind=StructureSourceRefKind.TABLE_CELL,
            source_id=row_cell_id,
        )

    tree = build_structure_tree(
        source_ir,
        resolver_id=RESOLVER_ID,
        resolver_version=RESOLVER_VERSION,
    )
    ids = {
        "root": next(
            node.structure_node_id
            for node in tree.nodes
            if node.kind is StructureNodeKind.DOCUMENT
        )
    }
    ids.update(
        {
            name: next(
                node.structure_node_id
                for node in tree.nodes
                if node.identity_key == f"source:{source_id}"
            )
            for name, source_id in source_ids.items()
        }
    )
    if include_parameter:
        ids["row"] = next(
            node.structure_node_id
            for node in tree.nodes
            if node.identity_key == f"table:{source_ids['table']}:row:0"
        )

    semantic_specs = [
        (
            "content" if include_continuation else "primary",
            SemanticClass.REQUIREMENT,
            RequirementSubtype.PRODUCT_REQUIREMENT,
        )
    ]
    if include_content and not include_continuation:
        semantic_specs.append(("content", SemanticClass.TECHNICAL_INFORMATION, None))
    if include_child:
        semantic_specs.append(
            (
                "child",
                SemanticClass.REQUIREMENT,
                RequirementSubtype.ACCEPTANCE_CRITERION,
            )
        )
    if include_continuation:
        semantic_specs.append(
            (
                "continuation",
                SemanticClass.REQUIREMENT,
                RequirementSubtype.PRODUCT_REQUIREMENT,
            )
        )
    if include_parameter:
        semantic_specs.append(
            (
                "row",
                SemanticClass.REQUIREMENT,
                RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT,
            )
        )
    if include_reference:
        semantic_specs.append(("reference", SemanticClass.REFERENCE, None))
    semantic_specs.sort(
        key=lambda item: next(
            node.source_order
            for node in tree.nodes
            if node.structure_node_id == ids[item[0]]
        )
    )
    scope = tuple(ids[name] for name, _, _ in semantic_specs)
    baseline_semantics = classify_content(
        source_ir,
        tree,
        scope_structure_node_ids=scope,
        classifier_id=CLASSIFIER_ID,
        classifier_version=CLASSIFIER_VERSION,
    )
    decisions = {
        ids[name]: (semantic_class, requirement_subtype)
        for name, semantic_class, requirement_subtype in semantic_specs
    }
    semantic_payload = baseline_semantics.model_dump(mode="python")
    for annotation in semantic_payload["annotations"]:
        semantic_class, requirement_subtype = decisions[
            annotation["owner_structure_node_id"]
        ]
        annotation["semantic_class"] = semantic_class
        annotation["requirement_subtype"] = requirement_subtype
        annotation["status"] = SemanticAnnotationStatus.CANDIDATE
        annotation["reason_codes"] = ("synthetic_fixture",)
    semantics = SemanticClassificationSnapshot.model_validate(semantic_payload)
    return _Fixture(
        source_ir=source_ir,
        tree=tree,
        semantics=semantics,
        ids=ids,
        refs=refs,
    )


def _with_status(
    semantics: SemanticClassificationSnapshot,
    status: SemanticAnnotationStatus,
) -> SemanticClassificationSnapshot:
    payload = semantics.model_dump(mode="python")
    for annotation in payload["annotations"]:
        annotation["status"] = status
    return SemanticClassificationSnapshot.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
