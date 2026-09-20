from __future__ import annotations

# Test runtime isolation must be active before application imports.

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import unittest

from pydantic import ValidationError

from app.application.agent_workflow.source_requirement_baseline import (
    ContextRowRole,
    NonRequirementRole,
    RequirementKind,
    Requirementness,
    SourceItemStructureOnlySpanV1,
    SourceItemScopeRevisionOperation,
    SourceItemScopeRevisionReceiptV1,
    SourceItemScopeRevisionV1,
    SourceItemTextBoundaryCandidateV1,
    SourceItemTextSliceCandidateV1,
    SourceItemTextSpanV1,
    SourceRecordForm,
    SourceRequirementBaselineArtifactV1,
    SourceRequirementBaselineError,
    SourceRequirementBoundaryCandidateV1,
    SourceRequirementClassificationCoordinationDecisionV1,
    SourceRequirementContextRowGroupV1,
    SourceRequirementRowGroupV1,
    SourceRequirementSliceCandidateV1,
    append_source_requirement_baseline,
    build_source_item_scope_revision,
    build_source_item_text_scope_request,
    build_source_requirement_scope_request,
    compile_document_map_artifact,
    compile_source_item_scope_revision,
    compile_source_requirement_baseline,
    compile_source_requirement_classification_coordination,
    canonical_json_sha256,
    grow_source_requirement_baseline,
    validate_source_item_text_slice_candidate,
    validate_source_requirement_slice_candidate,
)


class SourceRequirementBaselineTests(unittest.TestCase):
    def test_growth_inserts_earlier_scope_without_changing_stable_record_fields(
        self,
    ) -> None:
        dvp_candidate = _candidate(
            self.dvp_request,
            records=(
                _record(
                    "dvp-1",
                    "1 外观检查",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-one", row_start=1, row_end=2
                        ),
                    ),
                ),
                _record(
                    "dvp-2",
                    "2 跨页试验",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-one", row_start=2, row_end=4
                        ),
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-two", row_start=0, row_end=1
                        ),
                    ),
                ),
                _record(
                    "dvp-3",
                    "3 耐久试验",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-two", row_start=1, row_end=2
                        ),
                    ),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="dvp-one", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )
        previous, previous_validation = compile_source_requirement_baseline(
            document_map=self.document_map,
            requests=(self.dvp_request,),
            candidates=(dvp_candidate,),
        )
        bom_candidate = _candidate(
            self.bom_request,
            records=(
                _record(
                    "rear-bom",
                    "后减振器 BOM",
                    SourceRecordForm.TABLE_BRANCH,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="bom-fragment", row_start=1, row_end=3
                        ),
                    ),
                ),
                _record(
                    "front-bom",
                    "前减振器 BOM",
                    SourceRecordForm.TABLE_BRANCH,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="bom-fragment", row_start=3, row_end=5
                        ),
                    ),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="bom-fragment", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )

        grown, validation, receipt = grow_source_requirement_baseline(
            document_map=self.document_map,
            previous_artifact=previous,
            previous_validation_receipt=previous_validation,
            requests=(self.bom_request,),
            candidates=(bom_candidate,),
        )

        self.assertEqual(grown.revision, 2)
        self.assertEqual(
            [item.label for item in grown.records],
            [
                "后减振器 BOM",
                "前减振器 BOM",
                "1 外观检查",
                "2 跨页试验",
                "3 耐久试验",
            ],
        )
        self.assertEqual(
            [item.source_display_id for item in grown.records],
            ["SRC-0004", "SRC-0005", "SRC-0001", "SRC-0002", "SRC-0003"],
        )
        grown_by_id = {item.source_record_id: item for item in grown.records}
        for original in previous.records:
            successor = grown_by_id[original.source_record_id]
            self.assertEqual(
                original.model_dump(mode="json", exclude={"source_order"}),
                successor.model_dump(mode="json", exclude={"source_order"}),
            )
            self.assertEqual(successor.source_order, original.source_order + 2)
        self.assertEqual(validation.record_count, 5)
        self.assertEqual(receipt.growth_mode, "SOURCE_ORDER_INSERT")
        self.assertEqual(receipt.preserved_record_count, 3)
        self.assertEqual(receipt.added_record_count, 2)
        self.assertEqual(receipt.added_scope_ids, ("lt-bom",))
        self.assertEqual(
            receipt.predecessor_stable_records_sha256,
            receipt.successor_stable_records_sha256,
        )

    def test_append_scope_preserves_coordinated_predecessor_records_exactly(
        self,
    ) -> None:
        bom_candidate = _candidate(
            self.bom_request,
            records=(
                _record(
                    "rear-bom",
                    "后减振器 BOM",
                    SourceRecordForm.TABLE_BRANCH,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="bom-fragment", row_start=1, row_end=3
                        ),
                    ),
                ),
                _record(
                    "front-bom",
                    "前减振器 BOM",
                    SourceRecordForm.TABLE_BRANCH,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="bom-fragment", row_start=3, row_end=5
                        ),
                    ),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="bom-fragment", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )
        baseline, validation = compile_source_requirement_baseline(
            document_map=self.document_map,
            requests=(self.bom_request,),
            candidates=(bom_candidate,),
        )
        coordinated, coordinated_validation, _, _ = (
            compile_source_requirement_classification_coordination(
                previous_artifact=baseline,
                previous_validation_receipt=validation,
                decisions=(
                    SourceRequirementClassificationCoordinationDecisionV1(
                        source_record_id=baseline.records[0].source_record_id,
                        requirement_kinds=(
                            RequirementKind.FUNCTIONAL,
                            RequirementKind.QUALITY,
                        ),
                        confidence=0.99,
                        decision_summary="专业协调后的分类必须原样保留。",
                        reason_codes=("professional_coordination",),
                    ),
                ),
                model_id="gpt-5.6-sol",
            )
        )
        dvp_candidate = _candidate(
            self.dvp_request,
            records=(
                _record(
                    "dvp-1",
                    "1 外观检查",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-one", row_start=1, row_end=2
                        ),
                    ),
                ),
                _record(
                    "dvp-2",
                    "2 跨页试验",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-one", row_start=2, row_end=4
                        ),
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-two", row_start=0, row_end=1
                        ),
                    ),
                ),
                _record(
                    "dvp-3",
                    "3 耐久试验",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-two", row_start=1, row_end=2
                        ),
                    ),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="dvp-one", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )

        appended, appended_validation, append_receipt = (
            append_source_requirement_baseline(
                document_map=self.document_map,
                previous_artifact=coordinated,
                previous_validation_receipt=coordinated_validation,
                requests=(self.dvp_request,),
                candidates=(dvp_candidate,),
            )
        )

        self.assertEqual(appended.revision, coordinated.revision + 1)
        self.assertEqual(appended.records[:2], coordinated.records)
        self.assertEqual(
            [item.source_display_id for item in appended.records[2:]],
            ["SRC-0003", "SRC-0004", "SRC-0005"],
        )
        self.assertEqual(
            appended.records[0].requirement_kinds,
            (
                RequirementKind.FUNCTIONAL,
                RequirementKind.QUALITY,
            ),
        )
        self.assertEqual(appended_validation.record_count, 5)
        self.assertEqual(append_receipt.preserved_record_count, 2)
        self.assertEqual(append_receipt.added_record_count, 3)
        self.assertEqual(append_receipt.added_scope_ids, ("lt-dvp",))
        self.assertEqual(
            append_receipt.predecessor_coverage_sha256,
            coordinated_validation.coverage_sha256,
        )

    def setUp(self) -> None:
        self.public_reference = _public_reference()
        self.document_map = compile_document_map_artifact(self.public_reference)
        self.bom_request = build_source_requirement_scope_request(
            self.document_map,
            _table_preview(
                scope_id="lt-bom",
                title="产品信息",
                fragments=(
                    _fragment(
                        "bom-fragment",
                        "T0001",
                        page=2,
                        rows=(
                            ("序号", "名称"),
                            ("1", "后减振器总成"),
                            ("2", "后减振器子件"),
                            ("", "前减振器"),
                            ("1", "前减振器总成"),
                        ),
                    ),
                ),
            ),
        )
        self.dvp_request = build_source_requirement_scope_request(
            self.document_map,
            _table_preview(
                scope_id="lt-dvp",
                title="主动前减振器试验大纲",
                fragments=(
                    _fragment(
                        "dvp-one",
                        "T0002",
                        page=10,
                        rows=(
                            ("试验编号", "试验名称"),
                            ("1", "外观检查"),
                            ("2", "跨页试验"),
                            ("", "跨页试验子行"),
                        ),
                    ),
                    _fragment(
                        "dvp-two",
                        "T0003",
                        page=11,
                        rows=(
                            ("", "跨页试验续行"),
                            ("3", "耐久试验"),
                        ),
                    ),
                ),
            ),
        )

    def test_document_map_promotes_native_tree_without_a_second_hierarchy(self) -> None:
        self.assertEqual(self.document_map.source_sha256, "a" * 64)
        self.assertEqual(len(self.document_map.nodes), 3)
        self.assertEqual(
            self.document_map.nodes[2].ancestor_path,
            ("客户 PDS",),
        )
        self.assertEqual(
            {item.scope_id for item in self.document_map.logical_tables},
            {"lt-bom", "lt-dvp"},
        )
        replay = compile_document_map_artifact(self.public_reference)
        self.assertEqual(replay, self.document_map)

    def test_compiler_keeps_bom_branches_and_cross_page_dvp_records(self) -> None:
        bom = _candidate(
            self.bom_request,
            records=(
                _record(
                    "rear-damper-bom",
                    "后减振器 BOM",
                    SourceRecordForm.TABLE_BRANCH,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="bom-fragment", row_start=1, row_end=3
                        ),
                    ),
                    shared=("bom-context",),
                ),
                _record(
                    "front-damper-bom",
                    "前减振器 BOM",
                    SourceRecordForm.TABLE_BRANCH,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="bom-fragment", row_start=3, row_end=5
                        ),
                    ),
                    shared=("bom-context",),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="bom-fragment", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )
        dvp = _candidate(
            self.dvp_request,
            records=(
                _record(
                    "dvp-1-visual-inspection",
                    "1 外观检查",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-one", row_start=1, row_end=2
                        ),
                    ),
                ),
                _record(
                    "dvp-2-cross-page-test",
                    "2 跨页试验",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-one", row_start=2, row_end=4
                        ),
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-two", row_start=0, row_end=1
                        ),
                    ),
                ),
                _record(
                    "dvp-3-endurance",
                    "3 耐久试验",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-two", row_start=1, row_end=2
                        ),
                    ),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="dvp-one", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )
        artifact, receipt = compile_source_requirement_baseline(
            document_map=self.document_map,
            requests=(self.bom_request, self.dvp_request),
            candidates=(bom, dvp),
        )
        self.assertEqual(len(artifact.records), 5)
        self.assertEqual(
            [item.source_display_id for item in artifact.records],
            ["SRC-0001", "SRC-0002", "SRC-0003", "SRC-0004", "SRC-0005"],
        )
        self.assertTrue(all(item.requirement_display_id for item in artifact.records))
        cross_page = next(
            item for item in artifact.records if item.label == "2 跨页试验"
        )
        self.assertEqual(cross_page.pages, (10, 11))
        self.assertEqual(len(cross_page.table_fragments), 2)
        self.assertEqual(receipt.record_count, 5)
        self.assertEqual(receipt.requirement_count, 5)
        self.assertGreater(receipt.owned_cell_count, receipt.context_cell_count)
        replay, replay_receipt = compile_source_requirement_baseline(
            document_map=self.document_map,
            requests=(self.bom_request, self.dvp_request),
            candidates=(bom, dvp),
        )
        self.assertEqual(replay, artifact)
        self.assertEqual(replay_receipt, receipt)

    def test_record_inherits_only_the_nearest_contiguous_header_band(self) -> None:
        request = build_source_requirement_scope_request(
            self.document_map,
            _table_preview(
                scope_id="lt-bom",
                title="连接器评估表",
                fragments=(
                    _fragment(
                        "connector-fragment",
                        "T0001",
                        page=2,
                        rows=(
                            ("总表标题", ""),
                            ("上一分区", ""),
                            ("旧项目", "旧要求"),
                            ("旧条目", "旧内容"),
                            ("用电器端基本信息", ""),
                            ("项目", "要求"),
                            ("板端型号", "提供型号"),
                            ("端子信息", "提供型号"),
                        ),
                    ),
                ),
            ),
        )
        candidate = _candidate(
            request,
            records=(
                _record(
                    "old-record",
                    "旧条目",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="connector-fragment",
                            row_start=3,
                            row_end=4,
                        ),
                    ),
                ),
                _record(
                    "model-record",
                    "板端型号",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="connector-fragment",
                            row_start=6,
                            row_end=7,
                        ),
                    ),
                ),
                _record(
                    "terminal-record",
                    "端子信息",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="connector-fragment",
                            row_start=7,
                            row_end=8,
                        ),
                    ),
                ),
            ),
            context=tuple(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="connector-fragment",
                        row_start=row_start,
                        row_end=row_end,
                    ),
                    reason_codes=(reason,),
                )
                for row_start, row_end, reason in (
                    (0, 1, "table_title"),
                    (1, 2, "section_header"),
                    (2, 3, "column_header"),
                    (4, 5, "section_header"),
                    (5, 6, "column_header"),
                )
            ),
        )

        artifact, _ = compile_source_requirement_baseline(
            document_map=self.document_map,
            requests=(request,),
            candidates=(candidate,),
        )
        terminal = next(item for item in artifact.records if item.label == "端子信息")

        self.assertEqual(
            terminal.shared_header_cell_ids,
            (
                "connector-fragment-cell-4-0",
                "connector-fragment-cell-4-1",
                "connector-fragment-cell-5-0",
                "connector-fragment-cell-5-1",
            ),
        )
        self.assertNotIn(
            "connector-fragment-cell-0-0",
            terminal.shared_header_cell_ids,
        )

    def test_host_rejects_missing_rows_without_deciding_semantics(self) -> None:
        candidate = _candidate(
            self.bom_request,
            records=(
                _record(
                    "rear-damper-bom",
                    "后减振器 BOM",
                    SourceRecordForm.TABLE_BRANCH,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="bom-fragment", row_start=1, row_end=3
                        ),
                    ),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="bom-fragment", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )
        with self.assertRaisesRegex(
            SourceRequirementBaselineError,
            "SOURCE_REQUIREMENT_ROW_COVERAGE_INCOMPLETE",
        ):
            validate_source_requirement_slice_candidate(self.bom_request, candidate)

    def test_slice_extension_preserves_existing_ids_and_appends_new_ordinals(
        self,
    ) -> None:
        dvp = _candidate(
            self.dvp_request,
            records=(
                _record(
                    "dvp-1-visual-inspection",
                    "1 外观检查",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-one", row_start=1, row_end=2
                        ),
                    ),
                ),
                _record(
                    "dvp-2-cross-page-test",
                    "2 跨页试验",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-one", row_start=2, row_end=4
                        ),
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-two", row_start=0, row_end=1
                        ),
                    ),
                ),
                _record(
                    "dvp-3-endurance",
                    "3 耐久试验",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-two", row_start=1, row_end=2
                        ),
                    ),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="dvp-one", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )
        initial, _ = compile_source_requirement_baseline(
            document_map=self.document_map,
            requests=(self.dvp_request,),
            candidates=(dvp,),
        )
        initial_ids = {
            item.source_record_id: (
                item.source_display_id,
                item.requirement_display_id,
            )
            for item in initial.records
        }
        bom = _candidate(
            self.bom_request,
            records=(
                _record(
                    "rear-damper-bom",
                    "后减振器 BOM",
                    SourceRecordForm.TABLE_BRANCH,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="bom-fragment", row_start=1, row_end=3
                        ),
                    ),
                    shared=("bom-context",),
                ),
                _record(
                    "front-damper-bom",
                    "前减振器 BOM",
                    SourceRecordForm.TABLE_BRANCH,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="bom-fragment", row_start=3, row_end=5
                        ),
                    ),
                    shared=("bom-context",),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="bom-fragment", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )

        extended, _ = compile_source_requirement_baseline(
            document_map=self.document_map,
            requests=(self.bom_request, self.dvp_request),
            candidates=(bom, dvp),
            previous_artifact=initial,
        )

        self.assertEqual(extended.revision, 2)
        self.assertEqual(extended.predecessor_artifact_id, initial.artifact_id)
        self.assertEqual(
            extended.predecessor_artifact_sha256,
            initial.artifact_sha256,
        )
        for item in extended.records:
            if item.source_record_id in initial_ids:
                self.assertEqual(
                    (
                        item.source_display_id,
                        item.requirement_display_id,
                    ),
                    initial_ids[item.source_record_id],
                )
        self.assertEqual(
            [
                item.source_display_id
                for item in extended.records
                if item.source_form is SourceRecordForm.TABLE_BRANCH
            ],
            ["SRC-0004", "SRC-0005"],
        )
        self.assertEqual(
            [item.source_display_id for item in extended.records],
            ["SRC-0004", "SRC-0005", "SRC-0001", "SRC-0002", "SRC-0003"],
        )
        replay, _ = compile_source_requirement_baseline(
            document_map=self.document_map,
            requests=(self.bom_request, self.dvp_request),
            candidates=(bom, dvp),
            previous_artifact=initial,
        )
        self.assertEqual(replay, extended)

    def test_geometry_proven_merge_can_be_shared_across_records(self) -> None:
        fragment = _fragment(
            "shared-fragment",
            "T0001",
            page=2,
            rows=(("序号", "名称"), ("1", "记录一"), ("2", "记录二")),
        )
        fragment["column_count"] = 3
        fragment["cells"].append(
            {
                "cell_id": "shared-remark",
                "row": 1,
                "column": 2,
                "row_span": 2,
                "column_span": 1,
                "text": "共同备注",
                "bbox": [100.0, 20.0, 150.0, 60.0],
            }
        )
        preview = _table_preview(
            scope_id="lt-bom",
            title="产品信息",
            fragments=(fragment,),
        )
        preview["summary"]["cell_count"] += 1
        request = build_source_requirement_scope_request(self.document_map, preview)
        candidate = _candidate(
            request,
            records=(
                _record(
                    "record-one",
                    "记录一",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="shared-fragment", row_start=1, row_end=2
                        ),
                    ),
                ),
                _record(
                    "record-two",
                    "记录二",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="shared-fragment", row_start=2, row_end=3
                        ),
                    ),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="shared-fragment", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )
        artifact, receipt = compile_source_requirement_baseline(
            document_map=self.document_map,
            requests=(request,),
            candidates=(candidate,),
        )
        self.assertEqual(receipt.shared_cell_count, 1)
        self.assertEqual(
            [item.shared_table_cell_ids for item in artifact.records],
            [("shared-remark",), ("shared-remark",)],
        )

    def test_classification_coordination_preserves_all_source_evidence_and_ids(
        self,
    ) -> None:
        candidate = _candidate(
            self.dvp_request,
            records=(
                _record(
                    "dvp-1-visual-inspection",
                    "1 外观检查",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-one", row_start=1, row_end=2
                        ),
                    ),
                ),
                _record(
                    "dvp-2-cross-page-test",
                    "2 跨页试验",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-one", row_start=2, row_end=4
                        ),
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-two", row_start=0, row_end=1
                        ),
                    ),
                ),
                _record(
                    "dvp-3-endurance",
                    "3 耐久试验",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-two", row_start=1, row_end=2
                        ),
                    ),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="dvp-one", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )
        baseline, validation = compile_source_requirement_baseline(
            document_map=self.document_map,
            requests=(self.dvp_request,),
            candidates=(candidate,),
        )
        target = baseline.records[0]

        coordinated, coordinated_validation, decision, receipt = (
            compile_source_requirement_classification_coordination(
                previous_artifact=baseline,
                previous_validation_receipt=validation,
                decisions=(
                    SourceRequirementClassificationCoordinationDecisionV1(
                        source_record_id=target.source_record_id,
                        requirement_kinds=(
                            RequirementKind.FUNCTIONAL,
                            RequirementKind.QUALITY,
                        ),
                        confidence=0.97,
                        decision_summary="跨范围协调后统一为功能与质量要求。",
                        reason_codes=(
                            "cross_scope_professional_coordination",
                            "functional_performance_requirement",
                        ),
                    ),
                ),
                model_id="gpt-5.6-sol",
            )
        )

        self.assertEqual(coordinated.revision, 2)
        self.assertEqual(coordinated.predecessor_artifact_id, baseline.artifact_id)
        self.assertEqual(
            [item.source_record_id for item in coordinated.records],
            [item.source_record_id for item in baseline.records],
        )
        self.assertEqual(
            [item.source_display_id for item in coordinated.records],
            [item.source_display_id for item in baseline.records],
        )
        self.assertEqual(
            coordinated.records[0].table_fragments,
            baseline.records[0].table_fragments,
        )
        self.assertEqual(
            coordinated.records[0].requirement_kinds,
            (RequirementKind.FUNCTIONAL, RequirementKind.QUALITY),
        )
        self.assertEqual(
            coordinated_validation.coverage_sha256,
            validation.coverage_sha256,
        )
        self.assertEqual(decision.predecessor_artifact_id, baseline.artifact_id)
        self.assertEqual(receipt.artifact_id, coordinated.artifact_id)
        self.assertEqual(receipt.decision_count, 1)

    def test_classification_coordination_can_correct_a_and_f_then_restore_req_id(
        self,
    ) -> None:
        candidate = _candidate(
            self.dvp_request,
            records=(
                _record(
                    "risk-status",
                    "是否三新件：是",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-one", row_start=1, row_end=4
                        ),
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-two", row_start=0, row_end=2
                        ),
                    ),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="dvp-one", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )
        baseline, validation = compile_source_requirement_baseline(
            document_map=self.document_map,
            requests=(self.dvp_request,),
            candidates=(candidate,),
        )
        original = baseline.records[0]
        corrected, corrected_validation, _, _ = (
            compile_source_requirement_classification_coordination(
                previous_artifact=baseline,
                previous_validation_receipt=validation,
                decisions=(
                    SourceRequirementClassificationCoordinationDecisionV1(
                        source_record_id=original.source_record_id,
                        requirementness=Requirementness.NON_REQUIREMENT,
                        requirement_kinds=(),
                        non_requirement_role=NonRequirementRole.CONTEXT,
                        confidence=0.99,
                        decision_summary="该勾选项记录项目风险状态，不施加义务。",
                        reason_codes=("project_risk_context",),
                    ),
                ),
                model_id="gpt-5.6-sol",
            )
        )
        corrected_record = corrected.records[0]
        self.assertEqual(corrected_record.source_record_id, original.source_record_id)
        self.assertEqual(corrected_record.source_display_id, original.source_display_id)
        self.assertIsNone(corrected_record.requirement_display_id)
        self.assertEqual(
            corrected_record.non_requirement_role,
            NonRequirementRole.CONTEXT,
        )
        self.assertEqual(corrected_validation.requirement_count, 0)
        self.assertEqual(corrected_validation.non_requirement_count, 1)
        self.assertEqual(
            corrected_validation.coverage_sha256, validation.coverage_sha256
        )

        restored, restored_validation, _, _ = (
            compile_source_requirement_classification_coordination(
                previous_artifact=corrected,
                previous_validation_receipt=corrected_validation,
                decisions=(
                    SourceRequirementClassificationCoordinationDecisionV1(
                        source_record_id=original.source_record_id,
                        requirementness=Requirementness.REQUIREMENT,
                        requirement_kinds=(RequirementKind.CONSTRAINT,),
                        non_requirement_role=None,
                        confidence=0.98,
                        decision_summary="后续证据表明该项确实施加工程约束。",
                        reason_codes=("professional_reclassification",),
                    ),
                ),
                model_id="gpt-5.6-sol",
            )
        )
        self.assertEqual(
            restored.records[0].requirement_display_id,
            original.requirement_display_id,
        )
        self.assertEqual(restored_validation.requirement_count, 1)
        self.assertEqual(restored_validation.non_requirement_count, 0)

    def test_abf_contract_has_no_other_or_unresolved_escape_hatch(self) -> None:
        with self.assertRaises(ValidationError):
            SourceRequirementBoundaryCandidateV1(
                record_key="invalid",
                label="Invalid",
                source_form=SourceRecordForm.TABLE_RECORD,
                row_groups=(
                    SourceRequirementRowGroupV1(
                        fragment_id="bom-fragment", row_start=1, row_end=2
                    ),
                ),
                requirementness=Requirementness.REQUIREMENT,
                requirement_kinds=(),
                non_requirement_role=None,
                confidence=0.9,
                decision_summary="缺少 B。",
                reason_codes=("invalid_abf",),
            )

    def test_compiles_one_to_one_text_clause_with_exact_span(self) -> None:
        text = "1.5.1 交流过程中及最终交付的所有文件只接受简体中文文件。"
        document_map = compile_document_map_artifact(
            _reference_with_text_node(
                node_id="clause-1.5.1",
                label="1.5.1",
                title="交流过程中及最终交付的所有文件只接受简体中文文件。",
                contents=(("clause-content", text, True),),
            )
        )
        request = build_source_item_text_scope_request(
            document_map,
            node_id="clause-1.5.1",
        )
        candidate = SourceItemTextSliceCandidateV1(
            source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id,
            document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256,
            scope_id=request.scope_id,
            items=(
                SourceItemTextBoundaryCandidateV1(
                    item_key="simplified-chinese-deliverables",
                    label="1.5.1 简体中文交付文件",
                    source_form=SourceRecordForm.NUMBERED_CLAUSE,
                    text_spans=(
                        SourceItemTextSpanV1(
                            content_id="clause-content",
                            start_offset=0,
                            end_offset=len(text),
                        ),
                    ),
                    requirementness=Requirementness.REQUIREMENT,
                    requirement_kinds=(RequirementKind.CONSTRAINT,),
                    non_requirement_role=None,
                    confidence=0.98,
                    decision_summary="完整编号条款表达交付语言约束。",
                    reason_codes=("normative_delivery_constraint",),
                ),
            ),
        )
        artifact, receipt = compile_source_requirement_baseline(
            document_map=document_map,
            requests=(),
            candidates=(),
            text_requests=(request,),
            text_candidates=(candidate,),
        )
        self.assertEqual(len(artifact.records), 1)
        self.assertEqual(artifact.records[0].text_bindings[0].text, text)
        self.assertEqual(artifact.records[0].table_fragments, ())
        self.assertEqual(artifact.records[0].logical_table_scope_id, "")
        self.assertEqual(receipt.owned_cell_count, 0)
        self.assertGreater(receipt.owned_text_character_count, 0)

    def test_preserves_pure_title_as_structure_and_metadata_as_source_item(
        self,
    ) -> None:
        title = "主动前减振器试验大纲"
        metadata = "项目：YX2516\n部门：底盘设计部\n版本：1.0"
        document_map = compile_document_map_artifact(
            _reference_with_text_node(
                node_id="front-outline-meta",
                label="",
                title=title,
                contents=(
                    ("outline-title", title, True),
                    ("outline-metadata", metadata, False),
                ),
            )
        )
        request = build_source_item_text_scope_request(
            document_map,
            node_id="front-outline-meta",
        )
        candidate = SourceItemTextSliceCandidateV1(
            source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id,
            document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256,
            scope_id=request.scope_id,
            items=(
                SourceItemTextBoundaryCandidateV1(
                    item_key="front-outline-document-control",
                    label="项目、部门与版本信息",
                    source_form=SourceRecordForm.NATIVE_LIST,
                    text_spans=(
                        SourceItemTextSpanV1(
                            content_id="outline-metadata",
                            start_offset=0,
                            end_offset=len(metadata),
                        ),
                    ),
                    requirementness=Requirementness.NON_REQUIREMENT,
                    requirement_kinds=(),
                    non_requirement_role=NonRequirementRole.METADATA,
                    confidence=0.99,
                    decision_summary="该内容用于文档与项目识别，不表达产品义务。",
                    reason_codes=("document_control_metadata",),
                ),
            ),
            structure_only_spans=(
                SourceItemStructureOnlySpanV1(
                    text_span=SourceItemTextSpanV1(
                        content_id="outline-title",
                        start_offset=0,
                        end_offset=len(title),
                    ),
                    reason_codes=("source_hierarchy_title",),
                ),
            ),
        )
        validate_source_item_text_slice_candidate(request, candidate)
        artifact, receipt = compile_source_requirement_baseline(
            document_map=document_map,
            requests=(),
            candidates=(),
            text_requests=(request,),
            text_candidates=(candidate,),
        )
        self.assertEqual(artifact.records[0].requirement_display_id, None)
        self.assertEqual(
            artifact.records[0].non_requirement_role,
            NonRequirementRole.METADATA,
        )
        self.assertEqual(len(artifact.text_context_dispositions), 1)
        self.assertGreater(receipt.context_text_character_count, 0)

    def test_accepts_numbered_pure_title_with_fullwidth_or_ascii_parenthesis(
        self,
    ) -> None:
        title = "电器件防护等级要求"
        for visible_title in ("11）电器件防护等级要求", "11) 电器件防护等级要求"):
            with self.subTest(visible_title=visible_title):
                document_map = compile_document_map_artifact(
                    _reference_with_text_node(
                        node_id="protection-heading",
                        label="11）",
                        title=title,
                        contents=(("protection-title", visible_title, True),),
                    )
                )
                request = build_source_item_text_scope_request(
                    document_map,
                    node_id="protection-heading",
                )
                candidate = SourceItemTextSliceCandidateV1(
                    source_sha256=request.source_sha256,
                    document_map_artifact_id=request.document_map_artifact_id,
                    document_map_artifact_sha256=request.document_map_artifact_sha256,
                    request_sha256=request.request_sha256,
                    scope_id=request.scope_id,
                    structure_only_spans=(
                        SourceItemStructureOnlySpanV1(
                            text_span=SourceItemTextSpanV1(
                                content_id="protection-title",
                                start_offset=0,
                                end_offset=len(visible_title),
                            ),
                            reason_codes=("source_hierarchy_title",),
                        ),
                    ),
                )
                validate_source_item_text_slice_candidate(request, candidate)

    def test_accepts_source_numbering_separator_removed_from_title_tree(self) -> None:
        visible_title = "1、概述"
        document_map = compile_document_map_artifact(
            _reference_with_text_node(
                node_id="overview-heading",
                label="1",
                title="概述",
                contents=(("overview-title", visible_title, True),),
            )
        )
        request = build_source_item_text_scope_request(
            document_map,
            node_id="overview-heading",
        )
        candidate = SourceItemTextSliceCandidateV1(
            source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id,
            document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256,
            scope_id=request.scope_id,
            structure_only_spans=(
                SourceItemStructureOnlySpanV1(
                    text_span=SourceItemTextSpanV1(
                        content_id="overview-title",
                        start_offset=0,
                        end_offset=len(visible_title),
                    ),
                    reason_codes=("source_hierarchy_title",),
                ),
            ),
        )

        validate_source_item_text_slice_candidate(request, candidate)

    def test_exact_document_unit_title_is_structure_only_when_flag_is_missing(
        self,
    ) -> None:
        visible_title = "蓄能器试验大纲"
        document_map = compile_document_map_artifact(
            _reference_with_text_node(
                node_id="accumulator-outline",
                label="",
                title=visible_title,
                contents=(("accumulator-title", visible_title, False),),
            )
        )
        request = build_source_item_text_scope_request(
            document_map,
            node_id="accumulator-outline",
        )
        candidate = SourceItemTextSliceCandidateV1(
            source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id,
            document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256,
            scope_id=request.scope_id,
            structure_only_spans=(
                SourceItemStructureOnlySpanV1(
                    text_span=SourceItemTextSpanV1(
                        content_id="accumulator-title",
                        start_offset=0,
                        end_offset=len(visible_title),
                    ),
                    reason_codes=("source_hierarchy_title",),
                ),
            ),
        )

        validate_source_item_text_slice_candidate(request, candidate)

    def test_rejects_pure_title_duplicated_as_non_requirement_item(self) -> None:
        visible_title = "8）减振器调校支持及NVH要求"
        document_map = compile_document_map_artifact(
            _reference_with_text_node(
                node_id="support-heading",
                label="8）",
                title="减振器调校支持及NVH要求",
                contents=(("support-title", visible_title, True),),
            )
        )
        request = build_source_item_text_scope_request(
            document_map,
            node_id="support-heading",
        )
        candidate = SourceItemTextSliceCandidateV1(
            source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id,
            document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256,
            scope_id=request.scope_id,
            items=(
                SourceItemTextBoundaryCandidateV1(
                    item_key="support-heading",
                    label="减振器调校支持及NVH要求",
                    source_form=SourceRecordForm.NUMBERED_CLAUSE,
                    text_spans=(
                        SourceItemTextSpanV1(
                            content_id="support-title",
                            start_offset=0,
                            end_offset=len(visible_title),
                        ),
                    ),
                    requirementness=Requirementness.NON_REQUIREMENT,
                    requirement_kinds=(),
                    non_requirement_role=NonRequirementRole.HEADING,
                    confidence=0.99,
                    decision_summary="该文本只是来源标题。",
                    reason_codes=("source_hierarchy_title",),
                ),
            ),
        )
        with self.assertRaisesRegex(
            SourceRequirementBaselineError,
            "SOURCE_ITEM_PURE_TITLE_DUPLICATED_AS_ITEM",
        ):
            validate_source_item_text_slice_candidate(request, candidate)

    def test_rejects_unowned_text_characters(self) -> None:
        text = "1.5.1 完整要求"
        document_map = compile_document_map_artifact(
            _reference_with_text_node(
                node_id="clause-incomplete",
                label="1.5.1",
                title="完整要求",
                contents=(("incomplete-content", text, True),),
            )
        )
        request = build_source_item_text_scope_request(
            document_map,
            node_id="clause-incomplete",
        )
        candidate = SourceItemTextSliceCandidateV1(
            source_sha256=request.source_sha256,
            document_map_artifact_id=request.document_map_artifact_id,
            document_map_artifact_sha256=request.document_map_artifact_sha256,
            request_sha256=request.request_sha256,
            scope_id=request.scope_id,
            items=(
                SourceItemTextBoundaryCandidateV1(
                    item_key="incomplete",
                    label="不完整范围",
                    source_form=SourceRecordForm.NUMBERED_CLAUSE,
                    text_spans=(
                        SourceItemTextSpanV1(
                            content_id="incomplete-content",
                            start_offset=0,
                            end_offset=len(text) - 1,
                        ),
                    ),
                    requirementness=Requirementness.REQUIREMENT,
                    requirement_kinds=(RequirementKind.CONSTRAINT,),
                    non_requirement_role=None,
                    confidence=0.9,
                    decision_summary="故意遗漏末字。",
                    reason_codes=("incomplete_test_fixture",),
                ),
            ),
        )
        with self.assertRaisesRegex(
            SourceRequirementBaselineError,
            "SOURCE_ITEM_TEXT_COVERAGE_INCOMPLETE",
        ):
            validate_source_item_text_slice_candidate(request, candidate)

    def test_final_text_candidate_rejects_owned_and_shared_content_overlap(
        self,
    ) -> None:
        text = "供应商应提交验证报告。"
        document_map = compile_document_map_artifact(
            _reference_with_text_node(
                node_id="clause-owned-shared-overlap",
                label="1.2",
                title="提交验证报告",
                contents=(("content-owned-shared", text, False),),
            )
        )
        request = build_source_item_text_scope_request(
            document_map,
            node_id="clause-owned-shared-overlap",
        )
        candidate = _text_candidate(
            request,
            items=(
                SourceItemTextBoundaryCandidateV1(
                    item_key="owned-shared-overlap",
                    label="供应商提交验证报告",
                    source_form=SourceRecordForm.NUMBERED_CLAUSE,
                    text_spans=(
                        SourceItemTextSpanV1(
                            content_id="content-owned-shared",
                            start_offset=0,
                            end_offset=len(text),
                        ),
                    ),
                    shared_source_content_ids=("content-owned-shared",),
                    requirementness=Requirementness.REQUIREMENT,
                    requirement_kinds=(RequirementKind.CONSTRAINT,),
                    confidence=0.99,
                    decision_summary="该条目要求供应商提交验证报告。",
                    reason_codes=("supplier_deliverable_constraint",),
                ),
            ),
        )

        with self.assertRaisesRegex(
            SourceRequirementBaselineError,
            "^SOURCE_ITEM_TEXT_SHARED_CONTENT_OVERLAPS_OWNED_SPAN$",
        ):
            validate_source_item_text_slice_candidate(request, candidate)

    def test_initial_artifact_accepts_v1_payload_without_predecessor_fields(
        self,
    ) -> None:
        candidate = _candidate(
            self.bom_request,
            records=(
                _record(
                    "complete-bom",
                    "完整 BOM",
                    SourceRecordForm.TABLE_BRANCH,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="bom-fragment", row_start=1, row_end=5
                        ),
                    ),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="bom-fragment", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )
        artifact, _ = compile_source_requirement_baseline(
            document_map=self.document_map,
            requests=(self.bom_request,),
            candidates=(candidate,),
        )
        payload = artifact.model_dump(mode="json")
        payload.pop("predecessor_artifact_id")
        payload.pop("predecessor_artifact_sha256")

        restored = SourceRequirementBaselineArtifactV1.model_validate(payload)

        self.assertEqual(restored, artifact)

    def test_scope_revision_reuses_exact_table_evidence_without_recompiling_peer(
        self,
    ) -> None:
        bom = _candidate(
            self.bom_request,
            records=(
                _record(
                    "complete-bom",
                    "完整 BOM",
                    SourceRecordForm.TABLE_BRANCH,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="bom-fragment", row_start=1, row_end=5
                        ),
                    ),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="bom-fragment", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )
        dvp = _candidate(
            self.dvp_request,
            records=(
                _record(
                    "complete-dvp",
                    "完整 DVP",
                    SourceRecordForm.TABLE_RECORD,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-one", row_start=1, row_end=4
                        ),
                        SourceRequirementRowGroupV1(
                            fragment_id="dvp-two", row_start=0, row_end=2
                        ),
                    ),
                ),
            ),
            context=(
                SourceRequirementContextRowGroupV1(
                    role=ContextRowRole.HEADER,
                    row_group=SourceRequirementRowGroupV1(
                        fragment_id="dvp-one", row_start=0, row_end=1
                    ),
                    reason_codes=("shared_table_header",),
                ),
            ),
        )
        baseline, validation = compile_source_requirement_baseline(
            document_map=self.document_map,
            requests=(self.bom_request, self.dvp_request),
            candidates=(bom, dvp),
        )
        replacement = _candidate(
            self.bom_request,
            records=(
                _record(
                    "same-evidence-new-key",
                    "同一完整 BOM 证据",
                    SourceRecordForm.TABLE_BRANCH,
                    (
                        SourceRequirementRowGroupV1(
                            fragment_id="bom-fragment", row_start=1, row_end=5
                        ),
                    ),
                ),
            ),
            context=bom.context_row_groups,
        )
        scope_revision = build_source_item_scope_revision(
            document_map=self.document_map,
            current_artifact=baseline,
            current_validation_receipt=validation,
            operation=SourceItemScopeRevisionOperation.REPLACE,
            affected_scope_ids=(self.bom_request.logical_table_scope_id,),
            comparison_scope_ids=(self.dvp_request.logical_table_scope_id,),
            feedback_ids=("feedback-table-exact-evidence",),
            requests=(self.bom_request,),
            candidates=(replacement,),
        )

        artifact, _, receipt = compile_source_item_scope_revision(
            document_map=self.document_map,
            current_artifact=baseline,
            current_validation_receipt=validation,
            scope_revision=scope_revision,
            requests=(self.bom_request,),
            candidates=(replacement,),
        )

        before_bom = next(
            item for item in baseline.records if item.node_id == "bom-node"
        )
        after_bom = next(
            item for item in artifact.records if item.node_id == "bom-node"
        )
        before_dvp = next(
            item for item in baseline.records if item.node_id == "dvp-node"
        )
        after_dvp = next(
            item for item in artifact.records if item.node_id == "dvp-node"
        )
        self.assertEqual(after_bom.source_record_id, before_bom.source_record_id)
        self.assertEqual(after_bom.source_display_id, before_bom.source_display_id)
        self.assertEqual(
            before_dvp.model_dump(mode="json", exclude={"source_order"}),
            after_dvp.model_dump(mode="json", exclude={"source_order"}),
        )
        self.assertEqual(len(receipt.matched_identities), 1)
        self.assertEqual(receipt.created_identities, ())


class SourceItemScopeRevisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scope_text = "alpha beta gamma delta"
        self.comparison_text = "comparison scope 2.1.2 remains stable"
        self.second_comparison_text = "comparison scope 2.3 remains stable"
        self.document_map = compile_document_map_artifact(
            _reference_with_text_nodes(
                (
                    (
                        "section-1.2",
                        "1.2",
                        "Affected scope",
                        "scope-a-content",
                        self.scope_text,
                        False,
                        1,
                        20.0,
                    ),
                    (
                        "section-2.1.2",
                        "2.1.2",
                        "Comparison scope",
                        "scope-b-content",
                        self.comparison_text,
                        False,
                        1,
                        200.0,
                    ),
                    (
                        "section-2.3",
                        "2.3",
                        "Second comparison scope",
                        "scope-c-content",
                        self.second_comparison_text,
                        False,
                        1,
                        300.0,
                    ),
                )
            )
        )
        self.request = build_source_item_text_scope_request(
            self.document_map,
            node_id="section-1.2",
        )
        self.comparison_request = build_source_item_text_scope_request(
            self.document_map,
            node_id="section-2.1.2",
        )
        self.second_comparison_request = build_source_item_text_scope_request(
            self.document_map,
            node_id="section-2.3",
        )
        self.initial_candidate = _text_candidate(
            self.request,
            items=(
                _text_item(
                    self.request,
                    key="whole-scope-a",
                    label="Affected scope",
                    start=0,
                    end=len(self.scope_text),
                ),
            ),
        )
        self.comparison_candidate = _text_candidate(
            self.comparison_request,
            items=(
                _text_item(
                    self.comparison_request,
                    key="comparison-scope",
                    label="Comparison scope",
                    start=0,
                    end=len(self.comparison_text),
                ),
            ),
        )
        self.second_comparison_candidate = _text_candidate(
            self.second_comparison_request,
            items=(
                _text_item(
                    self.second_comparison_request,
                    key="second-comparison-scope",
                    label="Second comparison scope",
                    start=0,
                    end=len(self.second_comparison_text),
                ),
            ),
        )
        self.baseline, self.baseline_validation = compile_source_requirement_baseline(
            document_map=self.document_map,
            requests=(),
            candidates=(),
            text_requests=(
                self.request,
                self.comparison_request,
                self.second_comparison_request,
            ),
            text_candidates=(
                self.initial_candidate,
                self.comparison_candidate,
                self.second_comparison_candidate,
            ),
        )

    def _split_candidate(self) -> SourceItemTextSliceCandidateV1:
        split = self.scope_text.index("gamma")
        return _text_candidate(
            self.request,
            items=(
                _text_item(
                    self.request,
                    key="scope-a-first",
                    label="Affected scope first item",
                    start=0,
                    end=split,
                ),
                _text_item(
                    self.request,
                    key="scope-a-second",
                    label="Affected scope second item",
                    start=split,
                    end=len(self.scope_text),
                ),
            ),
        )

    def _replace(
        self,
        *,
        current,
        validation,
        candidate,
        previous_receipt=None,
        feedback_id="feedback-replace",
    ):
        revision = build_source_item_scope_revision(
            document_map=self.document_map,
            current_artifact=current,
            current_validation_receipt=validation,
            operation=SourceItemScopeRevisionOperation.REPLACE,
            affected_scope_ids=(self.request.scope_id,),
            comparison_scope_ids=(
                self.comparison_request.scope_id,
                self.second_comparison_request.scope_id,
            ),
            feedback_ids=(feedback_id,),
            text_requests=(self.request,),
            text_candidates=(candidate,),
            previous_scope_revision_receipt=previous_receipt,
        )
        return compile_source_item_scope_revision(
            document_map=self.document_map,
            current_artifact=current,
            current_validation_receipt=validation,
            scope_revision=revision,
            text_requests=(self.request,),
            text_candidates=(candidate,),
            previous_scope_revision_receipt=previous_receipt,
        )

    def test_duplicate_headings_become_structure_only_tombstones(self) -> None:
        title = "蓄能器试验大纲"
        repeated = f"{title}\n{title}"
        document_map = compile_document_map_artifact(
            _reference_with_text_nodes(
                (
                    (
                        "doc-accumulator-outline",
                        "",
                        title,
                        "duplicate-title-content",
                        repeated,
                        False,
                        1,
                        20.0,
                    ),
                    (
                        "unaffected-node",
                        "1.1",
                        "Unaffected",
                        "unaffected-content",
                        "unaffected requirement",
                        False,
                        1,
                        200.0,
                    ),
                )
            )
        )
        heading_request = build_source_item_text_scope_request(
            document_map,
            node_id="doc-accumulator-outline",
        )
        unaffected_request = build_source_item_text_scope_request(
            document_map,
            node_id="unaffected-node",
        )
        heading_candidate = _text_candidate(
            heading_request,
            items=(
                _text_item(
                    heading_request,
                    key="legacy-heading-one",
                    label=title,
                    start=0,
                    end=len(title),
                ),
                _text_item(
                    heading_request,
                    key="legacy-heading-two",
                    label=title,
                    start=len(title) + 1,
                    end=len(repeated),
                ),
            ),
        )
        unaffected_candidate = _text_candidate(
            unaffected_request,
            items=(
                _text_item(
                    unaffected_request,
                    key="unaffected",
                    label="Unaffected",
                    start=0,
                    end=len("unaffected requirement"),
                ),
            ),
        )
        baseline, validation = compile_source_requirement_baseline(
            document_map=document_map,
            requests=(),
            candidates=(),
            text_requests=(heading_request, unaffected_request),
            text_candidates=(heading_candidate, unaffected_candidate),
        )
        heading_records = tuple(
            item
            for item in baseline.records
            if item.node_id == "doc-accumulator-outline"
        )
        legacy, legacy_validation, _, _ = (
            compile_source_requirement_classification_coordination(
                previous_artifact=baseline,
                previous_validation_receipt=validation,
                decisions=tuple(
                    SourceRequirementClassificationCoordinationDecisionV1(
                        source_record_id=item.source_record_id,
                        requirementness=Requirementness.NON_REQUIREMENT,
                        requirement_kinds=(),
                        non_requirement_role=NonRequirementRole.HEADING,
                        confidence=0.99,
                        decision_summary="Legacy duplicate heading.",
                        reason_codes=("legacy_duplicate_heading",),
                    )
                    for item in heading_records
                ),
                model_id="gpt-5.6-sol",
            )
        )
        replacement = _text_candidate(
            heading_request,
            structure_only=(
                SourceItemStructureOnlySpanV1(
                    text_span=SourceItemTextSpanV1(
                        content_id="duplicate-title-content",
                        start_offset=0,
                        end_offset=len(title),
                    ),
                    reason_codes=("source_hierarchy_title",),
                ),
                SourceItemStructureOnlySpanV1(
                    text_span=SourceItemTextSpanV1(
                        content_id="duplicate-title-content",
                        start_offset=len(title) + 1,
                        end_offset=len(repeated),
                    ),
                    reason_codes=("source_hierarchy_title",),
                ),
            ),
        )
        revision = build_source_item_scope_revision(
            document_map=document_map,
            current_artifact=legacy,
            current_validation_receipt=legacy_validation,
            operation=SourceItemScopeRevisionOperation.REPLACE,
            affected_scope_ids=(heading_request.scope_id,),
            comparison_scope_ids=(unaffected_request.scope_id,),
            feedback_ids=("feedback-duplicate-heading",),
            text_requests=(heading_request,),
            text_candidates=(replacement,),
        )

        artifact, _, receipt = compile_source_item_scope_revision(
            document_map=document_map,
            current_artifact=legacy,
            current_validation_receipt=legacy_validation,
            scope_revision=revision,
            text_requests=(heading_request,),
            text_candidates=(replacement,),
        )

        self.assertFalse(
            any(item.node_id == "doc-accumulator-outline" for item in artifact.records)
        )
        self.assertEqual(len(receipt.tombstoned_identities), 2)
        self.assertEqual(
            {item.relation for item in receipt.successor_relations},
            {"STRUCTURE_ONLY"},
        )
        self.assertTrue(
            all(
                item.successor_ids == ("STRUCTURE_NODE:doc-accumulator-outline",)
                for item in receipt.successor_relations
            )
        )
        self.assertNotEqual(
            receipt.affected_partition_before_sha256,
            receipt.affected_partition_after_sha256,
        )

    def test_one_to_many_split_then_many_to_one_merge_uses_new_ids(self) -> None:
        original = next(
            item for item in self.baseline.records if item.node_id == "section-1.2"
        )
        split_artifact, split_validation, split_receipt = self._replace(
            current=self.baseline,
            validation=self.baseline_validation,
            candidate=self._split_candidate(),
            feedback_id="feedback-split",
        )

        self.assertEqual(len(split_receipt.created_identities), 2)
        self.assertEqual(len(split_receipt.tombstoned_identities), 1)
        self.assertEqual(split_receipt.successor_relations[0].relation, "SPLIT")
        self.assertNotIn(
            original.source_record_id,
            {item.source_record_id for item in split_artifact.records},
        )

        merged_artifact, _, merged_receipt = self._replace(
            current=split_artifact,
            validation=split_validation,
            candidate=_text_candidate(
                self.request,
                items=(
                    _text_item(
                        self.request,
                        key="merged-scope-a",
                        label="Merged affected scope",
                        start=0,
                        end=len(self.scope_text),
                    ),
                ),
            ),
            previous_receipt=split_receipt,
            feedback_id="feedback-merge",
        )

        merged = next(
            item for item in merged_artifact.records if item.node_id == "section-1.2"
        )
        self.assertEqual(len(merged_receipt.created_identities), 1)
        self.assertEqual(len(merged_receipt.tombstoned_identities), 2)
        self.assertEqual(
            {item.relation for item in merged_receipt.successor_relations},
            {"MERGED"},
        )
        self.assertNotEqual(merged.source_record_id, original.source_record_id)
        self.assertGreater(
            int(merged.source_display_id.removeprefix("SRC-")),
            split_receipt.source_display_id_high_watermark_after,
        )

    def test_exact_evidence_restores_opaque_src_and_req_suffix(self) -> None:
        original = next(
            item for item in self.baseline.records if item.node_id == "section-1.2"
        )
        non_requirement = _text_candidate(
            self.request,
            items=(
                _text_item(
                    self.request,
                    key="same-evidence-context",
                    label="Same evidence as context",
                    start=0,
                    end=len(self.scope_text),
                    requirementness=Requirementness.NON_REQUIREMENT,
                    non_requirement_role=NonRequirementRole.CONTEXT,
                ),
            ),
        )
        removed_req, removed_validation, removed_receipt = self._replace(
            current=self.baseline,
            validation=self.baseline_validation,
            candidate=non_requirement,
            feedback_id="feedback-remove-req",
        )
        changed = next(
            item for item in removed_req.records if item.node_id == "section-1.2"
        )
        self.assertEqual(changed.source_record_id, original.source_record_id)
        self.assertEqual(changed.source_display_id, original.source_display_id)
        self.assertIsNone(changed.requirement_display_id)
        self.assertEqual(len(removed_receipt.matched_identities), 1)
        self.assertEqual(removed_receipt.created_identities, ())
        self.assertEqual(removed_receipt.tombstoned_identities, ())
        self.assertEqual(
            removed_receipt.affected_partition_before_sha256,
            removed_receipt.affected_partition_after_sha256,
        )

        restored, _, restored_receipt = self._replace(
            current=removed_req,
            validation=removed_validation,
            candidate=_text_candidate(
                self.request,
                items=(
                    _text_item(
                        self.request,
                        key="same-evidence-requirement-restored",
                        label="Same evidence restored",
                        start=0,
                        end=len(self.scope_text),
                    ),
                ),
            ),
            previous_receipt=removed_receipt,
            feedback_id="feedback-restore-req",
        )
        restored_record = next(
            item for item in restored.records if item.node_id == "section-1.2"
        )
        self.assertEqual(restored_record.source_record_id, original.source_record_id)
        self.assertEqual(restored_record.source_display_id, original.source_display_id)
        self.assertEqual(
            restored_record.requirement_display_id,
            original.requirement_display_id,
        )
        self.assertEqual(
            restored_receipt.source_display_id_high_watermark_after,
            removed_receipt.source_display_id_high_watermark_after,
        )

    def test_unaffected_scope_is_field_identical_and_hash_stable(self) -> None:
        unaffected_before = next(
            item for item in self.baseline.records if item.node_id == "section-2.1.2"
        )
        artifact, _, receipt = self._replace(
            current=self.baseline,
            validation=self.baseline_validation,
            candidate=self._split_candidate(),
            feedback_id="feedback-unaffected",
        )
        unaffected_after = next(
            item for item in artifact.records if item.node_id == "section-2.1.2"
        )

        self.assertEqual(
            unaffected_before.model_dump(mode="json", exclude={"source_order"}),
            unaffected_after.model_dump(mode="json", exclude={"source_order"}),
        )
        self.assertEqual(
            receipt.unaffected_stable_records_before_sha256,
            receipt.unaffected_stable_records_after_sha256,
        )
        self.assertEqual(
            receipt.comparison_scope_ids,
            (
                self.comparison_request.scope_id,
                self.second_comparison_request.scope_id,
            ),
        )

    def test_scope_revision_receipt_is_canonical_and_tamper_evident(self) -> None:
        _, _, receipt = self._replace(
            current=self.baseline,
            validation=self.baseline_validation,
            candidate=self._split_candidate(),
            feedback_id="feedback-canonical-receipt",
        )
        payload = receipt.model_dump(mode="json")

        self.assertEqual(
            SourceItemScopeRevisionReceiptV1.model_validate(payload),
            receipt,
        )
        payload["coverage_after_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "receipt SHA drifted"):
            SourceItemScopeRevisionReceiptV1.model_validate(payload)

    def test_stale_current_baseline_cas_is_rejected(self) -> None:
        candidate = self._split_candidate()
        stale_revision = build_source_item_scope_revision(
            document_map=self.document_map,
            current_artifact=self.baseline,
            current_validation_receipt=self.baseline_validation,
            operation=SourceItemScopeRevisionOperation.REPLACE,
            affected_scope_ids=(self.request.scope_id,),
            comparison_scope_ids=(
                self.comparison_request.scope_id,
                self.second_comparison_request.scope_id,
            ),
            feedback_ids=("feedback-stale",),
            text_requests=(self.request,),
            text_candidates=(candidate,),
        )
        current, current_validation, current_receipt = self._replace(
            current=self.baseline,
            validation=self.baseline_validation,
            candidate=candidate,
            feedback_id="feedback-current",
        )

        with self.assertRaisesRegex(
            SourceRequirementBaselineError,
            "SOURCE_ITEM_SCOPE_REVISION_STALE_CURRENT_BASELINE_CAS",
        ):
            compile_source_item_scope_revision(
                document_map=self.document_map,
                current_artifact=current,
                current_validation_receipt=current_validation,
                scope_revision=stale_revision,
                text_requests=(self.request,),
                text_candidates=(candidate,),
                previous_scope_revision_receipt=current_receipt,
            )

    def test_replacement_rejects_incomplete_and_overlapping_coverage(self) -> None:
        invalid_candidates = (
            (
                _text_candidate(
                    self.request,
                    items=(
                        _text_item(
                            self.request,
                            key="incomplete",
                            label="Incomplete",
                            start=0,
                            end=len(self.scope_text) - 1,
                        ),
                    ),
                ),
                "SOURCE_ITEM_TEXT_COVERAGE_INCOMPLETE",
            ),
            (
                _text_candidate(
                    self.request,
                    items=(
                        _text_item(
                            self.request,
                            key="overlap-one",
                            label="Overlap one",
                            start=0,
                            end=12,
                        ),
                        _text_item(
                            self.request,
                            key="overlap-two",
                            label="Overlap two",
                            start=6,
                            end=len(self.scope_text),
                        ),
                    ),
                ),
                "SOURCE_ITEM_TEXT_SPAN_OVERLAP",
            ),
        )
        for index, (candidate, error_code) in enumerate(invalid_candidates):
            with self.subTest(error_code=error_code):
                revision = build_source_item_scope_revision(
                    document_map=self.document_map,
                    current_artifact=self.baseline,
                    current_validation_receipt=self.baseline_validation,
                    operation=SourceItemScopeRevisionOperation.REPLACE,
                    affected_scope_ids=(self.request.scope_id,),
                    comparison_scope_ids=(
                        self.comparison_request.scope_id,
                        self.second_comparison_request.scope_id,
                    ),
                    feedback_ids=(f"feedback-invalid-{index}",),
                    text_requests=(self.request,),
                    text_candidates=(candidate,),
                )
                with self.assertRaisesRegex(
                    SourceRequirementBaselineError,
                    error_code,
                ):
                    compile_source_item_scope_revision(
                        document_map=self.document_map,
                        current_artifact=self.baseline,
                        current_validation_receipt=self.baseline_validation,
                        scope_revision=revision,
                        text_requests=(self.request,),
                        text_candidates=(candidate,),
                    )

    def test_undo_appends_revision_restores_old_ids_and_keeps_high_watermark(
        self,
    ) -> None:
        original = next(
            item for item in self.baseline.records if item.node_id == "section-1.2"
        )
        split, split_validation, split_receipt = self._replace(
            current=self.baseline,
            validation=self.baseline_validation,
            candidate=self._split_candidate(),
            feedback_id="feedback-before-undo",
        )
        undo_revision = build_source_item_scope_revision(
            document_map=self.document_map,
            current_artifact=split,
            current_validation_receipt=split_validation,
            operation=SourceItemScopeRevisionOperation.UNDO,
            affected_scope_ids=(self.request.scope_id,),
            comparison_scope_ids=(
                self.comparison_request.scope_id,
                self.second_comparison_request.scope_id,
            ),
            feedback_ids=("feedback-undo",),
            undo_target_artifact=self.baseline,
            previous_scope_revision_receipt=split_receipt,
        )
        undone, undone_validation, undo_receipt = compile_source_item_scope_revision(
            document_map=self.document_map,
            current_artifact=split,
            current_validation_receipt=split_validation,
            scope_revision=undo_revision,
            undo_target_artifact=self.baseline,
            previous_scope_revision_receipt=split_receipt,
        )

        restored = next(
            item for item in undone.records if item.node_id == "section-1.2"
        )
        self.assertEqual(undone.revision, split.revision + 1)
        self.assertEqual(undone.predecessor_artifact_id, split.artifact_id)
        self.assertEqual(restored.source_record_id, original.source_record_id)
        self.assertEqual(restored.source_display_id, original.source_display_id)
        self.assertEqual(undo_receipt.operation, SourceItemScopeRevisionOperation.UNDO)
        self.assertEqual(
            undo_receipt.undo_target_artifact_id, self.baseline.artifact_id
        )
        self.assertEqual(
            undo_receipt.source_display_id_high_watermark_after,
            split_receipt.source_display_id_high_watermark_after,
        )

        split_again, _, split_again_receipt = self._replace(
            current=undone,
            validation=undone_validation,
            candidate=self._split_candidate(),
            previous_receipt=undo_receipt,
            feedback_id="feedback-after-undo",
        )
        prior_split_ids = {
            item.source_display_id for item in split_receipt.created_identities
        }
        new_split_ids = {
            item.source_display_id for item in split_again_receipt.created_identities
        }
        self.assertTrue(prior_split_ids.isdisjoint(new_split_ids))
        self.assertTrue(
            all(
                int(item.removeprefix("SRC-"))
                > undo_receipt.source_display_id_high_watermark_after
                for item in new_split_ids
            )
        )
        self.assertEqual(
            len(
                [item for item in split_again.records if item.node_id == "section-1.2"]
            ),
            2,
        )

    def test_undo_rejects_lower_revision_sibling_with_same_source_and_map(
        self,
    ) -> None:
        split, split_validation, split_receipt = self._replace(
            current=self.baseline,
            validation=self.baseline_validation,
            candidate=self._split_candidate(),
            feedback_id="feedback-before-sibling-undo",
        )
        sibling_candidate = _text_candidate(
            self.request,
            items=(
                _text_item(
                    self.request,
                    key="sibling-whole-scope",
                    label="Sibling non-requirement scope",
                    start=0,
                    end=len(self.scope_text),
                    requirementness=Requirementness.NON_REQUIREMENT,
                    non_requirement_role=NonRequirementRole.CONTEXT,
                ),
            ),
        )
        sibling, _ = compile_source_requirement_baseline(
            document_map=self.document_map,
            requests=(),
            candidates=(),
            text_requests=(
                self.request,
                self.comparison_request,
                self.second_comparison_request,
            ),
            text_candidates=(
                sibling_candidate,
                self.comparison_candidate,
                self.second_comparison_candidate,
            ),
        )
        self.assertEqual(sibling.revision, self.baseline.revision)
        self.assertEqual(sibling.source_sha256, self.baseline.source_sha256)
        self.assertEqual(
            sibling.document_map_artifact_id,
            self.baseline.document_map_artifact_id,
        )
        self.assertNotEqual(sibling.artifact_id, self.baseline.artifact_id)

        with self.assertRaisesRegex(
            SourceRequirementBaselineError,
            "SOURCE_ITEM_SCOPE_REVISION_UNDO_TARGET_NOT_DIRECT_PREDECESSOR",
        ):
            build_source_item_scope_revision(
                document_map=self.document_map,
                current_artifact=split,
                current_validation_receipt=split_validation,
                operation=SourceItemScopeRevisionOperation.UNDO,
                affected_scope_ids=(self.request.scope_id,),
                comparison_scope_ids=(
                    self.comparison_request.scope_id,
                    self.second_comparison_request.scope_id,
                ),
                feedback_ids=("feedback-sibling-undo",),
                undo_target_artifact=sibling,
                previous_scope_revision_receipt=split_receipt,
            )

        # Bind the sibling through a canonical request without relying on the
        # guarded builder, proving the pure compiler independently fails closed.
        valid_undo = build_source_item_scope_revision(
            document_map=self.document_map,
            current_artifact=split,
            current_validation_receipt=split_validation,
            operation=SourceItemScopeRevisionOperation.UNDO,
            affected_scope_ids=(self.request.scope_id,),
            comparison_scope_ids=(
                self.comparison_request.scope_id,
                self.second_comparison_request.scope_id,
            ),
            feedback_ids=("feedback-direct-undo",),
            undo_target_artifact=self.baseline,
            previous_scope_revision_receipt=split_receipt,
        )
        sibling_revision_payload = valid_undo.model_dump(
            mode="json", exclude={"revision_sha256"}
        )
        sibling_revision_payload.update(
            {
                "undo_target_artifact_id": sibling.artifact_id,
                "undo_target_artifact_sha256": sibling.artifact_sha256,
                "undo_target_artifact_revision": sibling.revision,
            }
        )
        sibling_revision = SourceItemScopeRevisionV1(
            **sibling_revision_payload,
            revision_sha256=canonical_json_sha256(sibling_revision_payload),
        )

        with self.assertRaisesRegex(
            SourceRequirementBaselineError,
            "SOURCE_ITEM_SCOPE_REVISION_UNDO_TARGET_NOT_DIRECT_PREDECESSOR",
        ):
            compile_source_item_scope_revision(
                document_map=self.document_map,
                current_artifact=split,
                current_validation_receipt=split_validation,
                scope_revision=sibling_revision,
                undo_target_artifact=sibling,
                previous_scope_revision_receipt=split_receipt,
            )

    def test_repeated_visible_title_positive_and_extra_body_negative(self) -> None:
        title = "连接器匹配评估表"
        repeated = f"{title}\n{title}"
        positive_map = compile_document_map_artifact(
            _reference_with_text_nodes(
                (
                    (
                        "doc-connector-matching",
                        "",
                        title,
                        "repeated-title-content",
                        repeated,
                        False,
                        1,
                        20.0,
                    ),
                )
            )
        )
        positive_request = build_source_item_text_scope_request(
            positive_map,
            node_id="doc-connector-matching",
        )
        positive = _text_candidate(
            positive_request,
            structure_only=tuple(
                SourceItemStructureOnlySpanV1(
                    text_span=SourceItemTextSpanV1(
                        content_id="repeated-title-content",
                        start_offset=start,
                        end_offset=end,
                    ),
                    reason_codes=("source_hierarchy_title",),
                )
                for start, end in (
                    (0, len(title)),
                    (len(title) + 1, len(repeated)),
                )
            ),
        )
        validate_source_item_text_slice_candidate(positive_request, positive)

        with_body = f"{title}\n额外正文"
        negative_map = compile_document_map_artifact(
            _reference_with_text_nodes(
                (
                    (
                        "title-with-body",
                        "",
                        title,
                        "title-body-content",
                        with_body,
                        False,
                        1,
                        20.0,
                    ),
                )
            )
        )
        negative_request = build_source_item_text_scope_request(
            negative_map,
            node_id="title-with-body",
        )
        negative = _text_candidate(
            negative_request,
            items=(
                _text_item(
                    negative_request,
                    key="extra-body",
                    label="Extra body",
                    start=len(title) + 1,
                    end=len(with_body),
                    requirementness=Requirementness.NON_REQUIREMENT,
                    non_requirement_role=NonRequirementRole.CONTEXT,
                ),
            ),
            structure_only=(
                SourceItemStructureOnlySpanV1(
                    text_span=SourceItemTextSpanV1(
                        content_id="title-body-content",
                        start_offset=0,
                        end_offset=len(title),
                    ),
                    reason_codes=("source_hierarchy_title",),
                ),
            ),
        )
        with self.assertRaisesRegex(
            SourceRequirementBaselineError,
            "SOURCE_ITEM_STRUCTURE_ONLY_SPAN_NOT_TITLE",
        ):
            validate_source_item_text_slice_candidate(negative_request, negative)


def _text_candidate(
    request,
    *,
    items=(),
    structure_only=(),
) -> SourceItemTextSliceCandidateV1:
    return SourceItemTextSliceCandidateV1(
        source_sha256=request.source_sha256,
        document_map_artifact_id=request.document_map_artifact_id,
        document_map_artifact_sha256=request.document_map_artifact_sha256,
        request_sha256=request.request_sha256,
        scope_id=request.scope_id,
        items=items,
        structure_only_spans=structure_only,
    )


def _text_item(
    request,
    *,
    key: str,
    label: str,
    start: int,
    end: int,
    requirementness: Requirementness = Requirementness.REQUIREMENT,
    non_requirement_role: NonRequirementRole | None = None,
) -> SourceItemTextBoundaryCandidateV1:
    return SourceItemTextBoundaryCandidateV1(
        item_key=key,
        label=label,
        source_form=SourceRecordForm.NUMBERED_CLAUSE,
        text_spans=(
            SourceItemTextSpanV1(
                content_id=request.source_content[0].content_id,
                start_offset=start,
                end_offset=end,
            ),
        ),
        requirementness=requirementness,
        requirement_kinds=(
            (RequirementKind.CONSTRAINT,)
            if requirementness is Requirementness.REQUIREMENT
            else ()
        ),
        non_requirement_role=(
            None
            if requirementness is Requirementness.REQUIREMENT
            else non_requirement_role or NonRequirementRole.CONTEXT
        ),
        confidence=0.97,
        decision_summary="Agent-owned candidate boundary for scope revision tests.",
        reason_codes=("scope_revision_candidate",),
    )


def _reference_with_text_nodes(
    nodes_to_add: tuple[tuple[str, str, str, str, str, bool, int, float], ...],
) -> dict[str, object]:
    reference = _public_reference()
    tree = reference["document_tree"]
    assert isinstance(tree, dict)
    nodes = tree["nodes"]
    assert isinstance(nodes, list)
    for node_id, label, title, content_id, text, is_title, page, y in nodes_to_add:
        nodes.append(
            _node(
                node_id,
                "root",
                len(nodes),
                title,
                label,
                (page,),
                (),
                source_content=(
                    {
                        "content_id": content_id,
                        "page": page,
                        "bbox": [0.0, y, 300.0, y + 20.0],
                        "source_bounds": [0.0, 0.0, 595.0, 842.0],
                        "source_handles": [f"source-{content_id}"],
                        "text": text,
                        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "precision": "EXACT_NATIVE_SPAN_RANGE",
                        "is_title": is_title,
                    },
                ),
            )
        )
    return reference


def _candidate(
    request,
    *,
    records,
    context,
) -> SourceRequirementSliceCandidateV1:
    return SourceRequirementSliceCandidateV1(
        source_sha256=request.source_sha256,
        document_map_artifact_id=request.document_map_artifact_id,
        document_map_artifact_sha256=request.document_map_artifact_sha256,
        request_sha256=request.request_sha256,
        logical_table_scope_id=request.logical_table_scope_id,
        records=records,
        context_row_groups=context,
        diagnostic_codes=(),
    )


def _record(
    key: str,
    label: str,
    form: SourceRecordForm,
    row_groups,
    *,
    shared=(),
) -> SourceRequirementBoundaryCandidateV1:
    return SourceRequirementBoundaryCandidateV1(
        record_key=key,
        label=label,
        source_form=form,
        row_groups=row_groups,
        shared_source_content_ids=shared,
        requirementness=Requirementness.REQUIREMENT,
        requirement_kinds=(RequirementKind.CONSTRAINT, RequirementKind.QUALITY),
        non_requirement_role=None,
        confidence=0.94,
        decision_summary="该完整业务记录表达可审核的产品或验证约束。",
        reason_codes=("normative_table_record",),
    )


def _public_reference() -> dict[str, object]:
    content_text = "供应商需要完成如下零部件的设计开发、生产制造。"
    return {
        "schema_version": "native-structure-reference-public/v1",
        "artifact_id": "native-reference-test",
        "source_sha256": "a" * 64,
        "document_tree": {
            "schema_version": "native-document-structure-tree/v1",
            "root_ids": ["root"],
            "nodes": [
                _node("root", None, 0, "客户 PDS", "", (1, 2, 10, 11), ()),
                _node(
                    "bom-node",
                    "root",
                    1,
                    "产品信息",
                    "3.2.1",
                    (2,),
                    ("lt-bom",),
                    source_content=(
                        {
                            "content_id": "bom-context",
                            "page": 2,
                            "bbox": [0.0, 0.0, 100.0, 20.0],
                            "source_bounds": [0.0, 0.0, 100.0, 200.0],
                            "source_handles": ["source-bom"],
                            "text": content_text,
                            "text_sha256": hashlib.sha256(
                                content_text.encode("utf-8")
                            ).hexdigest(),
                            "precision": "EXACT_NATIVE_SPAN_RANGE",
                            "is_title": False,
                        },
                    ),
                ),
                _node(
                    "dvp-node",
                    "root",
                    2,
                    "主动前减振器试验大纲",
                    "",
                    (10, 11),
                    ("lt-dvp",),
                ),
            ],
        },
        "logical_tables": [
            {
                "scope_id": "lt-bom",
                "title": "产品信息",
                "owner_unit_id": "main",
                "role": "COMPONENT_HIERARCHY",
                "pages": [2],
                "fragments": [{"table_token": "T0001", "page": 2, "row_range": [0, 5]}],
                "source_numbering_anomaly": "",
                "capture_gap": "",
            },
            {
                "scope_id": "lt-dvp",
                "title": "主动前减振器试验大纲",
                "owner_unit_id": "dvp",
                "role": "TEST_OUTLINE",
                "pages": [10, 11],
                "fragments": [
                    {"table_token": "T0002", "page": 10, "row_range": [0, 4]},
                    {"table_token": "T0003", "page": 11, "row_range": [0, 2]},
                ],
                "source_numbering_anomaly": "",
                "capture_gap": "",
            },
        ],
        "structure_proposals": [],
        "continuation": {"evidence_state": "SUFFICIENT"},
        "validation": {"receipt_sha256": "b" * 64},
    }


def _node(
    node_id,
    parent_id,
    order,
    title,
    label,
    pages,
    table_ids,
    *,
    source_content=(),
):
    return {
        "node_id": node_id,
        "parent_id": parent_id,
        "order": order,
        "kind": "DOCUMENT_ROOT" if parent_id is None else "SECTION",
        "label": label,
        "title": title,
        "title_authority": "SOURCE_VISIBLE",
        "pages": list(pages),
        "content_blocks": [
            {"kind": "LOGICAL_TABLE", "logical_table_scope_id": table_id}
            for table_id in table_ids
        ],
        "finding_codes": [],
        "source_anchors": [],
        "source_content": list(source_content),
    }


def _reference_with_text_node(
    *,
    node_id: str,
    label: str,
    title: str,
    contents: tuple[tuple[str, str, bool], ...],
) -> dict[str, object]:
    reference = _public_reference()
    tree = reference["document_tree"]
    assert isinstance(tree, dict)
    nodes = tree["nodes"]
    assert isinstance(nodes, list)
    source_content = tuple(
        {
            "content_id": content_id,
            "page": 1,
            "bbox": [0.0, float(index * 30), 200.0, float(index * 30 + 20)],
            "source_bounds": [0.0, 0.0, 595.0, 842.0],
            "source_handles": [f"source-{content_id}"],
            "text": text,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "precision": "EXACT_NATIVE_SPAN_RANGE",
            "is_title": is_title,
        }
        for index, (content_id, text, is_title) in enumerate(contents)
    )
    nodes.append(
        _node(
            node_id,
            "root",
            len(nodes),
            title,
            label,
            (1,),
            (),
            source_content=source_content,
        )
    )
    return reference


def _table_preview(*, scope_id, title, fragments):
    return {
        "schema_version": "native-structure-table-preview/v1",
        "artifact_id": "native-reference-test",
        "source_sha256": "a" * 64,
        "logical_table_scope_id": scope_id,
        "title": title,
        "fragments": list(fragments),
        "summary": {
            "fragment_count": len(fragments),
            "row_count": sum(item["row_count"] for item in fragments),
            "cell_count": sum(len(item["cells"]) for item in fragments),
        },
    }


def _fragment(fragment_id, table_token, *, page, rows):
    cells = []
    for row_index, row in enumerate(rows):
        for column_index, text in enumerate(row):
            cells.append(
                {
                    "cell_id": f"{fragment_id}-cell-{row_index}-{column_index}",
                    "row": row_index,
                    "column": column_index,
                    "row_span": 1,
                    "column_span": 1,
                    "text": text,
                    "bbox": [
                        float(column_index * 50),
                        float(row_index * 20),
                        float((column_index + 1) * 50),
                        float((row_index + 1) * 20),
                    ],
                }
            )
    return {
        "fragment_id": fragment_id,
        "table_token": table_token,
        "page": page,
        "row_range": [0, len(rows)],
        "row_count": len(rows),
        "column_count": 2,
        "bbox": [0.0, 0.0, 100.0, float(len(rows) * 20)],
        "source_bounds": [0.0, 0.0, 100.0, 500.0],
        "cells": cells,
    }


if __name__ == "__main__":
    unittest.main()
