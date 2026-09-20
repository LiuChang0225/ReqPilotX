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

from app.application.extraction.classify_content import (
    SemanticClassificationRejected,
    SemanticClassificationRejectionCode,
    classify_content,
    classify_table_row_information,
    validate_semantic_classification_against_inputs,
)
from app.application.extraction.project_requirements import project_requirements
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.requirement_view import (
    RequirementDecisionMode,
    RequirementProjectionRole,
)
from app.domain.extraction.semantics import (
    SEMANTIC_CLASSIFICATION_SCHEMA_VERSION,
    GroundedSemanticInformationFacet,
    RequirementSubtype,
    SemanticAnnotation,
    SemanticAnnotationStatus,
    SemanticCandidateSource,
    SemanticClass,
    SemanticClassificationSnapshot,
    SemanticGroundingSpan,
    SemanticInformationKind,
    stable_semantic_grounding_span_id,
    stable_semantic_information_facet_id,
)
from app.domain.extraction.source_ir import (
    MergedRegion,
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
    StructureSourceRefKind,
)
from app.domain.extraction.visual_text import (
    VisualTextObservation,
    VisualTextSnapshot,
    stable_visual_text_observation_id,
    stable_visual_text_snapshot_id,
)


SOURCE_HASH = "7" * 64
ADAPTER_ID = "semantic-test-pdf"
ADAPTER_VERSION = "1.0.0"
CREATED_AT = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)


class SemanticClassificationTests(unittest.TestCase):
    def test_document_control_and_change_log_are_not_requirements(self) -> None:
        source, ids = _flat_source(
            (
                (
                    SourceNodeKind.PARAGRAPH,
                    "示例编辑组管理本演示规范的译文；演示文件发生歧义时以中文版本为准。"
                    " The example editorial team maintains the translation. "
                    "For this synthetic document, the Chinese version is authoritative.",
                ),
                (
                    SourceNodeKind.PARAGRAPH,
                    "——标准的适用范围重新定义\n"
                    "The scope of application of the standard is redefined\n"
                    "——修改标称电压的描述（见3.6）",
                ),
            )
        )
        tree = build_structure_tree(source)
        semantics = classify_content(source, tree)

        observed = tuple(
            _annotation(
                semantics,
                _structure_id_for_source(tree, source_id),
            ).semantic_class
            for source_id in ids
        )
        self.assertEqual(
            observed,
            (
                SemanticClass.VERSION_METADATA,
                SemanticClass.VERSION_METADATA,
            ),
        )

    def test_equipment_requirement_needs_no_modal_word_and_keeps_full_context(
        self,
    ) -> None:
        source, ids = _hierarchical_source(
            leaf_heading="4.12.1 峰值保持式载荷测试仪",
            leaf_paragraph="量程覆盖规定的测量区间。",
        )
        tree = build_structure_tree(source)

        semantics = classify_content(source, tree)
        target_id = _structure_id_for_source(tree, ids["leaf"])
        annotation = _annotation(semantics, target_id)

        self.assertEqual(
            annotation.semantic_class,
            SemanticClass.REQUIREMENT,
        )
        self.assertEqual(
            annotation.requirement_subtype,
            RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT,
        )
        self.assertEqual(annotation.status, SemanticAnnotationStatus.CANDIDATE)
        self.assertNotIn("modal_requirement_signal", annotation.reason_codes)
        self.assertEqual(
            [span.source_ref.source_id for span in annotation.grounding_spans],
            [ids["root"], ids["branch"], ids["leaf"], ids["paragraph"]],
        )
        self.assertTrue(
            all(
                span.source_ref.kind is StructureSourceRefKind.SOURCE_NODE
                for span in annotation.grounding_spans
            )
        )
        self.assertNotIn("峰值保持式", semantics.model_dump_json())
        validate_semantic_classification_against_inputs(semantics, source, tree)

    def test_test_objective_is_requirement_without_modal_word(self) -> None:
        source, ids = _hierarchical_source(
            leaf_heading="5.2.1 试验目标",
            leaf_paragraph="用于确定静态能耗。",
        )
        tree = build_structure_tree(source)

        semantics = classify_content(source, tree)
        annotation = _annotation(
            semantics,
            _structure_id_for_source(tree, ids["leaf"]),
        )

        self.assertEqual(annotation.semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(
            annotation.requirement_subtype,
            RequirementSubtype.TEST_OBJECTIVE_REQUIREMENT,
        )
        self.assertEqual(annotation.reason_codes, ("test_objective_signal",))

    def test_tabular_requirement_section_keeps_table_and_rows_as_content(self) -> None:
        source = _tabular_source("8.3.1 接口要求")
        tree = build_structure_tree(source)

        semantics = classify_content(source, tree)
        section = next(node for node in tree.nodes if node.explicit_label == "8.3.1")
        section_annotation = _annotation(semantics, section.structure_node_id)
        table_annotations = [
            _annotation(semantics, node.structure_node_id)
            for node in tree.nodes
            if node.kind in {StructureNodeKind.TABLE, StructureNodeKind.TABLE_ROW}
        ]

        self.assertEqual(section_annotation.semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(
            section_annotation.requirement_subtype,
            RequirementSubtype.PRODUCT_REQUIREMENT,
        )
        self.assertEqual(
            section_annotation.reason_codes,
            ("tabular_requirement_container_signal",),
        )
        self.assertTrue(table_annotations)
        self.assertTrue(
            all(
                annotation.semantic_class is SemanticClass.TECHNICAL_INFORMATION
                for annotation in table_annotations
            )
        )

    def test_table_rows_with_explicit_applicable_note_become_parameter_requirements(
        self,
    ) -> None:
        source = _tabular_note_source()
        tree = build_structure_tree(source)

        semantics = classify_content(source, tree)
        rows = sorted(
            (node for node in tree.nodes if node.kind is StructureNodeKind.TABLE_ROW),
            key=lambda node: node.table_row_index or 0,
        )
        observed = tuple(
            (
                _annotation(semantics, row.structure_node_id).semantic_class,
                _annotation(semantics, row.structure_node_id).requirement_subtype,
            )
            for row in rows
        )

        self.assertEqual(
            observed,
            (
                (SemanticClass.TECHNICAL_INFORMATION, None),
                (
                    SemanticClass.REQUIREMENT,
                    RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT,
                ),
                (
                    SemanticClass.REQUIREMENT,
                    RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT,
                ),
                (SemanticClass.TECHNICAL_INFORMATION, None),
            ),
        )

    def test_explicit_performance_parameter_schema_is_conservative(
        self,
    ) -> None:
        source = _performance_parameter_table_source()
        tree = build_structure_tree(source)
        semantics = classify_content(source, tree)
        rows = {
            node.table_row_index: _annotation(
                semantics,
                node.structure_node_id,
            )
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
        }
        self.assertIsNot(rows[0].semantic_class, SemanticClass.REQUIREMENT)
        for row_index in (1, 2):
            with self.subTest(row_index=row_index):
                self.assertEqual(
                    rows[row_index].semantic_class,
                    SemanticClass.REQUIREMENT,
                )
                self.assertEqual(
                    rows[row_index].requirement_subtype,
                    RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT,
                )
                self.assertEqual(
                    rows[row_index].reason_codes,
                    ("explicit_performance_parameter_schema",),
                )

        counterexamples = (
            _performance_parameter_table_source(
                headers=(
                    "Parameter",
                    "Unit",
                    "Value",
                    "Condition",
                    "Owner",
                    "Comment",
                )
            ),
            _performance_parameter_table_source(
                headers=(
                    "Field",
                    "Unit",
                    "Lower limit",
                    "Nominal",
                    "Upper limit",
                    "Condition",
                )
            ),
            _performance_parameter_table_source(
                data_rows=(
                    ("", "mm", "1", "2", "3", "ambient"),
                    ("Second", "mm", "4", "5", "6", "ambient"),
                )
            ),
            _performance_parameter_table_source(
                headers=(
                    "Item",
                    "Unit",
                    "Minimum order",
                    "Nominal stock",
                    "Maximum order",
                    "Comment",
                )
            ),
            _performance_parameter_table_source(
                data_rows=(
                    ("First", "mm", "-", "N/A", "—", "ambient"),
                    ("Second", "mm", "4", "5", "6", "ambient"),
                )
            ),
            _performance_parameter_table_source(
                data_rows=(
                    (
                        "Parameter",
                        "Unit",
                        "Lower limit",
                        "Nominal",
                        "Upper limit",
                        "Condition",
                    ),
                    ("Second", "mm", "4", "5", "6", "ambient"),
                )
            ),
            _performance_parameter_table_source(
                data_rows=(
                    ("Note: reference only", "", "1", "", "", ""),
                    ("Second", "mm", "4", "5", "6", "ambient"),
                )
            ),
        )
        for index, counterexample in enumerate(counterexamples):
            with self.subTest(counterexample=index):
                counter_tree = build_structure_tree(counterexample)
                counter_semantics = classify_content(
                    counterexample,
                    counter_tree,
                )
                first_data_row = next(
                    node
                    for node in counter_tree.nodes
                    if node.kind is StructureNodeKind.TABLE_ROW
                    and node.table_row_index == 1
                )
                annotation = _annotation(
                    counter_semantics,
                    first_data_row.structure_node_id,
                )
                self.assertIsNot(
                    annotation.requirement_subtype,
                    RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT,
                )

    def test_root_xlsx_material_rows_use_unique_identity_and_shared_roles(
        self,
    ) -> None:
        source = _material_requirement_table_source()
        tree = build_structure_tree(source)
        semantics = classify_content(source, tree)
        rows = {
            node.table_row_index: _annotation(
                semantics,
                node.structure_node_id,
            )
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
        }

        self.assertIsNot(rows[0].semantic_class, SemanticClass.REQUIREMENT)
        for row_index in (1, 2, 3):
            with self.subTest(row_index=row_index):
                self.assertEqual(
                    rows[row_index].semantic_class,
                    SemanticClass.REQUIREMENT,
                )
                self.assertEqual(
                    rows[row_index].requirement_subtype,
                    RequirementSubtype.PRODUCT_REQUIREMENT,
                )
                self.assertEqual(
                    rows[row_index].reason_codes,
                    ("material_requirement_table_row",),
                )

        table = next(
            node for node in tree.nodes if node.kind is StructureNodeKind.TABLE
        )
        target_rows = tuple(
            node
            for node in tree.nodes
            if node.parent_structure_node_id == table.structure_node_id
            and node.kind is StructureNodeKind.TABLE_ROW
            and node.table_row_index in {1, 2, 3}
        )
        shared_sources = {
            relation.source_structure_node_id
            for relation in tree.relations
            if relation.target_structure_node_id
            in {row.structure_node_id for row in target_rows}
            and relation.kind is StructureRelationKind.APPLIES_TO
        }
        self.assertEqual(len(shared_sources), 4)

        view = project_requirements(source, tree, semantics)
        self.assertEqual(len(view.nodes), 3)
        self.assertTrue(
            all(
                node.role is RequirementProjectionRole.PRIMARY
                and node.decision_mode is RequirementDecisionMode.DIRECT
                and node.requirement_subtype
                is RequirementSubtype.PRODUCT_REQUIREMENT
                for node in view.nodes
            )
        )

    def test_material_requirement_schema_rejects_ambiguous_or_unsafe_tables(
        self,
    ) -> None:
        counterexamples = (
            _material_requirement_table_source(
                headers=(
                    "Material",
                    "Description",
                    "Category",
                    "Acceptance criteria",
                    "Standard",
                    "Material requirement",
                )
            ),
            _material_requirement_table_source(
                headers=(
                    "Material",
                    "Standard",
                    "Category",
                    "Standard",
                    "Acceptance criteria",
                    "Material requirement",
                )
            ),
            _material_requirement_table_source(
                headers=(
                    "Material",
                    "Description",
                    "Category",
                    "Standard / Acceptance criteria",
                    "Acceptance criteria",
                    "Material requirement",
                )
            ),
            _material_requirement_table_source(
                headers=(
                    "Item",
                    "Description",
                    "Category",
                    "Standard",
                    "Acceptance criteria",
                    "Material requirement",
                )
            ),
            _material_requirement_table_source(
                identities=("", "Material B", "Material C")
            ),
            _material_requirement_table_source(
                identities=("Material A", "Material A", "Material C")
            ),
            _material_requirement_table_source(shared_identity=True),
            _material_requirement_table_source(
                headers=(
                    "Material",
                    "BOM quantity",
                    "Category",
                    "Standard",
                    "Acceptance criteria",
                    "Material requirement",
                )
            ),
            _material_requirement_table_source(
                headers=(
                    "Material",
                    "Revision history",
                    "Category",
                    "Standard",
                    "Acceptance criteria",
                    "Material requirement",
                )
            ),
            _material_requirement_table_source(
                headers=(
                    "Material",
                    "Inventory stock",
                    "Category",
                    "Standard",
                    "Acceptance criteria",
                    "Material requirement",
                )
            ),
            _material_requirement_table_source(misaligned_roles=True),
        )
        for index, source in enumerate(counterexamples):
            with self.subTest(counterexample=index):
                tree = build_structure_tree(source)
                semantics = classify_content(source, tree)
                row_annotations = tuple(
                    _annotation(semantics, node.structure_node_id)
                    for node in tree.nodes
                    if node.kind is StructureNodeKind.TABLE_ROW
                    and node.table_row_index in {1, 2, 3}
                )
                self.assertTrue(
                    all(
                        annotation.reason_codes
                        != ("material_requirement_table_row",)
                        for annotation in row_annotations
                    )
                )

    def test_compound_test_row_keeps_one_requirement_and_grounded_information(
        self,
    ) -> None:
        source = _compound_test_row_source()
        tree = build_structure_tree(source)
        semantics = classify_content(source, tree)
        row = next(
            node
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
            and node.table_row_index == 1
        )
        annotation = _annotation(semantics, row.structure_node_id)

        self.assertEqual(annotation.semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(
            annotation.requirement_subtype,
            RequirementSubtype.PRODUCT_REQUIREMENT,
        )
        self.assertEqual(
            annotation.reason_codes,
            (
                "compound_test_requirement_row",
                "sample_and_phase_information_signal",
                "schedule_information_signal",
            ),
        )

        first = classify_table_row_information(
            source,
            tree,
            semantics,
            owner_structure_node_id=row.structure_node_id,
        )
        second = classify_table_row_information(
            source,
            tree,
            semantics,
            owner_structure_node_id=row.structure_node_id,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            tuple(item.information_kind for item in first),
            (
                SemanticInformationKind.SAMPLE_AND_PHASE_INFORMATION,
                SemanticInformationKind.SCHEDULE_INFORMATION,
            ),
        )
        self.assertEqual(
            tuple(item.semantic_class for item in first),
            (
                SemanticClass.TECHNICAL_INFORMATION,
                SemanticClass.SCHEDULE,
            ),
        )
        owner_refs = {span.source_ref for span in annotation.grounding_spans}
        self.assertTrue(
            all(
                item.source_ref.kind is StructureSourceRefKind.TABLE_CELL
                and item.source_ref in owner_refs
                and item.owner_semantic_unit_id == annotation.semantic_unit_id
                and item.owner_structure_node_id == row.structure_node_id
                for item in first
            )
        )
        for item in first:
            self.assertEqual(
                item.information_facet_id,
                stable_semantic_information_facet_id(
                    semantic_snapshot_id=item.semantic_snapshot_id,
                    owner_semantic_unit_id=item.owner_semantic_unit_id,
                    owner_structure_node_id=item.owner_structure_node_id,
                    information_kind=item.information_kind,
                    semantic_class=item.semantic_class,
                    source_ref=item.source_ref,
                    evidence_text_sha256=item.evidence_text_sha256,
                ),
            )
            self.assertNotIn("6 units / OTS", item.model_dump_json())
            self.assertNotIn("2 days", item.model_dump_json())

        tampered = first[0].model_dump(mode="python")
        tampered["evidence_text_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValidationError, "information_facet_id"):
            GroundedSemanticInformationFacet.model_validate(tampered)

        view = project_requirements(source, tree, semantics)
        target_views = tuple(
            item
            for item in view.nodes
            if item.owner_structure_node_id == row.structure_node_id
        )
        self.assertEqual(len(view.nodes), 1)
        self.assertEqual(len(target_views), 1)
        self.assertEqual(target_views[0].role, RequirementProjectionRole.PRIMARY)
        self.assertEqual(
            target_views[0].decision_mode,
            RequirementDecisionMode.DIRECT,
        )
        self.assertEqual(
            target_views[0].requirement_subtype,
            RequirementSubtype.PRODUCT_REQUIREMENT,
        )

    def test_compound_test_row_information_rejects_ambiguous_or_unsafe_shapes(
        self,
    ) -> None:
        ordinary_row = (
            "7",
            "Temperature verification",
            "-",
            "Run the verification through the complete controlled sequence.",
            "Accept only after every recorded limit remains within tolerance.",
            "OEM",
            "6 units / OTS",
            "2 days",
        )
        counterexamples = (
            _compound_test_row_source(
                data_row=(
                    *ordinary_row[:6],
                    ordinary_row[7],
                    ordinary_row[6],
                )
            ),
            _compound_test_row_source(
                data_row=(
                    "7",
                    "Test note",
                    "-",
                    "short",
                    "short",
                    "OEM",
                    "6 units / OTS",
                    "2 days",
                )
            ),
            _compound_test_row_source(
                data_row=(*ordinary_row, "3 days"),
            ),
            _compound_test_row_source(
                data_row=(
                    "7",
                    "Inventory record",
                    "-",
                    "This ordinary stock narrative is intentionally long enough.",
                    "Another ordinary narrative is intentionally long enough.",
                    "OEM",
                    "6 units / OTS",
                    "2 days",
                )
            ),
        )
        for index, source in enumerate(counterexamples):
            with self.subTest(counterexample=index):
                tree = build_structure_tree(source)
                semantics = classify_content(source, tree)
                row = max(
                    (
                        node
                        for node in tree.nodes
                        if node.kind is StructureNodeKind.TABLE_ROW
                    ),
                    key=lambda node: node.table_row_index or 0,
                )
                annotation = _annotation(semantics, row.structure_node_id)
                self.assertIsNot(
                    annotation.requirement_subtype,
                    RequirementSubtype.PRODUCT_REQUIREMENT,
                )
                self.assertEqual(
                    classify_table_row_information(
                        source,
                        tree,
                        semantics,
                        owner_structure_node_id=row.structure_node_id,
                    ),
                    (),
                )

        revision = _compound_test_row_source(revision_history=True)
        revision_tree = build_structure_tree(revision)
        revision_semantics = classify_content(revision, revision_tree)
        revision_row = next(
            node
            for node in revision_tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
            and node.table_row_index == 2
        )
        self.assertEqual(
            _annotation(
                revision_semantics,
                revision_row.structure_node_id,
            ).semantic_class,
            SemanticClass.VERSION_METADATA,
        )
        self.assertEqual(
            classify_table_row_information(
                revision,
                revision_tree,
                revision_semantics,
                owner_structure_node_id=revision_row.structure_node_id,
            ),
            (),
        )

        narrative, narrative_ids = _flat_source(
            (
                (
                    SourceNodeKind.PARAGRAPH,
                    "A narrative note mentions 6 units / OTS and 2 days.",
                ),
            )
        )
        narrative_tree = build_structure_tree(narrative)
        narrative_semantics = classify_content(narrative, narrative_tree)
        narrative_owner = _structure_id_for_source(
            narrative_tree,
            narrative_ids[0],
        )
        self.assertEqual(
            classify_table_row_information(
                narrative,
                narrative_tree,
                narrative_semantics,
                owner_structure_node_id=narrative_owner,
            ),
            (),
        )

    def test_table_cell_numeric_peers_become_two_exact_requirements(self) -> None:
        peer_text = (
            "5、供应商必须接受经确认的技术要求\n"
            "6、供应商应提供改进方案，并完成验证记录"
        )
        source = _explicit_peer_table_source((peer_text,))
        tree = build_structure_tree(source)
        row = next(
            node for node in tree.nodes if node.kind is StructureNodeKind.TABLE_ROW
        )
        peer_nodes = tuple(
            node
            for node in tree.nodes
            if node.parent_structure_node_id == row.structure_node_id
            and node.kind is StructureNodeKind.CLAUSE
        )
        self.assertEqual(
            tuple(node.list_ordinal for node in peer_nodes),
            (5, 6),
        )

        semantics = classify_content(source, tree)
        annotations = tuple(
            _annotation(semantics, node.structure_node_id) for node in peer_nodes
        )
        self.assertTrue(
            all(
                annotation.semantic_class is SemanticClass.REQUIREMENT
                and annotation.requirement_subtype
                is RequirementSubtype.PRODUCT_REQUIREMENT
                and annotation.reason_codes
                == ("explicit_table_cell_peer_requirement",)
                for annotation in annotations
            )
        )
        target_ref = peer_nodes[0].source_refs[0]
        target_spans = tuple(
            next(
                span
                for span in annotation.grounding_spans
                if span.source_ref == target_ref
            )
            for annotation in annotations
        )
        first_line_end = len(peer_text.splitlines(keepends=True)[0])
        self.assertEqual(
            tuple(
                (span.start_offset, span.end_offset) for span in target_spans
            ),
            ((0, first_line_end), (first_line_end, len(peer_text))),
        )

        view = project_requirements(source, tree, semantics)
        peer_view_nodes = tuple(
            node
            for node in view.nodes
            if node.owner_structure_node_id
            in {peer.structure_node_id for peer in peer_nodes}
        )
        self.assertEqual(len(view.nodes), 2)
        self.assertEqual(len(peer_view_nodes), 2)
        self.assertTrue(
            all(
                node.role is RequirementProjectionRole.PRIMARY
                and node.decision_mode is RequirementDecisionMode.DIRECT
                and len(node.semantic_annotation_ids) == 1
                and len(node.source_span_ids) == 1
                for node in peer_view_nodes
            )
        )
        self.assertEqual(
            len({node.source_span_ids[0] for node in peer_view_nodes}),
            2,
        )

    def test_table_cell_numeric_peer_partition_fails_closed(self) -> None:
        valid = (
            "5、供应商必须接受经确认的技术要求\n"
            "6、供应商应提供改进方案，并完成验证记录"
        )
        counterexamples = (
            ("5、供应商必须接受经确认的技术要求\n7、供应商应提交整改记录",),
            (
                "5、供应商必须接受经确认的技术要求\n"
                "6、供应商应提交整改记录\n"
                "7、供应商应完成验证记录",
            ),
            (valid, valid),
        )
        for index, peer_cells in enumerate(counterexamples):
            with self.subTest(counterexample=index):
                source = _explicit_peer_table_source(peer_cells)
                tree = build_structure_tree(source)
                self.assertFalse(
                    any(
                        node.kind is StructureNodeKind.CLAUSE
                        and node.parent_structure_node_id is not None
                        and next(
                            candidate
                            for candidate in tree.nodes
                            if candidate.structure_node_id
                            == node.parent_structure_node_id
                        ).kind
                        is StructureNodeKind.TABLE_ROW
                        for node in tree.nodes
                    )
                )

    def test_quality_adjective_predicate_is_a_grounded_requirement(self) -> None:
        source, ids = _hierarchical_source(
            leaf_heading="5.2.1 外观要求",
            leaf_paragraph="外观应完整、圆滑。",
        )
        tree = build_structure_tree(source)

        semantics = classify_content(source, tree)
        annotation = _annotation(
            semantics,
            _structure_id_for_source(tree, ids["paragraph"]),
        )

        self.assertEqual(annotation.semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(
            annotation.requirement_subtype,
            RequirementSubtype.PRODUCT_REQUIREMENT,
        )
        self.assertEqual(annotation.reason_codes, ("modal_requirement_signal",))

    def test_option_sections_separate_project_risk_from_regulatory_requirement(
        self,
    ) -> None:
        source, source_ids = _flat_source(
            (
                (SourceNodeKind.HEADING, "3.3 项目风险"),
                (SourceNodeKind.PARAGRAPH, "请选择风险属性"),
                (SourceNodeKind.PARAGRAPH, "[ √ ] 风险项甲"),
                (SourceNodeKind.PARAGRAPH, "[   ] 风险项乙"),
                (SourceNodeKind.HEADING, "4.1 法规与认证"),
                (SourceNodeKind.PARAGRAPH, "请选择适用认证区域"),
                (SourceNodeKind.PARAGRAPH, "[ √ ] 区域甲"),
                (SourceNodeKind.PARAGRAPH, "[   ] 区域乙"),
                (SourceNodeKind.PARAGRAPH, "[ √ ] 区域丙"),
            )
        )
        tree = build_structure_tree(source)

        semantics = classify_content(source, tree)
        sections = {
            node.explicit_label: _annotation(semantics, node.structure_node_id)
            for node in tree.nodes
            if node.kind is StructureNodeKind.SECTION
        }

        self.assertEqual(
            sections["3.3"].semantic_class,
            SemanticClass.PROJECT_RISK,
        )
        self.assertEqual(
            sections["4.1"].semantic_class,
            SemanticClass.REQUIREMENT,
        )
        self.assertEqual(
            sections["4.1"].requirement_subtype,
            RequirementSubtype.REGULATION_REQUIREMENT,
        )
        self.assertEqual(
            sections["4.1"].reason_codes,
            ("regulatory_certification_option_container_signal",),
        )
        risk_option = _annotation(
            semantics,
            _structure_id_for_source(tree, source_ids[2]),
        )
        regulatory_option = _annotation(
            semantics,
            _structure_id_for_source(tree, source_ids[6]),
        )
        self.assertEqual(risk_option.semantic_class, SemanticClass.PROJECT_RISK)
        self.assertEqual(
            regulatory_option.semantic_class,
            SemanticClass.TECHNICAL_INFORMATION,
        )

    def test_revision_history_data_rows_require_an_explicit_table_marker(
        self,
    ) -> None:
        source = _revision_history_source(marker="修订记录")
        tree = build_structure_tree(source)
        semantics = classify_content(source, tree)
        rows = {
            node.table_row_index: _annotation(
                semantics,
                node.structure_node_id,
            )
            for node in tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
        }

        for row_index in (2, 3):
            with self.subTest(row_index=row_index):
                self.assertEqual(
                    rows[row_index].semantic_class,
                    SemanticClass.VERSION_METADATA,
                )
                self.assertEqual(
                    rows[row_index].reason_codes,
                    ("revision_history_data_row",),
                )

        unrelated = _revision_history_source(marker="规格信息")
        unrelated_tree = build_structure_tree(unrelated)
        unrelated_semantics = classify_content(unrelated, unrelated_tree)
        unrelated_rows = {
            node.table_row_index: _annotation(
                unrelated_semantics,
                node.structure_node_id,
            )
            for node in unrelated_tree.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
        }
        self.assertIsNot(
            unrelated_rows[2].semantic_class,
            SemanticClass.VERSION_METADATA,
        )

    def test_schedule_and_reference_tables_override_requirement_noun(self) -> None:
        for heading, intro, expected in (
            ("1.1 项目里程碑计划要求", None, SemanticClass.SCHEDULE),
            ("1.1 规范性引用文件要求", None, SemanticClass.REFERENCE),
            ("1.1 表格要求", "项目里程碑计划", SemanticClass.SCHEDULE),
            ("1.1 表格要求", "规范性引用文件", SemanticClass.REFERENCE),
        ):
            with self.subTest(heading=heading, intro=intro):
                source = _tabular_source(heading, intro_text=intro)
                tree = build_structure_tree(source)
                semantics = classify_content(source, tree)
                section = next(
                    node for node in tree.nodes if node.explicit_label == "1.1"
                )
                annotation = _annotation(semantics, section.structure_node_id)

                self.assertEqual(annotation.semantic_class, expected)
                self.assertIsNone(annotation.requirement_subtype)

    def test_objective_uses_numbered_heading_and_first_paragraph_together(self) -> None:
        source, ids = _hierarchical_source(
            leaf_heading="5.2.1 目的",
            leaf_paragraph="确定静态能耗。",
        )
        tree = build_structure_tree(source)

        semantics = classify_content(source, tree)
        annotation = _annotation(
            semantics,
            _structure_id_for_source(tree, ids["leaf"]),
        )

        self.assertEqual(annotation.semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(
            annotation.requirement_subtype,
            RequirementSubtype.TEST_OBJECTIVE_REQUIREMENT,
        )

    def test_heading_background_information_and_unknown_are_kept_distinct(self) -> None:
        source, ids = _flat_source(
            (
                (SourceNodeKind.PARAGRAPH, "尚无足够证据的内容"),
                (SourceNodeKind.HEADING, "1 通用章节"),
                (SourceNodeKind.HEADING, "2 项目背景"),
                (SourceNodeKind.PARAGRAPH, "背景介绍"),
                (SourceNodeKind.HEADING, "3 工作原理"),
                (SourceNodeKind.PARAGRAPH, "工作原理说明"),
            )
        )
        tree = build_structure_tree(source)
        semantics = classify_content(source, tree)

        observed = [
            _annotation(
                semantics,
                _structure_id_for_source(tree, source_node_id),
            ).semantic_class
            for source_node_id in ids
        ]
        self.assertEqual(
            observed,
            [
                SemanticClass.UNRESOLVED,
                SemanticClass.HEADING,
                SemanticClass.BACKGROUND,
                SemanticClass.BACKGROUND,
                SemanticClass.TECHNICAL_INFORMATION,
                SemanticClass.TECHNICAL_INFORMATION,
            ],
        )

    def test_modal_content_inherits_equipment_subtype_from_all_ancestors(self) -> None:
        source, ids = _hierarchical_source(
            leaf_heading="4.12.1 测试设备",
            leaf_paragraph="结果必须保存在记录中。",
        )
        tree = build_structure_tree(source)
        semantics = classify_content(source, tree)
        content = _annotation(
            semantics,
            _structure_id_for_source(tree, ids["paragraph"]),
        )

        self.assertEqual(content.semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(
            content.requirement_subtype,
            RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT,
        )
        self.assertEqual(
            content.reason_codes,
            ("ancestor_test_equipment_context", "modal_requirement_signal"),
        )
        self.assertEqual(
            [span.source_ref.source_id for span in content.grounding_spans],
            [ids["root"], ids["branch"], ids["leaf"], ids["paragraph"]],
        )

    def test_conflicting_equipment_and_objective_signals_are_unresolved(self) -> None:
        source, ids = _flat_source(
            ((SourceNodeKind.HEADING, "4.3 试验设备与试验目的"),)
        )
        tree = build_structure_tree(source)
        semantics = classify_content(source, tree)
        annotation = _annotation(
            semantics,
            _structure_id_for_source(tree, ids[0]),
        )

        self.assertEqual(annotation.semantic_class, SemanticClass.UNRESOLVED)
        self.assertEqual(annotation.status, SemanticAnnotationStatus.UNRESOLVED)
        self.assertEqual(
            annotation.reason_codes,
            ("conflicting_requirement_subtype_signals",),
        )

    def test_numbered_test_method_container_is_one_parent_requirement(self) -> None:
        source, ids = _flat_source(
            (
                (SourceNodeKind.HEADING, "4 验证"),
                (SourceNodeKind.HEADING, "4.12 方法"),
                (SourceNodeKind.HEADING, "4.12.2.1 接合力"),
                (SourceNodeKind.PARAGRAPH, "试验顺序如下："),
                (SourceNodeKind.LIST_ITEM, "1）准备样品。"),
                (SourceNodeKind.LIST_ITEM, "2）记录结果。"),
            )
        )
        tree = build_structure_tree(source)

        annotation = _annotation(
            classify_content(source, tree),
            _structure_id_for_source(tree, ids[2]),
        )

        self.assertEqual(annotation.semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(
            annotation.requirement_subtype,
            RequirementSubtype.TEST_METHOD_REQUIREMENT,
        )
        self.assertEqual(
            annotation.reason_codes,
            ("numbered_test_method_container_signal",),
        )
        child_annotations = [
            _annotation(
                classify_content(source, tree),
                _structure_id_for_source(tree, source_node_id),
            )
            for source_node_id in ids[4:]
        ]
        self.assertTrue(
            all(
                child.semantic_class is SemanticClass.REQUIREMENT
                and child.requirement_subtype
                is RequirementSubtype.TEST_METHOD_REQUIREMENT
                for child in child_annotations
            )
        )

    def test_numbered_requirements_heading_is_one_parent_requirement(self) -> None:
        source, ids = _flat_source(
            (
                (SourceNodeKind.HEADING, "4 验证"),
                (SourceNodeKind.HEADING, "4.13 接合力"),
                (SourceNodeKind.HEADING, "4.13.3 要求。"),
                (SourceNodeKind.PARAGRAPH, "要求如下："),
                (SourceNodeKind.LIST_ITEM, "1）保持力应大于限值。"),
                (SourceNodeKind.LIST_ITEM, "2）解锁力应在范围内。"),
            )
        )
        tree = build_structure_tree(source)

        annotation = _annotation(
            classify_content(source, tree),
            _structure_id_for_source(tree, ids[2]),
        )

        self.assertEqual(annotation.semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(
            annotation.requirement_subtype,
            RequirementSubtype.PRODUCT_REQUIREMENT,
        )
        self.assertEqual(
            annotation.reason_codes,
            ("numbered_requirement_container_signal",),
        )
        child_annotations = [
            _annotation(
                classify_content(source, tree),
                _structure_id_for_source(tree, source_node_id),
            )
            for source_node_id in ids[4:]
        ]
        self.assertTrue(
            all(
                child.semantic_class is SemanticClass.REQUIREMENT
                and child.requirement_subtype is RequirementSubtype.PRODUCT_REQUIREMENT
                for child in child_annotations
            )
        )

    def test_method_step_word_attachment_is_not_misclassified_as_attachment(
        self,
    ) -> None:
        source, ids = _flat_source(
            (
                (SourceNodeKind.HEADING, "4 验证"),
                (SourceNodeKind.HEADING, "4.12.2 方法"),
                (SourceNodeKind.HEADING, "4.12.2.1 接合力"),
                (SourceNodeKind.PARAGRAPH, "试验顺序如下："),
                (SourceNodeKind.LIST_ITEM, "1）装配所有附件。"),
            )
        )
        tree = build_structure_tree(source)

        annotation = _annotation(
            classify_content(source, tree),
            _structure_id_for_source(tree, ids[4]),
        )

        self.assertEqual(annotation.semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(
            annotation.requirement_subtype,
            RequirementSubtype.TEST_METHOD_REQUIREMENT,
        )

    def test_non_modal_compound_words_do_not_create_a_requirement(self) -> None:
        source, _ = _flat_source(((SourceNodeKind.PARAGRAPH, "响应时间与应力曲线"),))
        tree = build_structure_tree(source)

        annotation = classify_content(source, tree).annotations[0]

        self.assertEqual(annotation.semantic_class, SemanticClass.UNRESOLVED)
        self.assertNotIn("modal_requirement_signal", annotation.reason_codes)

    def test_scope_is_complete_unique_and_in_structure_order(self) -> None:
        source, _ = _hierarchical_source(
            leaf_heading="4.2 普通标题",
            leaf_paragraph="普通内容",
        )
        tree = build_structure_tree(source)
        ordered_scope = tuple(node.structure_node_id for node in tree.nodes[1:])

        semantics = classify_content(
            source,
            tree,
            scope_structure_node_ids=ordered_scope[1:3],
        )
        self.assertEqual(
            tuple(item.target_structure_node_id for item in semantics.annotations),
            ordered_scope[1:3],
        )
        self.assertEqual(
            len(semantics.annotations),
            len(semantics.scope_structure_node_ids),
        )

        with self.assertRaises(SemanticClassificationRejected) as reversed_error:
            classify_content(
                source,
                tree,
                scope_structure_node_ids=tuple(reversed(ordered_scope[1:3])),
            )
        self.assertEqual(
            reversed_error.exception.code,
            SemanticClassificationRejectionCode.INVALID_SCOPE,
        )
        with self.assertRaisesRegex(ValidationError, "cover every scoped"):
            SemanticClassificationSnapshot.model_validate(
                {
                    **semantics.model_dump(mode="python"),
                    "annotations": semantics.annotations[:-1],
                }
            )

    def test_grounding_tamper_is_rejected_even_with_a_valid_stable_span_id(
        self,
    ) -> None:
        source, ids = _hierarchical_source(
            leaf_heading="4.12.1 测试设备",
            leaf_paragraph="设备覆盖目标范围。",
        )
        tree = build_structure_tree(source)
        semantics = classify_content(source, tree)
        target_id = _structure_id_for_source(tree, ids["leaf"])
        annotation = _annotation(semantics, target_id)
        last = annotation.grounding_spans[-1]
        shortened_end = max(0, last.end_offset - 1)
        shortened = SemanticGroundingSpan(
            grounding_span_id=stable_semantic_grounding_span_id(
                source_ref=last.source_ref,
                start_offset=last.start_offset,
                end_offset=shortened_end,
            ),
            source_ref=last.source_ref,
            start_offset=last.start_offset,
            end_offset=shortened_end,
        )
        tampered_annotation = annotation.model_copy(
            update={"grounding_spans": (*annotation.grounding_spans[:-1], shortened)}
        )
        tampered = semantics.model_copy(
            update={
                "annotations": tuple(
                    tampered_annotation
                    if item.target_structure_node_id == target_id
                    else item
                    for item in semantics.annotations
                )
            }
        )

        with self.assertRaises(SemanticClassificationRejected) as error:
            validate_semantic_classification_against_inputs(
                tampered,
                source,
                tree,
            )
        self.assertEqual(
            error.exception.code,
            SemanticClassificationRejectionCode.INVALID_GROUNDING,
        )

    def test_text_resolver_can_inject_a_future_ocr_view_and_is_part_of_validation(
        self,
    ) -> None:
        source, ids = _flat_source(
            ((SourceNodeKind.PARAGRAPH, "原生文本未提供足够线索"),)
        )
        tree = build_structure_tree(source)

        def resolver(source_ref, source_ir):  # type: ignore[no-untyped-def]
            del source_ir
            if source_ref.source_id == ids[0]:
                return "测量设备规格"
            return None

        semantics = classify_content(source, tree, text_resolver=resolver)
        annotation = semantics.annotations[0]
        self.assertEqual(annotation.semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(
            annotation.requirement_subtype,
            RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT,
        )
        validate_semantic_classification_against_inputs(
            semantics,
            source,
            tree,
            text_resolver=resolver,
        )
        with self.assertRaises(SemanticClassificationRejected) as error:
            validate_semantic_classification_against_inputs(semantics, source, tree)
        self.assertEqual(
            error.exception.code,
            SemanticClassificationRejectionCode.INVALID_GROUNDING,
        )

    def test_text_resolver_failure_stays_unresolved(self) -> None:
        source, _ = _flat_source(((SourceNodeKind.PARAGRAPH, "测试设备"),))
        tree = build_structure_tree(source)

        def failing_resolver(source_ref, source_ir):  # type: ignore[no-untyped-def]
            del source_ref, source_ir
            raise RuntimeError("synthetic resolver failure")

        semantics = classify_content(
            source,
            tree,
            text_resolver=failing_resolver,
        )
        annotation = semantics.annotations[0]
        self.assertEqual(annotation.semantic_class, SemanticClass.UNRESOLVED)
        self.assertEqual(annotation.status, SemanticAnnotationStatus.UNRESOLVED)
        self.assertEqual(annotation.reason_codes, ("text_resolution_failed",))
        self.assertEqual(annotation.grounding_spans[0].end_offset, 0)

    def test_visual_text_observation_is_a_first_class_grounding_source(self) -> None:
        source = _visual_source(("4 验证", "4.1 载荷测试仪"))
        tree = build_structure_tree(source)
        target = next(node for node in tree.nodes if node.explicit_label == "4.1")

        semantics = classify_content(source, tree)
        annotation = _annotation(semantics, target.structure_node_id)

        self.assertEqual(annotation.semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(
            annotation.requirement_subtype,
            RequirementSubtype.TEST_EQUIPMENT_REQUIREMENT,
        )
        self.assertTrue(
            all(
                span.source_ref.kind is StructureSourceRefKind.VISUAL_TEXT_OBSERVATION
                for span in annotation.grounding_spans
            )
        )
        self.assertNotIn("载荷测试仪", semantics.model_dump_json())
        validate_semantic_classification_against_inputs(semantics, source, tree)

    def test_normative_reference_keeps_one_applicability_requirement(self) -> None:
        source = _visual_source(
            (
                "1 General",
                "1.9 Normative references",
                "The following standards shall apply.",
            )
        )
        tree = build_structure_tree(source)
        section = next(node for node in tree.nodes if node.explicit_label == "1.9")
        statement = next(
            node
            for node in tree.nodes
            if node.parent_structure_node_id == section.structure_node_id
            and node.kind is StructureNodeKind.CONTENT
        )

        semantics = classify_content(source, tree)

        self.assertEqual(
            _annotation(semantics, section.structure_node_id).semantic_class,
            SemanticClass.REFERENCE,
        )
        statement_annotation = _annotation(semantics, statement.structure_node_id)
        self.assertEqual(
            statement_annotation.semantic_class,
            SemanticClass.REQUIREMENT,
        )
        self.assertEqual(
            statement_annotation.requirement_subtype,
            RequirementSubtype.REGULATION_REQUIREMENT,
        )

    def test_product_information_is_technical_information(self) -> None:
        source = _visual_source(("1 General", "1.3 Product information"))
        tree = build_structure_tree(source)
        section = next(node for node in tree.nodes if node.explicit_label == "1.3")

        semantics = classify_content(source, tree)

        self.assertEqual(
            _annotation(semantics, section.structure_node_id).semantic_class,
            SemanticClass.TECHNICAL_INFORMATION,
        )

    def test_factual_technical_explanations_are_not_requirements(self) -> None:
        source, source_ids = _flat_source(
            (
                (
                    SourceNodeKind.PARAGRAPH,
                    "The adjustment unit consists of two mechanical stages.",
                ),
                (
                    SourceNodeKind.PARAGRAPH,
                    "The stroke calculation is performed from the sensor values.",
                ),
                (
                    SourceNodeKind.PARAGRAPH,
                    "The internal sensor board provides a converted output signal.",
                ),
                (
                    SourceNodeKind.PARAGRAPH,
                    "These firmware versions provide the following operating modes.",
                ),
                (
                    SourceNodeKind.PARAGRAPH,
                    "The correction is given by the calibration curve.",
                ),
            )
        )
        tree = build_structure_tree(source)
        semantics = classify_content(source, tree)

        for source_id in source_ids:
            annotation = _annotation(
                semantics,
                _structure_id_for_source(tree, source_id),
            )
            self.assertEqual(
                annotation.semantic_class,
                SemanticClass.TECHNICAL_INFORMATION,
            )
            self.assertIsNone(annotation.requirement_subtype)
            self.assertEqual(
                annotation.reason_codes,
                ("technical_explanation_signal",),
            )

    def test_modal_requirement_precedes_technical_explanation_signal(self) -> None:
        source, source_ids = _flat_source(
            (
                (
                    SourceNodeKind.PARAGRAPH,
                    "The sensor system shall provide the converted output signal.",
                ),
            )
        )
        tree = build_structure_tree(source)
        semantics = classify_content(source, tree)
        annotation = _annotation(
            semantics,
            _structure_id_for_source(tree, source_ids[0]),
        )

        self.assertEqual(annotation.semantic_class, SemanticClass.REQUIREMENT)
        self.assertEqual(
            annotation.requirement_subtype,
            RequirementSubtype.PRODUCT_REQUIREMENT,
        )
        self.assertEqual(annotation.reason_codes, ("modal_requirement_signal",))

    def test_domain_is_strict_frozen_and_stable(self) -> None:
        source, _ = _flat_source(((SourceNodeKind.HEADING, "1 通用章节"),))
        tree = build_structure_tree(source)
        first = classify_content(source, tree)
        second = classify_content(source, tree)

        self.assertEqual(first, second)
        self.assertEqual(first.schema_version, SEMANTIC_CLASSIFICATION_SCHEMA_VERSION)
        self.assertEqual(first.semantic_snapshot_id, second.semantic_snapshot_id)
        with self.assertRaises(ValidationError):
            first.classifier_id = "mutated"  # type: ignore[misc]
        payload = first.annotations[0].model_dump(mode="python")
        payload["raw_text"] = "semantic snapshots must not copy source text"
        with self.assertRaisesRegex(ValidationError, "raw_text"):
            SemanticAnnotation.model_validate(payload)
        self.assertEqual(
            first.annotations[0].candidate_source,
            SemanticCandidateSource.RULE,
        )


def _hierarchical_source(
    *,
    leaf_heading: str,
    leaf_paragraph: str,
) -> tuple[SourceIRSnapshot, dict[str, str]]:
    specs = (
        (SourceNodeKind.HEADING, "4 验证", None),
        (SourceNodeKind.HEADING, "4.12 机械特性", 0),
        (SourceNodeKind.HEADING, leaf_heading, 1),
        (SourceNodeKind.PARAGRAPH, leaf_paragraph, 2),
    )
    source_nodes: list[SourceNode] = []
    for index, (kind, raw_text, parent_index) in enumerate(specs):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1 + (index // 3),
            block_index=index,
        )
        source_node_id = stable_node_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=kind,
            native_locator=locator,
        )
        source_nodes.append(
            SourceNode(
                source_node_id=source_node_id,
                kind=kind,
                source_order=index,
                raw_text=raw_text,
                native_locator=locator,
                parent_source_node_id=(
                    source_nodes[parent_index].source_node_id
                    if parent_index is not None
                    else None
                ),
            )
        )
    snapshot = _source_snapshot(tuple(source_nodes))
    return snapshot, {
        "root": source_nodes[0].source_node_id,
        "branch": source_nodes[1].source_node_id,
        "leaf": source_nodes[2].source_node_id,
        "paragraph": source_nodes[3].source_node_id,
    }


def _flat_source(
    specs: tuple[tuple[SourceNodeKind, str], ...],
) -> tuple[SourceIRSnapshot, tuple[str, ...]]:
    nodes: list[SourceNode] = []
    for index, (kind, raw_text) in enumerate(specs):
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=index,
        )
        source_node_id = stable_node_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=kind,
            native_locator=locator,
        )
        nodes.append(
            SourceNode(
                source_node_id=source_node_id,
                kind=kind,
                source_order=index,
                raw_text=raw_text,
                native_locator=locator,
            )
        )
    return _source_snapshot(tuple(nodes)), tuple(node.source_node_id for node in nodes)


def _tabular_source(
    heading_text: str,
    *,
    intro_text: str | None = None,
) -> SourceIRSnapshot:
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
        raw_text=heading_text,
        native_locator=heading_locator,
    )
    nodes = [heading]
    if intro_text is not None:
        intro_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=1,
            bbox=(70.0, 125.0, 520.0, 143.0),
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=SOURCE_HASH,
                    adapter_id=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    kind=SourceNodeKind.PARAGRAPH,
                    native_locator=intro_locator,
                ),
                kind=SourceNodeKind.PARAGRAPH,
                source_order=1,
                raw_text=intro_text,
                native_locator=intro_locator,
            )
        )
    table_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        table_index=0,
        bbox=(70.0, 150.0, 520.0, 310.0),
    )
    table_id = stable_table_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=table_locator,
    )
    cells = tuple(
        TableCellIR(
            cell_id=stable_cell_id(table_id, row, column),
            row=row,
            column=column,
            raw_text=(("字段", "内容"), ("A", "B"))[row][column],
            native_locator=NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=1,
                table_index=0,
                row_index=row,
                column_index=column,
                bbox=(
                    70.0 + column * 225.0,
                    150.0 + row * 80.0,
                    295.0 + column * 225.0,
                    230.0 + row * 80.0,
                ),
            ),
        )
        for row in range(2)
        for column in range(2)
    )
    table = SourceNode(
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
            row_count=2,
            column_count=2,
            cells=cells,
        ),
    )
    nodes.append(table)
    return _source_snapshot(tuple(nodes))


def _tabular_note_source() -> SourceIRSnapshot:
    source = _tabular_source("1.1 Performance requirements")
    source_table = next(
        node for node in source.nodes if node.kind is SourceNodeKind.TABLE
    )
    table = source_table.table_payload
    assert table is not None
    table_bbox = source_table.native_locator.bbox
    assert table_bbox is not None
    values = (
        ("Metric", "Tolerance"),
        ("Start", "±10%"),
        ("Stop", "±8%"),
        ("Note: shared tolerance applies.", ""),
    )
    row_height = (table_bbox[3] - table_bbox[1]) / 4.0
    column_width = (table_bbox[2] - table_bbox[0]) / 2.0
    cells: list[TableCellIR] = []
    for row in range(4):
        for column in range(2):
            bbox = (
                (
                    table_bbox[0],
                    table_bbox[1] + row * row_height,
                    table_bbox[2],
                    table_bbox[1] + (row + 1) * row_height,
                )
                if row == 3 and column == 0
                else None
                if row == 3
                else (
                    table_bbox[0] + column * column_width,
                    table_bbox[1] + row * row_height,
                    table_bbox[0] + (column + 1) * column_width,
                    table_bbox[1] + (row + 1) * row_height,
                )
            )
            cells.append(
                TableCellIR(
                    cell_id=stable_cell_id(table.table_id, row, column),
                    row=row,
                    column=column,
                    raw_text=values[row][column],
                    native_locator=NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=1,
                        table_index=0,
                        row_index=row,
                        column_index=column,
                        bbox=bbox,
                    ),
                )
            )
    replacement = source_table.model_copy(
        update={
            "table_payload": TableIR(
                table_id=table.table_id,
                row_count=4,
                column_count=2,
                cells=tuple(cells),
            )
        }
    )
    return source.model_copy(
        update={
            "nodes": tuple(
                replacement if node is source_table else node for node in source.nodes
            )
        }
    )


def _performance_parameter_table_source(
    *,
    headers: tuple[str, ...] = (
        "Parameter",
        "Unit",
        "Lower limit",
        "Typ.",
        "Upper limit",
        "Condition",
    ),
    data_rows: tuple[tuple[str, ...], ...] = (
        ("First", "mm", "1", "2", "3", "ambient"),
        ("Second", "mm", "4", "5", "6", "ambient"),
    ),
) -> SourceIRSnapshot:
    values = (headers, *data_rows)
    if not values or any(len(row) != len(headers) for row in values):
        raise ValueError("performance table rows must have one fixed width")
    table_locator = NativeLocator(
        format=NativeDocumentFormat.DOCX,
        body_element_index=0,
        table_index=0,
    )
    table_id = stable_table_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=table_locator,
    )
    cells = tuple(
        TableCellIR(
            cell_id=stable_cell_id(table_id, row, column),
            row=row,
            column=column,
            raw_text=values[row][column],
            native_locator=NativeLocator(
                format=NativeDocumentFormat.DOCX,
                body_element_index=0,
                table_index=0,
                row_index=row,
                column_index=column,
            ),
        )
        for row in range(len(values))
        for column in range(len(headers))
    )
    table = SourceNode(
        source_node_id=stable_node_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=SourceNodeKind.TABLE,
            native_locator=table_locator,
        ),
        kind=SourceNodeKind.TABLE,
        source_order=0,
        raw_text="",
        native_locator=table_locator,
        table_payload=TableIR(
            table_id=table_id,
            row_count=len(values),
            column_count=len(headers),
            cells=cells,
        ),
    )
    return _source_snapshot((table,))


def _material_requirement_table_source(
    *,
    headers: tuple[str, ...] = (
        "Material",
        "Description",
        "Category",
        "Standard",
        "Acceptance criteria",
        "Material requirement",
    ),
    identities: tuple[str, str, str] = (
        "Material A",
        "Material B",
        "Material C",
    ),
    shared_identity: bool = False,
    misaligned_roles: bool = False,
) -> SourceIRSnapshot:
    if len(headers) != 6:
        raise ValueError("material requirement table requires six headers")
    table_locator = NativeLocator(
        format=NativeDocumentFormat.XLSX,
        workbook="xl/workbook.xml",
        sheet="Material compatibility",
        cell_range="A1:F4",
    )
    table_id = stable_table_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=table_locator,
    )

    cells: list[TableCellIR] = []

    def add_cell(
        row: int,
        column: int,
        raw_text: str,
        *,
        row_span: int = 1,
    ) -> None:
        cells.append(
            TableCellIR(
                cell_id=stable_cell_id(table_id, row, column),
                row=row,
                column=column,
                row_span=row_span,
                raw_text=raw_text,
                native_locator=NativeLocator(
                    format=NativeDocumentFormat.XLSX,
                    workbook="xl/workbook.xml",
                    sheet="Material compatibility",
                    cell_range=f"{chr(ord('A') + column)}{row + 1}",
                ),
            )
        )

    for column, header in enumerate(headers):
        add_cell(0, column, header)

    if shared_identity:
        add_cell(1, 0, identities[0], row_span=3)
    else:
        for row, identity in enumerate(identities, start=1):
            add_cell(row, 0, identity)
    for row in range(1, 4):
        add_cell(row, 1, f"Description {row}")

    add_cell(1, 2, "Compatibility group", row_span=3)
    add_cell(1, 3, "Standard reference", row_span=3)
    if misaligned_roles:
        add_cell(1, 4, "Acceptance threshold", row_span=2)
        add_cell(3, 4, "Acceptance threshold")
    else:
        add_cell(1, 4, "Acceptance threshold", row_span=3)
    add_cell(1, 5, "Shared material constraint", row_span=3)

    merged_regions = [
        MergedRegion(row=1, column=2, row_span=3),
        MergedRegion(row=1, column=3, row_span=3),
        MergedRegion(
            row=1,
            column=4,
            row_span=2 if misaligned_roles else 3,
        ),
        MergedRegion(row=1, column=5, row_span=3),
    ]
    if shared_identity:
        merged_regions.append(MergedRegion(row=1, column=0, row_span=3))

    table = SourceNode(
        source_node_id=stable_node_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=SourceNodeKind.TABLE,
            native_locator=table_locator,
        ),
        kind=SourceNodeKind.TABLE,
        source_order=0,
        raw_text="",
        native_locator=table_locator,
        table_payload=TableIR(
            table_id=table_id,
            row_count=4,
            column_count=6,
            cells=tuple(cells),
            merged_regions=tuple(merged_regions),
        ),
    )
    return _source_snapshot((table,))


def _explicit_peer_table_source(
    peer_cells: tuple[str, ...],
) -> SourceIRSnapshot:
    values = ("review context", *peer_cells, "decision context")
    table_locator = NativeLocator(
        format=NativeDocumentFormat.XLSX,
        workbook="xl/workbook.xml",
        sheet="Deviation review",
        cell_range=f"A1:{chr(ord('A') + len(values) - 1)}1",
    )
    table_id = stable_table_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=table_locator,
    )
    cells = tuple(
        TableCellIR(
            cell_id=stable_cell_id(table_id, 0, column),
            row=0,
            column=column,
            raw_text=raw_text,
            native_locator=NativeLocator(
                format=NativeDocumentFormat.XLSX,
                workbook="xl/workbook.xml",
                sheet="Deviation review",
                cell_range=f"{chr(ord('A') + column)}1",
            ),
        )
        for column, raw_text in enumerate(values)
    )
    table = SourceNode(
        source_node_id=stable_node_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=SourceNodeKind.TABLE,
            native_locator=table_locator,
        ),
        kind=SourceNodeKind.TABLE,
        source_order=0,
        raw_text="",
        native_locator=table_locator,
        table_payload=TableIR(
            table_id=table_id,
            row_count=1,
            column_count=len(values),
            cells=cells,
        ),
    )
    return _source_snapshot((table,))


def _compound_test_row_source(
    *,
    data_row: tuple[str, ...] = (
        "7",
        "Temperature verification",
        "-",
        "Run the verification through the complete controlled sequence.",
        "Accept only after every recorded limit remains within tolerance.",
        "OEM",
        "6 units / OTS",
        "2 days",
    ),
    revision_history: bool = False,
) -> SourceIRSnapshot:
    if len(data_row) < 8:
        raise ValueError("compound test rows require at least eight columns")
    if revision_history:
        revision_row = (
            "V1.0",
            *data_row[1:],
        )
        values = (
            ("Revision history", *("",) * (len(data_row) - 1)),
            (
                "Identifier",
                "Date",
                "Owner",
                "Description",
                "Approval",
                "Comment",
                *("",) * (len(data_row) - 6),
            ),
            revision_row,
        )
    else:
        values = (
            tuple(f"Field {column + 1}" for column in range(len(data_row))),
            data_row,
        )
    if any(len(row) != len(data_row) for row in values):
        raise ValueError("compound test rows must have one fixed width")

    final_column = chr(ord("A") + len(data_row) - 1)
    table_locator = NativeLocator(
        format=NativeDocumentFormat.XLSX,
        workbook="xl/workbook.xml",
        sheet="Structured verification",
        cell_range=f"A1:{final_column}{len(values)}",
    )
    table_id = stable_table_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=table_locator,
    )
    cells = tuple(
        TableCellIR(
            cell_id=stable_cell_id(table_id, row, column),
            row=row,
            column=column,
            raw_text=values[row][column],
            native_locator=NativeLocator(
                format=NativeDocumentFormat.XLSX,
                workbook="xl/workbook.xml",
                sheet="Structured verification",
                cell_range=f"{chr(ord('A') + column)}{row + 1}",
            ),
        )
        for row in range(len(values))
        for column in range(len(data_row))
    )
    table = SourceNode(
        source_node_id=stable_node_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=SourceNodeKind.TABLE,
            native_locator=table_locator,
        ),
        kind=SourceNodeKind.TABLE,
        source_order=0,
        raw_text="",
        native_locator=table_locator,
        table_payload=TableIR(
            table_id=table_id,
            row_count=len(values),
            column_count=len(data_row),
            cells=cells,
        ),
    )
    return _source_snapshot((table,))


def _revision_history_source(*, marker: str) -> SourceIRSnapshot:
    values = (
        (marker, "", "", "", ""),
        ("版本号", "修订日期", "责任人", "修订内容", "备注"),
        ("V1.0", "2025/11/24", "甲", "首版", ""),
        ("V11\n.", "2025/12/15", "甲", "结构变更", ""),
        ("", "", "", "", ""),
    )
    table_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        table_index=0,
        bbox=(60.0, 80.0, 540.0, 330.0),
    )
    table_id = stable_table_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=table_locator,
    )
    row_height = 50.0
    column_width = 96.0
    cells = tuple(
        TableCellIR(
            cell_id=stable_cell_id(table_id, row, column),
            row=row,
            column=column,
            raw_text=values[row][column],
            native_locator=NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=1,
                table_index=0,
                row_index=row,
                column_index=column,
                bbox=(
                    60.0 + column * column_width,
                    80.0 + row * row_height,
                    60.0 + (column + 1) * column_width,
                    80.0 + (row + 1) * row_height,
                ),
            ),
        )
        for row in range(len(values))
        for column in range(len(values[0]))
    )
    table = SourceNode(
        source_node_id=stable_node_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=SourceNodeKind.TABLE,
            native_locator=table_locator,
        ),
        kind=SourceNodeKind.TABLE,
        source_order=0,
        raw_text="",
        native_locator=table_locator,
        table_payload=TableIR(
            table_id=table_id,
            row_count=len(values),
            column_count=len(values[0]),
            cells=cells,
        ),
    )
    return _source_snapshot((table,))


def _source_snapshot(nodes: tuple[SourceNode, ...]) -> SourceIRSnapshot:
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
        nodes=nodes,
        created_at=CREATED_AT,
    )


def _visual_source(lines: tuple[str, ...]) -> SourceIRSnapshot:
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
        created_at=CREATED_AT,
    )


def _structure_id_for_source(tree, source_node_id: str) -> str:  # type: ignore[no-untyped-def]
    return next(
        node.structure_node_id
        for node in tree.nodes
        if any(ref.source_id == source_node_id for ref in node.source_refs)
    )


def _annotation(
    semantics: SemanticClassificationSnapshot,
    target_id: str,
) -> SemanticAnnotation:
    return next(
        item
        for item in semantics.annotations
        if item.target_structure_node_id == target_id
    )


if __name__ == "__main__":
    unittest.main()
