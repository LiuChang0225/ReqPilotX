from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import db
from app.api.app_factory import create_app
from app.adapters.persistence.requirement_matrix_states import (
    clone_requirement_matrix_node_states,
    list_requirement_matrix_node_states,
    save_requirement_matrix_node_decision,
    set_requirement_matrix_node_disposition,
    set_requirement_matrix_node_dispositions,
)
from app.application.errors import ApplicationError
from app.application.extraction.requirement_tree_snapshots import (
    RequirementTreeSnapshotError,
)
from app.application.requirement_matrix import (
    _direct_presentation_tree_nodes,
    _is_system_non_business,
    _matrix_human_presentation,
    _matrix_counts,
    _record_ai_rows,
    _project_matrix_node,
    _restore_historical_table_containers,
    _resolved_table_record_id,
    build_requirement_matrix_snapshot,
    get_requirement_matrix_node_detail,
    patch_requirement_matrix_dispositions,
    patch_requirement_matrix_node_disposition,
    select_matrix_analysis_requirements,
)
from app.application.requirement_matrix_contracts import (
    MatrixDecisionRequestV1,
    MatrixDispositionBatchItemV1,
    MatrixDispositionBatchPatchV1,
    MatrixDispositionPatchV1,
)
from app.domain import requirement_matrix as requirement_matrix_domain
from app.domain.extraction.requirement_tree import RequirementTreeNodeV1
from app.domain.requirement_matrix import (
    MatrixAnalysisEligibility,
    MatrixAnalysisResultState,
    MatrixAnalysisStatus,
    MatrixCountsV1,
    MatrixLogicalTableV1,
    MatrixNodeKind,
    MatrixNodeDisposition,
    MatrixNodeStateV1,
    MatrixNodeVisibility,
    MatrixSnapshotState,
    RequirementMatrixSnapshotV1,
)


def _tree_node(
    *,
    node_id: str,
    role: str,
    kind: str = "CLAUSE",
    requirement_id: str | None = None,
    child_ids: tuple[str, ...] = (),
    classification: str = "",
    label: str = "测试节点",
) -> RequirementTreeNodeV1:
    structure_node_id = "owner-1"
    parent_id = "parent-1"
    if role == "DIRECT_CONTENT":
        parent_id = structure_node_id
        kind = "DIRECT_CONTENT"
    elif role == "WHOLE_TABLE_REVIEW":
        kind = "TABLE"
    elif role == "TABLE_RECORD":
        kind = "TABLE_RECORD"
    return RequirementTreeNodeV1(
        id=node_id,
        parent_id=parent_id,
        kind=kind,
        depth=1,
        source_order=1,
        label=label,
        summary="系统应记录结果。",
        child_ids=child_ids,
        structure_node_id=structure_node_id,
        requirement_entry_id=requirement_id,
        projection_role=role,
        classification=classification,
        table_record_id="record-1" if role == "TABLE_RECORD" else None,
    )


def _requirement(
    requirement_id: str,
    *,
    entry_kind: str,
    decision_mode: str = "",
    **overrides: object,
) -> dict:
    row = {
        "id": requirement_id,
        "entry_kind": entry_kind,
        "req_code": "REQ0001",
        "display_text": "系统应记录结果。",
        "decision_reference": (
            {"decision_mode": decision_mode} if decision_mode else {}
        ),
        "updated_at": "2026-07-31T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def _project(
    *,
    role: str,
    entry_kind: str,
    child_ids: tuple[str, ...] = (),
    persisted_disposition: str = "",
    decision_mode: str = "",
    classification: str = "",
) -> MatrixNodeStateV1:
    requirement_id = "requirement-1"
    return _project_matrix_node(
        tree_node=_tree_node(
            node_id="node-1",
            role=role,
            requirement_id=requirement_id,
            child_ids=child_ids,
            classification=classification,
        ),
        requirement=_requirement(
            requirement_id,
            entry_kind=entry_kind,
            decision_mode=decision_mode,
        ),
        persisted_state=(
            {
                "disposition": persisted_disposition,
                "reason": "人工处置说明",
                "revision": 2,
                "updated_at": "2026-07-31T01:00:00+00:00",
            }
            if persisted_disposition
            else None
        ),
        logical_table_ids=(),
    )


def _logical_table(
    *,
    logical_table_id: str = "logical-table-1",
    projection_id: str = "projection-1",
    projection_mode: str = "RECORDS",
    physical_table_ids: tuple[str, ...] = ("physical-table-1",),
    node_ids: tuple[str, ...] = (),
    record_ids: tuple[str, ...] = ("record-1",),
    evidence_bindings: tuple[dict, ...] = (),
) -> MatrixLogicalTableV1:
    records = (
        [{"record_id": record_id} for record_id in record_ids]
        if projection_mode == "RECORDS"
        else []
    )
    return MatrixLogicalTableV1(
        logical_table_id=logical_table_id,
        projection_id=projection_id,
        projection_mode=projection_mode,
        record_count=len(records),
        physical_table_ids=physical_table_ids,
        node_ids=node_ids,
        projection={
            "projection_id": projection_id,
            "mode": projection_mode,
            "table_fact": {
                "logical_table_id": logical_table_id,
                "table_parts": [
                    {"table_id": table_id} for table_id in physical_table_ids
                ],
            },
            "records": records,
        },
        evidence_bindings=evidence_bindings,
    )


def _analysis_contract_node(
    *,
    analysis_result_state: str = "NONE",
    analysis_status: str | None = None,
    ai_assessment: dict | None = None,
    manual_decision: dict | None = None,
    compliance_status: str = "",
    stale_ai_assessment: dict | None = None,
) -> MatrixNodeStateV1:
    resolved_analysis_status = analysis_status
    if resolved_analysis_status is None:
        resolved_analysis_status = "NOT_STARTED"
        if manual_decision is not None:
            resolved_analysis_status = "DECIDED"
        elif analysis_result_state == "CURRENT":
            resolved_analysis_status = "ASSESSED"
    return MatrixNodeStateV1.model_validate(
        {
            "node_id": "analysis-node",
            "parent_id": None,
            "child_ids": [],
            "depth": 0,
            "source_order": 0,
            "display_role": "ATOMIC_REQUIREMENT",
            "label": "测试需求",
            "requirement_entry_id": "requirement-1",
            "requirement_id": "requirement-1",
            "requirement_code": "REQ-001",
            "requirement_text": "系统应记录结果。",
            "matrix_visibility": "VISIBLE",
            "disposition": "ANALYZE",
            "analysis_eligibility": "ELIGIBLE",
            "analysis_status": resolved_analysis_status,
            "analysis_result_state": analysis_result_state,
            "ai_assessment": ai_assessment,
            "manual_decision": manual_decision,
            "stale_ai_assessment": stale_ai_assessment,
            "compliance_status": compliance_status,
        }
    )


class RequirementMatrixConclusionContractTests(unittest.TestCase):
    def test_analysis_target_requires_visible_analyze_and_eligible(self) -> None:
        self.assertTrue(
            requirement_matrix_domain.is_analysis_target(
                {
                    "matrix_visibility": "VISIBLE",
                    "disposition": "ANALYZE",
                    "analysis_eligibility": "ELIGIBLE",
                }
            )
        )
        for changed_field, changed_value in (
            ("matrix_visibility", "HUMAN_EXCLUDED"),
            ("matrix_visibility", "SYSTEM_NON_BUSINESS"),
            ("disposition", "RETAIN"),
            ("analysis_eligibility", "NOT_ELIGIBLE"),
        ):
            with self.subTest(changed_field=changed_field, changed_value=changed_value):
                candidate = {
                    "matrix_visibility": "VISIBLE",
                    "disposition": "ANALYZE",
                    "analysis_eligibility": "ELIGIBLE",
                }
                candidate[changed_field] = changed_value
                self.assertFalse(
                    requirement_matrix_domain.is_analysis_target(candidate)
                )

    def test_current_conclusion_contract_distinguishes_ai_and_human_sources(
        self,
    ) -> None:
        cases = (
            (
                "ai decision",
                _analysis_contract_node(
                    analysis_result_state="CURRENT",
                    ai_assessment={
                        "decision": "ACCEPT",
                        "reason": "证据充分",
                        "assessment_mode": "LOCAL_RULE",
                    },
                ),
                "ACCEPT",
                "AI_DEFAULT",
                False,
            ),
            (
                "ai compliance fallback",
                _analysis_contract_node(
                    analysis_result_state="CURRENT",
                    ai_assessment={
                        "compliance_status": "COMPLIANT",
                        "reason": "证据充分",
                        "assessment_mode": "LOCAL_RULE",
                    },
                ),
                "COMPLIANT",
                "AI_DEFAULT",
                False,
            ),
            (
                "human override",
                _analysis_contract_node(
                    analysis_result_state="CURRENT",
                    ai_assessment={
                        "decision": "ACCEPT",
                        "reason": "AI 建议",
                        "assessment_mode": "LOCAL_RULE",
                    },
                    manual_decision={
                        "decision": "REJECT",
                        "reason": "工程师覆盖",
                    },
                ),
                "REJECT",
                "HUMAN_OVERRIDE",
                True,
            ),
            (
                "human native",
                _analysis_contract_node(
                    analysis_result_state="CURRENT",
                    manual_decision={
                        "decision": "ACCEPT_WITH_CONDITIONS",
                        "reason": "工程师录入",
                    },
                ),
                "ACCEPT_WITH_CONDITIONS",
                "HUMAN_NATIVE",
                False,
            ),
            (
                "human native with reason-only ai assessment",
                _analysis_contract_node(
                    analysis_result_state="CURRENT",
                    ai_assessment={
                        "reason": "只有说明，没有 AI 结论",
                        "confidence": 0.4,
                        "assessment_mode": "LOCAL_RULE",
                    },
                    manual_decision={
                        "decision": "ACCEPT_WITH_CONDITIONS",
                        "reason": "工程师独立录入",
                    },
                ),
                "ACCEPT_WITH_CONDITIONS",
                "HUMAN_NATIVE",
                False,
            ),
        )

        for label, node, expected_value, expected_source, overrides_ai in cases:
            with self.subTest(label=label):
                contract = requirement_matrix_domain.matrix_conclusion_contract(node)
                self.assertTrue(contract.analysis_target)
                self.assertEqual(contract.effective_conclusion, expected_value)
                self.assertEqual(contract.conclusion_source.value, expected_source)
                self.assertEqual(contract.overrides_ai, overrides_ai)

    def test_stale_pending_reason_only_and_unproven_legacy_status_are_not_effective(
        self,
    ) -> None:
        cases = (
            (
                "stale",
                _analysis_contract_node(
                    analysis_result_state="STALE_REFERENCE",
                    stale_ai_assessment={
                        "decision": "ACCEPT",
                        "reason": "旧结论",
                        "assessment_mode": "LOCAL_RULE",
                    },
                ),
                "STALE_REFERENCE",
            ),
            ("pending", _analysis_contract_node(), "NONE"),
            (
                "reason only",
                _analysis_contract_node(
                    analysis_result_state="CURRENT",
                    ai_assessment={
                        "reason": "只有说明，没有结论",
                        "assessment_mode": "LOCAL_RULE",
                    },
                ),
                "NONE",
            ),
            (
                "legacy top-level compliance",
                _analysis_contract_node(
                    analysis_result_state="CURRENT",
                    compliance_status="COMPLIANT",
                ),
                "NONE",
            ),
        )

        for label, node, expected_source in cases:
            with self.subTest(label=label):
                contract = requirement_matrix_domain.matrix_conclusion_contract(node)
                self.assertEqual(contract.effective_conclusion, "")
                self.assertEqual(contract.conclusion_source.value, expected_source)
                self.assertFalse(contract.overrides_ai)

    def test_explicit_not_started_status_cannot_expose_ai_conclusion(self) -> None:
        node = _analysis_contract_node(
            analysis_result_state="CURRENT",
            analysis_status="NOT_STARTED",
            ai_assessment={
                "decision": "ACCEPT",
                "reason": "不应绕过未开始状态",
                "assessment_mode": "LOCAL_RULE",
            },
        )

        contract = requirement_matrix_domain.matrix_conclusion_contract(node)

        self.assertEqual(contract.effective_conclusion, "")
        self.assertEqual(contract.conclusion_source.value, "NONE")

    def test_legacy_node_without_result_state_uses_payload_currency_inference(
        self,
    ) -> None:
        # Historical raw snapshots may omit both currency and status fields.
        # Their AI payload remains the only available backward-compatible signal.
        contract = requirement_matrix_domain.matrix_conclusion_contract(
            {
                "matrix_visibility": "VISIBLE",
                "disposition": "ANALYZE",
                "analysis_eligibility": "ELIGIBLE",
                "ai_assessment": {
                    "decision": "ACCEPT",
                    "assessment_mode": "LOCAL_RULE",
                },
            }
        )

        self.assertEqual(contract.effective_conclusion, "ACCEPT")
        self.assertEqual(contract.conclusion_source.value, "AI_DEFAULT")

    def test_snapshot_conclusion_summary_uses_only_analysis_targets(self) -> None:
        nodes = (
            _analysis_contract_node(
                analysis_result_state="CURRENT",
                ai_assessment={
                    "decision": "ACCEPT",
                    "assessment_mode": "LOCAL_RULE",
                },
            ),
            _analysis_contract_node(
                analysis_result_state="CURRENT",
                ai_assessment={
                    "decision": "ACCEPT",
                    "assessment_mode": "LOCAL_RULE",
                },
                manual_decision={"decision": "REJECT"},
            ).model_copy(update={"node_id": "analysis-node-2"}),
            _analysis_contract_node(
                analysis_result_state="CURRENT",
                manual_decision={"decision": "ACCEPT"},
            ).model_copy(update={"node_id": "analysis-node-3"}),
            _analysis_contract_node(
                analysis_result_state="CURRENT",
                ai_assessment={
                    "reason": "尚无结论",
                    "assessment_mode": "LOCAL_RULE",
                },
            ).model_copy(update={"node_id": "analysis-node-4"}),
            _analysis_contract_node().model_copy(
                update={
                    "node_id": "retained-node",
                    "requirement_entry_id": None,
                    "requirement_id": None,
                    "disposition": MatrixNodeDisposition.RETAIN,
                    "analysis_eligibility": MatrixAnalysisEligibility.NOT_ELIGIBLE,
                    "analysis_status": MatrixAnalysisStatus.NOT_APPLICABLE,
                }
            ),
        )

        summary = requirement_matrix_domain.summarize_effective_conclusions(nodes)

        self.assertEqual(summary.analysis_target_count, 4)
        self.assertEqual(summary.effective_conclusion_count, 3)
        self.assertEqual(summary.missing_effective_conclusion_count, 1)
        self.assertEqual(summary.effective_conclusion_coverage, 0.75)
        self.assertEqual(summary.ai_default_count, 1)
        self.assertEqual(summary.human_override_count, 1)
        self.assertEqual(summary.human_native_count, 1)


class RequirementMatrixProjectionTests(unittest.TestCase):
    @staticmethod
    def _historical_record_projection_fixture() -> tuple[
        SimpleNamespace,
        tuple[MatrixNodeStateV1, ...],
    ]:
        parent = _project(
            role="STRUCTURE_PROXY",
            entry_kind="REFERENCE",
            child_ids=("record-node-1", "record-node-2"),
        ).model_copy(
            update={
                "node_id": "parent",
                "parent_id": None,
                "source_order": 0,
                "requirement_entry_id": None,
                "requirement_id": None,
            }
        )
        record_nodes = tuple(
            _project(role="TABLE_RECORD", entry_kind="REFERENCE").model_copy(
                update={
                    "node_id": f"record-node-{index}",
                    "parent_id": "parent",
                    "source_order": index,
                    "table_record_id": f"record-{index}",
                    "logical_table_ids": ("logical-table-1",),
                }
            )
            for index in (1, 2)
        )
        source_ref = SimpleNamespace(
            model_dump=lambda **_kwargs: {
                "page_start": 2,
                "page_end": 2,
                "bbox": [10, 20, 200, 260],
            }
        )
        projection = SimpleNamespace(
            mode="RECORDS",
            table_fact=SimpleNamespace(
                logical_table_id="logical-table-1",
                source_ir_id="source-ir-1",
                owner_structure_node_id="owner-1",
                whole_table_source_refs=(source_ref,),
            ),
            records=(
                SimpleNamespace(record_id="record-1"),
                SimpleNamespace(record_id="record-2"),
            ),
        )
        tree = SimpleNamespace(
            table_record_projections=(projection,),
            table_evidence_bindings=(
                SimpleNamespace(
                    logical_table_id="logical-table-1",
                    record_id=None,
                ),
                SimpleNamespace(
                    logical_table_id="logical-table-1",
                    record_id="record-1",
                ),
                SimpleNamespace(
                    logical_table_id="logical-table-1",
                    record_id="record-2",
                ),
            ),
        )
        return tree, (parent, *record_nodes)

    def test_historical_records_restore_one_read_only_table_parent(self) -> None:
        tree, original = self._historical_record_projection_fixture()

        restored = _restore_historical_table_containers(
            tree=tree,
            nodes=original,
        )

        table = next(
            node for node in restored if node.display_role is MatrixNodeKind.TABLE
        )
        by_id = {node.node_id: node for node in restored}
        self.assertEqual(by_id["parent"].child_ids, (table.node_id,))
        self.assertEqual(table.child_ids, ("record-node-1", "record-node-2"))
        self.assertEqual(table.disposition, MatrixNodeDisposition.RETAIN)
        self.assertEqual(
            table.analysis_eligibility,
            MatrixAnalysisEligibility.NOT_ELIGIBLE,
        )
        self.assertEqual(by_id["record-node-1"].parent_id, table.node_id)
        self.assertEqual(by_id["record-node-2"].parent_id, table.node_id)
        self.assertEqual(by_id["record-node-1"].depth, original[1].depth + 1)
        self.assertEqual(
            [
                node.node_id
                for node in restored
                if node.display_role is not MatrixNodeKind.TABLE
            ],
            [node.node_id for node in original],
        )

    def test_historical_table_parent_restore_fails_closed_without_whole_evidence(
        self,
    ) -> None:
        tree, original = self._historical_record_projection_fixture()
        tree.table_evidence_bindings = tuple(
            binding
            for binding in tree.table_evidence_bindings
            if binding.record_id is not None
        )

        self.assertEqual(
            _restore_historical_table_containers(tree=tree, nodes=original),
            original,
        )

    def test_parent_projection_keeps_only_direct_evidence(self) -> None:
        shared = {"kind": "text", "id": "child-text", "text": "子条款正文"}
        parent = _tree_node(
            node_id="parent",
            role="STRUCTURE_PROXY",
            kind="SECTION",
            child_ids=("child",),
            label="9 技术要求",
        ).model_copy(
            update={
                "parent_id": None,
                "content_blocks": (
                    {"kind": "text", "id": "intro", "text": "章节引言"},
                    shared,
                ),
                "figure_refs": (
                    {"id": "intro-figure"},
                    {"id": "child-figure"},
                ),
            }
        )
        child = _tree_node(
            node_id="child",
            role="STRUCTURE_PROXY",
            kind="CLAUSE",
        ).model_copy(
            update={
                "parent_id": "parent",
                "depth": 2,
                "content_blocks": (shared,),
                "figure_refs": ({"id": "child-figure"},),
            }
        )

        projected_parent, projected_child = _direct_presentation_tree_nodes(
            (parent, child)
        )

        self.assertEqual(
            [block["id"] for block in projected_parent.content_blocks],
            ["intro"],
        )
        self.assertEqual(
            [figure["id"] for figure in projected_parent.figure_refs],
            ["intro-figure"],
        )
        self.assertEqual(projected_child.content_blocks, (shared,))

    def test_parent_projection_deduplicates_source_fact_before_wrapper_id(
        self,
    ) -> None:
        parent = _tree_node(
            node_id="parent",
            role="STRUCTURE_PROXY",
            kind="SECTION",
            child_ids=("child",),
        ).model_copy(
            update={
                "parent_id": None,
                "content_blocks": (
                    {
                        "kind": "text",
                        "id": "parent-wrapper",
                        "source_id": "source-paragraph-1",
                        "text": "同一来源正文",
                    },
                ),
            }
        )
        child = _tree_node(
            node_id="child",
            role="STRUCTURE_PROXY",
            kind="CLAUSE",
        ).model_copy(
            update={
                "parent_id": "parent",
                "depth": 2,
                "content_blocks": (
                    {
                        "kind": "text",
                        "id": "child-wrapper",
                        "source_id": "source-paragraph-1",
                        "text": "同一来源正文",
                    },
                ),
            }
        )

        projected_parent, projected_child = _direct_presentation_tree_nodes(
            (parent, child)
        )

        self.assertEqual(projected_parent.content_blocks, ())
        self.assertEqual(
            projected_child.content_blocks[0]["id"],
            "child-wrapper",
        )

    def test_human_heading_separates_number_and_suppresses_duplicate_title(
        self,
    ) -> None:
        node = _tree_node(
            node_id="section-1",
            role="STRUCTURE_PROXY",
            kind="SECTION",
            label="1 说明",
        ).model_copy(
            update={
                "summary": "1 说明",
                "content_blocks": ({"kind": "text", "text": "1 说明"},),
            }
        )

        presentation = _matrix_human_presentation(
            node,
            table_context={},
            resolved_table_record_id=None,
        )

        self.assertEqual(presentation["source_number"], "1")
        self.assertEqual(presentation["display_title"], "说明")
        self.assertEqual(presentation["display_text"], "")

    def test_table_record_human_text_preserves_equal_values_by_position(
        self,
    ) -> None:
        node = _tree_node(
            node_id="record-1",
            role="TABLE_RECORD",
            label="表格记录",
        ).model_copy(update={"summary": "10 | 10"})

        presentation = _matrix_human_presentation(
            node,
            table_context={"records_by_id": {}},
            resolved_table_record_id="record-1",
        )

        self.assertEqual(presentation["display_text"], "10\n10")

    def test_container_never_falls_back_to_descendant_rollup_summary(self) -> None:
        node = _tree_node(
            node_id="section-9",
            role="STRUCTURE_PROXY",
            kind="SECTION",
            child_ids=("section-9-1",),
            label="9 技术要求",
        ).model_copy(
            update={
                "summary": "9 技术要求\n9.1 子条款正文不应重复到父级",
                "content_blocks": ({"kind": "text", "text": "9 技术要求"},),
            }
        )

        presentation = _matrix_human_presentation(
            node,
            table_context={},
            resolved_table_record_id=None,
        )
        projected = _project_matrix_node(
            tree_node=node,
            requirement=None,
            persisted_state=None,
            logical_table_ids=(),
        )

        self.assertEqual(presentation["display_text"], "")
        self.assertEqual(projected.requirement_text, "")

    def test_document_wrapper_and_exact_toc_titles_are_system_non_business(self) -> None:
        document = _tree_node(
            node_id="document",
            role="STRUCTURE",
            kind="DOCUMENT",
            label="客户规范.pdf",
        )
        toc = _tree_node(
            node_id="toc",
            role="STRUCTURE",
            kind="GROUP",
            label="目 录",
        )
        business_clause = _tree_node(
            node_id="business",
            role="STRUCTURE",
            kind="CLAUSE",
            label="目录管理要求",
        )

        self.assertTrue(_is_system_non_business(document))
        self.assertTrue(_is_system_non_business(toc))
        self.assertFalse(_is_system_non_business(business_clause))

    def test_table_record_ai_header_path_follows_native_cell_order(self) -> None:
        record = {
            "row_bindings": [{"cell_ids": ["value-0", "value-1"]}],
            "header_bindings": [
                {"text": "0.131", "cell_ids": ["header-2"]},
                {"text": "活塞速度 / 电流", "cell_ids": ["header-0"]},
                {"text": "0.052", "cell_ids": ["header-1"]},
            ],
        }
        cells = [
            {"id": "header-0", "text": "活塞速度 / 电流"},
            {"id": "header-1", "text": "0.052"},
            {"id": "header-2", "text": "0.131"},
            {"id": "value-0", "text": "I Start"},
            {"id": "value-1", "text": "±17.5%"},
        ]

        self.assertEqual(
            _record_ai_rows(record, cells),
            [["活塞速度 / 电流 / 0.052 / 0.131", "I Start | ±17.5%"]],
        )

    def test_semantic_requirement_is_analyzed_by_default(self) -> None:
        node = _project(role="REQUIREMENT", entry_kind="REQUIREMENT")

        self.assertEqual(node.analysis_eligibility, MatrixAnalysisEligibility.ELIGIBLE)
        self.assertEqual(node.disposition, MatrixNodeDisposition.ANALYZE)
        self.assertEqual(node.analysis_status, MatrixAnalysisStatus.NOT_STARTED)
        self.assertEqual(
            node.analysis_result_state,
            MatrixAnalysisResultState.NONE,
        )
        payload = node.model_dump(mode="json")
        for field in (
            "display_role",
            "matrix_visibility",
            "analysis_eligibility",
            "disposition_reason",
            "ai_assessment",
            "manual_decision",
            "save_revision",
        ):
            self.assertIn(field, payload)
        for legacy_field in (
            "kind",
            "visibility",
            "analysis_eligible",
            "ai_decision",
            "final_decision",
            "state_revision",
        ):
            self.assertNotIn(legacy_field, payload)

    def test_ai_basis_and_clarification_are_preserved_in_matrix_snapshot(self) -> None:
        requirement_id = "requirement-1"
        node = _project_matrix_node(
            tree_node=_tree_node(
                node_id="node-1",
                role="REQUIREMENT",
                requirement_id=requirement_id,
            ),
            requirement=_requirement(
                requirement_id,
                entry_kind="REQUIREMENT",
                assessment_mode="LOCAL_RULE",
                ai_decision="NEED_CLARIFICATION",
                compliance_status="INSUFFICIENT_INFORMATION",
                ai_reason="缺少目标容差。",
                analysis_basis="检索到 0 条可用证据。",
                clarification_question="请客户确认目标容差。",
                ai_confidence=0.61,
            ),
            persisted_state=None,
            logical_table_ids=(),
        )

        self.assertEqual(node.analysis_status, MatrixAnalysisStatus.ASSESSED)
        self.assertIsNotNone(node.ai_assessment)
        self.assertEqual(node.ai_assessment.analysis_basis, "检索到 0 条可用证据。")
        self.assertEqual(
            node.ai_assessment.clarification_question,
            "请客户确认目标容差。",
        )

    def test_direct_content_and_structure_leaf_are_candidates_but_retained(self) -> None:
        for role in ("DIRECT_CONTENT", "STRUCTURE_PROXY"):
            with self.subTest(role=role):
                node = _project(role=role, entry_kind="REFERENCE")
                self.assertEqual(
                    node.analysis_eligibility,
                    MatrixAnalysisEligibility.ELIGIBLE,
                )
                self.assertEqual(node.disposition, MatrixNodeDisposition.RETAIN)
                self.assertEqual(
                    node.analysis_status,
                    MatrixAnalysisStatus.NOT_APPLICABLE,
                )

                opted_in = _project(
                    role=role,
                    entry_kind="REFERENCE",
                    persisted_disposition="ANALYZE",
                )
                self.assertEqual(opted_in.disposition, MatrixNodeDisposition.ANALYZE)
                self.assertEqual(
                    opted_in.analysis_status,
                    MatrixAnalysisStatus.NOT_STARTED,
                )
                self.assertEqual(opted_in.disposition_reason, "人工处置说明")

    def test_rollups_containers_and_table_rows_are_not_direct_candidates(self) -> None:
        cases = (
            {
                "role": "STRUCTURE_PROXY",
                "entry_kind": "REFERENCE",
                "child_ids": ("child-1",),
            },
            {
                "role": "REQUIREMENT",
                "entry_kind": "REQUIREMENT",
                "decision_mode": "ROLLUP",
            },
            {"role": "WHOLE_TABLE_REVIEW", "entry_kind": "REFERENCE"},
            {"role": "TABLE_RECORD", "entry_kind": "REFERENCE"},
        )
        for case in cases:
            with self.subTest(case=case):
                node = _project(**case)
                self.assertEqual(
                    node.analysis_eligibility,
                    MatrixAnalysisEligibility.NOT_ELIGIBLE,
                )
                self.assertEqual(node.disposition, MatrixNodeDisposition.RETAIN)
                self.assertEqual(
                    node.analysis_status,
                    MatrixAnalysisStatus.NOT_APPLICABLE,
                )

    def test_visibility_disposition_and_eligibility_are_independent(self) -> None:
        excluded = _project(role="REQUIREMENT", entry_kind="EXCLUDED")
        self.assertEqual(
            excluded.matrix_visibility,
            MatrixNodeVisibility.HUMAN_EXCLUDED,
        )
        self.assertEqual(excluded.disposition, MatrixNodeDisposition.EXCLUDE)
        self.assertEqual(
            excluded.analysis_eligibility,
            MatrixAnalysisEligibility.ELIGIBLE,
        )
        self.assertEqual(
            excluded.analysis_status,
            MatrixAnalysisStatus.NOT_APPLICABLE,
        )

        system = _project(
            role="STRUCTURE_PROXY",
            # A legacy EXCLUDED row must not be misrepresented as a human
            # action when immutable structure proves it is system noise.
            entry_kind="EXCLUDED",
            classification="NOISE",
        )
        self.assertEqual(
            system.matrix_visibility,
            MatrixNodeVisibility.SYSTEM_NON_BUSINESS,
        )
        self.assertEqual(
            system.analysis_eligibility,
            MatrixAnalysisEligibility.NOT_ELIGIBLE,
        )
        self.assertEqual(system.disposition, MatrixNodeDisposition.RETAIN)

    def test_manual_assessment_never_masquerades_as_ai_advice(self) -> None:
        requirement_id = "requirement-1"
        requirement = _requirement(requirement_id, entry_kind="REQUIREMENT")
        requirement.update(
            {
                "assessment_mode": "manual",
                "ai_decision": "NEED_CLARIFICATION",
                "ai_reason": "No AI assessment available.",
                "ai_confidence": 0,
                "final_decision": "ACCEPT",
                "engineer_reason": "工程师已确认。",
                "assessment_updated_at": "2026-07-31T02:00:00+00:00",
            }
        )
        node = _project_matrix_node(
            tree_node=_tree_node(
                node_id="node-1",
                role="REQUIREMENT",
                requirement_id=requirement_id,
            ),
            requirement=requirement,
            persisted_state=None,
            logical_table_ids=(),
        )
        self.assertIsNone(node.ai_assessment)
        self.assertIsNotNone(node.manual_decision)
        self.assertEqual(node.manual_decision.decision, "ACCEPT")
        self.assertEqual(node.analysis_status, MatrixAnalysisStatus.DECIDED)
        self.assertEqual(
            node.analysis_result_state,
            MatrixAnalysisResultState.CURRENT,
        )

    def test_stale_assessment_is_history_not_current_matrix_result(self) -> None:
        requirement_id = "requirement-1"
        requirement = _requirement(requirement_id, entry_kind="REQUIREMENT")
        requirement.update(
            {
                "assessment_mode": "LOCAL_RULE",
                "ai_decision": "ACCEPT",
                "compliance_status": "COMPLIANT",
                "ai_reason": "旧版本证据曾支持该结论。",
                "ai_confidence": 0.94,
                "final_decision": "ACCEPT",
                "engineer_reason": "旧版本人工确认。",
                "assessment_updated_at": "2026-07-31T02:00:00+00:00",
                "assessment_reference_status": "STALE_REFERENCE",
            }
        )

        node = _project_matrix_node(
            tree_node=_tree_node(
                node_id="node-1",
                role="REQUIREMENT",
                requirement_id=requirement_id,
            ),
            requirement=requirement,
            persisted_state=None,
            logical_table_ids=(),
        ).model_copy(update={"parent_id": None})

        self.assertEqual(
            node.analysis_result_state,
            MatrixAnalysisResultState.STALE_REFERENCE,
        )
        self.assertEqual(node.analysis_status, MatrixAnalysisStatus.NOT_STARTED)
        self.assertIsNone(node.ai_assessment)
        self.assertIsNone(node.manual_decision)
        self.assertEqual(node.compliance_status, "")
        self.assertEqual(node.stale_ai_assessment.decision, "ACCEPT")
        self.assertEqual(node.stale_manual_decision.decision, "ACCEPT")

        counts = _matrix_counts(nodes=(node,), logical_tables=())
        snapshot = RequirementMatrixSnapshotV1(
            state=MatrixSnapshotState.READY,
            project_id="project-1",
            document_id="document-1",
            requirement_set_id="set-2",
            requirement_set_status="DRAFT",
            root_node_ids=(node.node_id,),
            nodes=(node,),
            counts=counts,
        )
        self.assertEqual(snapshot.counts.assessed_nodes, 0)
        self.assertEqual(snapshot.counts.decided_nodes, 0)
        self.assertEqual(snapshot.counts.stale_result_nodes, 1)
        payload = snapshot.model_dump(mode="json")
        stale_payload = payload["nodes"][0]
        self.assertIsNone(stale_payload["ai_assessment"])
        self.assertIsNone(stale_payload["manual_decision"])
        self.assertEqual(
            stale_payload["stale_ai_assessment"]["compliance_status"],
            "COMPLIANT",
        )

        stale_payload["ai_assessment"] = stale_payload["stale_ai_assessment"]
        with self.assertRaisesRegex(
            ValidationError,
            "cannot masquerade as current results",
        ):
            MatrixNodeStateV1.model_validate(stale_payload)

    def test_analyze_disposition_is_rejected_for_non_candidate_shape(self) -> None:
        with self.assertRaises(ValidationError):
            _project(
                role="TABLE_RECORD",
                entry_kind="REFERENCE",
                persisted_disposition="ANALYZE",
            )

    def test_patch_analyze_rejects_non_candidate_before_persistence(self) -> None:
        node = _project(
            role="TABLE_RECORD",
            entry_kind="REFERENCE",
        ).model_copy(
            update={"logical_table_ids": ("logical-table-1",)}
        )
        matrix = RequirementMatrixSnapshotV1(
            state=MatrixSnapshotState.READY,
            project_id="project-1",
            document_id="document-1",
            requirement_set_id="set-1",
            requirement_set_status="DRAFT",
            root_node_ids=(node.node_id,),
            nodes=(node.model_copy(update={"parent_id": None}),),
            logical_tables=(_logical_table(node_ids=(node.node_id,)),),
            counts=MatrixCountsV1(
                total_nodes=1,
                visible_nodes=1,
                retained_nodes=1,
                logical_tables=1,
            ),
        )
        with patch(
            "app.application.requirement_matrix.build_requirement_matrix_snapshot",
            return_value=matrix,
        ):
            with self.assertRaises(ApplicationError) as raised:
                patch_requirement_matrix_node_disposition(
                    "project-1",
                    "document-1",
                    "set-1",
                    "node-1",
                    MatrixDispositionPatchV1(
                        disposition=MatrixNodeDisposition.ANALYZE,
                        reason="需要分析",
                        expected_revision=0,
                    ),
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "matrix_node_not_analysis_candidate",
        )

    def test_mutation_contracts_require_expected_revision(self) -> None:
        with self.assertRaises(ValidationError):
            MatrixDispositionPatchV1(disposition=MatrixNodeDisposition.RETAIN)
        with self.assertRaises(ValidationError):
            MatrixDecisionRequestV1(
                final_decision="ACCEPT",
                engineer_reason="工程确认",
            )

    def test_snapshot_rejects_incomplete_hierarchy_and_count_drift(self) -> None:
        root = _project(
            role="STRUCTURE_PROXY",
            entry_kind="REFERENCE",
        ).model_copy(
            update={
                "node_id": "root",
                "parent_id": None,
                "requirement_entry_id": "root-entry",
                "requirement_id": "root-entry",
                "source_order": 0,
            }
        )
        child = _project(
            role="REQUIREMENT",
            entry_kind="REQUIREMENT",
        ).model_copy(
            update={
                "node_id": "child",
                "parent_id": "root",
                "requirement_entry_id": "child-entry",
                "requirement_id": "child-entry",
                "source_order": 1,
            }
        )
        with self.assertRaisesRegex(
            ValidationError,
            "parent must reference every child",
        ):
            RequirementMatrixSnapshotV1(
                state=MatrixSnapshotState.READY,
                project_id="project-1",
                document_id="document-1",
                requirement_set_id="set-1",
                requirement_set_status="DRAFT",
                root_node_ids=("root",),
                nodes=(root, child),
                counts=MatrixCountsV1(total_nodes=2),
            )

        with self.assertRaisesRegex(
            ValidationError,
            "counts do not match node states",
        ):
            RequirementMatrixSnapshotV1(
                state=MatrixSnapshotState.READY,
                project_id="project-1",
                document_id="document-1",
                requirement_set_id="set-1",
                requirement_set_status="DRAFT",
                root_node_ids=("root",),
                nodes=(root,),
                counts=MatrixCountsV1(total_nodes=1),
            )

    def test_snapshot_rejects_node_reference_to_unknown_logical_table(self) -> None:
        root = _project(
            role="STRUCTURE_PROXY",
            entry_kind="REFERENCE",
        ).model_copy(
            update={
                "parent_id": None,
                "logical_table_ids": ("logical-table-missing",),
            }
        )

        with self.assertRaisesRegex(
            ValidationError,
            "node references a logical table outside logical_tables",
        ):
            RequirementMatrixSnapshotV1(
                state=MatrixSnapshotState.READY,
                project_id="project-1",
                document_id="document-1",
                requirement_set_id="set-1",
                requirement_set_status="DRAFT",
                root_node_ids=(root.node_id,),
                nodes=(root,),
                counts=MatrixCountsV1(
                    total_nodes=1,
                    visible_nodes=1,
                    retained_nodes=1,
                ),
            )

    def test_snapshot_rejects_logical_table_projection_metadata_drift(self) -> None:
        table_node = _project(
            role="WHOLE_TABLE_REVIEW",
            entry_kind="REFERENCE",
        ).model_copy(
            update={
                "parent_id": None,
                "logical_table_ids": ("logical-table-1",),
            }
        )
        valid = _logical_table(node_ids=(table_node.node_id,))
        drifted_tables = (
            (
                "projection_id must match projection",
                valid.model_copy(update={"projection_id": "projection-drift"}),
            ),
            (
                "projection_mode must match projection",
                valid.model_copy(update={"projection_mode": "WHOLE_TABLE"}),
            ),
            (
                "physical table IDs must match projection",
                valid.model_copy(
                    update={"physical_table_ids": ("physical-table-drift",)}
                ),
            ),
            (
                "record_count must match projection",
                valid.model_copy(update={"record_count": 2}),
            ),
        )

        for message, logical_table in drifted_tables:
            with self.subTest(message=message), self.assertRaisesRegex(
                ValidationError,
                message,
            ):
                RequirementMatrixSnapshotV1(
                    state=MatrixSnapshotState.READY,
                    project_id="project-1",
                    document_id="document-1",
                    requirement_set_id="set-1",
                    root_node_ids=(table_node.node_id,),
                    nodes=(table_node,),
                    logical_tables=(logical_table,),
                    counts=MatrixCountsV1(
                        total_nodes=1,
                        visible_nodes=1,
                        retained_nodes=1,
                        logical_tables=1,
                    ),
                )

    def test_snapshot_requires_bidirectional_logical_table_node_membership(
        self,
    ) -> None:
        table_node = _project(
            role="WHOLE_TABLE_REVIEW",
            entry_kind="REFERENCE",
        ).model_copy(
            update={
                "parent_id": None,
                "logical_table_ids": ("logical-table-1",),
            }
        )

        with self.assertRaisesRegex(
            ValidationError,
            "node IDs must exactly match node references",
        ):
            RequirementMatrixSnapshotV1(
                state=MatrixSnapshotState.READY,
                project_id="project-1",
                document_id="document-1",
                requirement_set_id="set-1",
                root_node_ids=(table_node.node_id,),
                nodes=(table_node,),
                logical_tables=(_logical_table(node_ids=()),),
                counts=MatrixCountsV1(
                    total_nodes=1,
                    visible_nodes=1,
                    retained_nodes=1,
                    logical_tables=1,
                ),
            )

    def test_record_bound_node_cannot_cross_logical_table_boundaries(self) -> None:
        record_node = _project(
            role="TABLE_RECORD",
            entry_kind="REFERENCE",
        ).model_copy(
            update={
                "parent_id": None,
                "logical_table_ids": ("logical-table-1",),
            }
        )
        primary = _logical_table(node_ids=(record_node.node_id,))
        polluted = _logical_table(
            logical_table_id="logical-table-2",
            projection_id="projection-2",
            physical_table_ids=("physical-table-2",),
            node_ids=(),
            record_ids=("record-1",),
        )

        with self.assertRaisesRegex(
            ValidationError,
            "must belong to exactly one referenced RECORDS table",
        ):
            RequirementMatrixSnapshotV1(
                state=MatrixSnapshotState.READY,
                project_id="project-1",
                document_id="document-1",
                requirement_set_id="set-1",
                root_node_ids=(record_node.node_id,),
                nodes=(record_node,),
                logical_tables=(primary, polluted),
                counts=MatrixCountsV1(
                    total_nodes=1,
                    visible_nodes=1,
                    retained_nodes=1,
                    logical_tables=2,
                ),
            )

    def test_record_bound_roles_reference_exactly_one_logical_table(self) -> None:
        for role, entry_kind in (
            ("TABLE_RECORD", "REFERENCE"),
            ("REQUIREMENT", "REQUIREMENT"),
        ):
            with self.subTest(role=role):
                node = _project(role=role, entry_kind=entry_kind).model_copy(
                    update={
                        "parent_id": None,
                        "table_record_id": "record-1",
                        "logical_table_ids": (
                            "logical-table-1",
                            "logical-table-2",
                        ),
                    }
                )
                tables = (
                    _logical_table(node_ids=(node.node_id,)),
                    _logical_table(
                        logical_table_id="logical-table-2",
                        projection_id="projection-2",
                        physical_table_ids=("physical-table-2",),
                        node_ids=(node.node_id,),
                        record_ids=("record-2",),
                    ),
                )

                with self.assertRaisesRegex(
                    ValidationError,
                    "must reference exactly one logical table",
                ):
                    RequirementMatrixSnapshotV1(
                        state=MatrixSnapshotState.READY,
                        project_id="project-1",
                        document_id="document-1",
                        requirement_set_id="set-1",
                        root_node_ids=(node.node_id,),
                        nodes=(node,),
                        logical_tables=tables,
                        counts=MatrixCountsV1(
                            total_nodes=1,
                            visible_nodes=1,
                            analyze_nodes=int(
                                node.disposition
                                is MatrixNodeDisposition.ANALYZE
                            ),
                            retained_nodes=int(
                                node.disposition
                                is MatrixNodeDisposition.RETAIN
                            ),
                            analysis_eligible_nodes=int(
                                node.analysis_eligibility
                                is MatrixAnalysisEligibility.ELIGIBLE
                            ),
                            logical_tables=2,
                        ),
                    )

    def test_whole_table_container_may_reference_multiple_logical_tables(
        self,
    ) -> None:
        table_node = _project(
            role="WHOLE_TABLE_REVIEW",
            entry_kind="REFERENCE",
        ).model_copy(
            update={
                "parent_id": None,
                "logical_table_ids": (
                    "logical-table-1",
                    "logical-table-2",
                ),
            }
        )
        tables = (
            _logical_table(
                projection_mode="WHOLE_TABLE",
                node_ids=(table_node.node_id,),
                record_ids=(),
            ),
            _logical_table(
                logical_table_id="logical-table-2",
                projection_id="projection-2",
                projection_mode="WHOLE_TABLE",
                physical_table_ids=("physical-table-2",),
                node_ids=(table_node.node_id,),
                record_ids=(),
            ),
        )

        matrix = RequirementMatrixSnapshotV1(
            state=MatrixSnapshotState.READY,
            project_id="project-1",
            document_id="document-1",
            requirement_set_id="set-1",
            root_node_ids=(table_node.node_id,),
            nodes=(table_node,),
            logical_tables=tables,
            counts=MatrixCountsV1(
                total_nodes=1,
                visible_nodes=1,
                retained_nodes=1,
                logical_tables=2,
            ),
        )

        self.assertEqual(len(matrix.logical_tables), 2)

    def test_missing_frozen_tree_returns_reextraction_state(self) -> None:
        requirement_set = {
            "id": "set-1",
            "project_id": "project-1",
            "sor_document_id": "document-1",
            "status": "DRAFT",
        }
        with (
            patch(
                "app.application.requirement_matrix._require_matrix_scope",
                return_value=({}, requirement_set),
            ),
            patch(
                "app.application.requirement_matrix.load_requirement_set_tree_snapshot",
                side_effect=RequirementTreeSnapshotError("missing"),
            ),
        ):
            matrix = build_requirement_matrix_snapshot(
                "project-1",
                "document-1",
                "set-1",
            )
        self.assertEqual(matrix.state, MatrixSnapshotState.REQUIRES_REEXTRACTION)
        self.assertEqual(matrix.nodes, ())

    def test_ai_input_uses_only_the_matrix_target_in_tree_order(self) -> None:
        root = _project(
            role="STRUCTURE_PROXY",
            entry_kind="REFERENCE",
            child_ids=("second", "first"),
        ).model_copy(
            update={
                "node_id": "root",
                "parent_id": None,
                "requirement_entry_id": "root-entry",
                "requirement_id": "root-entry",
                "source_order": 0,
            }
        )
        first = _project(
            role="DIRECT_CONTENT",
            entry_kind="REFERENCE",
            persisted_disposition="ANALYZE",
        ).model_copy(
            update={
                "node_id": "first",
                "parent_id": "root",
                "requirement_entry_id": "first-entry",
                "requirement_id": "first-entry",
                "source_order": 1,
            }
        )
        second = _project(
            role="REQUIREMENT",
            entry_kind="REQUIREMENT",
        ).model_copy(
            update={
                "node_id": "second",
                "parent_id": "root",
                "requirement_entry_id": "second-entry",
                "requirement_id": "second-entry",
                "source_order": 2,
                "disposition": MatrixNodeDisposition.RETAIN,
                "analysis_status": MatrixAnalysisStatus.NOT_APPLICABLE,
            }
        )
        matrix = RequirementMatrixSnapshotV1(
            state=MatrixSnapshotState.READY,
            project_id="project-1",
            document_id="document-1",
            requirement_set_id="set-1",
            requirement_set_status="LOCKED",
            root_node_ids=("root",),
            nodes=(root, second, first),
            counts=MatrixCountsV1(
                total_nodes=3,
                visible_nodes=3,
                analyze_nodes=1,
                retained_nodes=2,
                analysis_eligible_nodes=2,
            ),
        )
        selected = select_matrix_analysis_requirements(
            matrix,
            [
                {"id": "second-entry", "entry_kind": "REQUIREMENT"},
                {
                    "id": "first-entry",
                    "entry_kind": "REFERENCE",
                    "raw_text": "数据库中的后续可变副本",
                },
                {"id": "root-entry", "entry_kind": "REFERENCE"},
            ],
        )

        self.assertEqual([row["id"] for row in selected], ["first-entry"])
        self.assertEqual(selected[0]["matrix_node_id"], "first")
        self.assertEqual(selected[0]["matrix_disposition"], "ANALYZE")
        self.assertEqual(selected[0]["entry_kind"], "REFERENCE")
        self.assertEqual(selected[0]["raw_text"], "系统应记录结果。")
        self.assertEqual(selected[0]["parent_section_label"], "测试节点")
        self.assertEqual(selected[0]["ancestor_path"][0]["node_id"], "root")

    def test_ai_input_preserves_frozen_child_ids_when_source_orders_tie(
        self,
    ) -> None:
        root = _project(
            role="STRUCTURE_PROXY",
            entry_kind="REFERENCE",
            child_ids=("z-frozen-first", "a-frozen-second"),
        ).model_copy(
            update={
                "node_id": "root",
                "parent_id": None,
                "requirement_entry_id": "root-entry",
                "requirement_id": "root-entry",
                "source_order": 0,
            }
        )
        frozen_first = _project(
            role="REQUIREMENT",
            entry_kind="REQUIREMENT",
        ).model_copy(
            update={
                "node_id": "z-frozen-first",
                "parent_id": "root",
                "requirement_entry_id": "frozen-first-entry",
                "requirement_id": "frozen-first-entry",
                "source_order": 7,
            }
        )
        frozen_second = _project(
            role="REQUIREMENT",
            entry_kind="REQUIREMENT",
        ).model_copy(
            update={
                "node_id": "a-frozen-second",
                "parent_id": "root",
                "requirement_entry_id": "frozen-second-entry",
                "requirement_id": "frozen-second-entry",
                "source_order": 7,
            }
        )
        matrix = RequirementMatrixSnapshotV1(
            state=MatrixSnapshotState.READY,
            project_id="project-1",
            document_id="document-1",
            requirement_set_id="set-1",
            requirement_set_status="LOCKED",
            tree_sha256="a" * 64,
            root_node_ids=("root",),
            # Collection order and lexical IDs deliberately disagree with the
            # immutable sibling sequence in root.child_ids.
            nodes=(frozen_second, root, frozen_first),
            counts=MatrixCountsV1(
                total_nodes=3,
                visible_nodes=3,
                analyze_nodes=2,
                retained_nodes=1,
                analysis_eligible_nodes=2,
            ),
        )

        selected = select_matrix_analysis_requirements(
            matrix,
            [
                {
                    "id": "frozen-second-entry",
                    "entry_kind": "REQUIREMENT",
                },
                {
                    "id": "frozen-first-entry",
                    "entry_kind": "REQUIREMENT",
                },
                {"id": "root-entry", "entry_kind": "REFERENCE"},
            ],
        )

        self.assertEqual(
            [row["id"] for row in selected],
            ["frozen-first-entry", "frozen-second-entry"],
        )

    def test_table_record_detail_keeps_path_light_and_does_not_repeat_full_table(
        self,
    ) -> None:
        full_table_marker = "完整逻辑表只应由表格详情接口返回"
        record_block = {
            "kind": "table_record",
            "record_id": "record-1",
            "cells": [{"text": "当前记录"}],
        }
        duplicated_table = {
            "kind": "table",
            "id": "physical-table-1",
            "logical_table_id": "logical-table-1",
            "cells": [{"text": full_table_marker}],
        }
        root = _project(
            role="STRUCTURE_PROXY",
            entry_kind="REFERENCE",
            child_ids=("clause",),
        ).model_copy(
            update={
                "node_id": "root",
                "parent_id": None,
                "requirement_entry_id": None,
                "requirement_id": None,
                "source_order": 0,
                "content_blocks": ({"kind": "text", "text": "文档根"},),
            }
        )
        clause = _project(
            role="STRUCTURE_PROXY",
            entry_kind="REFERENCE",
            child_ids=("record",),
        ).model_copy(
            update={
                "node_id": "clause",
                "parent_id": "root",
                "requirement_entry_id": None,
                "requirement_id": None,
                "source_order": 1,
                "content_blocks": ({"kind": "text", "text": "所属条款"},),
            }
        )
        record = _project(
            role="TABLE_RECORD",
            entry_kind="REFERENCE",
        ).model_copy(
            update={
                "node_id": "record",
                "parent_id": "clause",
                "requirement_entry_id": "record-entry",
                "requirement_id": "record-entry",
                "source_order": 2,
                "content_blocks": (duplicated_table, record_block),
                "table_refs": (duplicated_table,),
                "logical_table_ids": ("logical-table-1",),
            }
        )
        logical_table = _logical_table(
            node_ids=(record.node_id,),
        )
        matrix = RequirementMatrixSnapshotV1(
            state=MatrixSnapshotState.READY,
            project_id="project-1",
            document_id="document-1",
            requirement_set_id="set-1",
            requirement_set_status="DRAFT",
            tree_sha256="a" * 64,
            root_node_ids=(root.node_id,),
            nodes=(record, root, clause),
            logical_tables=(logical_table,),
            counts=MatrixCountsV1(
                total_nodes=3,
                visible_nodes=3,
                retained_nodes=3,
                logical_tables=1,
            ),
        )
        frozen_record = _tree_node(
            node_id="record",
            role="TABLE_RECORD",
            requirement_id="record-entry",
        )
        frozen_clause = _tree_node(
            node_id="clause",
            role="STRUCTURE_PROXY",
            kind="CLAUSE",
        ).model_copy(update={"parent_id": "root"})
        frozen_root = _tree_node(
            node_id="root",
            role="STRUCTURE_PROXY",
            kind="DOCUMENT",
        ).model_copy(update={"parent_id": None})

        with (
            patch(
                "app.application.requirement_matrix.build_requirement_matrix_snapshot",
                return_value=matrix,
            ),
            patch(
                "app.application.requirement_matrix.load_requirement_set_tree_snapshot",
                return_value=SimpleNamespace(
                    nodes=(frozen_root, frozen_clause, frozen_record)
                ),
            ),
            patch(
                "app.application.requirement_matrix.db.get_requirement_set",
                return_value={"id": "set-1"},
            ),
            patch(
                "app.application.requirement_matrix.db.get_requirement",
                return_value={
                    "id": "record-entry",
                    "project_id": "project-1",
                    "sor_document_id": "document-1",
                    "requirement_set_id": "set-1",
                    "req_code": "REQ0001",
                    "entry_kind": "REFERENCE",
                },
            ),
            patch(
                "app.application.requirement_matrix.db.get_assessment",
                return_value=None,
            ),
        ):
            detail = get_requirement_matrix_node_detail(
                "project-1",
                "document-1",
                "set-1",
                "record",
            )
            clause_detail = get_requirement_matrix_node_detail(
                "project-1",
                "document-1",
                "set-1",
                "clause",
            )

        self.assertEqual(
            detail["node"]["content_blocks"],
            [record_block],
        )
        self.assertEqual(detail["node"]["table_refs"], [])
        for path_item in detail["path"]:
            self.assertEqual(path_item["content_blocks"], [])
            self.assertEqual(path_item["table_refs"], [])
            self.assertEqual(path_item["figure_refs"], [])
            self.assertEqual(path_item["source_refs"], [])
        self.assertNotIn(full_table_marker, repr(detail))
        self.assertIsNone(clause_detail["parent_clause"])

    def test_derived_historical_table_parent_has_a_safe_detail_payload(self) -> None:
        tree, original = self._historical_record_projection_fixture()
        restored = _restore_historical_table_containers(tree=tree, nodes=original)
        table = next(
            node for node in restored if node.display_role is MatrixNodeKind.TABLE
        )
        matrix = SimpleNamespace(
            state=MatrixSnapshotState.READY,
            nodes=restored,
            logical_tables=(),
        )

        with (
            patch(
                "app.application.requirement_matrix.build_requirement_matrix_snapshot",
                return_value=matrix,
            ),
            patch(
                "app.application.requirement_matrix.load_requirement_set_tree_snapshot",
                return_value=SimpleNamespace(nodes=()),
            ),
            patch(
                "app.application.requirement_matrix.db.get_requirement_set",
                return_value={"id": "set-1"},
            ),
        ):
            detail = get_requirement_matrix_node_detail(
                "project-1",
                "document-1",
                "set-1",
                table.node_id,
            )

        self.assertEqual(detail["node"]["display_role"], "TABLE")
        self.assertEqual(detail["node"]["summary"], "2 条表格条目")
        self.assertEqual(
            detail["tree_node"]["projection_role"],
            "DERIVED_TABLE_CONTAINER",
        )
        self.assertTrue(detail["tree_node"]["derived_read_model"])

    def test_batch_disposition_rejects_stale_tree_before_persistence(self) -> None:
        node = _project(role="REQUIREMENT", entry_kind="REQUIREMENT").model_copy(
            update={"parent_id": None}
        )
        matrix = RequirementMatrixSnapshotV1(
            state=MatrixSnapshotState.READY,
            project_id="project-1",
            document_id="document-1",
            requirement_set_id="set-1",
            requirement_set_status="DRAFT",
            tree_sha256="a" * 64,
            root_node_ids=(node.node_id,),
            nodes=(node,),
            counts=MatrixCountsV1(
                total_nodes=1,
                visible_nodes=1,
                analyze_nodes=1,
                analysis_eligible_nodes=1,
            ),
        )
        payload = MatrixDispositionBatchPatchV1(
            expected_tree_sha256="b" * 64,
            items=(
                MatrixDispositionBatchItemV1(
                    node_id=node.node_id,
                    disposition=MatrixNodeDisposition.RETAIN,
                    expected_revision=0,
                ),
            ),
        )

        with (
            patch(
                "app.application.requirement_matrix.build_requirement_matrix_snapshot",
                return_value=matrix,
            ),
            patch(
                "app.application.requirement_matrix.set_requirement_matrix_node_dispositions"
            ) as persist,
            self.assertRaises(ApplicationError) as raised,
        ):
            patch_requirement_matrix_dispositions(
                "project-1",
                "document-1",
                "set-1",
                payload,
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "matrix_tree_revision_conflict",
        )
        persist.assert_not_called()

    def test_ai_input_fails_closed_when_target_entry_is_missing(self) -> None:
        target = _project(role="REQUIREMENT", entry_kind="REQUIREMENT").model_copy(
            update={"parent_id": None}
        )
        matrix = RequirementMatrixSnapshotV1(
            state=MatrixSnapshotState.READY,
            project_id="project-1",
            document_id="document-1",
            requirement_set_id="set-1",
            requirement_set_status="LOCKED",
            root_node_ids=(target.node_id,),
            nodes=(target,),
            counts=MatrixCountsV1(
                total_nodes=1,
                visible_nodes=1,
                analyze_nodes=1,
                analysis_eligible_nodes=1,
            ),
        )
        with self.assertRaises(ApplicationError) as raised:
            select_matrix_analysis_requirements(matrix, [])
        self.assertEqual(
            raised.exception.detail["code"],
            "matrix_analysis_target_binding_invalid",
        )

    def test_container_disposition_is_always_derived_from_leaf_selection(self) -> None:
        child = _project(role="REQUIREMENT", entry_kind="REQUIREMENT").model_copy(
            update={
                "node_id": "child",
                "parent_id": "root",
                "source_order": 1,
                "requirement_entry_id": "child-entry",
                "requirement_id": "child-entry",
            }
        )
        root = _project(
            role="STRUCTURE_PROXY",
            entry_kind="REFERENCE",
            child_ids=("child",),
        ).model_copy(
            update={
                "node_id": "root",
                "parent_id": None,
                "source_order": 0,
                "requirement_entry_id": "root-entry",
                "requirement_id": "root-entry",
            }
        )
        matrix = RequirementMatrixSnapshotV1(
            state=MatrixSnapshotState.READY,
            project_id="project-1",
            document_id="document-1",
            requirement_set_id="set-1",
            requirement_set_status="DRAFT",
            root_node_ids=("root",),
            nodes=(root, child),
            counts=MatrixCountsV1(
                total_nodes=2,
                visible_nodes=2,
                analyze_nodes=1,
                retained_nodes=1,
                analysis_eligible_nodes=1,
            ),
        )

        for disposition in (
            MatrixNodeDisposition.RETAIN,
            MatrixNodeDisposition.EXCLUDE,
        ):
            with self.subTest(disposition=disposition), patch(
                "app.application.requirement_matrix.build_requirement_matrix_snapshot",
                return_value=matrix,
            ), self.assertRaises(ApplicationError) as raised:
                patch_requirement_matrix_node_disposition(
                    "project-1",
                    "document-1",
                    "set-1",
                    "root",
                    MatrixDispositionPatchV1(
                        disposition=disposition,
                        expected_revision=0,
                    ),
                )
            self.assertEqual(
                raised.exception.detail["code"],
                "matrix_container_disposition_requires_leaf_selection",
            )

    def test_table_atom_resolves_record_through_requirement_evidence(self) -> None:
        tree_node = _tree_node(
            node_id="atom-1",
            role="REQUIREMENT",
            requirement_id="requirement-1",
        ).model_copy(
            update={
                "requirement_evidence_binding_id": "requirement-binding-1",
            }
        )
        table_evidence = SimpleNamespace(record_id="record-1")
        table_context = {
            "requirement_evidence_by_id": {
                "requirement-binding-1": SimpleNamespace(
                    table_evidence_binding_id="table-binding-1"
                )
            },
            "evidence_by_id": {"table-binding-1": table_evidence},
        }

        self.assertEqual(
            _resolved_table_record_id(tree_node, table_context=table_context),
            "record-1",
        )

    def test_ai_table_atom_receives_only_its_record_with_full_context(self) -> None:
        target = _project(role="REQUIREMENT", entry_kind="REQUIREMENT").model_copy(
            update={
                "parent_id": None,
                "table_record_id": "record-1",
                "logical_table_ids": ("logical-table-1",),
                "table_refs": (
                    {
                        "kind": "table",
                        "id": "table-1",
                        "logical_table_id": "logical-table-1",
                        "cells": [
                            {"id": "cell-header", "text": "项目 / 要求"},
                            {"id": "cell-target", "text": "响应时间 ≤ 100 ms"},
                            {"id": "cell-sibling", "text": "相邻记录不得泄漏"},
                        ],
                    },
                ),
            }
        )
        logical_table = _logical_table(
            physical_table_ids=("table-1",),
            node_ids=(target.node_id,),
            record_ids=("record-1", "record-2"),
        ).model_copy(
            update={
                "projection": {
                    "projection_id": "projection-1",
                    "mode": "RECORDS",
                    "table_fact": {
                        "logical_table_id": "logical-table-1",
                        "table_parts": [{"table_id": "table-1"}],
                    },
                    "title": "性能要求",
                    "records": [
                    {
                        "record_id": "record-1",
                        "row_bindings": [
                            {
                                "table_id": "table-1",
                                "row_index": 4,
                                "cell_ids": ["cell-target"],
                            }
                        ],
                        "header_bindings": [
                            {
                                "table_id": "table-1",
                                "text": "项目 / 要求",
                                "cell_ids": ["cell-header"],
                            }
                        ],
                        "shared_merged_values": [{"text": "适用于全部型号"}],
                        "notes": [{"text": "测试温度 23°C"}],
                        "image_refs": [{"asset_id": "image-1"}],
                    },
                    {
                        "record_id": "record-2",
                        "row_bindings": [
                            {
                                "table_id": "table-1",
                                "row_index": 5,
                                "cell_ids": ["cell-sibling"],
                            }
                        ],
                        "header_bindings": [
                            {"text": "不应泄漏给相邻记录"}
                        ],
                    },
                ],
                },
            }
        )
        matrix = RequirementMatrixSnapshotV1(
            state=MatrixSnapshotState.READY,
            project_id="project-1",
            document_id="document-1",
            requirement_set_id="set-1",
            requirement_set_status="LOCKED",
            root_node_ids=(target.node_id,),
            nodes=(target,),
            logical_tables=(logical_table,),
            counts=MatrixCountsV1(
                total_nodes=1,
                visible_nodes=1,
                analyze_nodes=1,
                analysis_eligible_nodes=1,
                logical_tables=1,
            ),
        )

        selected = select_matrix_analysis_requirements(
            matrix,
            [{"id": "requirement-1", "entry_kind": "REQUIREMENT"}],
        )

        self.assertEqual(selected[0]["matrix_table_record_id"], "record-1")
        table_ref = selected[0]["table_refs"][-1]
        self.assertEqual(table_ref["slice_ai_projection"]["record_id"], "record-1")
        self.assertEqual(table_ref["headers"][0]["text"], "项目 / 要求")
        self.assertEqual(table_ref["merged_cells"][0]["text"], "适用于全部型号")
        self.assertEqual(table_ref["notes"][0]["text"], "测试温度 23°C")
        self.assertEqual(
            table_ref["slice_rows"],
            [["项目 / 要求", "响应时间 ≤ 100 ms"]],
        )
        self.assertNotIn("相邻记录不得泄漏", str(selected[0]["table_refs"]))
        self.assertNotIn("不应泄漏给相邻记录", str(selected[0]["table_refs"]))


class RequirementMatrixPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.root = Path(self.temporary_directory.name)
        self.database_patch = patch.object(db, "DB_PATH", self.root / "app.db")
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        self.addCleanup(self.temporary_directory.cleanup)
        db.init_db()
        self.project = db.create_project("Matrix", "Customer")
        source_path = self.root / "source.pdf"
        source_path.write_bytes(b"matrix source")
        self.document = db.add_sor_document(
            project_id=self.project["id"],
            file_name="source.pdf",
            file_type="pdf",
            file_path=str(source_path),
            extracted_text="",
        )
        self.requirement_set = db.create_requirement_set(
            self.project["id"],
            self.document["id"],
        )
        self.requirement = db.insert_requirement(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            requirement_set_id=self.requirement_set["id"],
            req_code="REQ0001",
            source_page=1,
            source_anchor="TXT-001",
            requirement_text="系统应记录结果。",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
            entry_kind="REFERENCE",
        )
        db.activate_requirement_set(
            self.project["id"],
            self.document["id"],
            self.requirement_set["id"],
        )

    def test_api_routes_reject_cross_scope_ids_and_remove_legacy_detail(self) -> None:
        client = TestClient(create_app(), base_url="http://testserver")
        base = (
            f"/api/projects/{self.project['id']}/sor-documents/"
            f"{self.document['id']}/requirement-sets/{self.requirement_set['id']}"
            "/matrix"
        )

        missing_tree = client.get(base)
        self.assertEqual(missing_tree.status_code, 200)
        self.assertEqual(
            missing_tree.json()["state"],
            MatrixSnapshotState.REQUIRES_REEXTRACTION.value,
        )
        self.assertEqual(
            client.get(base.replace(self.project["id"], "wrong-project")).status_code,
            404,
        )
        self.assertEqual(
            client.get(base.replace(self.document["id"], "wrong-document")).status_code,
            404,
        )
        self.assertEqual(
            client.get(
                base.replace(self.requirement_set["id"], "wrong-set")
            ).status_code,
            404,
        )

        node = _project(role="REQUIREMENT", entry_kind="REQUIREMENT")
        node = node.model_copy(
            update={
                "parent_id": None,
                "label": "L" * 900,
                "summary": "S" * 1_200,
                "requirement_text": "R" * 1_200,
                "content_blocks": ({"kind": "table", "cells": [1, 2, 3]},),
                "table_refs": ({"logical_table_id": "logical-table-1"},),
                "logical_table_ids": ("logical-table-1",),
            }
        )
        logical_table = _logical_table(
            physical_table_ids=("table-1",),
            node_ids=(node.node_id,),
            record_ids=tuple(f"record-{index}" for index in range(500)),
            evidence_bindings=({"binding_id": "evidence-1"},),
        )
        ready = RequirementMatrixSnapshotV1(
            state=MatrixSnapshotState.READY,
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.requirement_set["id"],
            requirement_set_status="DRAFT",
            root_node_ids=(node.node_id,),
            nodes=(node,),
            logical_tables=(logical_table,),
            counts=MatrixCountsV1(
                total_nodes=1,
                visible_nodes=1,
                analyze_nodes=1,
                analysis_eligible_nodes=1,
                logical_tables=1,
            ),
        )
        with patch(
            "app.application.requirement_matrix.build_requirement_matrix_snapshot",
            return_value=ready,
        ):
            summary = client.get(base)
            wrong_node = client.get(
                f"{base}/nodes/node-from-another-set/detail"
            )
        self.assertEqual(summary.status_code, 200)
        self.assertEqual(summary.json()["nodes"][0]["content_blocks"], [])
        self.assertLessEqual(len(summary.json()["nodes"][0]["label"]), 321)
        self.assertLessEqual(len(summary.json()["nodes"][0]["summary"]), 601)
        self.assertLessEqual(
            len(summary.json()["nodes"][0]["requirement_text"]),
            601,
        )
        self.assertEqual(len(node.summary), 1_200)
        self.assertEqual(node.content_blocks[0]["kind"], "table")
        self.assertNotIn("projection", summary.json()["logical_tables"][0])
        self.assertNotIn("evidence_bindings", summary.json()["logical_tables"][0])
        self.assertEqual(wrong_node.status_code, 404)
        self.assertEqual(
            wrong_node.json()["detail"]["code"],
            "matrix_node_not_found",
        )

        legacy = client.get(
            f"/api/requirements/{self.requirement['id']}/analysis-detail"
        )
        self.assertEqual(legacy.status_code, 404)
        route_paths = {route.path for route in client.app.routes}
        self.assertNotIn(
            "/api/requirements/{requirement_id}/analysis-detail",
            route_paths,
        )
        for removed_path in (
            (
                "/api/projects/{project_id}/sor-documents/{document_id}/"
                "review-entry"
            ),
            "/api/projects/{project_id}/requirements/batch-decision/preview",
            "/api/projects/{project_id}/requirements/batch-decision",
            "/api/requirements/{requirement_id}/decision",
            "/api/requirements/{requirement_id}/analysis-label",
        ):
            self.assertNotIn(removed_path, route_paths)
        self.assertIn(
            (
                "/api/projects/{project_id}/sor-documents/{document_id}/"
                "requirement-sets/{requirement_set_id}/matrix/"
                "nodes/{node_id}/detail"
            ),
            route_paths,
        )
        self.assertIn(
            (
                "/api/projects/{project_id}/sor-documents/{document_id}/"
                "requirement-sets/{requirement_set_id}/matrix/"
                "tables/{logical_table_id}"
            ),
            route_paths,
        )
        self.assertIn(
            (
                "/api/projects/{project_id}/sor-documents/{document_id}/"
                "requirement-sets/{requirement_set_id}/matrix/dispositions"
            ),
            route_paths,
        )

    def test_sqlite_and_postgres_matrix_state_schemas_share_contract(self) -> None:
        list_requirement_matrix_node_states(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.requirement_set["id"],
        )
        expected_columns = (
            "id",
            "project_id",
            "sor_document_id",
            "requirement_set_id",
            "node_id",
            "disposition",
            "reason",
            "revision",
            "created_at",
            "updated_at",
        )
        with db.get_conn() as conn:
            sqlite_columns = tuple(
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(requirement_matrix_node_states)"
                ).fetchall()
            )
            table_sql = str(
                conn.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type = 'table'
                      AND name = 'requirement_matrix_node_states'
                    """
                ).fetchone()["sql"]
            )
            indexes = conn.execute(
                "PRAGMA index_list(requirement_matrix_node_states)"
            ).fetchall()
            indexed_columns = {
                tuple(
                    item["name"]
                    for item in conn.execute(
                        f"PRAGMA index_info('{row['name']}')"
                    ).fetchall()
                )
                for row in indexes
            }
        self.assertEqual(sqlite_columns, expected_columns)
        self.assertIn("CHECK (revision > 0)", table_sql)
        self.assertIn(("requirement_set_id", "node_id"), indexed_columns)
        self.assertIn(
            (
                "project_id",
                "sor_document_id",
                "requirement_set_id",
                "node_id",
            ),
            indexed_columns,
        )

        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "postgresql"
            / "0011_requirement_matrix_node_states.sql"
        ).read_text(encoding="utf-8")
        for column in expected_columns:
            self.assertIn(column, migration)
        self.assertIn("CHECK (revision > 0)", migration)
        self.assertIn("UNIQUE (requirement_set_id, node_id)", migration)
        self.assertIn("idx_requirement_matrix_node_states_scope", migration)

    def test_disposition_and_decision_share_one_optimistic_revision(self) -> None:
        first = set_requirement_matrix_node_disposition(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.requirement_set["id"],
            node_id="node-1",
            disposition="ANALYZE",
            reason="人工纳入分析",
            expected_revision=0,
        )
        self.assertEqual(first["revision"], 1)
        self.assertEqual(
            db.get_requirement(self.requirement["id"])["entry_kind"],
            "REFERENCE",
        )
        with self.assertRaisesRegex(RuntimeError, "matrix_state_revision_conflict"):
            set_requirement_matrix_node_disposition(
                project_id=self.project["id"],
                document_id=self.document["id"],
                requirement_set_id=self.requirement_set["id"],
                node_id="node-1",
                disposition="RETAIN",
                reason="过期写入",
                expected_revision=0,
            )

        db.lock_sor_document_extraction(
            self.document["id"],
            expected_requirement_set_id=self.requirement_set["id"],
        )
        decided = save_requirement_matrix_node_decision(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.requirement_set["id"],
            node_id="node-1",
            requirement_id=self.requirement["id"],
            disposition="ANALYZE",
            final_decision="ACCEPT",
            engineer_reason="工程师已确认。",
            expected_revision=1,
            analysis_input_schema_version=(
                "requirement-analysis-semantic-context/v1"
            ),
            analysis_input_sha256="a" * 64,
        )
        self.assertEqual(decided["revision"], 2)
        assessment = db.get_assessment(self.requirement["id"])
        self.assertEqual(assessment["assessment_mode"], "manual")
        self.assertEqual(assessment["final_decision"], "ACCEPT")
        states = list_requirement_matrix_node_states(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.requirement_set["id"],
        )
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0]["revision"], 2)

        with self.assertRaisesRegex(RuntimeError, "matrix_state_revision_conflict"):
            save_requirement_matrix_node_decision(
                project_id=self.project["id"],
                document_id=self.document["id"],
                requirement_set_id=self.requirement_set["id"],
                node_id="node-1",
                requirement_id=self.requirement["id"],
                disposition="ANALYZE",
                final_decision="REJECT",
                engineer_reason="过期写入",
                expected_revision=1,
                analysis_input_schema_version=(
                    "requirement-analysis-semantic-context/v1"
                ),
                analysis_input_sha256="a" * 64,
            )
        self.assertEqual(
            db.get_assessment(self.requirement["id"])["final_decision"],
            "ACCEPT",
        )

    def test_manual_decision_replaces_stale_payload_as_native_current_result(self) -> None:
        state = set_requirement_matrix_node_disposition(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.requirement_set["id"],
            node_id="node-1",
            disposition="ANALYZE",
            reason="纳入分析",
            expected_revision=0,
        )
        db.upsert_assessment(
            self.requirement["id"],
            profile_id="profile",
            provider="provider",
            ai_decision="COMPLIANT",
            ai_reason="上版 AI 结论",
            ai_confidence=0.8,
            assessment_mode="llm_primary",
            retrieval_backend="local",
            analysis_label="REQUIREMENT",
            analysis_basis="FULL",
            normalized_requirement="上版输入",
            risk_level="LOW",
            clarification_question="",
            model_name="model",
            prompt_version="prompt",
        )
        db.replace_evidences(
            self.requirement["id"],
            [
                {
                    "source_doc_id": "knowledge-1",
                    "source_doc_name": "source.pdf",
                    "source_location": "p.1",
                    "excerpt": "上版证据",
                    "relevance": 0.9,
                }
            ],
        )
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE assessments
                SET reference_status = 'STALE_REFERENCE',
                    result_origin = 'CARRIED',
                    source_requirement_id = 'old-requirement',
                    source_assessment_id = 'old-assessment',
                    impact_receipt_id = 'old-receipt'
                WHERE requirement_id = ?
                """,
                (self.requirement["id"],),
            )
        db.lock_sor_document_extraction(
            self.document["id"],
            expected_requirement_set_id=self.requirement_set["id"],
        )

        save_requirement_matrix_node_decision(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.requirement_set["id"],
            node_id="node-1",
            requirement_id=self.requirement["id"],
            disposition="ANALYZE",
            final_decision="ACCEPT",
            engineer_reason="本版人工确认",
            expected_revision=state["revision"],
            analysis_input_schema_version=(
                "requirement-analysis-semantic-context/v1"
            ),
            analysis_input_sha256="b" * 64,
        )

        assessment = db.get_assessment(self.requirement["id"])
        self.assertEqual(assessment["reference_status"], "CURRENT")
        self.assertEqual(assessment["result_origin"], "NATIVE")
        self.assertEqual(assessment["assessment_mode"], "manual")
        self.assertEqual(assessment["final_decision"], "ACCEPT")
        self.assertEqual(assessment["source_requirement_id"], "")
        self.assertEqual(assessment["source_assessment_id"], "")
        self.assertEqual(assessment["impact_receipt_id"], "")
        self.assertEqual(
            assessment["analysis_input_sha256"],
            "b" * 64,
        )
        with db.get_conn() as connection:
            evidence_count = connection.execute(
                "SELECT COUNT(*) FROM evidences WHERE requirement_id = ?",
                (self.requirement["id"],),
            ).fetchone()[0]
        self.assertEqual(evidence_count, 0)

    def test_batch_dispositions_increment_each_node_revision_together(self) -> None:
        first = set_requirement_matrix_node_dispositions(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.requirement_set["id"],
            updates=[
                {
                    "node_id": "node-a",
                    "disposition": "RETAIN",
                    "reason": "批量保留",
                    "expected_revision": 0,
                },
                {
                    "node_id": "node-b",
                    "disposition": "ANALYZE",
                    "reason": "批量分析",
                    "expected_revision": 0,
                },
            ],
        )
        self.assertEqual(
            {row["node_id"]: row["revision"] for row in first},
            {"node-a": 1, "node-b": 1},
        )

        second = set_requirement_matrix_node_dispositions(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.requirement_set["id"],
            updates=[
                {
                    "node_id": "node-a",
                    "disposition": "ANALYZE",
                    "reason": "第二次批量分析",
                    "expected_revision": 1,
                },
                {
                    "node_id": "node-b",
                    "disposition": "RETAIN",
                    "reason": "第二次批量保留",
                    "expected_revision": 1,
                },
            ],
        )
        self.assertEqual(
            {row["node_id"]: row["revision"] for row in second},
            {"node-a": 2, "node-b": 2},
        )
        rows = {
            row["node_id"]: row
            for row in list_requirement_matrix_node_states(
                project_id=self.project["id"],
                document_id=self.document["id"],
                requirement_set_id=self.requirement_set["id"],
            )
        }
        self.assertEqual(rows["node-a"]["disposition"], "ANALYZE")
        self.assertEqual(rows["node-b"]["disposition"], "RETAIN")

    def test_batch_dispositions_roll_back_all_nodes_when_one_revision_is_stale(
        self,
    ) -> None:
        set_requirement_matrix_node_disposition(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.requirement_set["id"],
            node_id="node-a",
            disposition="ANALYZE",
            reason="初始状态 A",
            expected_revision=0,
        )
        set_requirement_matrix_node_disposition(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.requirement_set["id"],
            node_id="node-b",
            disposition="RETAIN",
            reason="初始状态 B",
            expected_revision=0,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "matrix_state_revision_conflict",
        ):
            set_requirement_matrix_node_dispositions(
                project_id=self.project["id"],
                document_id=self.document["id"],
                requirement_set_id=self.requirement_set["id"],
                updates=[
                    {
                        "node_id": "node-a",
                        "disposition": "RETAIN",
                        "reason": "这项不能被部分写入",
                        "expected_revision": 1,
                    },
                    {
                        "node_id": "node-b",
                        "disposition": "ANALYZE",
                        "reason": "此项 revision 已过期",
                        "expected_revision": 0,
                    },
                ],
            )

        rows = {
            row["node_id"]: row
            for row in list_requirement_matrix_node_states(
                project_id=self.project["id"],
                document_id=self.document["id"],
                requirement_set_id=self.requirement_set["id"],
            )
        }
        self.assertEqual(rows["node-a"]["revision"], 1)
        self.assertEqual(rows["node-a"]["disposition"], "ANALYZE")
        self.assertEqual(rows["node-a"]["reason"], "初始状态 A")
        self.assertEqual(rows["node-b"]["revision"], 1)
        self.assertEqual(rows["node-b"]["disposition"], "RETAIN")
        self.assertEqual(rows["node-b"]["reason"], "初始状态 B")

    def test_derived_draft_inherits_explicit_matrix_dispositions(self) -> None:
        source = set_requirement_matrix_node_disposition(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.requirement_set["id"],
            node_id="node-1",
            disposition="RETAIN",
            reason="保留结构上下文",
            expected_revision=0,
        )
        self.assertEqual(source["revision"], 1)
        draft = db.create_requirement_set(
            self.project["id"],
            self.document["id"],
            supersedes_id=self.requirement_set["id"],
        )
        db.activate_requirement_set(
            self.project["id"],
            self.document["id"],
            draft["id"],
        )

        copied = clone_requirement_matrix_node_states(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_requirement_set_id=self.requirement_set["id"],
            target_requirement_set_id=draft["id"],
            allowed_node_ids={"node-1"},
        )

        self.assertEqual(copied, 1)
        target_rows = list_requirement_matrix_node_states(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=draft["id"],
        )
        self.assertEqual(target_rows[0]["disposition"], "RETAIN")
        self.assertEqual(target_rows[0]["reason"], "保留结构上下文")
        self.assertEqual(target_rows[0]["revision"], 1)


if __name__ == "__main__":
    unittest.main()
