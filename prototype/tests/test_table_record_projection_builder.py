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

from app.application.extraction.agent_table_boundaries import (
    AgentTableParentBoundaryV1,
    AgentTableProjectionMaterializationV1,
    AgentTableRowBindingV1,
    build_agent_table_parent_boundary,
    materialize_agent_table_parent_boundaries,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.application.extraction.revise_structure import get_base_effective_structure
from app.application.extraction.table_record_projection import (
    ManualMatrixIntersection,
    ManualRowGroup,
    build_manual_matrix_intersection_projection,
    build_manual_row_group_projection,
    build_table_record_projections,
    propose_manual_row_groups,
)
from app.application.extraction.table_evidence_projection import (
    build_table_evidence_bindings,
)
from app.domain.extraction.source_ir import (
    MergedRegion,
    NativeDocumentFormat,
    NativeLocator,
    SourceAssetKind,
    SourceAssetRef,
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
    StructureBoundaryBasis,
    StructureNodeKind,
    StructureRelation,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.domain.extraction.table_records import (
    TableRecordHeaderAxis,
    TableRecordKind,
    TableRecordProjectionDecisionOrigin,
    TableRecordProjectionMode,
)


SOURCE_SHA256 = "a" * 64
ADAPTER_ID = "table-record-test"
ADAPTER_VERSION = "1.0.0"


class TableRecordProjectionBuilderTests(unittest.TestCase):
    def test_plain_or_long_table_stays_whole_without_proven_boundaries(self) -> None:
        rows = tuple(
            (f"自由文本 {index}", f"说明 {index}") for index in range(40)
        )
        source = _source_with_tables((_table_spec(rows=rows),))
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(projection.records, ())
        self.assertEqual(projection.table_fact.table_parts[0].row_count, 40)

    def test_explicit_numbered_row_is_a_record_without_invented_headers(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        (
                            "5、供应商必须接受经确认的技术要求\n"
                            "6、供应商应提供改进方案并完成验证记录",
                            "完整说明",
                        ),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(len(projection.records), 1)
        record = projection.records[0]
        self.assertEqual(record.kind, TableRecordKind.ROW)
        self.assertFalse(record.is_requirement)
        self.assertEqual(record.row_bindings[0].row_index, 0)
        self.assertEqual(record.header_bindings, ())

    def test_declared_headers_and_unique_key_create_rows_not_requirements(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("序号", "参数", "要求"),
                        ("A-01", "温度", "≤ 80 ℃"),
                        ("A-02", "压力", "≥ 2 bar"),
                    ),
                    header_rows=(0,),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(
            [record.row_bindings[0].row_index for record in projection.records],
            [1, 2],
        )
        self.assertTrue(
            all(
                {
                    binding.axis for binding in record.header_bindings
                }
                == {TableRecordHeaderAxis.COLUMN}
                for record in projection.records
            )
        )
        self.assertTrue(all(not record.is_requirement for record in projection.records))

    def test_partial_automatic_records_fail_closed_when_business_row_is_unconsumed(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("序号", "部件", "要求"),
                        ("A-01", "制动控制器", "第一条"),
                        ("A-02", "防盗控制器", "第二条"),
                        ("", "车载充电器", "第三条"),
                    ),
                    header_rows=(0,),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(projection.records, ())
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_INCOMPLETE_FACT_COVERAGE_KEPT_WHOLE",
        )
        self.assertEqual(
            projection.decision.evidence,
            (
                "自动记录完整覆盖校验失败：未归属业务单元格 2 个，"
                "重复归属业务单元格 0 个，重复归属业务行 0 个",
            ),
        )

    def test_automatic_records_fail_closed_when_a_merged_fact_is_not_shared(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("序号", "共同条件", "要求"),
                        ("A-01", "适用于两行", "第一条"),
                        ("A-02", None, "第二条"),
                    ),
                    header_rows=(0,),
                    merged_regions=(
                        MergedRegion(row=1, column=1, row_span=2),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)
        structure = structure.model_copy(
            update={
                "relations": tuple(
                    relation
                    for relation in structure.relations
                    if relation.kind is not StructureRelationKind.APPLIES_TO
                )
            }
        )

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(projection.records, ())
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_INCOMPLETE_FACT_COVERAGE_KEPT_WHOLE",
        )
        self.assertEqual(
            projection.decision.evidence,
            (
                "自动记录完整覆盖校验失败：未归属业务单元格 0 个，"
                "重复归属业务单元格 1 个，重复归属业务行 0 个",
            ),
        )

    def test_automatic_records_require_shared_fact_proof_for_every_record(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("序号", "共同条件", "要求"),
                        ("A-01", "适用于两行", "第一条"),
                        ("A-02", None, "第二条"),
                    ),
                    header_rows=(0,),
                    merged_regions=(
                        MergedRegion(row=1, column=1, row_span=2),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)
        shared_relations = tuple(
            relation
            for relation in structure.relations
            if relation.kind is StructureRelationKind.APPLIES_TO
        )
        self.assertEqual(len(shared_relations), 2)
        structure = structure.model_copy(
            update={
                "relations": (
                    *tuple(
                        relation
                        for relation in structure.relations
                        if relation.kind is not StructureRelationKind.APPLIES_TO
                    ),
                    shared_relations[0],
                )
            }
        )

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(projection.records, ())
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_INCOMPLETE_FACT_COVERAGE_KEPT_WHOLE",
        )

    def test_automatic_records_do_not_treat_unbound_headers_as_consumed(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("项目", "说明"),
                        (
                            "",
                            "5、供应商必须接受经确认的技术要求\n"
                            "6、供应商应提供改进方案并完成验证记录",
                        ),
                    ),
                    header_rows=(0,),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(projection.records, ())
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_INCOMPLETE_FACT_COVERAGE_KEPT_WHOLE",
        )

    def test_automatic_records_do_not_drop_leading_full_width_business_fact(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("所有项目必须满足防水总则", ""),
                        (
                            "5、供应商必须接受经确认的技术要求\n"
                            "6、供应商应提供改进方案并完成验证记录",
                            "完整说明",
                        ),
                    ),
                    merged_regions=(
                        MergedRegion(row=0, column=0, column_span=2),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(projection.records, ())
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_INCOMPLETE_FACT_COVERAGE_KEPT_WHOLE",
        )

    def test_stub_schema_rejects_ellipsis_placeholder_record(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("序号", "类型", "型号"),
                        ("1", "SoC", ""),
                        ("2", "MCU", ""),
                        ("3", "DRAM", ""),
                        ("……", "……", ""),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(
            [record.row_bindings[0].row_index for record in projection.records],
            [1, 2, 3],
        )
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_STABLE_KEY_SCHEMA",
        )

    def test_dash_and_slash_cells_are_business_facts_not_placeholders(self) -> None:
        for marker in ("/", "-", "—"):
            with self.subTest(marker=marker):
                source = _source_with_tables(
                    (
                        _table_spec(
                            rows=(
                                ("序号", "要求"),
                                ("A-01", "第一条"),
                                ("A-02", "第二条"),
                                ("", marker),
                            ),
                            header_rows=(0,),
                        ),
                    )
                )
                structure = build_structure_tree(source)

                projection = build_table_record_projections(source, structure)[0]

                self.assertEqual(
                    projection.mode,
                    TableRecordProjectionMode.WHOLE_TABLE,
                )
                self.assertEqual(projection.records, ())
                self.assertEqual(
                    projection.decision.reason_code,
                    "AUTO_INCOMPLETE_FACT_COVERAGE_KEPT_WHOLE",
                )

    def test_merged_category_label_description_rows_are_structural_records(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("路面识别", "", "根据传感器识别当前路况"),
                        ("性能相关", "主动侧倾", "减小车身侧倾角"),
                        (None, "主动俯仰", "减小车身俯仰角"),
                        (None, "能量回收", "回收悬架能量"),
                    ),
                    merged_regions=(
                        MergedRegion(row=0, column=0, column_span=2),
                        MergedRegion(row=1, column=0, row_span=3),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(len(projection.records), 4)
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_MERGED_LABEL_DESCRIPTION_ROWS",
        )
        self.assertTrue(
            all(not record.header_bindings for record in projection.records)
        )
        self.assertEqual(
            projection.records[1].shared_merged_values[0].text,
            "性能相关",
        )

    def test_merged_category_continuation_with_unbound_title_fails_closed(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("系统参数", "", ""),
                        ("油泵总成", "噪声", "小于限值"),
                        (None, "峰值功率", "小于额定值"),
                        (None, "最大流量", "大于目标值"),
                    ),
                    merged_regions=(
                        MergedRegion(row=0, column=0, column_span=3),
                        MergedRegion(row=1, column=0, row_span=3),
                    ),
                    page_number=1,
                ),
                _table_spec(
                    rows=(
                        ("", "超调量", "小于 3%"),
                        ("", "通讯周期", "1ms"),
                    ),
                    page_number=2,
                ),
            )
        )
        structure = _ensure_first_table_continuation(build_structure_tree(source))

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(projection.records, ())
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_INCOMPLETE_FACT_COVERAGE_KEPT_WHOLE",
        )

    def test_merged_category_inheritance_survives_a_proven_repeated_header(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("分类", "参数", "要求"),
                        ("分类 A", "参数 1", "值 1"),
                        (None, "参数 2", "值 2"),
                    ),
                    header_rows=(0,),
                    merged_regions=(MergedRegion(row=1, column=0, row_span=2),),
                    page_number=1,
                ),
                _table_spec(
                    rows=(
                        ("分类", "参数", "要求"),
                        ("", "参数 3", "值 3"),
                        ("", "参数 4", "值 4"),
                    ),
                    header_rows=(0,),
                    page_number=2,
                ),
            )
        )
        structure = _ensure_first_table_continuation(build_structure_tree(source))

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(len(projection.records), 4)
        self.assertEqual(
            [
                tuple(value.text for value in record.shared_merged_values)
                for record in projection.records[-2:]
            ],
            [("分类 A",), ("分类 A",)],
        )

    def test_explicit_new_category_closes_cross_page_inheritance_fail_closed(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("分类", "参数", "要求"),
                        ("分类 A", "参数 1", "值 1"),
                        (None, "参数 2", "值 2"),
                    ),
                    header_rows=(0,),
                    merged_regions=(MergedRegion(row=1, column=0, row_span=2),),
                    page_number=1,
                ),
                _table_spec(
                    rows=(
                        ("分类", "参数", "要求"),
                        ("分类 B", "参数 3", "值 3"),
                        ("", "参数 4", "值 4"),
                    ),
                    header_rows=(0,),
                    page_number=2,
                ),
            )
        )
        structure = _ensure_first_table_continuation(build_structure_tree(source))

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_AMBIGUOUS_CONTINUED_CLASSIFICATION_KEPT_WHOLE",
        )
        self.assertFalse(projection.records)

    def test_blank_parameter_continuation_is_not_promoted_to_a_new_record(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("油泵总成", "噪声", "第一段"),
                        (None, "峰值功率", "第二段"),
                    ),
                    merged_regions=(
                        MergedRegion(row=0, column=0, row_span=2),
                    ),
                    page_number=1,
                ),
                _table_spec(
                    rows=(("", "", "峰值功率的续行说明"),),
                    page_number=2,
                ),
            )
        )
        structure = _ensure_first_table_continuation(build_structure_tree(source))

        projection = build_table_record_projections(source, structure)[0]

        second_table = source.nodes[1].table_payload
        assert second_table is not None
        self.assertEqual(len(projection.records), 2)
        self.assertEqual(
            [
                (binding.table_id, binding.row_index)
                for binding in projection.records[-1].row_bindings
            ],
            [
                (source.nodes[0].table_payload.table_id, 1),
                (second_table.table_id, 0),
            ],
        )

    def test_unbound_visual_parameter_note_row_fails_closed(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        (
                            "活塞速度（m/s）\n电流(A)",
                            "0.052",
                            "0.131",
                            "0.262",
                        ),
                        ("I Start", "±17.5%", "±17.5%", "±15%"),
                        ("I Stop", "±17.5%", "±17.5%", "±15%"),
                        ("注：公差小于 50N 时取 ±50N", "", "", ""),
                    ),
                    merged_regions=(
                        MergedRegion(row=3, column=0, column_span=4),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(projection.records, ())
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_INCOMPLETE_FACT_COVERAGE_KEPT_WHOLE",
        )

    def test_proven_table_note_binding_is_consumed_by_each_record(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("序号", "要求"),
                        ("A-01", "第一条"),
                        ("A-02", "第二条"),
                        ("注：所有数值均为标称值", ""),
                    ),
                    header_rows=(0,),
                    merged_regions=(
                        MergedRegion(row=3, column=0, column_span=2),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)
        note = next(
            node
            for node in structure.nodes
            if node.kind is StructureNodeKind.CONTENT
            and node.boundary_basis is StructureBoundaryBasis.MERGED_RANGE
        )
        business_rows = tuple(
            sorted(
                (
                    node
                    for node in structure.nodes
                    if node.kind is StructureNodeKind.TABLE_ROW
                    and node.table_row_index in {1, 2}
                ),
                key=lambda node: node.table_row_index or 0,
            )
        )
        structure = structure.model_copy(
            update={
                "relations": (
                    *structure.relations,
                    *tuple(
                        StructureRelation(
                            relation_id=f"relation-proven-note-{index}",
                            kind=StructureRelationKind.APPLIES_TO,
                            source_structure_node_id=note.structure_node_id,
                            target_structure_node_id=row.structure_node_id,
                            source_refs=note.source_refs,
                        )
                        for index, row in enumerate(business_rows)
                    ),
                )
            }
        )

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(len(projection.records), 2)
        self.assertTrue(
            all(
                tuple(note.text for note in record.notes)
                == ("注：所有数值均为标称值",)
                for record in projection.records
            )
        )

    def test_inferred_two_level_header_does_not_swallow_ordinal_one_row(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("索引", "参数分组", "", "目标值"),
                        ("1", "首项", "条件一", "值一"),
                        ("2", "第二项", "条件二", "值二"),
                        ("3", "第三项", "条件三", "值三"),
                        ("4", "第四项", "条件四", "值四"),
                    ),
                    merged_regions=(
                        MergedRegion(row=0, column=1, column_span=2),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(
            [record.row_bindings[0].row_index for record in projection.records],
            [1, 2, 3, 4],
        )
        self.assertNotEqual(
            projection.decision.reason_code,
            "AUTO_TWO_LEVEL_PARAMETER_ROWS",
        )
        self.assertIn(
            "连续序号：4 行",
            projection.decision.evidence,
        )

    def test_declared_header_does_not_exempt_ordinal_one_business_row(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("索引", "参数分组", "", "目标值"),
                        ("1", "首项", "条件一", "值一"),
                        ("2", "第二项", "条件二", "值二"),
                        ("3", "第三项", "条件三", "值三"),
                        ("4", "第四项", "条件四", "值四"),
                    ),
                    header_rows=(0, 1),
                    merged_regions=(
                        MergedRegion(row=0, column=1, column_span=2),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(
            [record.row_bindings[0].row_index for record in projection.records],
            [1, 2, 3, 4],
        )
        table = source.nodes[0].table_payload
        assert table is not None
        trusted_header_cell_ids = {
            cell.cell_id for cell in table.cells if cell.row == 0
        }
        self.assertTrue(
            all(
                {
                    cell_id
                    for binding in record.header_bindings
                    for cell_id in binding.cell_ids
                }.issubset(trusted_header_cell_ids)
                for record in projection.records
            )
        )

    def test_two_level_parameter_table_includes_continuation_part(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("活塞杆直径≥φ18mm", "", "活塞杆直径＜φ18mm", ""),
                        ("侧向力（N）", "摩擦力（N）", "侧向力（N）", "摩擦力（N）"),
                        ("0", "≤60", "0", "≤45"),
                        ("200", "≤75", "100", "≤60"),
                        ("400", "≤100", "200", "≤75"),
                    ),
                    merged_regions=(
                        MergedRegion(row=0, column=0, column_span=2),
                        MergedRegion(row=0, column=2, column_span=2),
                    ),
                    page_number=1,
                ),
                _table_spec(
                    rows=(("600", "≤125", "-", "-"),),
                    page_number=2,
                ),
            )
        )
        structure = _ensure_first_table_continuation(build_structure_tree(source))

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(len(projection.records), 4)
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_TWO_LEVEL_PARAMETER_ROWS",
        )
        self.assertEqual(
            projection.records[-1].row_bindings[0].table_id,
            source.nodes[1].table_payload.table_id,
        )
        self.assertTrue(
            any(
                binding.text == "侧向力（N）"
                for binding in projection.records[-1].header_bindings
            )
        )

    def test_three_level_cross_axis_matrix_stays_whole(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("Verification plan", "", "", "", ""),
                        ("测试项目 / Test Item", "测试分组 / Test Groups", "", "", ""),
                        (None, "Group 1", "Group 2", "Group 3", "Group 4"),
                        (None, "Test sequence", "", "", ""),
                        ("Pressure", "1", "2", "3", "4"),
                        ("Temperature", "2", "3", "4", "5"),
                    ),
                    merged_regions=(
                        MergedRegion(row=0, column=0, column_span=5),
                        MergedRegion(row=1, column=0, row_span=3),
                        MergedRegion(row=1, column=1, column_span=4),
                        MergedRegion(row=3, column=1, column_span=4),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_CROSS_AXIS_MATRIX_KEPT_WHOLE",
        )

    def test_hierarchical_leaf_rows_remain_records_across_continuation(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        (
                            "序号 / No.",
                            "测试项目 / Test Item",
                            "测试条件 / Test Condition",
                            "参数 / Parameter",
                            "样件 / Sample",
                            "",
                        ),
                        (None, None, None, None, "测量 1", "测量 2"),
                        ("1", "性能", "条件 A", "参数 A", "值 A1", "值 A2"),
                        (None, None, "条件 B", "参数 B", "值 B1", "值 B2"),
                        ("2", "耐久", "条件 C", "参数 C", "值 C1", "值 C2"),
                    ),
                    merged_regions=(
                        MergedRegion(row=0, column=0, row_span=2),
                        MergedRegion(row=0, column=1, row_span=2),
                        MergedRegion(row=0, column=2, row_span=2),
                        MergedRegion(row=0, column=3, row_span=2),
                        MergedRegion(row=0, column=4, column_span=2),
                        MergedRegion(row=2, column=0, row_span=2),
                        MergedRegion(row=2, column=1, row_span=2),
                    ),
                    page_number=1,
                ),
                _table_spec(
                    rows=(
                        ("", "", "条件 D", "参数 D", "值 D1", "值 D2"),
                        ("", "", "条件 E", "参数 E", "值 E1", "值 E2"),
                    ),
                    page_number=2,
                ),
            )
        )
        structure = _ensure_first_table_continuation(build_structure_tree(source))

        projection = build_table_record_projections(source, structure)[0]

        first_table = source.nodes[0].table_payload
        second_table = source.nodes[1].table_payload
        assert first_table is not None
        assert second_table is not None
        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_HIERARCHICAL_LEAF_ROWS",
        )
        self.assertEqual(
            [
                (
                    record.row_bindings[0].table_id,
                    record.row_bindings[0].row_index,
                )
                for record in projection.records
            ],
            [
                (first_table.table_id, 2),
                (first_table.table_id, 3),
                (first_table.table_id, 4),
                (second_table.table_id, 0),
                (second_table.table_id, 1),
            ],
        )
        self.assertTrue(
            all(len(record.row_bindings) == 1 for record in projection.records)
        )
        self.assertTrue(
            all(record.header_bindings for record in projection.records)
        )

    def test_bilingual_stub_alias_proves_a_stable_record_schema(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("序号 / No.", "项目 / Item", "要求 / Requirement"),
                        ("A-01", "压力", "不低于标称值"),
                        ("A-02", "温度", "不高于限值"),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(
            [record.row_bindings[0].row_index for record in projection.records],
            [1, 2],
        )
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_STABLE_KEY_SCHEMA",
        )

    def test_blank_key_continuation_row_is_appended_to_previous_record(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("区域\n零件", "中国", "欧洲", "北美"),
                        ("储油缸总成", "T5", "T6", "T6"),
                        ("紧固件", "480 小时盐雾", "720 小时盐雾", "960 小时盐雾"),
                    ),
                    page_number=1,
                ),
                _table_spec(
                    rows=(("", "无红锈", "无红锈", "无红锈"),),
                    page_number=2,
                ),
            )
        )
        structure = _ensure_first_table_continuation(build_structure_tree(source))

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(len(projection.records), 2)
        final_record = projection.records[-1]
        self.assertEqual(len(final_record.row_bindings), 2)
        self.assertEqual(
            {
                binding.table_id for binding in final_record.row_bindings
            },
            {
                source.nodes[0].table_payload.table_id,
                source.nodes[1].table_payload.table_id,
            },
        )
        self.assertIn(
            "续页空键内容并入上一记录：1 行",
            projection.decision.evidence,
        )

    def test_title_and_exact_stub_header_create_inherited_row_records(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("油泵总成性能要求", "", "", ""),
                        ("序号", "项目", "要求", "备注"),
                        ("1", "压力", "≥ 2 bar", "验收"),
                        ("2", "温度", "≤ 80 ℃", ""),
                    ),
                    merged_regions=(
                        MergedRegion(
                            row=0,
                            column=0,
                            column_span=4,
                        ),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(
            [record.row_bindings[0].row_index for record in projection.records],
            [2, 3],
        )
        inherited_text = {
            binding.text for binding in projection.records[0].header_bindings
        }
        self.assertIn("油泵总成性能要求", inherited_text)
        self.assertIn("序号", inherited_text)
        self.assertIn("要求", inherited_text)

    def test_exact_code_stub_header_allows_non_numeric_record_keys(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("编号", "安全目标", "等级"),
                        ("ASC 01", "避免非预期驱动", "D"),
                        ("ASC 02", "避免非预期制动", "C"),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(len(projection.records), 2)
        self.assertTrue(
            all(
                any(binding.text == "编号" for binding in record.header_bindings)
                for record in projection.records
            )
        )

    def test_mid_body_stub_label_does_not_reset_an_unproven_glossary_schema(
        self,
    ) -> None:
        rows = (
            ("术语", "说明"),
            ("CAN", "控制器局域网"),
            ("CANFD", "灵活数据速率控制器局域网"),
            ("DLC", "数据长度代码"),
            ("ECU", "电子控制单元"),
            ("EMC", "电磁兼容"),
            ("ESD", "静电放电"),
            ("ID", "标识符"),
            ("PCB", "印制电路板"),
            ("PLL", "锁相环"),
            ("TDC", "上止点"),
        )
        source = _source_with_tables((_table_spec(rows=rows),))
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(projection.records, ())
        self.assertEqual(
            projection.table_fact.table_parts[0].row_count,
            len(rows),
        )

    def test_declared_glossary_schema_keeps_rows_before_and_after_stub_values(
        self,
    ) -> None:
        rows = (
            ("术语", "说明"),
            ("CAN", "控制器局域网"),
            ("DLC", "数据长度代码"),
            ("ECU", "电子控制单元"),
            ("ID", "标识符"),
            ("PCB", "印制电路板"),
            ("PLL", "锁相环"),
        )
        source = _source_with_tables(
            (_table_spec(rows=rows, header_rows=(0,)),)
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(
            [record.row_bindings[0].row_index for record in projection.records],
            list(range(1, len(rows))),
        )
        self.assertTrue(
            all(
                {binding.text for binding in record.header_bindings}
                == {"术语", "说明"}
                for record in projection.records
            )
        )

    def test_page_table_with_unconsumed_segment_fails_closed(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("6.5）前悬架要求", "", ""),
                        ("序号", "项目", "要求"),
                        ("1", "刚度", "满足设计目标"),
                        ("2", "阻尼", "满足设计目标"),
                        ("6.6）后悬架要求", "", ""),
                        ("说明", "后续正文", "不得误投影"),
                    ),
                    merged_regions=(
                        MergedRegion(row=0, column=0, column_span=3),
                        MergedRegion(row=4, column=0, column_span=3),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(projection.records, ())
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_INCOMPLETE_FACT_COVERAGE_KEPT_WHOLE",
        )
        self.assertEqual(projection.table_fact.table_parts[0].row_count, 6)

    def test_multiple_schema_segments_with_unconsumed_tail_fail_closed(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("6.5）前悬架要求", "", ""),
                        ("序号", "项目", "要求"),
                        ("1", "刚度", "第一段一"),
                        ("2", "阻尼", "第一段二"),
                        ("6.6）后悬架要求", "", ""),
                        ("序号", "项目", "要求"),
                        ("1", "刚度", "第二段一"),
                        ("2", "阻尼", "第二段二"),
                        ("附录 A 参考信息", "", ""),
                        ("3", "孤立正文", "没有新表头不得投影"),
                    ),
                    merged_regions=(
                        MergedRegion(row=0, column=0, column_span=3),
                        MergedRegion(row=4, column=0, column_span=3),
                        MergedRegion(row=8, column=0, column_span=3),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(projection.records, ())
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_INCOMPLETE_FACT_COVERAGE_KEPT_WHOLE",
        )

    def test_numbered_value_in_unmerged_first_cell_is_not_a_structure_boundary(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("编号", "项目", "要求"),
                        ("A-01", "刚度", "满足设计目标"),
                        ("6.6）", "", ""),
                        ("A-03", "阻尼", "满足设计目标"),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(
            [record.row_bindings[0].row_index for record in projection.records],
            [1, 2, 3],
        )

    def test_numeric_matrix_without_stub_or_ordinal_topology_stays_whole(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("10", "20", "30"),
                        ("20", "30", "40"),
                        ("30", "40", "50"),
                    ),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(projection.records, ())

    def test_one_isolated_ordinal_row_does_not_prove_a_record_schema(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(("1", "单独一行并不足以证明表格拓扑"),),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(projection.records, ())

    def test_continuation_inherits_inferred_header_context(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("性能要求", ""),
                        ("序号", "要求"),
                        ("1", "第一条"),
                        ("2", "第二条"),
                    ),
                    merged_regions=(
                        MergedRegion(
                            row=0,
                            column=0,
                            column_span=2,
                        ),
                    ),
                    page_number=1,
                ),
                _table_spec(
                    rows=(("3", "第三条"), ("4", "第四条")),
                    page_number=2,
                ),
            )
        )
        structure = build_structure_tree(source)
        structure = structure.model_copy(
            update={
                "relations": tuple(
                    relation
                    for relation in structure.relations
                    if relation.kind is not StructureRelationKind.CONTINUES
                )
            }
        )
        table_nodes = tuple(
            node for node in structure.nodes if node.kind.value == "table"
        )
        if not any(
            relation.kind is StructureRelationKind.CONTINUES
            and relation.source_structure_node_id
            in {node.structure_node_id for node in table_nodes}
            and relation.target_structure_node_id
            in {node.structure_node_id for node in table_nodes}
            for relation in structure.relations
        ):
            structure = structure.model_copy(
                update={
                    "relations": (
                        *structure.relations,
                        StructureRelation(
                            relation_id="relation-inferred-header-continuation",
                            kind=StructureRelationKind.CONTINUES,
                            source_structure_node_id=table_nodes[0].structure_node_id,
                            target_structure_node_id=table_nodes[1].structure_node_id,
                            source_refs=table_nodes[1].source_refs,
                        ),
                    )
                }
            )

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(len(projection.records), 4)
        final_record = projection.records[-1]
        self.assertTrue(
            any(binding.text == "序号" for binding in final_record.header_bindings)
        )
        self.assertTrue(
            any(binding.text == "性能要求" for binding in final_record.header_bindings)
        )

    def test_record_projection_builds_whole_and_record_evidence_bindings(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("序号", "参数", "要求"),
                        ("A-01", "温度", "≤ 80 ℃"),
                        ("A-02", "压力", "≥ 2 bar"),
                    ),
                    header_rows=(0,),
                    notes=("表中数值为验收边界。",),
                ),
            )
        )
        structure = build_structure_tree(source)
        projections = build_table_record_projections(source, structure)
        table = source.nodes[0].table_payload
        assert table is not None

        bindings = build_table_evidence_bindings(
            table_record_projections=projections,
            tables=(
                {
                    "id": table.table_id,
                    "page_start": 1,
                    "notes": list(table.notes),
                    "cells": [
                        {
                            "id": cell.cell_id,
                            "row": cell.row,
                            "column": cell.column,
                            "text": cell.raw_text,
                        }
                        for cell in table.cells
                    ],
                },
            ),
        )

        self.assertEqual(len(bindings), 1 + len(projections[0].records))
        self.assertIsNone(bindings[0].record_id)
        self.assertEqual(
            {binding.record_id for binding in bindings[1:]},
            {record.record_id for record in projections[0].records},
        )
        self.assertTrue(all(binding.table_refs for binding in bindings))
        self.assertTrue(all(binding.header_refs for binding in bindings[1:]))

    def test_declared_headers_do_not_split_without_a_stable_key(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("类别", "要求"),
                        ("相同", "第一条"),
                        ("相同", "第二条"),
                    ),
                    header_rows=(0,),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(projection.records, ())

    def test_effective_structure_identity_is_bound_into_the_projection(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(("序号", "要求"), ("A-01", "第一条")),
                    header_rows=(0,),
                ),
            )
        )
        base = build_structure_tree(source)
        effective = get_base_effective_structure(source, base)

        projection = build_table_record_projections(source, effective)[0]

        self.assertEqual(
            projection.table_fact.structure_snapshot_id,
            effective.effective_snapshot_id,
        )
        self.assertEqual(projection.projection_revision, 0)

    def test_continuation_chain_skips_repeated_header_without_row_thresholds(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(("序号", "要求"), ("A-01", "第一条")),
                    header_rows=(0,),
                    page_number=1,
                ),
                _table_spec(
                    rows=(("序号", "要求"), ("A-02", "第二条")),
                    page_number=2,
                ),
            )
        )
        structure = build_structure_tree(source)
        table_nodes = tuple(
            node
            for node in structure.nodes
            if node.kind.value == "table"
        )
        existing = tuple(
            relation
            for relation in structure.relations
            if relation.kind is StructureRelationKind.CONTINUES
            and relation.source_structure_node_id
            in {node.structure_node_id for node in table_nodes}
            and relation.target_structure_node_id
            in {node.structure_node_id for node in table_nodes}
        )
        if not existing:
            continuation = StructureRelation(
                relation_id="relation-manual-table-continuation",
                kind=StructureRelationKind.CONTINUES,
                source_structure_node_id=table_nodes[0].structure_node_id,
                target_structure_node_id=table_nodes[1].structure_node_id,
                source_refs=table_nodes[1].source_refs,
            )
            structure = structure.model_copy(
                update={"relations": (*structure.relations, continuation)}
            )

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(len(projection.table_fact.table_parts), 2)
        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(
            [
                (record.row_bindings[0].table_id, record.row_bindings[0].row_index)
                for record in projection.records
            ],
            [
                (source.nodes[0].table_payload.table_id, 1),
                (source.nodes[1].table_payload.table_id, 1),
            ],
        )
        self.assertTrue(
            any(
                binding.text == "序号"
                for binding in projection.records[1].header_bindings
            )
        )
        second_table = source.nodes[1].table_payload
        assert second_table is not None
        self.assertTrue(
            all(
                binding.table_id == second_table.table_id
                for binding in projection.records[1].header_bindings
            )
        )

    def test_mismatched_declared_header_resets_blank_row_attachment(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("序号", "类别", "参数", "要求"),
                        ("A-01", "甲", "P", "R"),
                    ),
                    header_rows=(0,),
                    page_number=1,
                ),
                _table_spec(
                    rows=(
                        ("序号", "类别", "参数", "单位"),
                        ("", "", "", "续页内容"),
                    ),
                    header_rows=(0,),
                    page_number=2,
                ),
            )
        )
        structure = _ensure_first_table_continuation(build_structure_tree(source))

        projection = build_table_record_projections(source, structure)[0]
        second_table = source.nodes[1].table_payload
        assert second_table is not None

        self.assertFalse(
            any(
                binding.table_id == second_table.table_id
                and binding.row_index == 1
                for record in projection.records
                for binding in record.row_bindings
            )
        )
        self.assertNotIn(
            "续页空键内容并入上一记录：1 行",
            projection.decision.evidence,
        )

    def test_matching_declared_header_keeps_blank_row_attachment(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(("序号", "要求"), ("A-01", "第一条")),
                    header_rows=(0,),
                    page_number=1,
                ),
                _table_spec(
                    rows=(("序号", "要求"), ("", "续页内容")),
                    header_rows=(0,),
                    page_number=2,
                ),
            )
        )
        structure = _ensure_first_table_continuation(build_structure_tree(source))

        projection = build_table_record_projections(source, structure)[0]
        second_table = source.nodes[1].table_payload
        assert second_table is not None

        self.assertTrue(
            any(
                binding.table_id == second_table.table_id
                and binding.row_index == 1
                for record in projection.records
                for binding in record.row_bindings
            )
        )

    def test_continuation_without_repeated_header_inherits_and_keeps_ordinal_gap(
        self,
    ) -> None:
        first_rows = (
            ("", "项目", "约束"),
            *tuple(
                (str(index), f"条目 {index}", f"值 {index}")
                for index in range(1, 9)
            ),
        )
        second_ordinals = (9, 10, 11, 12, 13, 14, 16, 17)
        second_rows = tuple(
            (str(index), f"条目 {index}", f"值 {index}")
            for index in second_ordinals
        )
        source = _source_with_tables(
            (
                _table_spec(rows=first_rows, page_number=1),
                _table_spec(rows=second_rows, page_number=2),
            )
        )
        structure = _ensure_first_table_continuation(build_structure_tree(source))

        projection = build_table_record_projections(source, structure)[0]

        first_table = source.nodes[0].table_payload
        second_table = source.nodes[1].table_payload
        assert first_table is not None
        assert second_table is not None
        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(len(projection.records), 16)
        self.assertEqual(
            [
                (
                    record.row_bindings[0].table_id,
                    record.row_bindings[0].row_index,
                )
                for record in projection.records
            ],
            [
                *((first_table.table_id, row_index) for row_index in range(1, 9)),
                *((second_table.table_id, row_index) for row_index in range(8)),
            ],
        )
        self.assertEqual(
            [
                _cell_value(second_table, row_index, 0)
                for row_index in range(second_table.row_count)
            ],
            [str(value) for value in second_ordinals],
        )
        self.assertTrue(
            all(record.header_bindings for record in projection.records[8:])
        )
        self.assertTrue(
            all(
                binding.table_id == first_table.table_id
                for record in projection.records[8:]
                for binding in record.header_bindings
            )
        )

    def test_continuation_repeated_blank_key_header_and_trailing_note_are_grounded(
        self,
    ) -> None:
        note_text = "注：后续记录沿用同一说明。"
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("", "类别", "参数", "条件"),
                        ("A-01", "类别甲", "参数甲", "条件甲"),
                        ("A-02", "类别乙", "参数乙", "条件乙"),
                    ),
                    page_number=1,
                ),
                _table_spec(
                    rows=(
                        ("", "类别", "参数", "引用"),
                        ("A-03", "类别丙", "参数丙", "条件丙"),
                        ("A-04", "类别丁", "参数丁", "条件丁"),
                        (note_text, "", "", ""),
                    ),
                    merged_regions=(
                        MergedRegion(row=3, column=0, column_span=4),
                    ),
                    page_number=2,
                ),
            )
        )
        structure = _ensure_first_table_continuation(build_structure_tree(source))
        table_nodes = tuple(
            sorted(
                (
                    node
                    for node in structure.nodes
                    if node.kind is StructureNodeKind.TABLE
                ),
                key=lambda node: (node.source_order, node.structure_node_id),
            )
        )
        second_table_node = table_nodes[1]
        second_rows = {
            node.table_row_index: node
            for node in structure.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
            and node.parent_structure_node_id
            == second_table_node.structure_node_id
        }
        note_node = next(
            node
            for node in structure.nodes
            if node.kind is StructureNodeKind.CONTENT
            and node.boundary_basis is StructureBoundaryBasis.MERGED_RANGE
            and node.parent_structure_node_id
            == second_table_node.structure_node_id
        )
        structure = structure.model_copy(
            update={
                "relations": (
                    *structure.relations,
                    StructureRelation(
                        relation_id=(
                            "relation-fixture-merged-note-applies-to-record"
                        ),
                        kind=StructureRelationKind.APPLIES_TO,
                        source_structure_node_id=note_node.structure_node_id,
                        target_structure_node_id=second_rows[2].structure_node_id,
                        source_refs=note_node.source_refs,
                    ),
                )
            }
        )

        projection = build_table_record_projections(source, structure)[0]

        first_table = source.nodes[0].table_payload
        second_table = source.nodes[1].table_payload
        assert first_table is not None
        assert second_table is not None
        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(
            [
                (
                    record.row_bindings[0].table_id,
                    record.row_bindings[0].row_index,
                )
                for record in projection.records
            ],
            [
                (first_table.table_id, 1),
                (first_table.table_id, 2),
                (second_table.table_id, 1),
                (second_table.table_id, 2),
            ],
        )
        self.assertTrue(
            all(record.header_bindings for record in projection.records[2:])
        )
        self.assertTrue(
            all(
                binding.table_id == second_table.table_id
                for record in projection.records[2:]
                for binding in record.header_bindings
            )
        )
        self.assertEqual(
            [note.text for note in projection.records[-1].notes],
            [note_text],
        )
        self.assertFalse(projection.records[-1].shared_merged_values)
        with self.assertRaisesRegex(ValueError, "table header"):
            build_manual_row_group_projection(
                source,
                structure,
                table_structure_node_id=second_table_node.structure_node_id,
                row_groups=(
                    ManualRowGroup(
                        row_coordinates=((second_table.table_id, 0),)
                    ),
                ),
            )
        with self.assertRaisesRegex(ValueError, "table note"):
            build_manual_row_group_projection(
                source,
                structure,
                table_structure_node_id=second_table_node.structure_node_id,
                row_groups=(
                    ManualRowGroup(
                        row_coordinates=((second_table.table_id, 3),)
                    ),
                ),
            )

    def test_manual_matrix_requires_explicit_row_and_column_headers(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("车型", "低温", "高温"),
                        ("前轴", "10", "20"),
                        ("后轴", "11", "21"),
                    ),
                    header_rows=(0,),
                ),
            )
        )
        structure = build_structure_tree(source)
        table_node = next(
            node for node in structure.nodes if node.kind.value == "table"
        )
        table = source.nodes[0].table_payload
        assert table is not None
        cells = {(cell.row, cell.column): cell for cell in table.cells}

        projection = build_manual_matrix_intersection_projection(
            source,
            structure,
            table_structure_node_id=table_node.structure_node_id,
            intersections=(
                ManualMatrixIntersection(
                    table_id=table.table_id,
                    row_index=1,
                    column_index=1,
                    row_header_cell_ids=(cells[(1, 0)].cell_id,),
                    column_header_cell_ids=(cells[(0, 1)].cell_id,),
                ),
            ),
        )

        record = projection.records[0]
        self.assertEqual(record.kind, TableRecordKind.MATRIX_INTERSECTION)
        self.assertEqual(
            {binding.axis for binding in record.header_bindings},
            {TableRecordHeaderAxis.ROW, TableRecordHeaderAxis.COLUMN},
        )
        self.assertEqual(record.column_bindings[0].column_index, 1)

    def test_manual_row_groups_support_split_merge_and_multi_row_records(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("项目", "要求"),
                        ("第一部分", "基础条件"),
                        ("", "同一记录的补充条件"),
                        ("第二部分", "另一记录"),
                    ),
                    header_rows=(0,),
                    notes=("原表备注必须保留。",),
                ),
            )
        )
        structure = build_structure_tree(source)
        table_node = next(
            node for node in structure.nodes if node.kind.value == "table"
        )
        table = source.nodes[0].table_payload
        assert table is not None

        projection = build_manual_row_group_projection(
            source,
            structure,
            table_structure_node_id=table_node.structure_node_id,
            row_groups=(
                ManualRowGroup(
                    row_coordinates=(
                        (table.table_id, 1),
                        (table.table_id, 2),
                    )
                ),
                ManualRowGroup(
                    row_coordinates=((table.table_id, 3),)
                ),
            ),
            projection_revision=3,
        )

        self.assertEqual(projection.mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(projection.projection_revision, 3)
        self.assertEqual(len(projection.records), 2)
        self.assertEqual(len(projection.records[0].row_bindings), 2)
        self.assertEqual(projection.records[0].notes[0].text, "原表备注必须保留。")
        self.assertEqual(projection.decision.reason_code, "MANUAL_ROW_GROUPS")

        merged = build_manual_row_group_projection(
            source,
            structure,
            table_structure_node_id=table_node.structure_node_id,
            row_groups=(),
            projection_revision=4,
        )
        self.assertEqual(merged.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(merged.records, ())
        self.assertEqual(merged.decision.reason_code, "MANUAL_KEEP_WHOLE_TABLE")

    def test_agent_parent_boundaries_drive_existing_row_materializer(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("序号", "要求"),
                        ("1", "第一条及其补充"),
                        ("", "第一条的跨行内容"),
                        ("2", "第二条"),
                    ),
                    header_rows=(0,),
                ),
            )
        )
        structure = build_structure_tree(source)
        base_projection = build_table_record_projections(source, structure)[0]
        table = source.nodes[0].table_payload
        assert table is not None
        cells_by_row = {
            row_index: tuple(
                cell.cell_id
                for cell in table.cells
                if cell.row == row_index
            )
            for row_index in range(table.row_count)
        }
        artifact_id = "agent-table-boundaries-" + "1" * 32
        artifact_sha256 = "2" * 64
        agent_logical_table_id = "agent-logical-table-connector-procedure"

        def boundary(
            *,
            parent_id: str,
            source_order: int,
            row_indexes: tuple[int, ...],
            row_cell_ids: dict[int, tuple[str, ...]] | None = None,
        ) -> AgentTableParentBoundaryV1:
            resolved_cell_ids = cells_by_row if row_cell_ids is None else row_cell_ids
            row_bindings = tuple(
                AgentTableRowBindingV1(
                    table_id=table.table_id,
                    row_index=row_index,
                    cell_ids=resolved_cell_ids[row_index],
                )
                for row_index in row_indexes
            )
            return build_agent_table_parent_boundary(
                source_sha256=source.source_sha256,
                boundary_artifact_id=artifact_id,
                boundary_artifact_sha256=artifact_sha256,
                logical_table_id=agent_logical_table_id,
                parent_provision_id=parent_id,
                source_order=source_order,
                row_bindings=row_bindings,
                source_refs=tuple(
                    StructureSourceRef(
                        kind=StructureSourceRefKind.TABLE_CELL,
                        source_id=cell_id,
                    )
                    for binding in row_bindings
                    for cell_id in binding.cell_ids
                ),
            )

        boundaries = (
            boundary(
                parent_id="agent-parent-1",
                source_order=0,
                row_indexes=(1, 2),
            ),
            boundary(
                parent_id="agent-parent-2",
                source_order=1,
                row_indexes=(3,),
            ),
        )
        materialized = materialize_agent_table_parent_boundaries(
            source,
            structure,
            base_projection=base_projection,
            boundaries=boundaries,
            expected_boundary_artifact_id=artifact_id,
            expected_boundary_artifact_sha256=artifact_sha256,
            expected_agent_logical_table_id=agent_logical_table_id,
            agent_logical_table_scope_sha256="3" * 64,
            d1_table_map_artifact_id="agent-d1-table-map-fixture",
            d1_table_map_artifact_sha256="4" * 64,
        )

        self.assertNotEqual(
            materialized.agent_logical_table_id,
            materialized.materializer_logical_table_id,
        )
        self.assertEqual(
            materialized.projection.decision.origin,
            TableRecordProjectionDecisionOrigin.AGENT,
        )
        self.assertEqual(
            [
                tuple(binding.row_index for binding in record.row_bindings)
                for record in materialized.projection.records
            ],
            [(1, 2), (3,)],
        )
        self.assertEqual(
            [item.parent_provision_id for item in materialized.parent_record_bindings],
            ["agent-parent-1", "agent-parent-2"],
        )
        self.assertEqual(
            materialized.table_fact_binding_sha256,
            base_projection.table_fact.fact_binding_sha256,
        )
        tampered_payload = materialized.model_dump(mode="json")
        tampered_payload["parent_record_bindings"][0]["parent_provision_id"] = (
            "agent-parent-tampered"
        )
        with self.assertRaisesRegex(ValidationError, "hash does not bind"):
            AgentTableProjectionMaterializationV1.model_validate(tampered_payload)

        wrong_cells = dict(cells_by_row)
        wrong_cells[1] = cells_by_row[3]
        mismatched_boundaries = (
            boundary(
                parent_id="agent-parent-1",
                source_order=0,
                row_indexes=(1, 2),
                row_cell_ids=wrong_cells,
            ),
            boundaries[1],
        )
        with self.assertRaisesRegex(ValueError, "cell bindings differ"):
            materialize_agent_table_parent_boundaries(
                source,
                structure,
                base_projection=base_projection,
                boundaries=mismatched_boundaries,
                expected_boundary_artifact_id=artifact_id,
                expected_boundary_artifact_sha256=artifact_sha256,
                expected_agent_logical_table_id=agent_logical_table_id,
                agent_logical_table_scope_sha256="3" * 64,
                d1_table_map_artifact_id="agent-d1-table-map-fixture",
                d1_table_map_artifact_sha256="4" * 64,
            )

    def test_manual_row_group_proposal_attaches_blank_continuation(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("零件", "中国", "欧洲"),
                        ("紧固件", "480 小时盐雾", "720 小时盐雾"),
                        ("储油缸", "T5", "T6"),
                    ),
                    page_number=1,
                ),
                _table_spec(
                    rows=(("", "无红锈", "无红锈"),),
                    page_number=2,
                ),
            )
        )
        structure = _ensure_first_table_continuation(build_structure_tree(source))
        table_node = next(
            node
            for node in structure.nodes
            if node.kind.value == "table"
            and node.source_order
            == min(
                item.source_order
                for item in structure.nodes
                if item.kind.value == "table"
            )
        )

        proposals = propose_manual_row_groups(
            source,
            structure,
            table_structure_node_id=table_node.structure_node_id,
        )

        self.assertEqual(len(proposals), 2)
        self.assertEqual(len(proposals[-1].row_coordinates), 2)
        projection = build_manual_row_group_projection(
            source,
            structure,
            table_structure_node_id=table_node.structure_node_id,
            row_groups=proposals,
        )
        self.assertEqual(len(projection.records), 2)
        self.assertEqual(len(projection.records[-1].row_bindings), 2)
        with self.assertRaisesRegex(
            ValueError,
            "complete, ordered and contiguous partition",
        ):
            build_manual_row_group_projection(
                source,
                structure,
                table_structure_node_id=table_node.structure_node_id,
                row_groups=tuple(reversed(proposals)),
            )

    def test_manual_row_groups_require_an_ordered_complete_partition(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("项目", "要求"),
                        ("A", "第一条"),
                        ("B", "第二条"),
                        ("C", "-"),
                    ),
                    header_rows=(0,),
                ),
            )
        )
        structure = build_structure_tree(source)
        table_node = next(
            node for node in structure.nodes if node.kind.value == "table"
        )
        table = source.nodes[0].table_payload
        assert table is not None

        projection = build_manual_row_group_projection(
            source,
            structure,
            table_structure_node_id=table_node.structure_node_id,
            row_groups=(
                ManualRowGroup(
                    row_coordinates=(
                        (table.table_id, 1),
                        (table.table_id, 2),
                    )
                ),
                ManualRowGroup(
                    row_coordinates=((table.table_id, 3),)
                ),
            ),
        )
        self.assertEqual(len(projection.records), 2)
        self.assertEqual(len(projection.records[0].row_bindings), 2)

        invalid_partitions = (
            (
                ManualRowGroup(
                    row_coordinates=((table.table_id, 1),)
                ),
                ManualRowGroup(
                    row_coordinates=((table.table_id, 3),)
                ),
            ),
            (
                ManualRowGroup(
                    row_coordinates=(
                        (table.table_id, 1),
                        (table.table_id, 3),
                    )
                ),
                ManualRowGroup(
                    row_coordinates=((table.table_id, 2),)
                ),
            ),
            (
                ManualRowGroup(
                    row_coordinates=((table.table_id, 2),)
                ),
                ManualRowGroup(
                    row_coordinates=((table.table_id, 1),)
                ),
                ManualRowGroup(
                    row_coordinates=((table.table_id, 3),)
                ),
            ),
        )
        for invalid_groups in invalid_partitions:
            with self.subTest(invalid_groups=invalid_groups):
                with self.assertRaisesRegex(
                    ValueError,
                    "complete, ordered and contiguous partition",
                ):
                    build_manual_row_group_projection(
                        source,
                        structure,
                        table_structure_node_id=(
                            table_node.structure_node_id
                        ),
                        row_groups=invalid_groups,
                    )

    def test_manual_row_group_proposal_keeps_image_only_business_row(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(("序号", "证据"), ("A-01", "第一条"), ("", "")),
                    header_rows=(0,),
                ),
            )
        )
        table_source = source.nodes[0]
        table_payload = table_source.table_payload
        assert table_payload is not None
        image_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            table_index=0,
            row_index=2,
            column_index=1,
            bbox=(100.0, 100.0, 130.0, 130.0),
        )
        asset = SourceAssetRef(
            asset_id="asset-manual-image-only-row",
            kind=SourceAssetKind.IMAGE,
            content_sha256="d" * 64,
            media_type="image/png",
            storage_key="tables/manual-image-only-row.png",
            native_locator=image_locator,
        )
        image = SourceNode(
            source_node_id=stable_node_id(
                source_sha256=SOURCE_SHA256,
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                kind=SourceNodeKind.IMAGE,
                native_locator=image_locator,
            ),
            kind=SourceNodeKind.IMAGE,
            source_order=1,
            native_locator=image_locator,
            parent_source_node_id=table_source.source_node_id,
            asset_ref=asset.asset_id,
        )
        source = SourceIRSnapshot.model_validate(
            {
                **source.model_dump(mode="python"),
                "nodes": (*source.nodes, image),
                "assets": (asset,),
            }
        )
        structure = build_structure_tree(source)
        table_node = next(
            node
            for node in structure.nodes
            if node.kind is StructureNodeKind.TABLE
        )
        row_node = next(
            node
            for node in structure.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
            and node.table_row_index == 2
        )
        image_cell = next(
            cell
            for cell in table_payload.cells
            if cell.row == 2 and cell.column == 1
        )
        structure = structure.model_copy(
            update={
                "relations": (
                    *structure.relations,
                    StructureRelation(
                        relation_id="relation-manual-image-only-row",
                        kind=StructureRelationKind.PLACED_IN_CELL,
                        source_structure_node_id=row_node.structure_node_id,
                        target_structure_node_id=table_node.structure_node_id,
                        source_refs=(
                            StructureSourceRef(
                                kind=StructureSourceRefKind.SOURCE_NODE,
                                source_id=image.source_node_id,
                            ),
                            StructureSourceRef(
                                kind=StructureSourceRefKind.TABLE_CELL,
                                source_id=image_cell.cell_id,
                            ),
                        ),
                    ),
                )
            }
        )

        groups = propose_manual_row_groups(
            source,
            structure,
            table_structure_node_id=table_node.structure_node_id,
        )
        self.assertEqual(
            tuple(
                coordinate
                for group in groups
                for coordinate in group.row_coordinates
            ),
            ((table_payload.table_id, 1), (table_payload.table_id, 2)),
        )
        projection = build_manual_row_group_projection(
            source,
            structure,
            table_structure_node_id=table_node.structure_node_id,
            row_groups=groups,
        )
        self.assertEqual(
            [
                visual.asset_id
                for record in projection.records
                for visual in record.image_refs
            ],
            [asset.asset_id],
        )

    def test_manual_row_groups_reject_overlap_and_rows_from_another_table(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(rows=(("项目", "要求"), ("A", "第一条"))),
                _table_spec(
                    rows=(("项目", "要求"), ("B", "第二条")),
                    page_number=2,
                ),
            )
        )
        structure = build_structure_tree(source)
        structure = structure.model_copy(
            update={
                "relations": tuple(
                    relation
                    for relation in structure.relations
                    if relation.kind is not StructureRelationKind.CONTINUES
                )
            }
        )
        table_nodes = tuple(
            node for node in structure.nodes if node.kind.value == "table"
        )
        first_table = source.nodes[0].table_payload
        second_table = source.nodes[1].table_payload
        assert first_table is not None
        assert second_table is not None

        with self.assertRaisesRegex(
            ValueError,
            "only one manual row group",
        ):
            build_manual_row_group_projection(
                source,
                structure,
                table_structure_node_id=table_nodes[0].structure_node_id,
                row_groups=(
                    ManualRowGroup(
                        row_coordinates=((first_table.table_id, 1),)
                    ),
                    ManualRowGroup(
                        row_coordinates=((first_table.table_id, 1),)
                    ),
                ),
            )

        with self.assertRaisesRegex(
            ValueError,
            "another logical table",
        ):
            build_manual_row_group_projection(
                source,
                structure,
                table_structure_node_id=table_nodes[0].structure_node_id,
                row_groups=(
                    ManualRowGroup(
                        row_coordinates=((second_table.table_id, 1),)
                    ),
                ),
            )

    def test_vertical_merge_and_table_notes_follow_affected_rows(self) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("序号", "共同条件", "要求"),
                        ("A-01", "适用于两行", "第一条"),
                        ("A-02", None, "第二条"),
                    ),
                    header_rows=(0,),
                    merged_regions=(
                        MergedRegion(row=1, column=1, row_span=2),
                    ),
                    notes=("所有数值均为标称值。",),
                ),
            )
        )
        structure = build_structure_tree(source)

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(len(projection.records), 2)
        self.assertTrue(
            all(len(record.shared_merged_values) == 1 for record in projection.records)
        )
        self.assertTrue(all(len(record.notes) == 1 for record in projection.records))
        self.assertEqual(
            projection.records[0].shared_merged_values[0].text,
            "适用于两行",
        )

    def test_spanning_cell_image_is_bound_once_without_losing_covered_cells(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(("序号", "证据"), ("A-01", "")),
                    header_rows=(0,),
                ),
            )
        )
        table_source = source.nodes[0]
        image_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            table_index=0,
            row_index=1,
            column_index=1,
            bbox=(100.0, 100.0, 130.0, 130.0),
        )
        asset = SourceAssetRef(
            asset_id="asset-cell-image",
            kind=SourceAssetKind.IMAGE,
            content_sha256="b" * 64,
            media_type="image/png",
            storage_key="tables/cell-image.png",
            native_locator=image_locator,
        )
        image = SourceNode(
            source_node_id=stable_node_id(
                source_sha256=SOURCE_SHA256,
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                kind=SourceNodeKind.IMAGE,
                native_locator=image_locator,
            ),
            kind=SourceNodeKind.IMAGE,
            source_order=1,
            native_locator=image_locator,
            parent_source_node_id=table_source.source_node_id,
            asset_ref=asset.asset_id,
        )
        source = SourceIRSnapshot.model_validate(
            {
                **source.model_dump(mode="python"),
                "nodes": (
                    *source.nodes,
                    image,
                ),
                "assets": (asset,),
            }
        )
        structure = build_structure_tree(source)
        image_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id=image.source_node_id,
        )
        table = next(
            node for node in structure.nodes if node.kind.value == "table"
        )
        row = next(
            node
            for node in structure.nodes
            if node.kind.value == "table_row"
            and node.table_row_index == 1
        )
        row_cells = tuple(
            cell
            for cell in table_source.table_payload.cells
            if cell.row == 1
        )
        structure = structure.model_copy(
            update={
                "relations": (
                    *structure.relations,
                    StructureRelation(
                        relation_id="relation-spanning-cell-image",
                        kind=StructureRelationKind.PLACED_OVER_CELLS,
                        source_structure_node_id=row.structure_node_id,
                        target_structure_node_id=table.structure_node_id,
                        source_refs=(
                            image_ref,
                            *tuple(
                                StructureSourceRef(
                                    kind=StructureSourceRefKind.TABLE_CELL,
                                    source_id=cell.cell_id,
                                )
                                for cell in row_cells
                            ),
                        ),
                    ),
                )
            }
        )

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(len(projection.records[0].image_refs), 1)
        self.assertEqual(projection.records[0].image_refs[0].asset_id, asset.asset_id)
        self.assertEqual(
            {
                ref.source_id
                for ref in projection.records[0].image_refs[0].source_refs
                if ref.kind is StructureSourceRefKind.TABLE_CELL
            },
            {cell.cell_id for cell in row_cells},
        )
        self.assertIn(
            asset.asset_id,
            projection.table_fact.table_parts[0].visual_asset_refs,
        )

    def test_automatic_records_fail_closed_when_visual_placement_is_unconsumed(
        self,
    ) -> None:
        source = _source_with_tables(
            (
                _table_spec(
                    rows=(
                        ("序号", "证据"),
                        ("A-01", "第一条"),
                        ("A-02", "第二条"),
                        ("", ""),
                    ),
                    header_rows=(0,),
                ),
            )
        )
        table_source = source.nodes[0]
        table_payload = table_source.table_payload
        assert table_payload is not None
        image_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            table_index=0,
            row_index=3,
            column_index=1,
            bbox=(100.0, 100.0, 130.0, 130.0),
        )
        asset = SourceAssetRef(
            asset_id="asset-unconsumed-cell-image",
            kind=SourceAssetKind.IMAGE,
            content_sha256="c" * 64,
            media_type="image/png",
            storage_key="tables/unconsumed-cell-image.png",
            native_locator=image_locator,
        )
        image = SourceNode(
            source_node_id=stable_node_id(
                source_sha256=SOURCE_SHA256,
                adapter_id=ADAPTER_ID,
                adapter_version=ADAPTER_VERSION,
                kind=SourceNodeKind.IMAGE,
                native_locator=image_locator,
            ),
            kind=SourceNodeKind.IMAGE,
            source_order=1,
            native_locator=image_locator,
            parent_source_node_id=table_source.source_node_id,
            asset_ref=asset.asset_id,
        )
        source = SourceIRSnapshot.model_validate(
            {
                **source.model_dump(mode="python"),
                "nodes": (*source.nodes, image),
                "assets": (asset,),
            }
        )
        structure = build_structure_tree(source)
        table = next(
            node for node in structure.nodes if node.kind is StructureNodeKind.TABLE
        )
        row = next(
            node
            for node in structure.nodes
            if node.kind is StructureNodeKind.TABLE_ROW
            and node.table_row_index == 3
        )
        image_cell = next(
            cell
            for cell in table_payload.cells
            if cell.row == 3 and cell.column == 1
        )
        structure = structure.model_copy(
            update={
                "relations": (
                    *structure.relations,
                    StructureRelation(
                        relation_id="relation-unconsumed-cell-image",
                        kind=StructureRelationKind.PLACED_IN_CELL,
                        source_structure_node_id=row.structure_node_id,
                        target_structure_node_id=table.structure_node_id,
                        source_refs=(
                            StructureSourceRef(
                                kind=StructureSourceRefKind.SOURCE_NODE,
                                source_id=image.source_node_id,
                            ),
                            StructureSourceRef(
                                kind=StructureSourceRefKind.TABLE_CELL,
                                source_id=image_cell.cell_id,
                            ),
                        ),
                    ),
                )
            }
        )

        projection = build_table_record_projections(source, structure)[0]

        self.assertEqual(projection.mode, TableRecordProjectionMode.WHOLE_TABLE)
        self.assertEqual(projection.records, ())
        self.assertEqual(
            projection.decision.reason_code,
            "AUTO_INCOMPLETE_FACT_COVERAGE_KEPT_WHOLE",
        )


def _ensure_first_table_continuation(structure):
    table_nodes = tuple(
        sorted(
            (
                node
                for node in structure.nodes
                if node.kind.value == "table"
            ),
            key=lambda node: (node.source_order, node.structure_node_id),
        )
    )
    if len(table_nodes) < 2:
        raise AssertionError("fixture requires two table parts")
    if any(
        relation.kind is StructureRelationKind.CONTINUES
        and relation.source_structure_node_id
        == table_nodes[0].structure_node_id
        and relation.target_structure_node_id
        == table_nodes[1].structure_node_id
        for relation in structure.relations
    ):
        return structure
    return structure.model_copy(
        update={
            "relations": (
                *structure.relations,
                StructureRelation(
                    relation_id="relation-fixture-table-continuation",
                    kind=StructureRelationKind.CONTINUES,
                    source_structure_node_id=table_nodes[0].structure_node_id,
                    target_structure_node_id=table_nodes[1].structure_node_id,
                    source_refs=table_nodes[1].source_refs,
                ),
            )
        }
    )


def _source_with_tables(
    specs: tuple[dict[str, object], ...],
) -> SourceIRSnapshot:
    document_id = stable_document_id(SOURCE_SHA256)
    nodes: list[SourceNode] = []
    for source_order, spec in enumerate(specs):
        page_number = int(spec["page_number"])
        rows = spec["rows"]
        assert isinstance(rows, tuple)
        table_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            table_index=0,
        )
        table_id = stable_table_id(
            source_sha256=SOURCE_SHA256,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            native_locator=table_locator,
        )
        row_count = len(rows)
        column_count = max(len(row) for row in rows)
        merged_regions = spec["merged_regions"]
        assert isinstance(merged_regions, tuple)
        merged_by_anchor = {
            (region.row, region.column): region for region in merged_regions
        }
        covered_non_anchors = {
            (row_index, column_index)
            for region in merged_regions
            for row_index in range(region.row, region.row + region.row_span)
            for column_index in range(
                region.column,
                region.column + region.column_span,
            )
            if (row_index, column_index) != (region.row, region.column)
        }
        cells: list[TableCellIR] = []
        for row_index, row in enumerate(rows):
            for column_index in range(column_count):
                if (row_index, column_index) in covered_non_anchors:
                    continue
                region = merged_by_anchor.get((row_index, column_index))
                value = row[column_index] if column_index < len(row) else ""
                cells.append(
                    TableCellIR(
                        cell_id=stable_cell_id(
                            table_id,
                            row_index,
                            column_index,
                        ),
                        row=row_index,
                        column=column_index,
                        row_span=region.row_span if region is not None else 1,
                        column_span=region.column_span if region is not None else 1,
                        raw_text="" if value is None else str(value),
                        native_locator=NativeLocator(
                            format=NativeDocumentFormat.PDF,
                            page_number=page_number,
                            table_index=0,
                            row_index=row_index,
                            column_index=column_index,
                        ),
                    )
                )
        header_rows = spec["header_rows"]
        assert isinstance(header_rows, tuple)
        header_regions = tuple(
            MergedRegion(row=row_index, column=column_index)
            for row_index in header_rows
            for column_index in range(column_count)
        )
        notes = spec["notes"]
        assert isinstance(notes, tuple)
        table = TableIR(
            table_id=table_id,
            row_count=row_count,
            column_count=column_count,
            header_regions=header_regions,
            cells=tuple(cells),
            notes=notes,
            merged_regions=merged_regions,
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=SOURCE_SHA256,
                    adapter_id=ADAPTER_ID,
                    adapter_version=ADAPTER_VERSION,
                    kind=SourceNodeKind.TABLE,
                    native_locator=table_locator,
                ),
                kind=SourceNodeKind.TABLE,
                source_order=source_order,
                native_locator=table_locator,
                table_payload=table,
            )
        )
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_SHA256,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
        ),
        document_id=document_id,
        source_sha256=SOURCE_SHA256,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(nodes),
        created_at=datetime.now(timezone.utc),
    )


def _table_spec(
    *,
    rows: tuple[tuple[str | None, ...], ...],
    header_rows: tuple[int, ...] = (),
    merged_regions: tuple[MergedRegion, ...] = (),
    notes: tuple[str, ...] = (),
    page_number: int = 1,
) -> dict[str, object]:
    return {
        "rows": rows,
        "header_rows": header_rows,
        "merged_regions": merged_regions,
        "notes": notes,
        "page_number": page_number,
    }


def _cell_value(table: TableIR, row_index: int, column_index: int) -> str:
    cell = next(
        item
        for item in table.cells
        if item.row <= row_index < item.row + item.row_span
        and item.column <= column_index < item.column + item.column_span
    )
    return cell.raw_text


if __name__ == "__main__":
    unittest.main()
