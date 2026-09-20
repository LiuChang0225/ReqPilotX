from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app import db
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.extraction.inspect_structure import (
    STRUCTURE_INSPECTION_RUNTIME_VERSION,
)
from app.application.extraction.requirement_tree import (
    attach_toc_navigation_entries,
    build_requirement_tree_snapshot,
)
from app.application.extraction.requirement_tree_snapshots import (
    RequirementTreeSnapshotError,
    clear_verified_requirement_tree_snapshot_cache,
    clone_requirement_set_tree_snapshot,
    load_requirement_set_tree_snapshot,
    missing_requirement_set_tree_snapshot,
    persist_requirement_tree_snapshot,
)
from app.application.extraction.serialize_structure_workbench import (
    STRUCTURE_BILINGUAL_PROJECTION_SCHEMA_VERSION,
    _node_label,
    _serialize_bilingual_projection_sidecar,
    _serialize_relations,
)
from app.application.extraction.working_tree_publication import (
    _working_tree_publication_key,
)
from app.application.errors import ApplicationError
from app.application.requirements import activate_document_requirement_set
from app.application.runtime import RUNTIME_STATE
from app.domain.extraction.requirement_tree import (
    REQUIREMENT_TREE_SCHEMA_VERSION,
    RequirementTreeBilingualContextV1,
    RequirementTreeBilingualSourceSpanV1,
    RequirementTreeMode,
    RequirementTreeSnapshotV1,
    RequirementTreeState,
    canonical_requirement_tree_bytes,
    canonical_requirement_tree_sha256,
)
from app.domain.extraction.structure import (
    BilingualCounterpartGroup,
    BilingualLanguageEvidence,
    BilingualMismatchSignal,
    BilingualSourceSpan,
    BilingualSpanBoundaryBasis,
    DocumentLanguagePolicy,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
    stable_bilingual_counterpart_group_id,
    stable_bilingual_mismatch_signal_id,
    stable_bilingual_source_span_id,
    stable_document_language_policy_id,
    stable_structure_relation_id,
)
from app.domain.extraction.table_records import (
    TableFactPartV1,
    TableRecordColumnBindingV1,
    TableRecordHeaderAxis,
    TableRecordHeaderBindingV1,
    TableRecordKind,
    TableRecordProjectionMode,
    TableRecordRowBindingV1,
    build_table_fact_binding,
    build_table_record,
    build_table_record_projection,
)
from app.domain.extraction.table_evidence import (
    EvidenceReferenceKind,
    EvidenceReferenceV1,
    TableEvidenceBindingV1,
)


ROOT_ID = "structure-node-" + "1" * 32
SECTION_ID = "structure-node-" + "2" * 32
CLAUSE_ID = "structure-node-" + "3" * 32
SECOND_CLAUSE_ID = "structure-node-" + "4" * 32
TABLE_ROW_ID = "structure-node-" + "5" * 32
TABLE_ID = "structure-node-" + "6" * 32
REGION_SCOPE_ID = "structure-node-" + "7" * 32
ROOT_ONE_ID = "structure-node-" + "8" * 32
ROOT_TWO_ID = "structure-node-" + "9" * 32
SOURCE_HASH = "a" * 64
TREE_HASH = "b" * 64
EFFECTIVE_ID = "effective-structure-" + "c" * 32


def _workbench() -> dict:
    return {
        "project_id": "project-1",
        "document_id": "document-1",
        "source": {
            "project_id": "project-1",
            "document_id": "document-1",
            "source_ir_id": "source-ir-1",
            "source_sha256": SOURCE_HASH,
            "preview_generation_id": "preview-1",
        },
        "agent": {"status": "COMPLETED", "result_state": "STABLE"},
        "structure": {
            "base_snapshot_id": "base-1",
            "effective_snapshot_id": EFFECTIVE_ID,
            "effective_tree_sha256": TREE_HASH,
            "revision": 2,
            "nodes": [
                {
                    "id": ROOT_ID,
                    "parent_id": "",
                    "kind": "DOCUMENT",
                    "order": 0,
                    "label": "主动减振器 PDS",
                    "source_refs": [],
                },
                {
                    "id": SECTION_ID,
                    "parent_id": ROOT_ID,
                    "kind": "SECTION",
                    "order": 10,
                    "label": "3 产品要求",
                    "source_refs": [{"kind": "SOURCE_NODE", "source_id": "TXT-100"}],
                },
                {
                    "id": CLAUSE_ID,
                    "parent_id": SECTION_ID,
                    "kind": "CLAUSE",
                    "order": 20,
                    "label": "3.2 尺寸要求",
                    "health_codes": ["UNCERTAIN_PARENT"],
                    "source_refs": [{"kind": "SOURCE_NODE", "source_id": "TXT-101"}],
                },
                {
                    "id": SECOND_CLAUSE_ID,
                    "parent_id": SECTION_ID,
                    "kind": "CLAUSE",
                    "order": 30,
                    "label": "3.3 强度要求",
                    "source_refs": [{"kind": "SOURCE_NODE", "source_id": "TXT-102"}],
                },
            ],
        },
    }


def _bilingual_span(
    *,
    source_ref: StructureSourceRef,
    end_offset: int,
    language: BilingualLanguageEvidence,
) -> BilingualSourceSpan:
    boundary_basis = BilingualSpanBoundaryBasis.SOURCE_BLOCK_BOUNDARY
    return BilingualSourceSpan(
        bilingual_span_id=stable_bilingual_source_span_id(
            source_ref=source_ref,
            start_offset=0,
            end_offset=end_offset,
            language_evidence=language,
            boundary_basis=boundary_basis,
        ),
        source_ref=source_ref,
        start_offset=0,
        end_offset=end_offset,
        language_evidence=language,
        boundary_basis=boundary_basis,
    )


def _typed_bilingual_workbench() -> tuple[
    dict,
    DocumentLanguagePolicy,
    BilingualCounterpartGroup,
    BilingualMismatchSignal,
]:
    document_id = "document-bilingual-projection"
    section_id = "section-bilingual-projection"
    english_id = "clause-bilingual-english"
    chinese_id = "clause-bilingual-chinese"
    english_text = "1) Supplier shall submit a validation report."
    chinese_text = "1）供应商应提交验证报告。"
    english_source_ref = StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id="node-" + "1" * 32,
    )
    chinese_source_ref = StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id="node-" + "2" * 32,
    )
    policy_english_ref = StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id="node-" + "3" * 32,
    )
    policy_chinese_ref = StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id="node-" + "4" * 32,
    )
    policy_english_span = _bilingual_span(
        source_ref=policy_english_ref,
        end_offset=20,
        language=BilingualLanguageEvidence.EN,
    )
    policy_chinese_span = _bilingual_span(
        source_ref=policy_chinese_ref,
        end_offset=12,
        language=BilingualLanguageEvidence.ZH_CN,
    )
    policy_id = stable_document_language_policy_id(
        english_declaration_spans=(policy_english_span,),
        chinese_declaration_spans=(policy_chinese_span,),
        equal_validity=True,
        conflict_precedence="zh-CN",
    )
    policy = DocumentLanguagePolicy(
        document_language_policy_id=policy_id,
        english_declaration_spans=(policy_english_span,),
        chinese_declaration_spans=(policy_chinese_span,),
    )
    english_span = _bilingual_span(
        source_ref=english_source_ref,
        end_offset=len(english_text),
        language=BilingualLanguageEvidence.EN,
    )
    chinese_span = _bilingual_span(
        source_ref=chinese_source_ref,
        end_offset=len(chinese_text),
        language=BilingualLanguageEvidence.ZH_CN,
    )
    relation_id = stable_structure_relation_id(
        source_ir_id="source-ir-bilingual-projection",
        resolver_id="synthetic-bilingual-resolver",
        resolver_version="1.0.0",
        kind=StructureRelationKind.TRANSLATION_OF,
        source_structure_node_id=english_id,
        target_structure_node_id=chinese_id,
        source_refs=(english_source_ref, chinese_source_ref),
    )
    group_id = stable_bilingual_counterpart_group_id(
        document_language_policy_id=policy_id,
        canonical_structure_node_id=english_id,
        member_structure_node_ids=(english_id, chinese_id),
        english_spans=(english_span,),
        chinese_spans=(chinese_span,),
    )
    group = BilingualCounterpartGroup(
        counterpart_group_id=group_id,
        document_language_policy_id=policy_id,
        canonical_structure_node_id=english_id,
        member_structure_node_ids=(english_id, chinese_id),
        english_spans=(english_span,),
        chinese_spans=(chinese_span,),
        translation_relation_id=relation_id,
    )
    mismatch_id = stable_bilingual_mismatch_signal_id(
        counterpart_group_id=group_id,
        english_span_ids=(english_span.bilingual_span_id,),
        chinese_span_ids=(chinese_span.bilingual_span_id,),
    )
    mismatch = BilingualMismatchSignal(
        mismatch_signal_id=mismatch_id,
        counterpart_group_id=group_id,
        english_span_ids=(english_span.bilingual_span_id,),
        chinese_span_ids=(chinese_span.bilingual_span_id,),
    )
    effective = SimpleNamespace(
        document_language_policy=policy,
        bilingual_counterpart_groups=(group,),
        bilingual_mismatch_signals=(mismatch,),
    )
    bilingual_sidecar = json.loads(
        json.dumps(
            _serialize_bilingual_projection_sidecar(effective),
            ensure_ascii=False,
        )
    )
    workbench = _workbench()
    workbench["structure"]["nodes"] = [
        {
            "id": document_id,
            "parent_id": "",
            "kind": "DOCUMENT",
            "order": 0,
            "label": "Bilingual document",
            "source_refs": [],
        },
        {
            "id": section_id,
            "parent_id": document_id,
            "kind": "SECTION",
            "order": 10,
            "label": "1 Scope",
            "source_refs": [{"source_id": "node-" + "5" * 32}],
        },
        {
            "id": english_id,
            "parent_id": section_id,
            "kind": "CLAUSE",
            "order": 11,
            "physical_source_order": 11,
            "label": english_text,
            "explicit_label": "1",
            "list_ordinal": 1,
            "boundary_basis": "EXPLICIT_NUMBER",
            "source_refs": [english_source_ref.model_dump(mode="json")],
            "source_spans": [
                {
                    "source_ref": english_source_ref.model_dump(mode="json"),
                    "start_offset": 0,
                    "end_offset": len(english_text),
                }
            ],
        },
        {
            "id": chinese_id,
            "parent_id": section_id,
            "kind": "CLAUSE",
            "order": 12,
            "physical_source_order": 12,
            "label": chinese_text,
            "explicit_label": "1",
            "list_ordinal": 1,
            "boundary_basis": "EXPLICIT_NUMBER",
            "source_refs": [chinese_source_ref.model_dump(mode="json")],
            "source_spans": [
                {
                    "source_ref": chinese_source_ref.model_dump(mode="json"),
                    "start_offset": 0,
                    "end_offset": len(chinese_text),
                }
            ],
        },
    ]
    workbench["structure"]["bilingual_projection"] = bilingual_sidecar
    workbench["relations"] = _serialize_relations(
        [
            {
                "relation_id": relation_id,
                "kind": StructureRelationKind.TRANSLATION_OF.value,
                "source_structure_node_id": english_id,
                "target_structure_node_id": chinese_id,
                "source_refs": [
                    english_source_ref.model_dump(mode="json"),
                    chinese_source_ref.model_dump(mode="json"),
                ],
            }
        ],
        source_index={},
    )
    return workbench, policy, group, mismatch


def _entries(*, published: bool = False) -> list[dict]:
    prefix = "published" if published else "working"
    return [
        {
            "id": f"{prefix}-provider-id-1",
            "review_node_id": f"{prefix}-review-node-1",
            "requirement_entry_id": f"{prefix}-entry-1",
            "owner_structure_node_id": CLAUSE_ID,
            "source_order": 21,
            "display_text": "活塞桶直径应为 Φ32–48 mm。",
            "source_spans": [
                {
                    "semantic_unit_id": "semantic-unit-1",
                    "owner_structure_node_id": CLAUSE_ID,
                    "start_offset": 0,
                    "end_offset": 20,
                }
            ],
            "status": "REVIEWED",
        },
        {
            "id": f"{prefix}-provider-id-2",
            "review_node_id": f"{prefix}-review-node-2",
            "requirement_entry_id": f"{prefix}-entry-2",
            "owner_structure_node_id": SECOND_CLAUSE_ID,
            "source_order": 31,
            "display_text": "接口强度应不低于 40 kN。",
            "source_refs": [
                {
                    "kind": "TEXT_RANGE",
                    "source_id": "TXT-102",
                    "owner_structure_node_id": SECOND_CLAUSE_ID,
                    "start_offset": 2,
                    "end_offset": 18,
                }
            ],
            "status": "NEEDS_ATTENTION",
        },
    ]


def _test_block_text(blocks: tuple[dict, ...]) -> str:
    return "\n".join(
        str(block.get("text") or block.get("raw_text") or "").strip()
        for block in blocks
        if str(block.get("text") or block.get("raw_text") or "").strip()
    )


def _records_projection(
    *,
    owner_id: str,
    table_id: str,
    source_id: str,
    table_source_order: int = 20,
    record_source_order: int = 20,
):
    cells = tuple(f"cell-{index:032x}" for index in range(1, 5))
    table_ref = StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id=source_id,
    )
    fact = build_table_fact_binding(
        source_ir_id="source-ir-1",
        source_sha256=SOURCE_HASH,
        source_ir_canonical_sha256="d" * 64,
        structure_snapshot_id=EFFECTIVE_ID,
        logical_table_id="logical-table-direct-content",
        owner_structure_node_id=owner_id,
        table_parts=(
            TableFactPartV1(
                table_id=table_id,
                source_order=table_source_order,
                row_count=2,
                column_count=2,
                cell_ids=cells,
                table_content_sha256="e" * 64,
                source_refs=(table_ref,),
            ),
        ),
        whole_table_source_refs=(table_ref,),
    )
    record = build_table_record(
        table_fact=fact,
        kind=TableRecordKind.ROW,
        source_order=record_source_order,
        row_bindings=(
            TableRecordRowBindingV1(
                table_id=table_id,
                row_index=1,
                cell_ids=(cells[2], cells[3]),
            ),
        ),
        column_bindings=(
            TableRecordColumnBindingV1(
                table_id=table_id,
                column_index=0,
                cell_ids=(cells[2],),
            ),
        ),
        header_bindings=(),
        source_refs=(table_ref,),
    )
    return build_table_record_projection(
        table_fact=fact,
        mode=TableRecordProjectionMode.RECORDS,
        projection_revision=0,
        records=(record,),
    )


def _records_projection_for_rows(
    *,
    owner_id: str,
    table_id: str,
    source_id: str,
    rows: tuple[tuple[str, ...], ...],
    table_source_order: int = 20,
):
    """Build a record projection and matching table payload for label tests."""

    if len(rows) < 2 or not rows[0]:
        raise ValueError("row fixture requires a header and at least one data row")
    if len({len(row) for row in rows}) != 1:
        raise ValueError("row fixture must be rectangular")
    row_count = len(rows)
    column_count = len(rows[0])
    cell_ids = tuple(
        f"cell-{index:032x}" for index in range(1, row_count * column_count + 1)
    )
    table_ref = StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id=source_id,
    )
    fact = build_table_fact_binding(
        source_ir_id="source-ir-1",
        source_sha256=SOURCE_HASH,
        source_ir_canonical_sha256="d" * 64,
        structure_snapshot_id=EFFECTIVE_ID,
        logical_table_id=f"logical-{table_id}",
        owner_structure_node_id=owner_id,
        table_parts=(
            TableFactPartV1(
                table_id=table_id,
                source_order=table_source_order,
                row_count=row_count,
                column_count=column_count,
                cell_ids=cell_ids,
                table_content_sha256="e" * 64,
                source_refs=(table_ref,),
            ),
        ),
        whole_table_source_refs=(table_ref,),
    )
    records = []
    for row_index in range(1, row_count):
        start = row_index * column_count
        row_cell_ids = cell_ids[start : start + column_count]
        records.append(
            build_table_record(
                table_fact=fact,
                kind=TableRecordKind.ROW,
                source_order=row_index - 1,
                row_bindings=(
                    TableRecordRowBindingV1(
                        table_id=table_id,
                        row_index=row_index,
                        cell_ids=row_cell_ids,
                    ),
                ),
                column_bindings=(
                    TableRecordColumnBindingV1(
                        table_id=table_id,
                        column_index=0,
                        cell_ids=(row_cell_ids[0],),
                    ),
                ),
                header_bindings=(),
                source_refs=(table_ref,),
            )
        )
    projection = build_table_record_projection(
        table_fact=fact,
        mode=TableRecordProjectionMode.RECORDS,
        projection_revision=0,
        records=tuple(records),
    )
    table = {
        "id": table_id,
        "source_node_id": source_id,
        "source_order": table_source_order,
        "row_count": row_count,
        "column_count": column_count,
        "header_regions": [
            {
                "row": 0,
                "column": 0,
                "row_span": 1,
                "column_span": column_count,
            }
        ],
        "page_start": 5,
        "page_end": 5,
        "cells": [
            {
                "id": cell_id,
                "row": index // column_count,
                "column": index % column_count,
                "text": rows[index // column_count][index % column_count],
                "location": {"page_number": 5},
            }
            for index, cell_id in enumerate(cell_ids)
        ],
    }
    return projection, table


def _matrix_projection_for_same_row(
    *,
    owner_id: str,
    table_id: str,
    source_id: str,
    column_labels: tuple[str, str],
):
    """Build two intersections on one row for display-label tests."""

    cell_ids = tuple(f"cell-{index:032x}" for index in range(1, 7))
    table_ref = StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id=source_id,
    )
    fact = build_table_fact_binding(
        source_ir_id="source-ir-1",
        source_sha256=SOURCE_HASH,
        source_ir_canonical_sha256="d" * 64,
        structure_snapshot_id=EFFECTIVE_ID,
        logical_table_id=f"logical-{table_id}",
        owner_structure_node_id=owner_id,
        table_parts=(
            TableFactPartV1(
                table_id=table_id,
                source_order=20,
                row_count=2,
                column_count=3,
                cell_ids=cell_ids,
                table_content_sha256="e" * 64,
                source_refs=(table_ref,),
            ),
        ),
        whole_table_source_refs=(table_ref,),
    )
    row_header = TableRecordHeaderBindingV1(
        table_id=table_id,
        axis=TableRecordHeaderAxis.ROW,
        level=0,
        cell_ids=(cell_ids[3],),
        text="执行器",
        source_refs=(table_ref,),
    )
    records = tuple(
        build_table_record(
            table_fact=fact,
            kind=TableRecordKind.MATRIX_INTERSECTION,
            source_order=source_order,
            row_bindings=(
                TableRecordRowBindingV1(
                    table_id=table_id,
                    row_index=1,
                    cell_ids=cell_ids[3:6],
                ),
            ),
            column_bindings=(
                TableRecordColumnBindingV1(
                    table_id=table_id,
                    column_index=column_index,
                    cell_ids=(cell_ids[3 + column_index],),
                ),
            ),
            header_bindings=(
                row_header,
                TableRecordHeaderBindingV1(
                    table_id=table_id,
                    axis=TableRecordHeaderAxis.COLUMN,
                    level=0,
                    cell_ids=(cell_ids[column_index],),
                    text=column_labels[source_order],
                    source_refs=(table_ref,),
                ),
            ),
            source_refs=(table_ref,),
        )
        for source_order, column_index in enumerate((1, 2))
    )
    projection = build_table_record_projection(
        table_fact=fact,
        mode=TableRecordProjectionMode.RECORDS,
        projection_revision=0,
        records=records,
    )
    table = {
        "id": table_id,
        "source_node_id": source_id,
        "source_order": 20,
        "row_count": 2,
        "column_count": 3,
        "header_regions": [{"row": 0, "column": 0, "row_span": 1, "column_span": 3}],
        "page_start": 5,
        "page_end": 5,
        "cells": [
            {
                "id": cell_id,
                "row": index // 3,
                "column": index % 3,
                "text": (
                    ("项目", *column_labels)
                    if index < 3
                    else ("执行器", "环境耐久", "环境耐久")
                )[index % 3],
                "location": {"page_number": 5},
            }
            for index, cell_id in enumerate(cell_ids)
        ],
    }
    return projection, table


def _whole_table_projection_with_evidence(
    *,
    owner_id: str,
    table_id: str,
    source_id: str,
    logical_table_id: str = "logical-table-whole-review",
):
    cells = tuple(f"whole-cell-{index:028x}" for index in range(1, 5))
    table_ref = StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id=source_id,
    )
    fact = build_table_fact_binding(
        source_ir_id="source-ir-1",
        source_sha256=SOURCE_HASH,
        source_ir_canonical_sha256="d" * 64,
        structure_snapshot_id=EFFECTIVE_ID,
        logical_table_id=logical_table_id,
        owner_structure_node_id=owner_id,
        table_parts=(
            TableFactPartV1(
                table_id=table_id,
                source_order=20,
                row_count=2,
                column_count=2,
                cell_ids=cells,
                table_content_sha256="e" * 64,
                source_refs=(table_ref,),
            ),
        ),
        whole_table_source_refs=(table_ref,),
    )
    projection = build_table_record_projection(
        table_fact=fact,
        mode=TableRecordProjectionMode.WHOLE_TABLE,
        projection_revision=0,
        records=(),
    )
    evidence_ref = EvidenceReferenceV1(
        kind=EvidenceReferenceKind.TABLE,
        reference_id=table_id,
        resolver_key=f"source-ir-1/{table_id}",
        content_sha256=hashlib.sha256(table_id.encode("utf-8")).hexdigest(),
        page_number=2,
    )
    evidence = TableEvidenceBindingV1.create(
        logical_table_id=logical_table_id,
        source_table_ids=(table_id,),
        projection_id=projection.projection_id,
        projection_revision=projection.projection_revision,
        projection_sha256=projection.projection_sha256,
        table_refs=(evidence_ref,),
    )
    return projection, evidence


def _interposed_row_caption_snapshot(
    *,
    captions: tuple[tuple[str, int, int, str], ...],
):
    """Build a table whose TABLE_ROW sorts between caption and TABLE."""

    section_id = "section-local-table-caption"
    other_section_id = "section-other-table-caption"
    table_node_id = "zzz-table-node-local-caption"
    table_row_id = "aaa-table-row-local-caption"
    table_id = "table-local-caption"
    source_id = "node-" + "a" * 32
    projection, evidence = _whole_table_projection_with_evidence(
        owner_id=table_node_id,
        table_id=table_id,
        source_id=source_id,
        logical_table_id=table_node_id,
    )
    workbench = _workbench()
    workbench["structure"]["nodes"] = [
        workbench["structure"]["nodes"][0],
        {
            "id": section_id,
            "parent_id": ROOT_ID,
            "kind": "SECTION",
            "order": 10,
            "physical_source_order": 10,
            "label": "5.6 参数要求",
            "source_refs": [{"source_id": "source-local-caption-section"}],
        },
        {
            "id": other_section_id,
            "parent_id": ROOT_ID,
            "kind": "SECTION",
            "order": 11,
            "physical_source_order": 11,
            "label": "5.7 其他要求",
            "source_refs": [{"source_id": "source-other-caption-section"}],
        },
        *(
            {
                "id": f"caption-local-table-{index}",
                "parent_id": parent_id,
                "kind": "CONTENT",
                "order": source_order,
                "physical_source_order": source_order,
                "label": label,
                "page_start": page_number,
                "page_end": page_number,
                "source_refs": [
                    {
                        "source_id": f"source-local-caption-{index}",
                        "page_number": page_number,
                    }
                ],
            }
            for index, (label, source_order, page_number, parent_id) in enumerate(
                captions
            )
        ),
        {
            "id": table_row_id,
            "parent_id": table_node_id,
            "kind": "TABLE_ROW",
            "order": 20,
            "physical_source_order": 20,
            "label": "参数 要求",
            "page_start": 2,
            "page_end": 2,
            "source_refs": [{"source_id": "cell-local-caption", "page_number": 2}],
        },
        {
            "id": table_node_id,
            "parent_id": section_id,
            "kind": "TABLE",
            "order": 20,
            "physical_source_order": 20,
            "label": "Table",
            "page_start": 2,
            "page_end": 2,
            "source_refs": [{"source_id": source_id, "page_number": 2}],
        },
    ]
    snapshot = build_requirement_tree_snapshot(
        workbench=workbench,
        payload={
            "mode": "LOCAL_STRUCTURE_PROJECTION",
            "tables": [
                {
                    "id": table_id,
                    "source_node_id": source_id,
                    "page_start": 2,
                    "page_end": 2,
                    "cells": [],
                }
            ],
            "table_record_projections": [projection.model_dump(mode="json")],
            "table_evidence_bindings": [evidence.model_dump(mode="json")],
        },
    )
    return snapshot, table_node_id, section_id, other_section_id


class RequirementTreeProjectionTests(unittest.TestCase):
    def test_navigation_only_duplicate_region_scope_is_flattened(self) -> None:
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            {
                "id": ROOT_ID,
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": REGION_SCOPE_ID,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "1. Reliability goals",
                "explicit_label": None,
                "boundary_basis": "NATIVE_BLOCK",
                "source_refs": [{"kind": "SOURCE_NODE", "source_id": "TXT-200"}],
                "source_spans": [],
                "requirement_ids": ["presentation-only-association"],
            },
            {
                "id": ROOT_ONE_ID,
                "parent_id": REGION_SCOPE_ID,
                "kind": "SECTION",
                "order": 11,
                "label": "1. Reliability goals",
                "explicit_label": "1",
                "boundary_basis": "EXPLICIT_NUMBER",
                "source_refs": [{"kind": "SOURCE_NODE", "source_id": "TXT-200"}],
                "source_spans": [
                    {
                        "source_ref": {
                            "kind": "SOURCE_NODE",
                            "source_id": "TXT-200",
                        },
                        "start_offset": 0,
                        "end_offset": 20,
                    }
                ],
            },
            {
                "id": ROOT_TWO_ID,
                "parent_id": REGION_SCOPE_ID,
                "kind": "SECTION",
                "order": 20,
                "label": "2. Validation goals",
                "explicit_label": "2",
                "boundary_basis": "EXPLICIT_NUMBER",
                "source_refs": [{"kind": "SOURCE_NODE", "source_id": "TXT-200"}],
                "source_spans": [
                    {
                        "source_ref": {
                            "kind": "SOURCE_NODE",
                            "source_id": "TXT-200",
                        },
                        "start_offset": 80,
                        "end_offset": 110,
                    }
                ],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            requirement_entries=[],
        )

        by_id = {node.id: node for node in snapshot.nodes}
        self.assertNotIn(REGION_SCOPE_ID, by_id)
        self.assertEqual(by_id[ROOT_ONE_ID].parent_id, ROOT_ID)
        self.assertEqual(by_id[ROOT_TWO_ID].parent_id, ROOT_ID)
        self.assertEqual(
            by_id[ROOT_ID].child_ids,
            (ROOT_ONE_ID, ROOT_TWO_ID),
        )

    def test_standalone_region_heading_remains_visible(self) -> None:
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            {
                "id": ROOT_ID,
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": REGION_SCOPE_ID,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "FMEA and special characteristics",
                "explicit_label": None,
                "boundary_basis": "NATIVE_BLOCK",
                "source_refs": [{"kind": "SOURCE_NODE", "source_id": "TXT-199"}],
                "source_spans": [],
            },
            {
                "id": ROOT_ONE_ID,
                "parent_id": REGION_SCOPE_ID,
                "kind": "SECTION",
                "order": 11,
                "label": "1 FMEA requirements",
                "explicit_label": "1",
                "boundary_basis": "EXPLICIT_NUMBER",
                "source_refs": [{"kind": "SOURCE_NODE", "source_id": "TXT-200"}],
                "source_spans": [
                    {
                        "source_ref": {
                            "kind": "SOURCE_NODE",
                            "source_id": "TXT-200",
                        },
                        "start_offset": 0,
                        "end_offset": 20,
                    }
                ],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            requirement_entries=[],
        )

        by_id = {node.id: node for node in snapshot.nodes}
        self.assertEqual(by_id[REGION_SCOPE_ID].parent_id, ROOT_ID)
        self.assertEqual(by_id[ROOT_ONE_ID].parent_id, REGION_SCOPE_ID)

    def test_no_llm_result_projects_structure_and_keeps_rules_as_evidence(self) -> None:
        snapshot = build_requirement_tree_snapshot(
            workbench=_workbench(),
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "items": [
                    {
                        "id": "rule-1",
                        "target_structure": {"id": CLAUSE_ID},
                        "text": "活塞桶直径应为 Φ32–48 mm。",
                        "semantic_class": "REQUIREMENT",
                        "requirement_subtype": "PERFORMANCE_PARAMETER_REQUIREMENT",
                        "reason_codes": ["explicit_performance_parameter_schema"],
                    }
                ],
            },
        )

        by_id = {node.id: node for node in snapshot.nodes}
        self.assertEqual(snapshot.state, RequirementTreeState.READY)
        self.assertFalse(any(node.kind == "REQUIREMENT" for node in snapshot.nodes))
        self.assertEqual(by_id[CLAUSE_ID].projection_role, "STRUCTURE_PROXY")
        self.assertEqual(by_id[CLAUSE_ID].semantic_state, "NOT_CLASSIFIED")
        self.assertEqual(
            by_id[CLAUSE_ID].deterministic_rule_evidence[0]["authority"],
            "SUGGESTION_ONLY",
        )
        self.assertEqual(by_id[ROOT_ID].requirement_count, 0)
        self.assertEqual(by_id[ROOT_ID].review_item_count, 1)
        self.assertEqual(by_id[SECOND_CLAUSE_ID].projection_role, "STRUCTURE")

    def test_semantic_evidence_does_not_promote_a_pure_heading(self) -> None:
        heading_id = "pure-heading-with-semantic-evidence"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": heading_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "4.1 测试管理",
                "explicit_label": "4.1",
                "boundary_basis": "EXPLICIT_NUMBER",
                "source_refs": [{"source_id": "source-pure-heading"}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "items": [
                    {
                        "id": "rule-heading-guess",
                        "target_structure": {"id": heading_id},
                        "text": "4.1 测试管理",
                        "semantic_class": "REQUIREMENT",
                        "requirement_subtype": "PROCESS_REQUIREMENT",
                        "reason_codes": ["semantic_heading_false_positive"],
                    }
                ],
            },
        )
        by_id = {node.id: node for node in snapshot.nodes}
        heading = by_id[heading_id]

        self.assertEqual(heading.projection_role, "STRUCTURE")
        self.assertEqual(heading.classification, "STRUCTURE")
        self.assertEqual(heading.review_item_count, 0)
        self.assertEqual(by_id[ROOT_ID].review_item_count, 0)
        self.assertEqual(
            heading.deterministic_rule_evidence[0]["semantic_class"],
            "REQUIREMENT",
        )
        self.assertEqual(
            heading.deterministic_rule_evidence[0]["authority"],
            "SUGGESTION_ONLY",
        )

    def test_terminal_explicit_clause_survives_heading_semantic_guess(self) -> None:
        workbench = _workbench()
        terminal_id = "terminal-explicit-clause"
        bare_id = "bare-explicit-marker"
        dotted_terminal_id = "terminal-dotted-statement"
        dotted_title_id = "terminal-dotted-title"
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": SECTION_ID,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "9 技术要求",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": terminal_id,
                "parent_id": SECTION_ID,
                "kind": "CLAUSE",
                "order": 20,
                "label": "3、供应商应提交验证报告。",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 3,
                "source_refs": [
                    {"source_id": "source-terminal", "source_kind": "PARAGRAPH"}
                ],
            },
            {
                "id": bare_id,
                "parent_id": SECTION_ID,
                "kind": "CLAUSE",
                "order": 30,
                "label": "4、",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 4,
                "source_refs": [
                    {"source_id": "source-bare", "source_kind": "PARAGRAPH"}
                ],
            },
            {
                "id": dotted_terminal_id,
                "parent_id": SECTION_ID,
                "kind": "SECTION",
                "order": 40,
                "label": "4.6.4 如果测试失败，应通知项目负责人。",
                "explicit_label": "4.6.4",
                "boundary_basis": "EXPLICIT_NUMBER",
                "source_refs": [
                    {"source_id": "source-dotted", "source_kind": "PARAGRAPH"}
                ],
                "source_spans": [
                    {
                        "source_ref": {"source_id": "source-dotted"},
                        "start_offset": 12,
                        "end_offset": 48,
                    }
                ],
            },
            {
                "id": dotted_title_id,
                "parent_id": SECTION_ID,
                "kind": "SECTION",
                "order": 50,
                "label": "4.6.5 测试管理",
                "explicit_label": "4.6.5",
                "boundary_basis": "EXPLICIT_NUMBER",
                "source_refs": [
                    {"source_id": "source-title", "source_kind": "PARAGRAPH"}
                ],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "items": [
                    {
                        "target_structure": {"id": terminal_id},
                        "text": "3、供应商应提交验证报告。",
                        "semantic_class": "HEADING",
                    }
                ],
            },
        )
        by_id = {node.id: node for node in snapshot.nodes}

        self.assertEqual(by_id[terminal_id].projection_role, "STRUCTURE_PROXY")
        self.assertEqual(by_id[terminal_id].classification, "NOT_CLASSIFIED")
        self.assertEqual(by_id[bare_id].projection_role, "STRUCTURE")
        self.assertEqual(
            by_id[dotted_terminal_id].projection_role,
            "STRUCTURE_PROXY",
        )
        self.assertEqual(
            by_id[dotted_terminal_id].source_refs,
            (
                {
                    "source_id": "source-dotted",
                    "source_kind": "PARAGRAPH",
                },
                {
                    "kind": "SOURCE_NODE",
                    "source_id": "source-dotted",
                    "source_kind": "PARAGRAPH",
                    "start_offset": 12,
                    "end_offset": 48,
                },
                {
                    "kind": "TEXT",
                    "source_id": "source-dotted",
                    "owner_structure_node_id": dotted_terminal_id,
                    "start_offset": 12,
                    "end_offset": 48,
                    "page_start": None,
                    "page_end": None,
                },
            ),
        )
        self.assertEqual(by_id[dotted_title_id].projection_role, "STRUCTURE")

    def test_same_owner_content_zone_is_non_atomic_regardless_of_continues(
        self,
    ) -> None:
        section_id = "section-fragment-continuation"
        child_id = "child-fragment-boundary"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "1 范围",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": "physical-a",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "供应商应提",
                "page_start": 1,
                "page_end": 1,
                "source_refs": [{"source_id": "source-physical-a"}],
            },
            {
                "id": "physical-b",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 12,
                "label": "交验证报告。",
                "page_start": 1,
                "page_end": 1,
                "source_refs": [{"source_id": "source-physical-b"}],
            },
            {
                "id": "semantic-a",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 13,
                "label": "供应商应澄清冲突。",
                "page_start": 1,
                "page_end": 1,
                "source_refs": [{"source_id": "source-semantic-a"}],
            },
            {
                "id": "semantic-b",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 14,
                "label": "供应商应联系项目团队。",
                "page_start": 2,
                "page_end": 2,
                "source_refs": [{"source_id": "source-semantic-b"}],
            },
            {
                "id": child_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "1.1 后续",
                "source_refs": [{"source_id": "source-child"}],
            },
        ]
        workbench["relations"] = [
            {"kind": "CONTINUES", "from_id": "physical-a", "to_id": "physical-b"},
            {"kind": "CONTINUES", "from_id": "semantic-a", "to_id": "semantic-b"},
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        direct = [
            node for node in snapshot.nodes if node.projection_role == "DIRECT_CONTENT"
        ]

        self.assertEqual(len(direct), 1)
        self.assertEqual(
            _test_block_text(direct[0].content_blocks),
            ("供应商应提\n交验证报告。\n供应商应澄清冲突。\n供应商应联系项目团队。"),
        )
        self.assertEqual(
            tuple(block["structure_node_id"] for block in direct[0].content_blocks),
            ("physical-a", "physical-b", "semantic-a", "semantic-b"),
        )
        self.assertEqual((direct[0].page_start, direct[0].page_end), (1, 2))

    def test_explicit_clause_and_physical_wrap_form_one_direct_content_leaf(
        self,
    ) -> None:
        clause_id = "clause-inline-physical-wrap"
        continuation_id = "content-inline-physical-wrap"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": clause_id,
                "parent_id": ROOT_ID,
                "kind": "CLAUSE",
                "order": 10,
                "label": ("b) 按照提供的要求对清单中零件进行硬件开发，进行零部件的生"),
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 2,
                "page_start": 11,
                "page_end": 11,
                "source_refs": [
                    {
                        "kind": "SOURCE_NODE",
                        "source_id": "source-inline-physical-wrap",
                        "page_number": 11,
                    }
                ],
            },
            {
                "id": continuation_id,
                "parent_id": clause_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "产。",
                "boundary_basis": "NATIVE_BLOCK",
                "page_start": 11,
                "page_end": 11,
                "source_refs": [
                    {
                        "kind": "SOURCE_NODE",
                        "source_id": "source-inline-physical-continuation",
                        "page_number": 11,
                    }
                ],
            },
        ]
        workbench["relations"] = [
            {
                "kind": "CONTINUES",
                "from_id": clause_id,
                "to_id": continuation_id,
            }
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        by_id = {node.id: node for node in snapshot.nodes}
        direct = [
            node for node in snapshot.nodes if node.projection_role == "DIRECT_CONTENT"
        ]

        self.assertEqual(by_id[clause_id].projection_role, "STRUCTURE_PROXY")
        self.assertEqual(direct, [])
        self.assertEqual(
            by_id[clause_id].label,
            "b) 按照提供的要求对清单中零件进行硬件开发，进行零部件的生产。",
        )
        self.assertEqual(
            tuple(
                block["structure_node_id"] for block in by_id[clause_id].content_blocks
            ),
            (clause_id, continuation_id),
        )
        self.assertEqual(
            _test_block_text(by_id[clause_id].content_blocks),
            ("b) 按照提供的要求对清单中零件进行硬件开发，进行零部件的生\n产。"),
        )
        self.assertEqual(
            {
                str(ref.get("source_id") or "")
                for ref in by_id[clause_id].source_refs
                if str(ref.get("source_id") or "")
            },
            {
                "source-inline-physical-wrap",
                "source-inline-physical-continuation",
            },
        )

    def test_inline_clause_owner_is_not_exposed_as_a_reparent_target(self) -> None:
        clause_id = "clause-inline-with-child"
        continuation_id = "content-inline-with-child"
        child_id = "clause-after-inline"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": clause_id,
                "parent_id": ROOT_ID,
                "kind": "CLAUSE",
                "order": 10,
                "label": "e) 供应商应提交完整的验证报",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 5,
                "page_start": 11,
                "page_end": 11,
                "source_refs": [{"source_id": "source-inline-owner"}],
            },
            {
                "id": continuation_id,
                "parent_id": clause_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "告。",
                "boundary_basis": "NATIVE_BLOCK",
                "page_start": 11,
                "page_end": 11,
                "source_refs": [{"source_id": "source-inline-content"}],
            },
            {
                "id": child_id,
                "parent_id": clause_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "f) 后续要求",
                "boundary_basis": "EXPLICIT_NUMBER",
                "source_refs": [{"source_id": "source-next-clause"}],
            },
        ]
        workbench["relations"] = [
            {
                "kind": "CONTINUES",
                "from_id": clause_id,
                "to_id": continuation_id,
            }
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        direct = next(
            node
            for node in snapshot.nodes
            if node.projection_role == "DIRECT_CONTENT"
            and node.structure_node_id == clause_id
        )

        self.assertEqual(
            tuple(block.get("structure_node_id") for block in direct.content_blocks),
            (clause_id, continuation_id),
        )
        self.assertEqual(
            direct.editable_structure_node_ids,
            (continuation_id,),
        )

    def test_numbered_title_is_not_projected_into_owned_intro_content(self) -> None:
        title_id = "clause-numbered-title-with-intro"
        intro_id = "content-numbered-title-intro"
        first_child_id = "clause-numbered-title-child-a"
        second_child_id = "clause-numbered-title-child-b"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": title_id,
                "parent_id": ROOT_ID,
                "kind": "CLAUSE",
                "order": 10,
                "label": "1）执行器环境耐久",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 1,
                "page_start": 30,
                "page_end": 30,
                "source_refs": [{"source_id": "source-numbered-title"}],
            },
            {
                "id": intro_id,
                "parent_id": title_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "环境耐久试验过后满足如下要求：",
                "boundary_basis": "NATIVE_BLOCK",
                "page_start": 30,
                "page_end": 30,
                "source_refs": [{"source_id": "source-numbered-intro"}],
            },
            {
                "id": first_child_id,
                "parent_id": title_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "a）高温耐久",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 1,
                "source_refs": [{"source_id": "source-numbered-child-a"}],
            },
            {
                "id": second_child_id,
                "parent_id": title_id,
                "kind": "CLAUSE",
                "order": 30,
                "label": "b）低温耐久",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 2,
                "source_refs": [{"source_id": "source-numbered-child-b"}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        by_id = {node.id: node for node in snapshot.nodes}
        direct = next(
            node
            for node in snapshot.nodes
            if node.projection_role == "DIRECT_CONTENT"
            and node.structure_node_id == title_id
        )

        self.assertEqual(by_id[title_id].label, "1）执行器环境耐久")
        self.assertEqual(direct.label, "环境耐久试验过后满足如下要求：")
        self.assertEqual(
            tuple(block.get("structure_node_id") for block in direct.content_blocks),
            (intro_id,),
        )
        self.assertEqual(direct.editable_structure_node_ids, (intro_id,))
        self.assertNotIn(
            "source-numbered-title",
            {str(ref.get("source_id") or "") for ref in direct.source_refs},
        )

    def test_numbered_title_container_without_direct_content_has_no_review_leaf(
        self,
    ) -> None:
        title_id = "clause-numbered-title-with-children-only"
        child_id = "clause-under-numbered-title"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": title_id,
                "parent_id": ROOT_ID,
                "kind": "CLAUSE",
                "order": 10,
                "label": "3、系统开发过程中的必备需求",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 3,
                "source_refs": [{"source_id": "source-numbered-title-only"}],
            },
            {
                "id": child_id,
                "parent_id": title_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "1）供应商应提交开发计划。",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 1,
                "source_refs": [{"source_id": "source-title-child"}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        by_id = {node.id: node for node in snapshot.nodes}

        self.assertEqual(by_id[title_id].projection_role, "STRUCTURE")
        self.assertFalse(
            any(
                node.projection_role == "DIRECT_CONTENT"
                and node.structure_node_id == title_id
                for node in snapshot.nodes
            )
        )

    def test_numbered_title_stays_structural_before_later_owned_content(
        self,
    ) -> None:
        title_id = "clause-numbered-title-before-later-content"
        child_id = "clause-between-title-and-later-content"
        later_content_id = "content-after-numbered-title-child"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": title_id,
                "parent_id": ROOT_ID,
                "kind": "CLAUSE",
                "order": 10,
                "label": "3、系统开发过程中的必备需求",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 3,
                "source_refs": [{"source_id": "source-title-before-child"}],
            },
            {
                "id": child_id,
                "parent_id": title_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "1）供应商应提交开发计划。",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 1,
                "source_refs": [{"source_id": "source-title-middle-child"}],
            },
            {
                "id": later_content_id,
                "parent_id": title_id,
                "kind": "CONTENT",
                "order": 30,
                "label": "其余资料应在设计评审前提交。",
                "boundary_basis": "NATIVE_BLOCK",
                "source_refs": [{"source_id": "source-after-title-child"}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        direct = [
            node
            for node in snapshot.nodes
            if node.projection_role == "DIRECT_CONTENT"
            and node.structure_node_id == title_id
        ]

        self.assertEqual(len(direct), 1)
        self.assertEqual(
            tuple(block.get("structure_node_id") for block in direct[0].content_blocks),
            (later_content_id,),
        )

    def test_numbered_colon_lead_in_without_direct_content_stays_reviewable(
        self,
    ) -> None:
        owner_id = "clause-numbered-colon-lead-in"
        child_id = "clause-under-numbered-colon-lead-in"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": owner_id,
                "parent_id": ROOT_ID,
                "kind": "CLAUSE",
                "order": 10,
                "label": "1）供应商应满足以下要求：",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 1,
                "source_refs": [{"source_id": "source-numbered-lead-in"}],
            },
            {
                "id": child_id,
                "parent_id": owner_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "a）提交完整验证报告。",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 1,
                "source_refs": [{"source_id": "source-lead-in-child"}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        lead_in = next(
            node
            for node in snapshot.nodes
            if node.projection_role == "DIRECT_CONTENT"
            and node.structure_node_id == owner_id
        )

        self.assertEqual(lead_in.label, "1）供应商应满足以下要求：")
        self.assertEqual(
            tuple(block.get("structure_node_id") for block in lead_in.content_blocks),
            (owner_id,),
        )

    def test_numbered_inline_body_uses_exact_adjacent_source_spans(self) -> None:
        owner_id = "clause-inline-exact-span"
        continuation_id = "content-inline-exact-span"
        child_id = "clause-after-inline-exact-span"
        source_id = "source-inline-exact-span"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": owner_id,
                "parent_id": ROOT_ID,
                "kind": "CLAUSE",
                "order": 10,
                "label": "1）供应商应提交完整的验证报",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 1,
                "source_refs": [{"source_id": source_id}],
                "source_spans": [
                    {
                        "source_ref": {"source_id": source_id},
                        "start_offset": 0,
                        "end_offset": 15,
                    }
                ],
            },
            {
                "id": continuation_id,
                "parent_id": owner_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "告。",
                "boundary_basis": "NATIVE_BLOCK",
                "source_refs": [{"source_id": source_id}],
                "source_spans": [
                    {
                        "source_ref": {"source_id": source_id},
                        "start_offset": 15,
                        "end_offset": 17,
                    }
                ],
            },
            {
                "id": child_id,
                "parent_id": owner_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "a）后续要求",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 1,
                "source_refs": [{"source_id": "source-after-exact-span"}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        direct = next(
            node
            for node in snapshot.nodes
            if node.projection_role == "DIRECT_CONTENT"
            and node.structure_node_id == owner_id
        )

        self.assertEqual(
            tuple(block.get("structure_node_id") for block in direct.content_blocks),
            (owner_id, continuation_id),
        )

    def test_continuation_to_later_content_does_not_absorb_numbered_title(
        self,
    ) -> None:
        owner_id = "clause-title-with-later-continuation"
        first_content_id = "content-before-later-continuation"
        later_content_id = "content-later-continuation"
        child_id = "clause-after-later-continuation"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": owner_id,
                "parent_id": ROOT_ID,
                "kind": "CLAUSE",
                "order": 10,
                "label": "1）环境耐久",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 1,
                "page_start": 3,
                "page_end": 3,
                "source_refs": [{"source_id": "source-later-title"}],
            },
            {
                "id": first_content_id,
                "parent_id": owner_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "试验后满足如下要求：",
                "page_start": 3,
                "page_end": 3,
                "source_refs": [{"source_id": "source-first-owned-content"}],
            },
            {
                "id": later_content_id,
                "parent_id": owner_id,
                "kind": "CONTENT",
                "order": 12,
                "label": "并保留完整记录。",
                "page_start": 3,
                "page_end": 3,
                "source_refs": [{"source_id": "source-later-owned-content"}],
            },
            {
                "id": child_id,
                "parent_id": owner_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "a）后续要求",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 1,
                "source_refs": [{"source_id": "source-after-later-content"}],
            },
        ]
        workbench["relations"] = [
            {
                "kind": "CONTINUES",
                "from_id": owner_id,
                "to_id": later_content_id,
            }
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        direct = next(
            node
            for node in snapshot.nodes
            if node.projection_role == "DIRECT_CONTENT"
            and node.structure_node_id == owner_id
        )

        self.assertEqual(
            tuple(block.get("structure_node_id") for block in direct.content_blocks),
            (first_content_id, later_content_id),
        )

    def test_complete_numbered_statement_stays_separate_despite_continues(self) -> None:
        owner_id = "clause-complete-before-content"
        content_id = "content-after-complete-clause"
        child_id = "clause-after-complete-content"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": owner_id,
                "parent_id": ROOT_ID,
                "kind": "CLAUSE",
                "order": 10,
                "label": "1）供应商应提交验证报告。",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 1,
                "page_start": 3,
                "page_end": 3,
                "source_refs": [{"source_id": "source-complete-owner"}],
            },
            {
                "id": content_id,
                "parent_id": owner_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "报告还应包含原始记录。",
                "page_start": 3,
                "page_end": 3,
                "source_refs": [{"source_id": "source-after-complete-owner"}],
            },
            {
                "id": child_id,
                "parent_id": owner_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "a）后续要求",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 1,
                "source_refs": [{"source_id": "source-after-complete-content"}],
            },
        ]
        workbench["relations"] = [
            {
                "kind": "CONTINUES",
                "from_id": owner_id,
                "to_id": content_id,
            }
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        direct = [
            node
            for node in snapshot.nodes
            if node.projection_role == "DIRECT_CONTENT"
            and node.structure_node_id == owner_id
        ]

        self.assertEqual(len(direct), 2)
        self.assertEqual(
            {
                tuple(block.get("structure_node_id") for block in node.content_blocks)
                for node in direct
            },
            {(owner_id,), (content_id,)},
        )

    def test_proxy_keeps_independent_title_exact_span_and_body_refs(self) -> None:
        clause_id = "section-independent-title-and-body"
        body_id = "content-independent-title-and-body"
        title_source_id = "source-independent-title"
        body_source_id = "source-independent-body"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": clause_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "1.2.1 基本原则",
                "explicit_label": "1.2.1",
                "boundary_basis": "EXPLICIT_NUMBER",
                "page_start": 9,
                "page_end": 9,
                "source_refs": [
                    {
                        "kind": "SOURCE_NODE",
                        "source_id": title_source_id,
                        "page_number": 9,
                    }
                ],
                "source_spans": [
                    {
                        "source_ref": {
                            "kind": "SOURCE_NODE",
                            "source_id": title_source_id,
                            "page_number": 9,
                        },
                        "start_offset": 0,
                        "end_offset": 8,
                    }
                ],
            },
            {
                "id": body_id,
                "parent_id": clause_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "供应商的报价应包含全部必要活动。",
                "page_start": 9,
                "page_end": 9,
                "source_refs": [
                    {
                        "kind": "SOURCE_NODE",
                        "source_id": body_source_id,
                        "page_number": 9,
                    }
                ],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        projected = {node.id: node for node in snapshot.nodes}[clause_id]

        self.assertEqual(projected.projection_role, "STRUCTURE_PROXY")
        self.assertEqual(
            [
                (
                    str(ref.get("kind") or ""),
                    str(ref.get("source_id") or ""),
                    ref.get("start_offset"),
                    ref.get("end_offset"),
                )
                for ref in projected.source_refs
            ],
            [
                ("SOURCE_NODE", title_source_id, None, None),
                ("SOURCE_NODE", title_source_id, 0, 8),
                ("SOURCE_NODE", body_source_id, None, None),
                ("TEXT", body_source_id, None, None),
            ],
        )
        self.assertEqual(
            _test_block_text(projected.content_blocks),
            "供应商的报价应包含全部必要活动。",
        )

    def test_explicit_clause_does_not_join_content_across_table_boundary(
        self,
    ) -> None:
        clause_id = "clause-inline-table-boundary"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": clause_id,
                "parent_id": ROOT_ID,
                "kind": "CLAUSE",
                "order": 10,
                "label": "b) 供应商应提交下列",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 2,
                "page_start": 1,
                "page_end": 1,
                "source_refs": [
                    {
                        "source_id": "source-inline-before-table",
                        "page_number": 1,
                    }
                ],
            },
            {
                "id": "table-inline-boundary",
                "parent_id": clause_id,
                "kind": "TABLE",
                "order": 11,
                "label": "交付物表",
                "source_refs": [{"source_id": "source-inline-table"}],
            },
            {
                "id": "content-after-inline-table",
                "parent_id": clause_id,
                "kind": "CONTENT",
                "order": 12,
                "label": "文件。",
                "page_start": 1,
                "page_end": 1,
                "source_refs": [
                    {
                        "source_id": "source-content-after-inline-table",
                        "page_number": 1,
                    }
                ],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        direct = [
            node for node in snapshot.nodes if node.projection_role == "DIRECT_CONTENT"
        ]

        self.assertEqual(len(direct), 2)
        self.assertEqual(
            [_test_block_text(node.content_blocks) for node in direct],
            ["b) 供应商应提交下列", "文件。"],
        )

    def test_child_table_and_figure_boundaries_split_non_atomic_content_zones(
        self,
    ) -> None:
        section_id = "section-content-zone-boundaries"
        child_id = "child-content-zone-boundary"
        table_id = "table-content-zone-boundary"
        figure_id = "figure-content-zone-boundary"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "1 范围",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": "content-before-child",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "子结构之前的正文。",
                "source_refs": [{"source_id": "source-before-child"}],
            },
            {
                "id": child_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "1.1 子条款",
                "source_refs": [{"source_id": "source-child"}],
            },
            {
                "id": "content-before-table",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 30,
                "label": "表格之前的正文。",
                "source_refs": [{"source_id": "source-before-table"}],
            },
            {
                "id": table_id,
                "parent_id": section_id,
                "kind": "TABLE",
                "order": 40,
                "label": "参数表",
                "source_refs": [{"source_id": "source-table"}],
            },
            {
                "id": "content-before-figure",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 50,
                "label": "图片之前的正文。",
                "source_refs": [{"source_id": "source-before-figure"}],
            },
            {
                "id": figure_id,
                "parent_id": section_id,
                "kind": "FIGURE",
                "order": 60,
                "label": "试验布置图",
                "source_refs": [{"source_id": "source-figure"}],
            },
            {
                "id": "content-after-figure",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 70,
                "label": "图片之后的正文。",
                "source_refs": [{"source_id": "source-after-figure"}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        direct = [
            node
            for node in snapshot.nodes
            if node.projection_role == "DIRECT_CONTENT"
            and node.structure_node_id == section_id
        ]

        self.assertEqual(
            [_test_block_text(node.content_blocks) for node in direct],
            [
                "子结构之前的正文。",
                "表格之前的正文。",
                "图片之前的正文。",
                "图片之后的正文。",
            ],
        )
        self.assertEqual(
            tuple(
                block["structure_node_id"]
                for node in direct
                for block in node.content_blocks
            ),
            (
                "content-before-child",
                "content-before-table",
                "content-before-figure",
                "content-after-figure",
            ),
        )

    def test_only_explicit_body_references_bind_shared_image_to_content_zone(
        self,
    ) -> None:
        section_id = "section-shared-figure-reference"
        before_id = "content-before-shared-figure"
        media_id = "media-shared-figure"
        caption_id = "caption-shared-figure"
        after_id = "content-after-shared-figure"

        def snapshot_for(
            relations: list[dict[str, str]],
            *,
            media_page: int = 20,
        ) -> RequirementTreeSnapshotV1:
            workbench = _workbench()
            workbench["structure"]["nodes"] = [
                workbench["structure"]["nodes"][0],
                {
                    "id": section_id,
                    "parent_id": ROOT_ID,
                    "kind": "SECTION",
                    "order": 10,
                    "label": "9 系统方案",
                    "page_start": 20,
                    "page_end": 20,
                    "source_refs": [{"source_id": "source-section", "page_number": 20}],
                },
                {
                    "id": before_id,
                    "parent_id": section_id,
                    "kind": "CONTENT",
                    "order": 11,
                    "label": "正文 A 明确说明系统方案。",
                    "page_start": 20,
                    "page_end": 20,
                    "source_refs": [
                        {
                            "source_id": "source-before-shared-figure",
                            "source_kind": "PARAGRAPH",
                            "page_number": 20,
                        }
                    ],
                },
                {
                    "id": media_id,
                    "parent_id": section_id,
                    "kind": "CONTENT",
                    "order": 12,
                    "label": "图片",
                    "page_start": media_page,
                    "page_end": media_page,
                    "source_refs": [
                        {
                            "source_id": "source-shared-figure",
                            "source_kind": "IMAGE",
                            "page_number": media_page,
                        }
                    ],
                },
                {
                    "id": caption_id,
                    "parent_id": section_id,
                    "kind": "CONTENT",
                    "order": 13,
                    "label": "图 1 系统方案",
                    "page_start": 20,
                    "page_end": 20,
                    "source_refs": [
                        {
                            "source_id": "source-shared-caption",
                            "source_kind": "PARAGRAPH",
                            "page_number": 20,
                        }
                    ],
                },
                {
                    "id": after_id,
                    "parent_id": section_id,
                    "kind": "CONTENT",
                    "order": 14,
                    "label": "正文 B 规定后续交付要求。",
                    "page_start": 20,
                    "page_end": 20,
                    "source_refs": [
                        {
                            "source_id": "source-after-shared-figure",
                            "source_kind": "PARAGRAPH",
                            "page_number": 20,
                        }
                    ],
                },
            ]
            workbench["relations"] = relations
            return build_requirement_tree_snapshot(
                workbench=workbench,
                payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
            )

        cases = (
            ("no-reference", [], 20, (False, False)),
            (
                "caption-only",
                [{"kind": "REFERENCES", "from_id": caption_id, "to_id": media_id}],
                20,
                (False, False),
            ),
            (
                "body-before",
                [{"kind": "REFERENCES", "from_id": before_id, "to_id": media_id}],
                20,
                (True, False),
            ),
            (
                "body-after",
                [{"kind": "REFERENCES", "from_id": after_id, "to_id": media_id}],
                20,
                (False, True),
            ),
            (
                "both-bodies",
                [
                    {"kind": "REFERENCES", "from_id": before_id, "to_id": media_id},
                    {"kind": "REFERENCES", "from_id": after_id, "to_id": media_id},
                ],
                20,
                (True, True),
            ),
            (
                "target-is-not-media",
                [{"kind": "REFERENCES", "from_id": before_id, "to_id": after_id}],
                20,
                (False, False),
            ),
            (
                "contradictory-page",
                [{"kind": "REFERENCES", "from_id": before_id, "to_id": media_id}],
                21,
                (False, False),
            ),
        )
        stable_candidate_boundaries: (
            tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] | None
        ) = None
        for case, relations, media_page, expected_media_bindings in cases:
            with self.subTest(case=case):
                snapshot = snapshot_for(relations, media_page=media_page)
                owner = next(node for node in snapshot.nodes if node.id == section_id)
                direct = tuple(
                    node
                    for node in snapshot.nodes
                    if node.projection_role == "DIRECT_CONTENT"
                    and node.structure_node_id == section_id
                )

                self.assertEqual(len(owner.figure_refs), 1)
                self.assertEqual(len(direct), 2)
                self.assertEqual(
                    tuple(_test_block_text(node.content_blocks) for node in direct),
                    (
                        "正文 A 明确说明系统方案。",
                        "正文 B 规定后续交付要求。",
                    ),
                )
                self.assertEqual(
                    tuple(bool(node.figure_refs) for node in direct),
                    expected_media_bindings,
                )
                for node, expected_binding in zip(
                    direct,
                    expected_media_bindings,
                    strict=True,
                ):
                    self.assertEqual(
                        tuple(
                            str(block.get("source_id") or "")
                            for block in node.figure_refs
                        ),
                        ("source-shared-figure",) if expected_binding else (),
                    )
                    self.assertFalse(
                        any(
                            str(block.get("kind") or "").lower() in {"image", "figure"}
                            for block in node.content_blocks
                        )
                    )

                candidate_boundaries = tuple(
                    (
                        node.id,
                        tuple(
                            str(block.get("structure_node_id") or "")
                            for block in node.content_blocks
                        ),
                        tuple(
                            str(ref.get("source_id") or "") for ref in node.source_refs
                        ),
                    )
                    for node in direct
                )
                if stable_candidate_boundaries is None:
                    stable_candidate_boundaries = candidate_boundaries
                else:
                    self.assertEqual(
                        candidate_boundaries,
                        stable_candidate_boundaries,
                    )

    def test_direct_content_page_range_uses_its_own_source_page(self) -> None:
        section_id = "section-owned-page-range"
        content_id = "content-owned-page-range"
        child_id = "child-after-owned-page-range"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "1 引用文件",
                "page_start": 13,
                "page_end": 14,
                "source_refs": [{"source_id": "source-section", "page_number": 13}],
            },
            {
                "id": content_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "如文件存在冲突，应联系责任工程师澄清。",
                # The presentation owner leaked its adjacent-page end into
                # this source block; the source point still proves page 13.
                "page_start": 13,
                "page_end": 14,
                "source_refs": [{"source_id": "source-conflict", "page_number": 13}],
            },
            {
                "id": child_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "1.1 后续条款",
                "page_start": 14,
                "page_end": 14,
                "source_refs": [
                    {"source_id": "source-next-sibling", "page_number": 14}
                ],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        direct = next(
            node for node in snapshot.nodes if node.projection_role == "DIRECT_CONTENT"
        )

        self.assertEqual((direct.page_start, direct.page_end), (13, 13))
        self.assertEqual(
            (
                direct.content_blocks[0]["page_start"],
                direct.content_blocks[0]["page_end"],
            ),
            (13, 13),
        )
        self.assertNotIn(
            14,
            {
                value
                for ref in direct.source_refs
                for value in (ref.get("page_number"), ref.get("page_end"))
                if value is not None
            },
        )

    def test_direct_content_page_range_keeps_proven_cross_page_content(
        self,
    ) -> None:
        section_id = "section-cross-page-range"
        first_id = "content-cross-page-first"
        second_id = "content-cross-page-second"
        child_id = "child-after-cross-page-range"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "1 跨页要求",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": first_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "The supplier shall pro-",
                "page_start": 13,
                "page_end": 13,
                "source_refs": [{"source_id": "source-cross-first", "page_number": 13}],
            },
            {
                "id": second_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 12,
                "label": "vide the validation report.",
                "page_start": 14,
                "page_end": 14,
                "source_refs": [
                    {"source_id": "source-cross-second", "page_number": 14}
                ],
            },
            {
                "id": child_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "1.1 后续条款",
                "source_refs": [{"source_id": "source-child"}],
            },
        ]
        workbench["relations"] = [
            {"kind": "CONTINUES", "from_id": first_id, "to_id": second_id}
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        direct = [
            node for node in snapshot.nodes if node.projection_role == "DIRECT_CONTENT"
        ]

        self.assertEqual(len(direct), 1)
        self.assertEqual((direct[0].page_start, direct[0].page_end), (13, 14))
        self.assertEqual(
            [block["page_start"] for block in direct[0].content_blocks],
            [13, 14],
        )

    def test_terminal_proxy_source_refs_do_not_drift_to_next_sibling(self) -> None:
        section_id = "section-terminal-source-boundary"
        terminal_id = "terminal-owned-source-boundary"
        sibling_id = "sibling-after-terminal-source-boundary"
        terminal_text = "e) 供应商对产品设计和质量负责。"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "1 职责与分工",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": terminal_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": terminal_text,
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 5,
                "page_start": 13,
                "page_end": 13,
                # A broad fallback points at the following sibling, while the
                # exact span still proves this terminal clause's own source.
                "source_refs": [
                    {"source_id": "source-next-sibling", "page_number": 14}
                ],
                "source_spans": [
                    {
                        "source_ref": {
                            "kind": "SOURCE_NODE",
                            "source_id": "source-terminal",
                            "page_number": 13,
                        },
                        "start_offset": 0,
                        "end_offset": len(terminal_text),
                    }
                ],
            },
            {
                "id": sibling_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 30,
                "label": "供应商与主机厂的职责如下表所示：",
                "source_refs": [
                    {"source_id": "source-next-sibling", "page_number": 14}
                ],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "review_groups": [
                    {
                        "boundary_structure_node_id": section_id,
                        "context_blocks": [
                            {
                                "kind": "text",
                                "source_id": "source-section",
                                "text": "1 职责与分工",
                            },
                            {
                                "kind": "text",
                                "source_id": "source-terminal",
                                "text": terminal_text,
                                "page_start": 13,
                                "page_end": 13,
                                "source_refs": [
                                    {
                                        "source_id": "source-terminal",
                                        "page_number": 13,
                                    },
                                    {
                                        "source_id": "source-next-sibling",
                                        "page_number": 14,
                                    },
                                ],
                            },
                            {
                                "kind": "text",
                                "source_id": "source-next-sibling",
                                "text": "供应商与主机厂的职责如下表所示：",
                                "page_start": 14,
                                "page_end": 14,
                            },
                        ],
                    }
                ],
            },
        )
        terminal = next(node for node in snapshot.nodes if node.id == terminal_id)
        source_ids = {str(ref.get("source_id") or "") for ref in terminal.source_refs}

        self.assertEqual(terminal.projection_role, "STRUCTURE_PROXY")
        self.assertEqual(_test_block_text(terminal.content_blocks), terminal_text)
        self.assertEqual(source_ids, {"source-terminal"})
        self.assertNotIn("source-next-sibling", source_ids)

    def test_published_terminal_proxy_ignores_stale_next_sibling_source_ref(
        self,
    ) -> None:
        section_id = "section-published-terminal-boundary"
        terminal_id = "published-terminal-boundary"
        sibling_id = "published-sibling-after-terminal"
        terminal_text = "e) 供应商对产品设计和质量负责。"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "1 职责与分工",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": terminal_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": terminal_text,
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 5,
                "source_refs": [
                    {"source_id": "source-next-sibling", "page_number": 14}
                ],
                "source_spans": [
                    {
                        "source_ref": {
                            "kind": "SOURCE_NODE",
                            "source_id": "source-terminal",
                            "page_number": 13,
                        },
                        "start_offset": 0,
                        "end_offset": len(terminal_text),
                    }
                ],
            },
            {
                "id": sibling_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 30,
                "label": "供应商与主机厂的职责如下表所示：",
                "source_refs": [
                    {"source_id": "source-next-sibling", "page_number": 14}
                ],
            },
        ]
        published = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
            mode=RequirementTreeMode.REQUIREMENT_SET,
            requirement_set_id="set-published-terminal-boundary",
            requirement_entries=[
                {
                    "id": "published-terminal-provider",
                    "requirement_entry_id": "published-terminal-entry",
                    "requirement_tree_node_id": terminal_id,
                    "owner_structure_node_id": terminal_id,
                    "source_order": 20,
                    "display_text": terminal_text,
                    "source_refs": [
                        {
                            "source_id": "source-next-sibling",
                            "page_number": 14,
                        }
                    ],
                    "projection_role": "STRUCTURE_PROXY",
                }
            ],
            forced_state=RequirementTreeState.READY,
        )
        terminal = next(node for node in published.nodes if node.id == terminal_id)
        source_ids = {str(ref.get("source_id") or "") for ref in terminal.source_refs}

        self.assertEqual(terminal.projection_role, "STRUCTURE_PROXY")
        self.assertEqual(source_ids, {"source-terminal"})
        self.assertNotIn("source-next-sibling", source_ids)

    def test_container_direct_content_uses_non_atomic_zones_beside_full_context(
        self,
    ) -> None:
        document_id = "document-direct-content"
        section_id = "section-direct-content"
        child_id = "child-direct-content"
        pure_heading_id = "child-pure-heading"
        intro_one_id = "content-intro-one"
        intro_two_id = "content-intro-two"
        child_body_id = "content-child-body"
        child_body_two_id = "content-child-body-two"
        shared_intro_id = "content-shared-intro"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            {
                "id": document_id,
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": section_id,
                "parent_id": document_id,
                "kind": "SECTION",
                "order": 10,
                "label": "1 责任范围",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": intro_one_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "供应商应提交设计说明。",
                "source_refs": [{"source_id": "source-intro-one"}],
            },
            {
                "id": intro_two_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 12,
                "label": "并对其完整性负责。",
                "source_refs": [{"source_id": "source-intro-two"}],
            },
            {
                "id": child_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "a) 设计资料",
                "source_refs": [{"source_id": "source-child"}],
            },
            {
                "id": child_body_id,
                "parent_id": child_id,
                "kind": "CONTENT",
                "order": 21,
                "label": "设计资料至少包括二维图。",
                "source_refs": [{"source_id": "source-child-body"}],
            },
            {
                "id": child_body_two_id,
                "parent_id": child_id,
                "kind": "CONTENT",
                "order": 22,
                "label": "还应包括材料清单。",
                "source_refs": [{"source_id": "source-child-body-two"}],
            },
            {
                "id": shared_intro_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 30,
                "label": "下列选项均适用：",
                "source_refs": [{"source_id": "source-shared-intro"}],
            },
            {
                "id": pure_heading_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 40,
                "label": "b) 验证资料",
                "source_refs": [{"source_id": "source-pure-heading"}],
            },
        ]
        workbench["relations"] = [
            {
                "kind": "CONTINUES",
                "from_id": intro_one_id,
                "to_id": intro_two_id,
            },
            {
                "kind": "APPLIES_TO",
                "from_id": shared_intro_id,
                "to_id": pure_heading_id,
            },
            {
                # A broad cross-page reading relation cannot erase a grounded
                # child boundary; the source groups stay independently
                # reviewable.
                "kind": "CONTINUES",
                "from_id": intro_two_id,
                "to_id": shared_intro_id,
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "review_groups": [
                    {
                        "boundary_structure_node_id": section_id,
                        "context_blocks": [
                            {
                                "kind": "text",
                                "source_id": "source-section",
                                "text": "1 责任范围",
                            },
                            {
                                "kind": "text",
                                "source_id": "source-intro-one",
                                "text": "供应商应提交设计说明。",
                            },
                            {
                                "kind": "text",
                                "source_id": "source-intro-two",
                                "text": "并对其完整性负责。",
                            },
                            {
                                "kind": "text",
                                "source_id": "source-child",
                                "text": "a) 设计资料",
                            },
                            {
                                "kind": "text",
                                "source_id": "source-child-body",
                                "text": "设计资料至少包括二维图。",
                            },
                            {
                                "kind": "text",
                                "source_id": "source-child-body-two",
                                "text": "还应包括材料清单。",
                            },
                            {
                                "kind": "text",
                                "source_id": "source-shared-intro",
                                "text": "下列选项均适用：",
                            },
                            {
                                "kind": "text",
                                "source_id": "source-pure-heading",
                                "text": "b) 验证资料",
                            },
                        ],
                    }
                ],
            },
        )
        by_id = {node.id: node for node in snapshot.nodes}
        direct = [
            node
            for node in snapshot.nodes
            if node.kind == "DIRECT_CONTENT" and node.structure_node_id == section_id
        ]

        self.assertEqual(by_id[section_id].projection_role, "STRUCTURE")
        self.assertEqual(len(direct), 2)
        self.assertTrue(
            all(node.projection_role == "DIRECT_CONTENT" for node in direct)
        )
        self.assertEqual(
            [_test_block_text(node.content_blocks) for node in direct],
            [
                "供应商应提交设计说明。\n并对其完整性负责。",
                "下列选项均适用：",
            ],
        )
        self.assertIn(
            "设计资料至少包括二维图。",
            _test_block_text(by_id[section_id].content_blocks),
        )
        self.assertTrue(
            all(
                "设计资料至少包括二维图。" not in _test_block_text(node.content_blocks)
                for node in direct
            )
        )
        self.assertEqual(by_id[child_id].projection_role, "STRUCTURE_PROXY")
        self.assertEqual(
            _test_block_text(by_id[child_id].content_blocks),
            "设计资料至少包括二维图。\n还应包括材料清单。",
        )
        self.assertEqual(by_id[pure_heading_id].projection_role, "STRUCTURE")
        self.assertEqual(by_id[document_id].requirement_count, 0)
        self.assertEqual(by_id[document_id].review_item_count, 3)
        self.assertEqual(
            direct[1].content_blocks[0]["applies_to_structure_node_ids"],
            (pure_heading_id,),
        )

        invalid = snapshot.model_dump(mode="json")
        direct_payload = next(
            item for item in invalid["nodes"] if item["kind"] == "DIRECT_CONTENT"
        )
        direct_payload["projection_role"] = "STRUCTURE"
        with self.assertRaisesRegex(
            ValidationError,
            "direct content must be a leaf review projection",
        ):
            RequirementTreeSnapshotV1.model_validate(invalid)

    def test_document_root_content_does_not_manufacture_review_candidates(
        self,
    ) -> None:
        workbench = _workbench()
        workbench["structure"]["nodes"].extend(
            [
                {
                    "id": "root-furniture-one",
                    "parent_id": ROOT_ID,
                    "kind": "CONTENT",
                    "order": 1,
                    "label": "企业标准封面",
                    "source_refs": [{"source_id": "source-root-one"}],
                },
                {
                    "id": "root-furniture-two",
                    "parent_id": ROOT_ID,
                    "kind": "CONTENT",
                    "order": 2,
                    "label": "第 1 页 共 20 页",
                    "source_refs": [{"source_id": "source-root-two"}],
                },
            ]
        )

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )

        self.assertFalse(
            any(
                node.projection_role == "DIRECT_CONTENT"
                and node.structure_node_id == ROOT_ID
                for node in snapshot.nodes
            )
        )
        root = next(node for node in snapshot.nodes if node.id == ROOT_ID)
        self.assertEqual(root.review_item_count, 0)

    def test_adjacent_spans_share_non_atomic_zone_and_keep_translation_lineage(
        self,
    ) -> None:
        document_id = "document-no-implicit-continuation"
        section_id = "section-no-implicit-continuation"
        child_id = "child-no-implicit-continuation"
        workbench = _workbench()
        content_specs = (
            ("content-adjacent-a", 11, "第一条独立说明。", "source-shared", 0, 10),
            ("content-adjacent-b", 12, "第二条独立说明。", "source-shared", 10, 20),
            ("content-zh", 13, "中文要求。", "source-zh", 0, 5),
            ("content-en", 14, "English requirement.", "source-en", 0, 20),
        )
        workbench["structure"]["nodes"] = [
            {
                "id": document_id,
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": section_id,
                "parent_id": document_id,
                "kind": "SECTION",
                "order": 10,
                "label": "1 范围",
                "source_refs": [{"source_id": "source-section"}],
            },
            *[
                {
                    "id": node_id,
                    "parent_id": section_id,
                    "kind": "CONTENT",
                    "order": order,
                    "label": text,
                    "source_spans": [
                        {
                            "source_ref": {"source_id": source_id},
                            "start_offset": start,
                            "end_offset": end,
                        }
                    ],
                }
                for node_id, order, text, source_id, start, end in content_specs
            ],
            {
                "id": child_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "a) 子项",
                "source_refs": [{"source_id": "source-child"}],
            },
        ]
        workbench["relations"] = [
            {
                "kind": "TRANSLATION_OF",
                "from_id": "content-en",
                "to_id": "content-zh",
            }
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        direct = [node for node in snapshot.nodes if node.kind == "DIRECT_CONTENT"]

        self.assertEqual(len(direct), 1)
        translated = direct[0]
        self.assertEqual(
            _test_block_text(translated.content_blocks),
            ("第一条独立说明。\n第二条独立说明。\n中文要求。\nEnglish requirement."),
        )
        self.assertEqual(translated.summary, "第一条独立说明。")
        self.assertEqual(
            tuple(block["structure_node_id"] for block in translated.content_blocks),
            (
                "content-adjacent-a",
                "content-adjacent-b",
                "content-zh",
                "content-en",
            ),
        )
        translated_block = translated.content_blocks[-1]
        self.assertEqual(
            translated_block["translation_peer_structure_node_ids"],
            ("content-zh",),
        )

    def test_typed_bilingual_group_projects_one_candidate_with_both_exact_sides(
        self,
    ) -> None:
        workbench, policy, group, mismatch = _typed_bilingual_workbench()

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )

        reviewable = tuple(
            node
            for node in snapshot.nodes
            if node.projection_role in {"STRUCTURE_PROXY", "DIRECT_CONTENT"}
        )
        self.assertEqual(len(reviewable), 1)
        candidate = reviewable[0]
        self.assertEqual(candidate.id, group.canonical_structure_node_id)
        self.assertEqual(candidate.review_item_count, 1)
        self.assertEqual(
            tuple(block["text"] for block in candidate.content_blocks),
            (
                "1) Supplier shall submit a validation report.",
                "1）供应商应提交验证报告。",
            ),
        )
        self.assertEqual(
            candidate.summary,
            "1) Supplier shall submit a validation report.",
        )
        self.assertIsNotNone(candidate.bilingual_context)
        assert candidate.bilingual_context is not None
        context = candidate.bilingual_context
        self.assertEqual(context.counterpart_group_id, group.counterpart_group_id)
        self.assertEqual(
            context.document_language_policy_id,
            policy.document_language_policy_id,
        )
        self.assertEqual(context.authority_language, "zh-CN")
        self.assertEqual(context.authority_basis, "DECLARED_DOCUMENT_POLICY")
        self.assertTrue(context.equal_validity)
        self.assertEqual(
            context.mismatch_signal_ids,
            (mismatch.mismatch_signal_id,),
        )
        self.assertTrue(context.mismatch_review_required)
        self.assertEqual(candidate.status, "NEEDS_ATTENTION")
        self.assertEqual(candidate.attention_count, 1)
        self.assertEqual(
            {span.language for span in context.source_spans},
            {"en", "zh-CN"},
        )
        exact_refs = tuple(
            ref for ref in candidate.source_refs if ref.get("bilingual_span_id")
        )
        self.assertEqual(len(exact_refs), 2)
        self.assertEqual(
            {ref["owner_structure_node_id"] for ref in exact_refs},
            set(group.member_structure_node_ids),
        )
        self.assertEqual(
            {ref["bilingual_language"] for ref in exact_refs},
            {"en", "zh-CN"},
        )
        counterpart = next(
            node
            for node in snapshot.nodes
            if node.id
            == next(
                member_id
                for member_id in group.member_structure_node_ids
                if member_id != group.canonical_structure_node_id
            )
        )
        self.assertEqual(counterpart.projection_role, "STRUCTURE")
        self.assertEqual(counterpart.review_item_count, 0)
        self.assertIsNone(counterpart.bilingual_context)

        serialized = json.loads(snapshot.model_dump_json())
        restored = RequirementTreeSnapshotV1.model_validate(serialized)
        restored_candidate = next(
            node for node in restored.nodes if node.id == candidate.id
        )
        self.assertEqual(restored_candidate.bilingual_context, context)

    def test_full_block_sidecar_coalesces_multisegment_side_without_source_ir(
        self,
    ) -> None:
        """Display trimming must not invalidate a persisted SourceIR block."""

        source_ir_id = "source-ir-bilingual-fast-read"
        document_id = "document-bilingual-fast-read"
        section_id = "section-bilingual-fast-read"
        clause_id = "clause-bilingual-fast-read"
        chinese_text = "2、供应商应依次完成试验；"
        english_first_source_text = (
            "The supplier shall complete the tests in the prescribed sequence.\n"
        )
        english_first_display_text = english_first_source_text.rstrip()
        english_second_text = "Written approval is required for changes;"
        chinese_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id="node-" + "a" * 32,
        )
        english_first_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id="node-" + "b" * 32,
        )
        english_second_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id="node-" + "c" * 32,
        )
        chinese_span = _bilingual_span(
            source_ref=chinese_ref,
            end_offset=len(chinese_text),
            language=BilingualLanguageEvidence.ZH_CN,
        )
        english_first_span = _bilingual_span(
            source_ref=english_first_ref,
            end_offset=len(english_first_source_text),
            language=BilingualLanguageEvidence.EN,
        )
        english_second_span = _bilingual_span(
            source_ref=english_second_ref,
            end_offset=len(english_second_text),
            language=BilingualLanguageEvidence.EN,
        )
        policy_id = stable_document_language_policy_id(
            english_declaration_spans=(),
            chinese_declaration_spans=(),
            equal_validity=None,
            conflict_precedence="UNKNOWN",
            authority_basis="UNKNOWN",
            policy_scope_source_ir_id=source_ir_id,
        )
        policy = DocumentLanguagePolicy(
            document_language_policy_id=policy_id,
            authority_basis="UNKNOWN",
            policy_scope_source_ir_id=source_ir_id,
            equal_validity=None,
            conflict_precedence="UNKNOWN",
        )
        group_id = stable_bilingual_counterpart_group_id(
            document_language_policy_id=policy_id,
            canonical_structure_node_id=clause_id,
            member_structure_node_ids=(clause_id,),
            english_spans=(english_first_span, english_second_span),
            chinese_spans=(chinese_span,),
        )
        group = BilingualCounterpartGroup(
            counterpart_group_id=group_id,
            document_language_policy_id=policy_id,
            canonical_structure_node_id=clause_id,
            member_structure_node_ids=(clause_id,),
            english_spans=(english_first_span, english_second_span),
            chinese_spans=(chinese_span,),
        )
        bilingual_sidecar = json.loads(
            json.dumps(
                _serialize_bilingual_projection_sidecar(
                    SimpleNamespace(
                        document_language_policy=policy,
                        bilingual_counterpart_groups=(group,),
                        bilingual_mismatch_signals=(),
                    )
                ),
                ensure_ascii=False,
            )
        )
        workbench = _workbench()
        workbench["source"]["source_ir_id"] = source_ir_id
        workbench["document_id"] = document_id
        workbench["structure"].update(
            {
                "source_ir_id": source_ir_id,
                "bilingual_projection": bilingual_sidecar,
                "nodes": [
                    {
                        "id": document_id,
                        "parent_id": "",
                        "kind": "DOCUMENT",
                        "order": 0,
                        "label": "Bilingual fast-read document",
                        "source_refs": [],
                    },
                    {
                        "id": section_id,
                        "parent_id": document_id,
                        "kind": "SECTION",
                        "order": 10,
                        "label": "2 Test sequence",
                        "source_refs": [{"source_id": "node-" + "d" * 32}],
                    },
                    {
                        "id": clause_id,
                        "parent_id": section_id,
                        "kind": "CLAUSE",
                        "order": 20,
                        "physical_source_order": 20,
                        "label": chinese_text,
                        "explicit_label": "2",
                        "list_ordinal": 2,
                        "boundary_basis": "EXPLICIT_NUMBER",
                        "source_refs": [chinese_ref.model_dump(mode="json")],
                    },
                    {
                        "id": "content-bilingual-fast-read-en-1",
                        "parent_id": clause_id,
                        "kind": "CONTENT",
                        "order": 21,
                        "physical_source_order": 21,
                        # Durable presentation is display-oriented and strips
                        # the source block's trailing newline.
                        "label": english_first_display_text,
                        "source_refs": [english_first_ref.model_dump(mode="json")],
                    },
                    {
                        "id": "content-bilingual-fast-read-en-2",
                        "parent_id": clause_id,
                        "kind": "CONTENT",
                        "order": 22,
                        "physical_source_order": 22,
                        "label": english_second_text,
                        "source_refs": [english_second_ref.model_dump(mode="json")],
                    },
                ],
            }
        )
        workbench["relations"] = []

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )

        reviewable = tuple(
            node
            for node in snapshot.nodes
            if node.projection_role in {"STRUCTURE_PROXY", "DIRECT_CONTENT"}
        )
        self.assertEqual(len(reviewable), 1)
        candidate = reviewable[0]
        self.assertEqual(candidate.id, clause_id)
        self.assertEqual(candidate.review_item_count, 1)
        self.assertEqual(
            tuple(block["text"] for block in candidate.content_blocks),
            (
                chinese_text,
                english_first_display_text,
                english_second_text,
            ),
        )
        self.assertIsNotNone(candidate.bilingual_context)
        assert candidate.bilingual_context is not None
        self.assertEqual(
            candidate.bilingual_context.counterpart_group_id,
            group.counterpart_group_id,
        )
        self.assertEqual(len(candidate.bilingual_context.source_spans), 3)
        exact_refs = tuple(
            ref for ref in candidate.source_refs if ref.get("bilingual_span_id")
        )
        self.assertEqual(len(exact_refs), 3)
        self.assertEqual(
            {ref["source_id"] for ref in exact_refs},
            {
                chinese_ref.source_id,
                english_first_ref.source_id,
                english_second_ref.source_id,
            },
        )

    def test_translation_relation_without_typed_group_does_not_merge_candidates(
        self,
    ) -> None:
        workbench, _, _, _ = _typed_bilingual_workbench()
        workbench["structure"]["bilingual_projection"] = (
            _serialize_bilingual_projection_sidecar(None)
        )

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )

        reviewable = tuple(
            node
            for node in snapshot.nodes
            if node.projection_role in {"STRUCTURE_PROXY", "DIRECT_CONTENT"}
        )
        self.assertEqual(len(reviewable), 2)
        self.assertTrue(all(node.bilingual_context is None for node in reviewable))

    def test_tampered_bilingual_relation_grounding_fails_closed_without_merge(
        self,
    ) -> None:
        workbench, _, _, _ = _typed_bilingual_workbench()
        self.assertEqual(
            workbench["structure"]["bilingual_projection"]["schema_version"],
            STRUCTURE_BILINGUAL_PROJECTION_SCHEMA_VERSION,
        )
        workbench["relations"][0]["source_refs"] = workbench["relations"][0][
            "source_refs"
        ][:1]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )

        reviewable = tuple(
            node
            for node in snapshot.nodes
            if node.projection_role in {"STRUCTURE_PROXY", "DIRECT_CONTENT"}
        )
        self.assertEqual(len(reviewable), 2)
        self.assertTrue(all(node.bilingual_context is None for node in reviewable))

    def test_unknown_bilingual_authority_cannot_select_a_language(self) -> None:
        _, _, group, _ = _typed_bilingual_workbench()
        spans = tuple(
            RequirementTreeBilingualSourceSpanV1(
                bilingual_span_id=span.bilingual_span_id,
                language=span.language_evidence.value,
                source_ref=span.source_ref.model_dump(mode="json"),
                owner_structure_node_id=owner_id,
                start_offset=span.start_offset,
                end_offset=span.end_offset,
                boundary_basis=span.boundary_basis.value,
            )
            for span, owner_id in zip(
                (*group.english_spans, *group.chinese_spans),
                group.member_structure_node_ids,
            )
        )
        unknown = RequirementTreeBilingualContextV1(
            counterpart_group_id=group.counterpart_group_id,
            canonical_structure_node_id=group.canonical_structure_node_id,
            member_structure_node_ids=group.member_structure_node_ids,
            authority_language="UNKNOWN",
            authority_basis="UNKNOWN",
            source_spans=spans,
        )
        self.assertEqual(unknown.authority_language, "UNKNOWN")
        with self.assertRaisesRegex(
            ValidationError,
            "unknown authority cannot select a language",
        ):
            RequirementTreeBilingualContextV1(
                **(unknown.model_dump(mode="python") | {"authority_language": "zh-CN"})
            )

    def test_bilingual_group_context_can_appear_on_only_one_candidate(self) -> None:
        workbench, _, _, _ = _typed_bilingual_workbench()
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        candidate = next(
            node for node in snapshot.nodes if node.bilingual_context is not None
        )
        duplicate = candidate.model_copy(
            update={
                "id": f"{candidate.id}-duplicate",
                "parent_id": None,
                "child_ids": (),
            }
        )

        with self.assertRaisesRegex(
            ValidationError,
            "bilingual counterpart group can project only once",
        ):
            RequirementTreeSnapshotV1(
                **(
                    snapshot.model_dump(mode="python")
                    | {
                        "root_node_ids": (*snapshot.root_node_ids, duplicate.id),
                        "nodes": (*snapshot.nodes, duplicate),
                    }
                )
            )

    def test_direct_content_ids_survive_publication_and_requirements_count_separately(
        self,
    ) -> None:
        workbench = _workbench()
        content_id = "content-published-direct"
        workbench["structure"]["nodes"].insert(
            2,
            {
                "id": content_id,
                "parent_id": SECTION_ID,
                "kind": "CONTENT",
                "order": 11,
                "label": "本节要求适用于所有接口。",
                "source_refs": [{"source_id": "source-published-direct"}],
            },
        )
        local_payload = {
            "mode": "LOCAL_STRUCTURE_PROJECTION",
            "items": [
                {
                    "id": "rule-clause",
                    "target_structure": {"id": CLAUSE_ID},
                    "text": "活塞桶直径应为 Φ32–48 mm。",
                }
            ],
        }
        working = build_requirement_tree_snapshot(
            workbench=workbench,
            payload=local_payload,
        )
        working_reviewables = [
            node
            for node in working.nodes
            if node.projection_role in {"STRUCTURE_PROXY", "DIRECT_CONTENT"}
        ]
        published_entries = [
            {
                "id": f"published-{index}",
                "requirement_entry_id": f"entry-{index}",
                "requirement_tree_node_id": node.id,
                "owner_structure_node_id": node.structure_node_id,
                "source_order": node.source_order,
                "display_text": node.summary,
                "source_refs": list(node.source_refs),
                "projection_role": node.projection_role,
            }
            for index, node in enumerate(working_reviewables)
        ]
        published_entries.append(
            {
                **_entries(published=True)[0],
                "requirement_entry_id": "semantic-entry",
            }
        )

        published = build_requirement_tree_snapshot(
            workbench=workbench,
            payload=local_payload,
            mode=RequirementTreeMode.REQUIREMENT_SET,
            requirement_set_id="set-direct-content",
            requirement_entries=published_entries,
            forced_state=RequirementTreeState.READY,
        )
        published_reviewables = {
            node.id
            for node in published.nodes
            if node.projection_role in {"STRUCTURE_PROXY", "DIRECT_CONTENT"}
        }

        self.assertEqual(
            {node.id for node in working_reviewables},
            published_reviewables,
        )
        root = next(node for node in published.nodes if node.id == ROOT_ID)
        self.assertEqual(root.requirement_count, 1)
        self.assertEqual(root.review_item_count, len(working_reviewables) + 1)

    def test_record_projection_is_a_table_barrier_not_a_text_barrier(self) -> None:
        document_id = "document-table-barrier"
        section_id = "section-table-barrier"
        table_node_id = "table-node-barrier"
        table_id = "table-physical-barrier"
        source_id = "node-" + "f" * 32
        projection = _records_projection(
            owner_id=section_id,
            table_id=table_id,
            source_id=source_id,
        )
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            {
                "id": document_id,
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": section_id,
                "parent_id": document_id,
                "kind": "SECTION",
                "order": 10,
                "label": "1 参数要求",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": "content-before-table",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "表前说明不得丢失。",
                "source_refs": [{"source_id": "source-before-table"}],
            },
            {
                "id": "table-caption-barrier",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 19,
                "label": "表 1 参数矩阵 Table 1 Parameter matrix",
                "source_refs": [{"source_id": "source-table-caption"}],
            },
            {
                "id": table_node_id,
                "parent_id": section_id,
                "kind": "TABLE",
                "order": 20,
                "label": "参数表",
                "source_refs": [{"source_id": source_id}],
            },
            {
                "id": "table-merged-label",
                "parent_id": table_node_id,
                "kind": "CONTENT",
                "order": 21,
                "label": "第一组",
                "source_refs": [{"source_id": "cell-merged-label"}],
            },
            {
                "id": "content-after-table",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 30,
                "label": "表后说明也不得丢失。",
                "source_refs": [{"source_id": "source-after-table"}],
            },
            {
                "id": "child-after-table",
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 40,
                "label": "1.1 后续条款",
                "source_refs": [{"source_id": "source-child-after-table"}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "tables": [
                    {
                        "id": table_id,
                        "source_node_id": source_id,
                        "cells": [
                            {"id": f"cell-{index:032x}", "text": str(index)}
                            for index in range(1, 5)
                        ],
                    }
                ],
                "table_record_projections": [projection.model_dump(mode="json")],
            },
        )
        direct = [node for node in snapshot.nodes if node.kind == "DIRECT_CONTENT"]
        records = [node for node in snapshot.nodes if node.kind == "TABLE_RECORD"]
        whole_tables = [
            node
            for node in snapshot.nodes
            if node.projection_role == "WHOLE_TABLE_REVIEW"
        ]

        self.assertEqual(len(direct), 2)
        self.assertEqual(len(records), 1)
        self.assertEqual(whole_tables, [])
        self.assertEqual(
            [_test_block_text(node.content_blocks) for node in direct],
            ["表前说明不得丢失。", "表后说明也不得丢失。"],
        )
        self.assertFalse(
            any(
                str(block.get("kind") or "").lower() == "table"
                for node in direct
                for block in node.content_blocks
            )
        )

    def test_untyped_content_is_not_guessed_as_an_image(
        self,
    ) -> None:
        document_id = "document-image-context"
        section_id = "section-image-context"
        child_id = "child-after-image"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            {
                "id": document_id,
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": section_id,
                "parent_id": document_id,
                "kind": "SECTION",
                "order": 10,
                "label": "9 系统方案",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": "intro-before-image",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "系统方案如下图所示。",
                "source_refs": [{"source_id": "source-intro"}],
            },
            {
                "id": "image-misclassified-as-content",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 12,
                "label": "Content",
                # Without immutable source_kind this is ordinary text.  The
                # v3 presentation boundary rejects old durable indexes rather
                # than guessing media from an English placeholder label.
                "source_refs": [{"source_id": "source-image"}],
            },
            {
                "id": "caption-after-image",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 13,
                "label": "图 1 系统方案 Figure 1 System concept",
                "source_refs": [{"source_id": "source-caption"}],
            },
            {
                "id": child_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "9.1 功能要求",
                "source_refs": [{"source_id": "source-child"}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )

        direct = [
            node for node in snapshot.nodes if node.projection_role == "DIRECT_CONTENT"
        ]
        self.assertEqual(len(direct), 1)
        self.assertEqual(
            _test_block_text(direct[0].content_blocks),
            "系统方案如下图所示。\nContent",
        )

    def test_proven_media_and_adjacent_pure_caption_are_not_reviewable(self) -> None:
        for source_kind, caption in (
            ("IMAGE", "图 1 系统方案"),
            ("FIGURE", "Figure 1 System concept"),
        ):
            with self.subTest(source_kind=source_kind):
                section_id = f"section-pure-caption-{source_kind.lower()}"
                media_id = f"media-pure-caption-{source_kind.lower()}"
                caption_id = f"caption-pure-{source_kind.lower()}"
                workbench = _workbench()
                workbench["structure"]["nodes"] = [
                    workbench["structure"]["nodes"][0],
                    {
                        "id": section_id,
                        "parent_id": ROOT_ID,
                        "kind": "SECTION",
                        "order": 10,
                        "label": "9 系统方案",
                        "source_refs": [{"source_id": "source-section"}],
                    },
                    {
                        "id": media_id,
                        "parent_id": section_id,
                        "kind": "CONTENT",
                        "order": 11,
                        "label": "Content",
                        "source_refs": [
                            {
                                "source_id": f"source-{source_kind.lower()}",
                                "source_kind": source_kind,
                                "page_number": 20,
                            }
                        ],
                    },
                    {
                        "id": caption_id,
                        "parent_id": section_id,
                        "kind": "CONTENT",
                        "order": 12,
                        "label": caption,
                        "source_refs": [
                            {
                                "source_id": f"source-caption-{source_kind.lower()}",
                                "source_kind": "PARAGRAPH",
                                "page_number": 20,
                            }
                        ],
                    },
                ]

                snapshot = build_requirement_tree_snapshot(
                    workbench=workbench,
                    payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
                )
                section = next(node for node in snapshot.nodes if node.id == section_id)

                self.assertEqual(section.review_item_count, 0)
                self.assertEqual(len(section.figure_refs), 1)
                media_block = next(
                    block
                    for block in section.content_blocks
                    if str(block.get("kind") or "") in {"image", "figure"}
                )
                self.assertEqual(media_block.get("source_order"), 11)
                self.assertEqual(media_block.get("caption"), caption)
                self.assertEqual(media_block.get("caption_source_order"), 12)
                self.assertEqual(media_block.get("caption_source_offset"), 0)
                self.assertEqual(
                    media_block.get("caption_source_refs"),
                    (
                        {
                            "source_id": f"source-caption-{source_kind.lower()}",
                            "source_kind": "PARAGRAPH",
                            "page_number": 20,
                        },
                    ),
                )
                self.assertFalse(
                    any(
                        block.get("structure_node_id") == caption_id
                        for block in section.content_blocks
                    )
                )
                self.assertFalse(
                    any(
                        node.structure_node_id == caption_id
                        and node.projection_role
                        in {"DIRECT_CONTENT", "STRUCTURE_PROXY"}
                        for node in snapshot.nodes
                    )
                )

    def test_figure_caption_adjacency_uses_physical_order_after_structure_edit(
        self,
    ) -> None:
        section_id = "section-physical-caption"
        media_id = "media-physical-caption"
        caption_id = "caption-physical-caption"
        body_id = "body-after-physical-caption"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "physical_source_order": 10,
                "label": "9 系统方案",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                # Editable Structure order deliberately differs from immutable
                # source order: the caption is still physically after the image.
                "id": caption_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "physical_source_order": 30,
                "physical_source_offset": 10,
                "label": "图 1 系统方案",
                "source_refs": [
                    {
                        "source_id": "source-physical-caption",
                        "source_kind": "PARAGRAPH",
                        "page_number": 20,
                    }
                ],
            },
            {
                "id": body_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 12,
                "physical_source_order": 30,
                "physical_source_offset": 20,
                "label": "后续正文。",
                "source_refs": [
                    {
                        "source_id": "source-after-physical-caption",
                        "source_kind": "PARAGRAPH",
                        "page_number": 20,
                    }
                ],
            },
            {
                "id": media_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 13,
                "physical_source_order": 30,
                "physical_source_offset": 0,
                "label": "Content",
                "source_refs": [
                    {
                        "source_id": "source-physical-image",
                        "source_kind": "IMAGE",
                        "page_number": 20,
                    }
                ],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        section = next(node for node in snapshot.nodes if node.id == section_id)
        media_block = next(
            block
            for block in section.content_blocks
            if block.get("structure_node_id") == media_id
        )

        self.assertEqual(media_block.get("caption"), "图 1 系统方案")
        self.assertEqual(media_block.get("caption_source_offset"), 10)
        self.assertEqual(
            [block.get("structure_node_id") for block in section.content_blocks],
            [media_id, body_id],
        )
        self.assertEqual(section.summary, "图 1 系统方案\n后续正文。")

    def test_nonadjacent_figure_caption_uses_proven_reference_relation(self) -> None:
        section_id = "section-related-caption"
        caption_id = "caption-related"
        body_id = "body-between-caption-and-media"
        media_id = "media-related"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "9 系统方案",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": caption_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "physical_source_order": 40,
                "label": "图 5 系统方案",
                "source_refs": [
                    {
                        "source_id": "source-caption-related",
                        "source_kind": "PARAGRAPH",
                        "page_number": 20,
                    }
                ],
            },
            {
                "id": body_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 12,
                "physical_source_order": 41,
                "label": "图 5 应满足接口隔离要求。",
                "source_refs": [
                    {
                        "source_id": "source-body-between",
                        "source_kind": "PARAGRAPH",
                        "page_number": 20,
                    }
                ],
            },
            {
                "id": media_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 13,
                "physical_source_order": 42,
                "label": "Content",
                "source_refs": [
                    {
                        "source_id": "source-related-image",
                        "source_kind": "IMAGE",
                        "page_number": 20,
                    }
                ],
            },
        ]
        workbench["relations"] = [
            {"kind": "REFERENCES", "from_id": caption_id, "to_id": media_id}
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        section = next(node for node in snapshot.nodes if node.id == section_id)
        media_block = next(
            block
            for block in section.content_blocks
            if block.get("structure_node_id") == media_id
        )

        self.assertEqual(media_block.get("caption"), "图 5 系统方案")
        self.assertEqual(media_block.get("caption_source_order"), 40)
        self.assertEqual(media_block.get("caption_source_offset"), 0)
        self.assertFalse(
            any(
                block.get("structure_node_id") == caption_id
                for block in section.content_blocks
            )
        )
        self.assertTrue(
            any(
                node.projection_role in {"DIRECT_CONTENT", "STRUCTURE_PROXY"}
                and any(
                    block.get("structure_node_id") == body_id
                    for block in node.content_blocks
                )
                for node in snapshot.nodes
            )
        )

    def test_ambiguous_figure_reference_keeps_caption_as_reading_evidence(
        self,
    ) -> None:
        section_id = "section-ambiguous-caption"
        caption_id = "caption-ambiguous"
        body_id = "body-after-ambiguous-caption"
        media_ids = ("media-ambiguous-a", "media-ambiguous-b")
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "9 系统方案",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": caption_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 13,
                "physical_source_order": 40,
                "label": "图 5 系统方案",
                "source_refs": [
                    {
                        "source_id": "source-caption-ambiguous",
                        "source_kind": "PARAGRAPH",
                        "page_number": 20,
                    }
                ],
            },
            {
                "id": body_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "physical_source_order": 41,
                "label": "正文仍应保持可读。",
                "source_refs": [
                    {
                        "source_id": "source-body-ambiguous",
                        "source_kind": "PARAGRAPH",
                        "page_number": 20,
                    }
                ],
            },
            *(
                {
                    "id": media_id,
                    "parent_id": section_id,
                    "kind": "CONTENT",
                    "order": 20 + index,
                    "physical_source_order": 42 + index,
                    "label": "Content",
                    "source_refs": [
                        {
                            "source_id": f"source-{media_id}",
                            "source_kind": "IMAGE",
                            "page_number": 20,
                        }
                    ],
                }
                for index, media_id in enumerate(media_ids)
            ),
        ]
        workbench["relations"] = [
            {"kind": "REFERENCES", "from_id": caption_id, "to_id": media_id}
            for media_id in media_ids
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        section = next(node for node in snapshot.nodes if node.id == section_id)

        caption_block = next(
            block
            for block in section.content_blocks
            if block.get("structure_node_id") == caption_id
        )
        self.assertEqual(caption_block.get("source_order"), 40)
        self.assertFalse(
            any(
                block.get("caption")
                for block in section.content_blocks
                if block.get("structure_node_id") in media_ids
            )
        )

        self.assertEqual(
            [block.get("structure_node_id") for block in section.content_blocks],
            [caption_id, body_id, media_ids[0], media_ids[1]],
        )
        self.assertEqual(section.summary, "图 5 系统方案\n正文仍应保持可读。")

    def test_direct_content_exposes_ordered_editable_structure_nodes(self) -> None:
        section_id = "section-with-physical-fragments"
        first_id = "content-fragment-first"
        second_id = "content-fragment-second"
        child_id = "child-after-fragments"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "4 接口要求",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": first_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "physical_source_order": 101,
                "physical_source_offset": 12,
                "label": "接口应支持诊断，",
                "source_refs": [{"source_id": "source-first", "page_number": 3}],
            },
            {
                "id": second_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 12,
                "physical_source_order": 101,
                "physical_source_offset": 0,
                "label": "并提供状态反馈。",
                "source_refs": [{"source_id": "source-second", "page_number": 3}],
            },
            {
                "id": child_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "4.1 通讯接口",
                "source_refs": [{"source_id": "source-child", "page_number": 3}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        direct = next(
            node
            for node in snapshot.nodes
            if node.projection_role == "DIRECT_CONTENT"
            and node.structure_node_id == section_id
        )

        self.assertEqual(direct.structure_node_id, section_id)
        self.assertEqual(
            direct.editable_structure_node_ids,
            (second_id, first_id),
        )
        self.assertEqual(
            [block.get("source_order") for block in direct.content_blocks],
            [101, 101],
        )
        self.assertEqual(
            [block.get("source_offset") for block in direct.content_blocks],
            [0, 12],
        )
        self.assertTrue(direct.label.startswith("并提供状态反馈。"))
        self.assertEqual(direct.source_order, 101)
        self.assertEqual(direct.source_offset, 0)

    def test_toc_split_lines_create_navigation_not_candidates(self) -> None:
        root_id = "toc-document"
        toc_id = "toc-navigation"
        target_id = "toc-target-section"
        nodes = [
            {
                "id": root_id,
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Standard",
                "source_refs": [],
            },
            {
                "id": toc_id,
                "parent_id": root_id,
                "kind": "GROUP",
                "boundary_basis": "TOC_BLOCK",
                "order": 1,
                "label": "目 录",
                "source_refs": [
                    {"source_id": "toc-number", "page_number": 2},
                    {"source_id": "toc-title", "page_number": 2},
                ],
            },
            {
                "id": target_id,
                "parent_id": root_id,
                "kind": "SECTION",
                "order": 10,
                "explicit_label": "1.2",
                "label": "1.2 报价要求",
                "page_start": 12,
                "page_end": 12,
                "source_refs": [{"source_id": "target-source", "page_number": 12}],
            },
        ]
        enriched = attach_toc_navigation_entries(
            nodes,
            source_index={
                "toc-number": {"text": "1.2"},
                "toc-title": {"text": "报价要求........................7"},
            },
        )
        toc_structure = next(node for node in enriched if node["id"] == toc_id)
        self.assertEqual(
            toc_structure["toc_entries"][0]["target_structure_node_id"],
            target_id,
        )
        self.assertEqual(toc_structure["toc_entries"][0]["target_page"], 12)
        self.assertEqual(
            [
                ref["source_id"]
                for ref in toc_structure["toc_entries"][0]["source_refs"]
            ],
            ["toc-number", "toc-title"],
        )

        workbench = _workbench()
        workbench["structure"]["nodes"] = enriched
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        toc_node = next(node for node in snapshot.nodes if node.id == toc_id)
        self.assertEqual(toc_node.review_item_count, 0)
        self.assertEqual(len(toc_node.toc_entries), 1)
        self.assertTrue(toc_node.toc_entries[0].entry_id.startswith("toc-entry-"))
        self.assertEqual(
            toc_node.toc_entries[0].target_structure_node_id,
            target_id,
        )
        self.assertFalse(
            any(
                node.projection_role in {"DIRECT_CONTENT", "STRUCTURE_PROXY"}
                and any(
                    ref.get("source_id") in {"toc-number", "toc-title"}
                    for ref in node.source_refs
                )
                for node in snapshot.nodes
            )
        )

    def test_toc_split_number_keeps_number_shaped_title_prefix(self) -> None:
        nodes = [
            {
                "id": "toc-prefix-root",
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": "toc-prefix-navigation",
                "parent_id": "toc-prefix-root",
                "kind": "GROUP",
                "boundary_basis": "TOC_BLOCK",
                "order": 1,
                "label": "Contents",
                "source_refs": [{"source_id": "toc-prefix-source"}],
            },
            {
                "id": "toc-prefix-target",
                "parent_id": "toc-prefix-root",
                "kind": "SECTION",
                "order": 10,
                "explicit_label": "8.4",
                "label": "8.4 Appendix B Interface Profile",
                "page_start": 46,
                "source_refs": [{"source_id": "toc-prefix-target-source"}],
            },
        ]

        enriched = attach_toc_navigation_entries(
            nodes,
            source_index={
                "toc-prefix-source": {
                    "text": "8.4\nAppendix B Interface Profile................41"
                }
            },
        )
        entry = enriched[1]["toc_entries"][0]

        self.assertEqual(entry["number"], "8.4")
        self.assertEqual(entry["title"], "Appendix B Interface Profile")
        self.assertEqual(entry["printed_page_label"], "41")
        self.assertEqual(entry["target_structure_node_id"], "toc-prefix-target")
        self.assertEqual(entry["target_page"], 46)

    def test_toc_multiline_title_keeps_number_shaped_first_fragment(self) -> None:
        nodes = [
            {
                "id": "toc-multiline-root",
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": "toc-multiline-navigation",
                "parent_id": "toc-multiline-root",
                "kind": "GROUP",
                "boundary_basis": "TOC_BLOCK",
                "order": 1,
                "label": "Contents",
                "source_refs": [{"source_id": "toc-multiline-source"}],
            },
            {
                "id": "toc-multiline-target",
                "parent_id": "toc-multiline-root",
                "kind": "SECTION",
                "order": 10,
                "explicit_label": "4.2",
                "label": "4.2 Appendix B Interface Profile",
                "page_start": 46,
                "source_refs": [{"source_id": "toc-multiline-target-source"}],
            },
        ]

        enriched = attach_toc_navigation_entries(
            nodes,
            source_index={
                "toc-multiline-source": {
                    "text": "4.2\nAppendix B Interface\nProfile................41"
                }
            },
        )
        entry = enriched[1]["toc_entries"][0]

        self.assertEqual(entry["number"], "4.2")
        self.assertEqual(entry["title"], "Appendix B Interface Profile")
        self.assertEqual(entry["printed_page_label"], "41")
        self.assertEqual(entry["target_structure_node_id"], "toc-multiline-target")
        self.assertEqual(entry["target_page"], 46)

    def test_toc_number_only_starts_next_entry_after_unpaged_title(self) -> None:
        nodes = [
            {
                "id": "toc-boundary-root",
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": "toc-boundary-navigation",
                "parent_id": "toc-boundary-root",
                "kind": "GROUP",
                "boundary_basis": "TOC_BLOCK",
                "order": 1,
                "label": "Contents",
                "source_refs": [{"source_id": "toc-boundary-source"}],
            },
            {
                "id": "toc-boundary-target",
                "parent_id": "toc-boundary-root",
                "kind": "SECTION",
                "order": 10,
                "explicit_label": "1.2",
                "label": "1.2 Verified scope",
                "page_start": 12,
                "source_refs": [{"source_id": "toc-boundary-target-source"}],
            },
        ]

        enriched = attach_toc_navigation_entries(
            nodes,
            source_index={
                "toc-boundary-source": {
                    "text": (
                        "1.1\nOverview without page\n"
                        "1.2\nVerified scope................5"
                    )
                }
            },
        )
        entries = enriched[1]["toc_entries"]

        self.assertEqual(len(entries), 2)
        self.assertEqual(
            (entries[0]["number"], entries[0]["title"]),
            ("1.1", "Overview without page"),
        )
        self.assertIsNone(entries[0]["target_page"])
        self.assertEqual(
            (entries[1]["number"], entries[1]["title"]),
            ("1.2", "Verified scope"),
        )
        self.assertEqual(entries[1]["target_page"], 12)

    def test_toc_complete_line_starts_next_entry_after_unpaged_title(self) -> None:
        nodes = [
            {
                "id": "toc-complete-boundary-root",
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": "toc-complete-boundary-navigation",
                "parent_id": "toc-complete-boundary-root",
                "kind": "GROUP",
                "boundary_basis": "TOC_BLOCK",
                "order": 1,
                "label": "Contents",
                "source_refs": [{"source_id": "toc-complete-boundary-source"}],
            },
            {
                "id": "toc-complete-boundary-target",
                "parent_id": "toc-complete-boundary-root",
                "kind": "SECTION",
                "order": 10,
                "explicit_label": "1.2",
                "label": "1.2 Verified scope",
                "page_start": 12,
                "source_refs": [{"source_id": "toc-complete-boundary-target-source"}],
            },
        ]

        enriched = attach_toc_navigation_entries(
            nodes,
            source_index={
                "toc-complete-boundary-source": {
                    "text": (
                        "1.1\nOverview without page\n"
                        "1.2 Verified scope................5"
                    )
                }
            },
        )
        entries = enriched[1]["toc_entries"]

        self.assertEqual(len(entries), 2)
        self.assertEqual(
            (entries[0]["number"], entries[0]["title"]),
            ("1.1", "Overview without page"),
        )
        self.assertIsNone(entries[0]["target_page"])
        self.assertEqual(
            (entries[1]["number"], entries[1]["title"]),
            ("1.2", "Verified scope"),
        )
        self.assertEqual(entries[1]["target_page"], 12)

    def test_toc_exact_target_keeps_number_shaped_final_title_fragment(self) -> None:
        nodes = [
            {
                "id": "toc-final-fragment-root",
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": "toc-final-fragment-navigation",
                "parent_id": "toc-final-fragment-root",
                "kind": "GROUP",
                "boundary_basis": "TOC_BLOCK",
                "order": 1,
                "label": "Contents",
                "source_refs": [{"source_id": "toc-final-fragment-source"}],
            },
            {
                "id": "toc-final-fragment-target",
                "parent_id": "toc-final-fragment-root",
                "kind": "SECTION",
                "order": 10,
                "explicit_label": "2.4",
                "label": "2.4 Overview of Appendix C Interface",
                "page_start": 46,
                "source_refs": [{"source_id": "toc-final-fragment-target-source"}],
            },
        ]

        enriched = attach_toc_navigation_entries(
            nodes,
            source_index={
                "toc-final-fragment-source": {
                    "text": "2.4\nOverview of\nAppendix C Interface................41"
                }
            },
        )
        entries = enriched[1]["toc_entries"]

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["number"], "2.4")
        self.assertEqual(entries[0]["title"], "Overview of Appendix C Interface")
        self.assertEqual(
            entries[0]["target_structure_node_id"],
            "toc-final-fragment-target",
        )
        self.assertEqual(entries[0]["target_page"], 46)

    def test_toc_conflicting_title_fails_closed_and_keeps_printed_page(self) -> None:
        nodes = [
            {
                "id": "toc-root-conflict",
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": "toc-conflict",
                "parent_id": "toc-root-conflict",
                "kind": "GROUP",
                "boundary_basis": "TOC_BLOCK",
                "order": 1,
                "label": "目录",
                "source_refs": [{"source_id": "toc-conflict-source"}],
            },
            {
                "id": "toc-conflict-target",
                "parent_id": "toc-root-conflict",
                "kind": "SECTION",
                "order": 10,
                "explicit_label": "1.2",
                "label": "1.2 完全不同标题",
                "page_start": 12,
                "source_refs": [{"source_id": "toc-target-source"}],
            },
        ]

        enriched = attach_toc_navigation_entries(
            nodes,
            source_index={
                "toc-conflict-source": {"text": "1.2 报价要求........................7"}
            },
        )
        entry = enriched[1]["toc_entries"][0]

        self.assertIsNone(entry["target_structure_node_id"])
        self.assertIsNone(entry["target_page"])
        self.assertEqual(entry["printed_page_label"], "7")

    def test_toc_without_page_is_preserved_as_clean_source_bound_line(self) -> None:
        nodes = [
            {
                "id": "toc-root-unresolved",
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": "toc-unresolved",
                "parent_id": "toc-root-unresolved",
                "kind": "GROUP",
                "boundary_basis": "TOC_BLOCK",
                "order": 1,
                "label": "目录",
                "source_refs": [{"source_id": "toc-unresolved-source"}],
            },
        ]

        enriched = attach_toc_navigation_entries(
            nodes,
            source_index={"toc-unresolved-source": {"text": "2.3 尚未分页章节"}},
        )
        entry = enriched[1]["toc_entries"][0]

        self.assertEqual((entry["number"], entry["title"]), ("2.3", "尚未分页章节"))
        self.assertEqual(entry["printed_page_label"], "")
        self.assertIsNone(entry["target_page"])
        self.assertEqual(
            [ref["source_id"] for ref in entry["source_refs"]],
            ["toc-unresolved-source"],
        )

    def test_unreferenced_image_stays_shared_owner_context_not_exact_text_evidence(
        self,
    ) -> None:
        section_id = "section-text-and-image"
        text_id = "content-before-image"
        media_id = "media-after-content"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "10.7 报价图纸",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": text_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "附件是本资料不可分割的一部分。",
                "source_refs": [
                    {
                        "source_id": "source-body",
                        "source_kind": "PARAGRAPH",
                        "page_number": 60,
                    }
                ],
            },
            {
                "id": media_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 12,
                "label": "图片",
                "source_refs": [
                    {
                        "source_id": "source-image",
                        "source_kind": "IMAGE",
                        "page_number": 60,
                    }
                ],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        section = next(node for node in snapshot.nodes if node.id == section_id)
        direct = next(
            node
            for node in snapshot.nodes
            if node.projection_role == "DIRECT_CONTENT"
            and node.structure_node_id == section_id
        )

        self.assertEqual(section.projection_role, "STRUCTURE")
        self.assertEqual(section.review_item_count, 1)
        self.assertEqual(
            [str(block.get("kind") or "") for block in section.content_blocks],
            ["text", "image"],
        )
        self.assertEqual(len(section.figure_refs), 1)
        self.assertEqual(section.figure_refs[0].get("source_id"), "source-image")
        self.assertEqual(
            [str(block.get("kind") or "") for block in direct.content_blocks],
            ["text"],
        )
        self.assertEqual(direct.figure_refs, ())
        self.assertEqual(
            {str(ref.get("source_id") or "") for ref in direct.source_refs},
            {"source-body"},
        )

    def test_caption_like_normative_statements_remain_reviewable(self) -> None:
        statements = (
            "图 1 应满足所有规定的安装间隙要求。",
            "Figure 1 shall comply with all specified clearance requirements.",
        )
        for index, statement in enumerate(statements, start=1):
            with self.subTest(statement=statement):
                section_id = f"section-caption-like-normative-{index}"
                content_id = f"caption-like-normative-{index}"
                workbench = _workbench()
                workbench["structure"]["nodes"] = [
                    workbench["structure"]["nodes"][0],
                    {
                        "id": section_id,
                        "parent_id": ROOT_ID,
                        "kind": "SECTION",
                        "order": 10,
                        "label": "9 系统方案",
                        "source_refs": [{"source_id": "source-section"}],
                    },
                    {
                        "id": f"media-before-normative-{index}",
                        "parent_id": section_id,
                        "kind": "CONTENT",
                        "order": 11,
                        "label": "Content",
                        "source_refs": [
                            {
                                "source_id": f"source-image-{index}",
                                "source_kind": "IMAGE",
                                "page_number": 20,
                            }
                        ],
                    },
                    {
                        "id": content_id,
                        "parent_id": section_id,
                        "kind": "CONTENT",
                        "order": 12,
                        "label": statement,
                        "source_refs": [
                            {
                                "source_id": f"source-normative-{index}",
                                "source_kind": "PARAGRAPH",
                                "page_number": 20,
                            }
                        ],
                    },
                ]

                snapshot = build_requirement_tree_snapshot(
                    workbench=workbench,
                    payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
                )
                reviewable = [
                    node
                    for node in snapshot.nodes
                    if node.projection_role in {"DIRECT_CONTENT", "STRUCTURE_PROXY"}
                ]

                self.assertEqual(len(reviewable), 1)
                self.assertEqual(reviewable[0].structure_node_id, section_id)
                self.assertEqual(
                    _test_block_text(reviewable[0].content_blocks),
                    statement,
                )

    def test_content_node_with_image_source_kind_projects_media_not_candidate(
        self,
    ) -> None:
        document_id = "document-resolver-image-content"
        section_id = "section-resolver-image-content"
        image_node_id = "content-resolver-image"
        image_source_id = "source-resolver-image"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            {
                "id": document_id,
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": section_id,
                "parent_id": document_id,
                "kind": "SECTION",
                "order": 10,
                "label": "10.4 布置示意图",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": image_node_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "Content",
                "page_start": 59,
                "page_end": 59,
                "source_refs": [
                    {
                        "kind": "SOURCE_NODE",
                        "source_kind": "IMAGE",
                        "source_id": image_source_id,
                        "page_number": 59,
                        "asset_ref": "asset-layout-image",
                    }
                ],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        by_id = {node.id: node for node in snapshot.nodes}
        section = by_id[section_id]

        self.assertEqual(section.projection_role, "STRUCTURE")
        self.assertEqual(section.review_item_count, 0)
        self.assertEqual(len(section.content_blocks), 1)
        self.assertEqual(section.content_blocks[0]["kind"], "image")
        self.assertEqual(section.content_blocks[0]["source_id"], image_source_id)
        self.assertNotIn("text", section.content_blocks[0])
        self.assertEqual(section.figure_refs, section.content_blocks)
        self.assertFalse(
            any(
                node.projection_role in {"DIRECT_CONTENT", "STRUCTURE_PROXY"}
                or node.kind == "REQUIREMENT"
                for node in snapshot.nodes
                if node.id != section_id
            )
        )

    def test_whole_table_has_one_review_leaf_and_survives_publication(self) -> None:
        document_id = "document-whole-table"
        section_id = "section-whole-table"
        table_node_id = "table-node-whole-table"
        table_id = "table-physical-whole-table"
        source_id = "node-" + "9" * 32
        child_id = "child-after-whole-table"
        projection, evidence = _whole_table_projection_with_evidence(
            owner_id=table_node_id,
            table_id=table_id,
            source_id=source_id,
        )
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            {
                "id": document_id,
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": section_id,
                "parent_id": document_id,
                "kind": "SECTION",
                "order": 10,
                "label": "5.6 参数要求",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": "content-before-whole-table",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "下表规定了参数要求。",
                "source_refs": [{"source_id": "source-before-whole-table"}],
            },
            {
                "id": "caption-before-whole-table",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 19,
                "label": "表 62 参数要求 Table 62 Parameter requirements",
                "source_refs": [{"source_id": "source-whole-caption"}],
            },
            {
                "id": table_node_id,
                "parent_id": section_id,
                "kind": "TABLE",
                "order": 20,
                "label": "Table",
                "page_start": 2,
                "page_end": 3,
                "source_refs": [{"source_id": source_id}],
            },
            {
                "id": child_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 30,
                "label": "1）表格注释",
                "source_refs": [{"source_id": "source-table-note"}],
            },
        ]
        payload = {
            "mode": "LOCAL_STRUCTURE_PROJECTION",
            "tables": [
                {
                    "id": table_id,
                    "source_node_id": source_id,
                    "page_start": 2,
                    "page_end": 3,
                    "cells": [
                        {"id": f"whole-cell-{index:028x}", "text": str(index)}
                        for index in range(1, 5)
                    ],
                }
            ],
            "table_record_projections": [projection.model_dump(mode="json")],
            "table_evidence_bindings": [evidence.model_dump(mode="json")],
        }

        working = build_requirement_tree_snapshot(
            workbench=workbench,
            payload=payload,
        )
        whole_nodes = [
            node
            for node in working.nodes
            if node.projection_role == "WHOLE_TABLE_REVIEW"
        ]
        self.assertEqual(len(whole_nodes), 1)
        whole = whole_nodes[0]
        self.assertEqual(whole.kind, "TABLE")
        self.assertEqual(
            whole.label,
            "表 62 参数要求 Table 62 Parameter requirements",
        )
        self.assertEqual(whole.parent_id, section_id)
        self.assertEqual(whole.structure_node_id, table_node_id)
        self.assertEqual(whole.source_order, 20)
        self.assertEqual(whole.requirement_count, 0)
        self.assertEqual(whole.table_evidence_binding_id, evidence.binding_id)
        self.assertEqual(len(whole.table_refs), 1)
        self.assertEqual(whole.table_refs[0]["id"], table_id)
        direct = [
            node for node in working.nodes if node.projection_role == "DIRECT_CONTENT"
        ]
        self.assertEqual(len(direct), 1)
        self.assertEqual(
            _test_block_text(direct[0].content_blocks),
            "下表规定了参数要求。",
        )
        root = next(node for node in working.nodes if node.id == document_id)
        self.assertEqual(root.requirement_count, 0)
        self.assertEqual(root.review_item_count, 2)

        reviewables = [
            node
            for node in working.nodes
            if node.projection_role
            in {
                "STRUCTURE_PROXY",
                "DIRECT_CONTENT",
                "WHOLE_TABLE_REVIEW",
                "TABLE_RECORD",
                "REQUIREMENT",
            }
        ]
        published_entries = [
            {
                "requirement_tree_node_id": node.id,
                "requirement_entry_id": f"entry-whole-{index}",
                "owner_structure_node_id": node.structure_node_id,
                "source_order": node.source_order,
                "title": node.label,
                "source_text": node.summary,
                "page_start": node.page_start,
                "page_end": node.page_end,
                "source_refs": list(node.source_refs),
                "status": node.status,
                "classification": node.classification,
                "projection_role": node.projection_role,
                "table_evidence_binding_id": node.table_evidence_binding_id,
                "content_blocks": list(node.content_blocks),
                "table_refs": list(node.table_refs),
                "figure_refs": list(node.figure_refs),
            }
            for index, node in enumerate(reviewables)
        ]
        published = build_requirement_tree_snapshot(
            workbench=workbench,
            payload=payload,
            mode=RequirementTreeMode.REQUIREMENT_SET,
            requirement_set_id="set-whole-table",
            requirement_entries=published_entries,
            forced_state=RequirementTreeState.READY,
        )
        published_whole = next(
            node
            for node in published.nodes
            if node.projection_role == "WHOLE_TABLE_REVIEW"
        )
        self.assertEqual(published_whole.id, whole.id)
        self.assertEqual(
            published_whole.table_evidence_binding_id,
            whole.table_evidence_binding_id,
        )
        self.assertIsNotNone(published_whole.requirement_entry_id)
        self.assertIsNotNone(published_whole.requirement_evidence_binding_id)

    def test_numbered_caption_binds_across_owned_table_row_sort_tie(self) -> None:
        snapshot, table_node_id, section_id, _ = _interposed_row_caption_snapshot(
            captions=(
                (
                    "表 1 参数要求\nTable 1 Parameter requirements",
                    19,
                    2,
                    "section-local-table-caption",
                ),
            )
        )
        whole = next(
            node
            for node in snapshot.nodes
            if node.projection_role == "WHOLE_TABLE_REVIEW"
        )

        self.assertEqual(
            whole.label,
            "表 1 参数要求 Table 1 Parameter requirements",
        )
        self.assertEqual(whole.parent_id, section_id)
        self.assertEqual(whole.structure_node_id, table_node_id)
        self.assertFalse(
            any(
                node.structure_node_id == "caption-local-table-0"
                and node.projection_role == "DIRECT_CONTENT"
                for node in snapshot.nodes
            )
        )

    def test_numbered_caption_local_fallback_rejects_unproven_pairs(self) -> None:
        cases = {
            "wrong-number": (("表 2 错号题注", 19, 2, "section-local-table-caption"),),
            "multiple-captions": (
                ("表 1 第一题注", 18, 2, "section-local-table-caption"),
                ("Table 1 Second caption", 19, 2, "section-local-table-caption"),
            ),
            "cross-owner": (
                ("表 1 其他章节题注", 19, 2, "section-other-table-caption"),
            ),
            "cross-page": (("表 1 前页题注", 19, 1, "section-local-table-caption"),),
        }
        for case, captions in cases.items():
            with self.subTest(case=case):
                snapshot, _, _, _ = _interposed_row_caption_snapshot(captions=captions)
                whole = next(
                    node
                    for node in snapshot.nodes
                    if node.projection_role == "WHOLE_TABLE_REVIEW"
                )
                self.assertEqual(
                    whole.label,
                    "5.6 参数要求 · 表格 1（第2页）",
                )

    def test_whole_table_without_evidence_is_attention_only(self) -> None:
        section_id = "section-whole-table-missing-evidence"
        table_node_id = "table-node-whole-table-missing-evidence"
        table_id = "table-physical-missing-evidence"
        source_id = "node-" + "8" * 32
        projection, _ = _whole_table_projection_with_evidence(
            owner_id=table_node_id,
            table_id=table_id,
            source_id=source_id,
            logical_table_id="logical-table-missing-evidence",
        )
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "2 表格",
                "source_refs": [{"source_id": "source-section-missing"}],
            },
            {
                "id": table_node_id,
                "parent_id": section_id,
                "kind": "TABLE",
                "order": 20,
                "label": "Table",
                "source_refs": [{"source_id": source_id}],
            },
        ]
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "tables": [
                    {
                        "id": table_id,
                        "source_node_id": source_id,
                        "cells": [],
                    }
                ],
                "table_record_projections": [projection.model_dump(mode="json")],
            },
        )
        whole = next(
            node
            for node in snapshot.nodes
            if node.projection_role == "WHOLE_TABLE_REVIEW"
        )

        self.assertEqual(whole.status, "NEEDS_ATTENTION")
        self.assertEqual(whole.attention_count, 1)
        self.assertIsNone(whole.table_evidence_binding_id)
        self.assertEqual(whole.requirement_count, 0)
        self.assertEqual(whole.label, "2 表格 · 表格 1")

    def test_normative_table_prefixed_sentence_remains_direct_content(self) -> None:
        section_id = "section-normative-table-prefix"
        table_node_id = "table-node-normative-prefix"
        child_id = "child-after-normative-table"
        table_id = "table-normative-prefix"
        source_id = "node-" + "7" * 32
        projection, evidence = _whole_table_projection_with_evidence(
            owner_id=table_node_id,
            table_id=table_id,
            source_id=source_id,
            logical_table_id="logical-table-normative-prefix",
        )
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "1 表格要求",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": "normative-table-prefix",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 19,
                "label": "表 1 应由供应商填写并提交。",
                "source_refs": [{"source_id": "source-normative-prefix"}],
            },
            {
                "id": table_node_id,
                "parent_id": section_id,
                "kind": "TABLE",
                "order": 20,
                "label": "Table",
                "source_refs": [{"source_id": source_id}],
            },
            {
                "id": child_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 30,
                "label": "1.1 后续",
                "source_refs": [{"source_id": "source-child"}],
            },
        ]
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "tables": [{"id": table_id, "source_node_id": source_id, "cells": []}],
                "table_record_projections": [projection.model_dump(mode="json")],
                "table_evidence_bindings": [evidence.model_dump(mode="json")],
            },
        )

        direct = [
            node for node in snapshot.nodes if node.projection_role == "DIRECT_CONTENT"
        ]
        self.assertEqual(len(direct), 1)
        self.assertEqual(
            _test_block_text(direct[0].content_blocks),
            "表 1 应由供应商填写并提交。",
        )
        self.assertEqual(
            sum(
                node.projection_role == "WHOLE_TABLE_REVIEW" for node in snapshot.nodes
            ),
            1,
        )

    def test_table_owner_keeps_inline_prose_and_table_as_separate_candidates(
        self,
    ) -> None:
        owner_id = "section-inline-prose-with-table"
        table_node_id = "table-node-inline-prose"
        table_id = "table-physical-inline-prose"
        table_source_id = "node-" + "a" * 32
        inline_text = (
            "4.1.4 试验电压应符合表 1。\nTest voltage shall comply with Table 1."
        )
        projection, evidence = _whole_table_projection_with_evidence(
            owner_id=table_node_id,
            table_id=table_id,
            source_id=table_source_id,
            logical_table_id="logical-table-inline-prose",
        )
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": owner_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": inline_text,
                "explicit_label": "4.1.4",
                "boundary_basis": "EXPLICIT_NUMBER",
                "page_start": 2,
                "page_end": 2,
                "source_refs": [
                    {
                        "source_id": "source-inline-prose",
                        "page_number": 2,
                    }
                ],
                "source_spans": [
                    {
                        "source_ref": {
                            "source_id": "source-inline-prose",
                            "page_number": 2,
                        },
                        "start_offset": 0,
                        "end_offset": len(inline_text),
                    }
                ],
            },
            {
                "id": "table-caption-inline-prose",
                "parent_id": owner_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "表 1\n试验电压\nTable 1 Test voltage",
                "source_refs": [{"source_id": "source-table-caption"}],
            },
            {
                "id": table_node_id,
                "parent_id": owner_id,
                "kind": "TABLE",
                "order": 12,
                "label": "Table",
                "source_refs": [{"source_id": table_source_id}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "tables": [
                    {
                        "id": table_id,
                        "source_node_id": table_source_id,
                        "cells": [],
                    }
                ],
                "table_record_projections": [projection.model_dump(mode="json")],
                "table_evidence_bindings": [evidence.model_dump(mode="json")],
            },
        )
        by_id = {node.id: node for node in snapshot.nodes}
        direct = [
            node
            for node in snapshot.nodes
            if node.projection_role == "DIRECT_CONTENT"
            and node.structure_node_id == owner_id
        ]
        table_review = [
            node
            for node in snapshot.nodes
            if node.projection_role == "WHOLE_TABLE_REVIEW"
        ]

        self.assertEqual(by_id[owner_id].projection_role, "STRUCTURE")
        self.assertEqual(len(direct), 1)
        self.assertEqual(_test_block_text(direct[0].content_blocks), inline_text)
        self.assertEqual(direct[0].parent_id, owner_id)
        self.assertEqual(direct[0].table_refs, ())
        exact_source_ref = direct[0].source_refs[0]
        self.assertEqual(exact_source_ref["kind"], "SOURCE_NODE")
        self.assertEqual(exact_source_ref["source_id"], "source-inline-prose")
        self.assertEqual(exact_source_ref["start_offset"], 0)
        self.assertEqual(exact_source_ref["end_offset"], len(inline_text))
        self.assertTrue(
            any(
                ref.get("kind") == "TEXT"
                and ref.get("owner_structure_node_id") == owner_id
                and ref.get("start_offset") == 0
                and ref.get("end_offset") == len(inline_text)
                for ref in direct[0].source_refs
            )
        )
        self.assertEqual(len(table_review), 1)
        self.assertEqual(table_review[0].parent_id, owner_id)
        self.assertEqual(by_id[owner_id].review_item_count, 2)

    def test_table_owner_keeps_numbered_normative_colon_lead_in(self) -> None:
        owner_id = "clause-normative-colon-lead-in-with-table"
        table_node_id = "table-node-after-normative-colon-lead-in"
        table_id = "table-after-normative-colon-lead-in"
        table_source_id = "node-" + "d" * 32
        projection, evidence = _whole_table_projection_with_evidence(
            owner_id=table_node_id,
            table_id=table_id,
            source_id=table_source_id,
            logical_table_id="logical-table-after-normative-colon-lead-in",
        )
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": owner_id,
                "parent_id": ROOT_ID,
                "kind": "CLAUSE",
                "order": 10,
                "label": "1）供应商应提交下表所列全部资料：",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 1,
                "source_refs": [{"source_id": "source-normative-table-lead-in"}],
            },
            {
                "id": table_node_id,
                "parent_id": owner_id,
                "kind": "TABLE",
                "order": 20,
                "label": "Table",
                "source_refs": [{"source_id": table_source_id}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "tables": [
                    {
                        "id": table_id,
                        "source_node_id": table_source_id,
                        "cells": [],
                    }
                ],
                "table_record_projections": [projection.model_dump(mode="json")],
                "table_evidence_bindings": [evidence.model_dump(mode="json")],
            },
        )

        direct = [
            node
            for node in snapshot.nodes
            if node.projection_role == "DIRECT_CONTENT"
            and node.structure_node_id == owner_id
        ]
        table_review = [
            node
            for node in snapshot.nodes
            if node.projection_role == "WHOLE_TABLE_REVIEW"
        ]
        self.assertEqual(len(direct), 1)
        self.assertEqual(direct[0].label, "1）供应商应提交下表所列全部资料：")
        self.assertEqual(len(table_review), 1)

    def test_table_owner_keeps_proven_numbered_physical_continuation(self) -> None:
        owner_id = "clause-wrapped-prose-with-table"
        continuation_id = "content-wrapped-prose-with-table"
        table_node_id = "table-node-wrapped-prose"
        table_id = "table-physical-wrapped-prose"
        table_source_id = "node-" + "b" * 32
        projection, evidence = _whole_table_projection_with_evidence(
            owner_id=table_node_id,
            table_id=table_id,
            source_id=table_source_id,
            logical_table_id="logical-table-wrapped-prose",
        )
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": owner_id,
                "parent_id": ROOT_ID,
                "kind": "CLAUSE",
                "order": 10,
                "label": "a）供应商应提交完整的元器件统",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 1,
                "page_start": 2,
                "page_end": 2,
                "source_refs": [
                    {"source_id": "source-wrapped-owner", "page_number": 2}
                ],
            },
            {
                "id": continuation_id,
                "parent_id": owner_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "计清单。",
                "boundary_basis": "NATIVE_BLOCK",
                "page_start": 2,
                "page_end": 2,
                "source_refs": [
                    {
                        "source_id": "source-wrapped-continuation",
                        "page_number": 2,
                    }
                ],
            },
            {
                "id": table_node_id,
                "parent_id": owner_id,
                "kind": "TABLE",
                "order": 12,
                "label": "Table",
                "source_refs": [{"source_id": table_source_id}],
            },
        ]
        workbench["relations"] = [
            {
                "kind": "CONTINUES",
                "from_id": owner_id,
                "to_id": continuation_id,
            }
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "tables": [
                    {
                        "id": table_id,
                        "source_node_id": table_source_id,
                        "cells": [],
                    }
                ],
                "table_record_projections": [projection.model_dump(mode="json")],
                "table_evidence_bindings": [evidence.model_dump(mode="json")],
            },
        )
        direct = next(
            node
            for node in snapshot.nodes
            if node.projection_role == "DIRECT_CONTENT"
            and node.structure_node_id == owner_id
        )

        self.assertEqual(
            tuple(block.get("structure_node_id") for block in direct.content_blocks),
            (owner_id, continuation_id),
        )
        self.assertEqual(
            _test_block_text(direct.content_blocks),
            "a）供应商应提交完整的元器件统\n计清单。",
        )
        self.assertEqual(
            sum(
                node.projection_role == "WHOLE_TABLE_REVIEW" for node in snapshot.nodes
            ),
            1,
        )

    def test_pure_table_leaf_has_no_duplicate_structure_proxy(self) -> None:
        clause_id = "pure-table-clause"
        table_node_id = "pure-table-node"
        table_id = "pure-table-physical"
        source_id = "node-" + "6" * 32
        projection, evidence = _whole_table_projection_with_evidence(
            owner_id=table_node_id,
            table_id=table_id,
            source_id=source_id,
            logical_table_id="logical-pure-table",
        )
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": clause_id,
                "parent_id": ROOT_ID,
                "kind": "CLAUSE",
                "order": 10,
                "label": "1、参数表：",
                "boundary_basis": "EXPLICIT_NUMBER",
                "list_ordinal": 1,
                "source_refs": [{"source_id": "source-clause"}],
            },
            {
                "id": table_node_id,
                "parent_id": clause_id,
                "kind": "TABLE",
                "order": 20,
                "label": "Table",
                "source_refs": [{"source_id": source_id}],
            },
        ]
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "items": [
                    {
                        "target_structure": {"id": clause_id},
                        "text": "1、参数表：",
                        "semantic_class": "REQUIREMENT",
                        "reason_codes": ["synthetic_rule_evidence"],
                    }
                ],
                "review_groups": [
                    {
                        "boundary_structure_node_id": clause_id,
                        "context_blocks": [
                            {
                                "kind": "text",
                                "source_id": "source-caption-fallback",
                                "text": "参数表",
                            },
                            {"kind": "table", "table_id": table_id},
                        ],
                    }
                ],
                "tables": [{"id": table_id, "source_node_id": source_id, "cells": []}],
                "table_record_projections": [projection.model_dump(mode="json")],
                "table_evidence_bindings": [evidence.model_dump(mode="json")],
            },
        )
        by_id = {node.id: node for node in snapshot.nodes}

        self.assertEqual(by_id[clause_id].projection_role, "STRUCTURE")
        self.assertEqual(
            sum(
                node.projection_role == "WHOLE_TABLE_REVIEW" for node in snapshot.nodes
            ),
            1,
        )
        self.assertEqual(by_id[ROOT_ID].review_item_count, 1)

    def test_root_table_is_front_matter_when_semantic_sections_exist(self) -> None:
        table_node_id = "root-front-matter-table"
        table_id = "root-front-matter-physical"
        source_id = "node-" + "4" * 32
        projection, evidence = _whole_table_projection_with_evidence(
            owner_id=table_node_id,
            table_id=table_id,
            source_id=source_id,
            logical_table_id="logical-root-front-matter",
        )
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": table_node_id,
                "parent_id": ROOT_ID,
                "kind": "TABLE",
                "order": 1,
                "label": "封面信息",
                "source_refs": [{"source_id": source_id}],
            },
            {
                "id": SECTION_ID,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "1 技术要求",
                "source_refs": [{"source_id": "source-section"}],
            },
        ]
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "pipeline": {"source_ir": {"status": "COMPLETE"}},
                "tables": [{"id": table_id, "source_node_id": source_id, "cells": []}],
                "table_record_projections": [projection.model_dump(mode="json")],
                "table_evidence_bindings": [evidence.model_dump(mode="json")],
            },
        )

        self.assertFalse(
            any(node.projection_role == "WHOLE_TABLE_REVIEW" for node in snapshot.nodes)
        )

    def test_root_whole_table_inside_unique_body_interval_uses_that_owner(
        self,
    ) -> None:
        section_id = "section-root-whole-body"
        clause_id = "clause-root-whole-body"
        table_node_id = "root-whole-body-table"
        table_id = "root-whole-body-physical"
        source_id = "node-" + "d" * 32
        projection, evidence = _whole_table_projection_with_evidence(
            owner_id=table_node_id,
            table_id=table_id,
            source_id=source_id,
            logical_table_id="logical-root-whole-body",
        )
        original_projection = projection.model_dump(mode="json")
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "physical_source_order": 10,
                "label": "1 技术要求",
                "source_refs": [{"source_id": "source-body-section"}],
            },
            {
                "id": clause_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 20,
                "physical_source_order": 20,
                "label": "1.1 参数要求",
                "source_refs": [{"source_id": "source-body-clause"}],
            },
            {
                "id": table_node_id,
                "parent_id": ROOT_ID,
                "kind": "TABLE",
                "order": 25,
                "physical_source_order": 25,
                "page_start": 5,
                "page_end": 5,
                "label": "参数表",
                "source_refs": [{"source_id": source_id, "page_number": 5}],
            },
            {
                "id": "section-after-root-whole-body",
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 30,
                "physical_source_order": 30,
                "label": "2 验证要求",
                "source_refs": [{"source_id": "source-next-section"}],
            },
        ]
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "pipeline": {"source_ir": {"status": "COMPLETE"}},
                "tables": [
                    {
                        "id": table_id,
                        "source_node_id": source_id,
                        "page_start": 5,
                        "page_end": 5,
                        "cells": [],
                    }
                ],
                "table_record_projections": [projection.model_dump(mode="json")],
                "table_evidence_bindings": [evidence.model_dump(mode="json")],
            },
        )

        whole = next(
            node
            for node in snapshot.nodes
            if node.projection_role == "WHOLE_TABLE_REVIEW"
        )
        self.assertEqual(whole.parent_id, clause_id)
        self.assertEqual(whole.structure_node_id, table_node_id)
        self.assertIsNone(whole.table_record_id)
        self.assertEqual(projection.model_dump(mode="json"), original_projection)
        self.assertEqual(
            projection.table_fact.owner_structure_node_id,
            table_node_id,
        )

    def test_root_whole_table_without_closed_complete_body_stays_suppressed(
        self,
    ) -> None:
        for source_status, include_closing_section in (
            ("COMPLETE", False),
            ("PARTIAL", True),
        ):
            with self.subTest(
                source_status=source_status,
                include_closing_section=include_closing_section,
            ):
                table_node_id = f"root-whole-unclosed-{source_status.lower()}"
                table_id = f"root-whole-unclosed-physical-{source_status.lower()}"
                source_id = "node-" + ("e" if source_status == "COMPLETE" else "f") * 32
                projection, evidence = _whole_table_projection_with_evidence(
                    owner_id=table_node_id,
                    table_id=table_id,
                    source_id=source_id,
                    logical_table_id=(
                        f"logical-root-whole-unclosed-{source_status.lower()}"
                    ),
                )
                workbench = _workbench()
                workbench["structure"]["nodes"] = [
                    workbench["structure"]["nodes"][0],
                    {
                        "id": "section-root-whole-unclosed",
                        "parent_id": ROOT_ID,
                        "kind": "SECTION",
                        "order": 10,
                        "physical_source_order": 10,
                        "label": "1 技术要求",
                        "source_refs": [{"source_id": "source-body-section"}],
                    },
                    {
                        "id": "clause-root-whole-unclosed",
                        "parent_id": "section-root-whole-unclosed",
                        "kind": "CLAUSE",
                        "order": 20,
                        "physical_source_order": 20,
                        "label": "1.1 参数要求",
                        "source_refs": [{"source_id": "source-body-clause"}],
                    },
                    {
                        "id": table_node_id,
                        "parent_id": ROOT_ID,
                        "kind": "TABLE",
                        "order": 25,
                        "physical_source_order": 25,
                        "label": "参数表",
                        "source_refs": [{"source_id": source_id}],
                    },
                    *(
                        [
                            {
                                "id": "section-after-root-whole-unclosed",
                                "parent_id": ROOT_ID,
                                "kind": "SECTION",
                                "order": 30,
                                "physical_source_order": 30,
                                "label": "2 验证要求",
                                "source_refs": [{"source_id": "source-next-section"}],
                            }
                        ]
                        if include_closing_section
                        else []
                    ),
                ]
                snapshot = build_requirement_tree_snapshot(
                    workbench=workbench,
                    payload={
                        "mode": "LOCAL_STRUCTURE_PROJECTION",
                        "pipeline": {"source_ir": {"status": source_status}},
                        "tables": [
                            {
                                "id": table_id,
                                "source_node_id": source_id,
                                "cells": [],
                            }
                        ],
                        "table_record_projections": [
                            projection.model_dump(mode="json")
                        ],
                        "table_evidence_bindings": [evidence.model_dump(mode="json")],
                    },
                )
                self.assertFalse(
                    any(
                        node.projection_role == "WHOLE_TABLE_REVIEW"
                        for node in snapshot.nodes
                    )
                )

    def test_root_record_table_inside_unique_body_interval_uses_that_owner(
        self,
    ) -> None:
        section_id = "section-root-record-body"
        clause_id = "clause-root-record-body"
        table_node_id = "root-record-body-table"
        table_id = "root-record-body-physical"
        source_id = "node-" + "c" * 32
        projection = _records_projection(
            owner_id=table_node_id,
            table_id=table_id,
            source_id=source_id,
            table_source_order=25,
        )
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "physical_source_order": 10,
                "label": "1 技术要求",
                "source_refs": [{"source_id": "source-body-section"}],
            },
            {
                "id": clause_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 20,
                "physical_source_order": 20,
                "label": "1.1 性能要求",
                "source_refs": [{"source_id": "source-body-clause"}],
            },
            {
                "id": table_node_id,
                "parent_id": ROOT_ID,
                "kind": "TABLE",
                "order": 25,
                "physical_source_order": 25,
                "page_start": 5,
                "page_end": 5,
                "label": "50 Hz",
                "source_refs": [{"source_id": source_id, "page_number": 5}],
            },
            {
                "id": "section-after-root-record-body",
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 30,
                "physical_source_order": 30,
                "label": "2 验证要求",
                "source_refs": [{"source_id": "source-next-section"}],
            },
        ]
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "pipeline": {"source_ir": {"status": "COMPLETE"}},
                "tables": [
                    {
                        "id": table_id,
                        "source_node_id": source_id,
                        "page_start": 5,
                        "page_end": 5,
                        "cells": [
                            {"id": f"cell-{index:032x}", "text": str(index)}
                            for index in range(1, 5)
                        ],
                    }
                ],
                "table_record_projections": [projection.model_dump(mode="json")],
            },
        )

        container = next(
            node for node in snapshot.nodes if node.projection_role == "TABLE_CONTAINER"
        )
        record = next(node for node in snapshot.nodes if node.kind == "TABLE_RECORD")
        self.assertEqual(container.parent_id, clause_id)
        self.assertEqual(record.parent_id, container.id)
        self.assertEqual(container.structure_node_id, table_node_id)

    def test_root_record_table_before_or_after_bounded_body_stays_suppressed(
        self,
    ) -> None:
        for table_order in (1, 40):
            with self.subTest(table_order=table_order):
                table_node_id = f"root-record-outside-body-{table_order}"
                table_id = f"root-record-outside-body-physical-{table_order}"
                source_id = "node-" + f"{table_order:032x}"
                projection = _records_projection(
                    owner_id=table_node_id,
                    table_id=table_id,
                    source_id=source_id,
                    table_source_order=table_order,
                )
                workbench = _workbench()
                workbench["structure"]["nodes"] = [
                    workbench["structure"]["nodes"][0],
                    {
                        "id": table_node_id,
                        "parent_id": ROOT_ID,
                        "kind": "TABLE",
                        "order": table_order,
                        "physical_source_order": table_order,
                        "label": "修订或索引",
                        "source_refs": [{"source_id": source_id}],
                    },
                    {
                        "id": SECTION_ID,
                        "parent_id": ROOT_ID,
                        "kind": "SECTION",
                        "order": 10,
                        "physical_source_order": 10,
                        "label": "1 技术要求",
                        "source_refs": [{"source_id": "source-section"}],
                    },
                    {
                        "id": SECOND_CLAUSE_ID,
                        "parent_id": ROOT_ID,
                        "kind": "SECTION",
                        "order": 30,
                        "physical_source_order": 30,
                        "label": "2 验证要求",
                        "source_refs": [{"source_id": "source-next"}],
                    },
                ]
                snapshot = build_requirement_tree_snapshot(
                    workbench=workbench,
                    payload={
                        "mode": "LOCAL_STRUCTURE_PROJECTION",
                        "pipeline": {"source_ir": {"status": "COMPLETE"}},
                        "tables": [
                            {
                                "id": table_id,
                                "source_node_id": source_id,
                                "cells": [
                                    {
                                        "id": f"cell-{index:032x}",
                                        "text": str(index),
                                    }
                                    for index in range(1, 5)
                                ],
                            }
                        ],
                        "table_record_projections": [
                            projection.model_dump(mode="json")
                        ],
                    },
                )
                self.assertFalse(
                    any(
                        node.projection_role in {"TABLE_CONTAINER", "TABLE_RECORD"}
                        for node in snapshot.nodes
                    )
                )

    def test_complete_table_only_document_can_review_root_table(self) -> None:
        table_node_id = "root-global-table"
        table_id = "root-global-physical"
        source_id = "node-" + "3" * 32
        projection, evidence = _whole_table_projection_with_evidence(
            owner_id=table_node_id,
            table_id=table_id,
            source_id=source_id,
            logical_table_id="logical-root-global-table",
        )
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": table_node_id,
                "parent_id": ROOT_ID,
                "kind": "TABLE",
                "order": 1,
                "label": "全局需求表",
                "source_refs": [{"source_id": source_id}],
            },
        ]
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "pipeline": {"source_ir": {"status": "COMPLETE"}},
                "tables": [{"id": table_id, "source_node_id": source_id, "cells": []}],
                "table_record_projections": [projection.model_dump(mode="json")],
                "table_evidence_bindings": [evidence.model_dump(mode="json")],
            },
        )

        whole = [
            node
            for node in snapshot.nodes
            if node.projection_role == "WHOLE_TABLE_REVIEW"
        ]
        self.assertEqual(len(whole), 1)
        self.assertEqual(whole[0].parent_id, ROOT_ID)

    def test_table_record_order_is_anchored_at_global_table_position(self) -> None:
        section_id = "section-record-global-order"
        table_node_id = "table-node-global-order"
        child_id = "child-after-record-table"
        table_id = "table-global-order"
        source_id = "node-" + "5" * 32
        projection = _records_projection(
            owner_id=table_node_id,
            table_id=table_id,
            source_id=source_id,
            table_source_order=200,
            record_source_order=0,
        )
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "1 参数",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": "content-before-record-table",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 190,
                "label": "表前说明。",
                "source_refs": [{"source_id": "source-before"}],
            },
            {
                "id": table_node_id,
                "parent_id": section_id,
                "kind": "TABLE",
                "order": 200,
                "label": "Table",
                "source_refs": [{"source_id": source_id}],
            },
            {
                "id": "content-after-record-table",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 210,
                "label": "表后说明。",
                "source_refs": [{"source_id": "source-after"}],
            },
            {
                "id": child_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 300,
                "label": "1.1 后续",
                "source_refs": [{"source_id": "source-child"}],
            },
        ]
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "tables": [{"id": table_id, "source_node_id": source_id, "cells": []}],
                "table_record_projections": [projection.model_dump(mode="json")],
            },
        )
        by_id = {node.id: node for node in snapshot.nodes}
        section_children = list(by_id[section_id].child_ids)
        record = next(node for node in snapshot.nodes if node.kind == "TABLE_RECORD")
        before = next(
            node
            for node in snapshot.nodes
            if _test_block_text(node.content_blocks) == "表前说明。"
        )
        after = next(
            node
            for node in snapshot.nodes
            if _test_block_text(node.content_blocks) == "表后说明。"
        )
        table_container = next(node for node in snapshot.nodes if node.kind == "TABLE")

        self.assertEqual(record.source_order, 200)
        self.assertLess(
            section_children.index(before.id),
            section_children.index(table_container.id),
        )
        self.assertLess(
            section_children.index(table_container.id),
            section_children.index(after.id),
        )
        self.assertEqual(record.parent_id, table_container.id)
        self.assertEqual(table_container.child_ids, (record.id,))

    def test_table_container_uses_proven_header_not_arbitrary_table_text(
        self,
    ) -> None:
        section_id = "section-header-named-table"
        table_node_id = "table-node-header-named"
        source_id = "node-" + "e" * 32
        projection, table = _records_projection_for_rows(
            owner_id=table_node_id,
            table_id="table-header-named",
            source_id=source_id,
            rows=(
                ("参数", "要求", "适用对象"),
                ("频率", "50 Hz", "前轴"),
            ),
        )
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "5.6 参数要求",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": table_node_id,
                "parent_id": section_id,
                "kind": "TABLE",
                "order": 20,
                "label": "50 Hz",
                "page_start": 5,
                "page_end": 5,
                "source_refs": [{"source_id": source_id, "page_number": 5}],
            },
        ]
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "tables": [table],
                "table_record_projections": [projection.model_dump(mode="json")],
            },
        )

        container = next(
            node for node in snapshot.nodes if node.projection_role == "TABLE_CONTAINER"
        )
        self.assertEqual(container.label, "参数 / 要求 / 适用对象 · 表格 1")
        self.assertNotIn("50 Hz", container.label)

    def test_uncaptioned_table_falls_back_to_parent_sequence_and_page(self) -> None:
        section_id = "section-fallback-named-table"
        table_node_id = "table-node-fallback-named"
        table_id = "table-fallback-named"
        source_id = "node-" + "f" * 32
        projection, evidence = _whole_table_projection_with_evidence(
            owner_id=table_node_id,
            table_id=table_id,
            source_id=source_id,
            logical_table_id="logical-table-fallback-named",
        )
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "5.6 参数要求",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": table_node_id,
                "parent_id": section_id,
                "kind": "TABLE",
                "order": 20,
                "label": "50 Hz",
                "page_start": 2,
                "page_end": 2,
                "source_refs": [{"source_id": source_id, "page_number": 2}],
            },
        ]
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "tables": [
                    {
                        "id": table_id,
                        "source_node_id": source_id,
                        "page_start": 2,
                        "page_end": 2,
                        "cells": [],
                    }
                ],
                "table_record_projections": [projection.model_dump(mode="json")],
                "table_evidence_bindings": [evidence.model_dump(mode="json")],
            },
        )

        table_node = next(
            node
            for node in snapshot.nodes
            if node.projection_role == "WHOLE_TABLE_REVIEW"
        )
        self.assertEqual(table_node.label, "5.6 参数要求 · 表格 1（第2页）")
        self.assertNotIn("50 Hz", table_node.label)

    def test_duplicate_record_labels_use_context_then_stable_location(self) -> None:
        for suffix_rows, expected in (
            (
                (("前轴",), ("后轴",)),
                ("执行器 · 环境耐久 · 前轴", "执行器 · 环境耐久 · 后轴"),
            ),
            (
                ((), ()),
                (
                    "执行器 · 环境耐久 · 第5页第2行",
                    "执行器 · 环境耐久 · 第5页第3行",
                ),
            ),
        ):
            with self.subTest(suffix_rows=suffix_rows):
                section_id = "section-record-label"
                table_node_id = "table-node-record-label"
                table_id = f"table-record-label-{len(suffix_rows[0])}"
                source_id = "node-" + ("1" if suffix_rows[0] else "2") * 32
                rows = (
                    (
                        "项目",
                        "试验",
                        *(("适用对象",) if suffix_rows[0] else ()),
                    ),
                    ("执行器", "环境耐久", *suffix_rows[0]),
                    ("执行器", "环境耐久", *suffix_rows[1]),
                )
                projection, table = _records_projection_for_rows(
                    owner_id=table_node_id,
                    table_id=table_id,
                    source_id=source_id,
                    rows=rows,
                )
                original_ids = tuple(record.record_id for record in projection.records)
                workbench = _workbench()
                workbench["structure"]["nodes"] = [
                    workbench["structure"]["nodes"][0],
                    {
                        "id": section_id,
                        "parent_id": ROOT_ID,
                        "kind": "SECTION",
                        "order": 10,
                        "label": "9.16 耐久要求",
                        "source_refs": [{"source_id": "source-section"}],
                    },
                    {
                        "id": table_node_id,
                        "parent_id": section_id,
                        "kind": "TABLE",
                        "order": 20,
                        "label": "Table",
                        "source_refs": [{"source_id": source_id, "page_number": 5}],
                    },
                ]
                snapshot = build_requirement_tree_snapshot(
                    workbench=workbench,
                    payload={
                        "mode": "LOCAL_STRUCTURE_PROJECTION",
                        "tables": [table],
                        "table_record_projections": [
                            projection.model_dump(mode="json")
                        ],
                    },
                )
                records = [
                    node for node in snapshot.nodes if node.kind == "TABLE_RECORD"
                ]
                self.assertEqual(tuple(node.label for node in records), expected)
                self.assertEqual(tuple(node.id for node in records), original_ids)

    def test_matrix_record_labels_use_column_context_then_column_location(
        self,
    ) -> None:
        for column_labels, expected in (
            (
                ("前轴", "后轴"),
                (
                    "环境耐久 · 环境耐久 · 前轴",
                    "环境耐久 · 环境耐久 · 后轴",
                ),
            ),
            (
                ("工况", "工况"),
                (
                    "环境耐久 · 环境耐久 · 第5页第2行第2列",
                    "环境耐久 · 环境耐久 · 第5页第2行第3列",
                ),
            ),
        ):
            with self.subTest(column_labels=column_labels):
                section_id = "section-matrix-record-label"
                table_node_id = "table-node-matrix-record-label"
                table_id = f"table-matrix-record-label-{column_labels[0]}"
                source_id = "node-" + ("3" if column_labels[0] == "前轴" else "4") * 32
                projection, table = _matrix_projection_for_same_row(
                    owner_id=table_node_id,
                    table_id=table_id,
                    source_id=source_id,
                    column_labels=column_labels,
                )
                original_ids = tuple(record.record_id for record in projection.records)
                workbench = _workbench()
                workbench["structure"]["nodes"] = [
                    workbench["structure"]["nodes"][0],
                    {
                        "id": section_id,
                        "parent_id": ROOT_ID,
                        "kind": "SECTION",
                        "order": 10,
                        "label": "9.16 耐久要求",
                        "source_refs": [{"source_id": "source-section"}],
                    },
                    {
                        "id": table_node_id,
                        "parent_id": section_id,
                        "kind": "TABLE",
                        "order": 20,
                        "label": "Table",
                        "source_refs": [{"source_id": source_id, "page_number": 5}],
                    },
                ]
                snapshot = build_requirement_tree_snapshot(
                    workbench=workbench,
                    payload={
                        "mode": "LOCAL_STRUCTURE_PROJECTION",
                        "tables": [table],
                        "table_record_projections": [
                            projection.model_dump(mode="json")
                        ],
                    },
                )
                records = [
                    node for node in snapshot.nodes if node.kind == "TABLE_RECORD"
                ]

                self.assertEqual(tuple(node.label for node in records), expected)
                self.assertEqual(tuple(node.id for node in records), original_ids)

    def test_children_share_one_physical_reading_coordinate_system(self) -> None:
        section_id = "section-physical-child-order"
        intro_id = "content-physical-intro"
        first_clause_id = "clause-physical-a"
        second_clause_id = "clause-physical-b"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "physical_source_order": 100,
                "physical_source_offset": 0,
                "label": "1.3 职责与分工",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": intro_id,
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "physical_source_order": 101,
                "physical_source_offset": 10,
                "label": "主机厂负责以下工作：",
                "source_refs": [{"source_id": "source-intro"}],
            },
            {
                "id": first_clause_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                # Editable DFS order and physical reading order are different
                # coordinate systems.  The public tree must use the latter.
                "order": 12,
                "physical_source_order": 101,
                "physical_source_offset": 20,
                "label": "a）提供技术要求。",
                "source_refs": [{"source_id": "source-a"}],
            },
            {
                "id": second_clause_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 13,
                "physical_source_order": 102,
                "physical_source_offset": 0,
                "label": "b）确认交付状态。",
                "source_refs": [{"source_id": "source-b"}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={"mode": "LOCAL_STRUCTURE_PROJECTION"},
        )
        by_id = {node.id: node for node in snapshot.nodes}
        direct = next(
            node
            for node in snapshot.nodes
            if node.parent_id == section_id and node.projection_role == "DIRECT_CONTENT"
        )

        self.assertEqual(
            by_id[section_id].child_ids,
            (direct.id, first_clause_id, second_clause_id),
        )
        self.assertEqual(
            [
                (by_id[node_id].source_order, by_id[node_id].source_offset)
                for node_id in by_id[section_id].child_ids
            ],
            [(101, 10), (101, 20), (102, 0)],
        )

    def test_direct_content_precedes_later_table_despite_editable_order(self) -> None:
        section_id = "section-physical-table-order"
        table_node_id = "table-physical-order"
        table_id = "table-physical-order-source"
        source_id = "node-" + "7" * 32
        projection = _records_projection(
            owner_id=table_node_id,
            table_id=table_id,
            source_id=source_id,
            table_source_order=101,
            record_source_order=0,
        )
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "physical_source_order": 90,
                "label": "2.2 交付要求",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": "content-before-physical-table",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "physical_source_order": 100,
                "label": "交付物应包括下表内容：",
                "source_refs": [{"source_id": "source-before-table"}],
            },
            {
                "id": table_node_id,
                "parent_id": section_id,
                "kind": "TABLE",
                # This editable order is intentionally lower than the prose's
                # physical order, matching a tree produced after hierarchy edits.
                "order": 20,
                "physical_source_order": 101,
                "label": "交付物清单",
                "source_refs": [{"source_id": source_id}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "tables": [
                    {
                        "id": table_id,
                        "source_node_id": source_id,
                        "cells": [],
                    }
                ],
                "table_record_projections": [projection.model_dump(mode="json")],
            },
        )
        by_id = {node.id: node for node in snapshot.nodes}
        direct = next(
            node
            for node in snapshot.nodes
            if node.parent_id == section_id and node.projection_role == "DIRECT_CONTENT"
        )
        table = next(
            node
            for node in snapshot.nodes
            if node.parent_id == section_id
            and node.projection_role == "TABLE_CONTAINER"
        )

        self.assertEqual(by_id[section_id].child_ids, (direct.id, table.id))
        self.assertEqual(direct.source_order, 100)
        self.assertEqual(table.source_order, 101)

    def test_structure_container_keeps_heading_key_not_aggregate_content_key(
        self,
    ) -> None:
        wrapper_id = "group-physical-heading-order"
        first_id = "section-first-heading"
        second_id = "section-second-heading"
        nested_id = "clause-second-child"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": wrapper_id,
                "parent_id": ROOT_ID,
                "kind": "GROUP",
                "order": 10,
                "physical_source_order": 90,
                "label": "设计要求",
                "source_refs": [{"source_id": "source-wrapper"}],
            },
            {
                "id": first_id,
                "parent_id": wrapper_id,
                "kind": "SECTION",
                "order": 20,
                "physical_source_order": 100,
                "physical_source_offset": 0,
                "label": "1. 可靠性设计目标",
                "source_refs": [{"source_id": "source-first"}],
            },
            {
                "id": second_id,
                "parent_id": wrapper_id,
                "kind": "SECTION",
                "order": 30,
                "physical_source_order": 100,
                "physical_source_offset": 20,
                "label": "2. 主要设计和验证要求",
                "source_refs": [{"source_id": "source-second"}],
            },
            {
                "id": nested_id,
                "parent_id": second_id,
                "kind": "CLAUSE",
                "order": 31,
                "physical_source_order": 101,
                "label": "2.1 验证要求",
                "source_refs": [{"source_id": "source-nested"}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "review_groups": [
                    {
                        "boundary_structure_node_id": second_id,
                        # Aggregate/context content can physically precede a
                        # container heading; it must not rename that heading's
                        # position among sibling Structure nodes.
                        "context_blocks": [
                            {
                                "kind": "table",
                                "source_order": 50,
                                "source_offset": 0,
                                "text": "Earlier aggregate table",
                            }
                        ],
                    }
                ],
            },
        )
        by_id = {node.id: node for node in snapshot.nodes}

        self.assertEqual(by_id[wrapper_id].child_ids, (first_id, second_id))
        self.assertEqual(
            (by_id[first_id].source_order, by_id[first_id].source_offset),
            (100, 0),
        )
        self.assertEqual(
            (by_id[second_id].source_order, by_id[second_id].source_offset),
            (100, 20),
        )

    def test_structure_is_authoritative_and_requirement_leaves_are_unique(self) -> None:
        snapshot = build_requirement_tree_snapshot(
            workbench=_workbench(),
            requirement_entries=_entries(),
        )

        self.assertEqual(snapshot.state, RequirementTreeState.READY)
        self.assertEqual(snapshot.root_node_ids, (ROOT_ID,))
        by_id = {node.id: node for node in snapshot.nodes}
        self.assertEqual(by_id[SECTION_ID].parent_id, ROOT_ID)
        self.assertEqual(by_id[CLAUSE_ID].parent_id, SECTION_ID)
        self.assertEqual(by_id[SECOND_CLAUSE_ID].parent_id, SECTION_ID)
        leaves = [node for node in snapshot.nodes if node.kind == "REQUIREMENT"]
        self.assertEqual(len(leaves), 2)
        self.assertEqual(len({node.requirement_entry_id for node in leaves}), 2)
        self.assertEqual(
            {node.parent_id for node in leaves},
            {CLAUSE_ID, SECOND_CLAUSE_ID},
        )
        self.assertEqual(by_id[ROOT_ID].requirement_count, 2)
        self.assertEqual(by_id[ROOT_ID].reviewed_count, 1)
        self.assertEqual(by_id[ROOT_ID].attention_count, 2)
        self.assertEqual(
            [node.source_order for node in snapshot.nodes],
            sorted(node.source_order for node in snapshot.nodes),
        )

    def test_leaf_identity_survives_working_to_requirement_set_transition(self) -> None:
        working = build_requirement_tree_snapshot(
            workbench=_workbench(),
            requirement_entries=_entries(),
        )
        published = build_requirement_tree_snapshot(
            workbench=_workbench(),
            mode=RequirementTreeMode.REQUIREMENT_SET,
            requirement_set_id="set-1",
            requirement_entries=_entries(published=True),
            forced_state=RequirementTreeState.READY,
        )

        working_ids = {node.id for node in working.nodes if node.kind == "REQUIREMENT"}
        published_ids = {
            node.id for node in published.nodes if node.kind == "REQUIREMENT"
        }
        self.assertEqual(working_ids, published_ids)
        self.assertNotEqual(
            {
                node.workflow_node_id
                for node in working.nodes
                if node.kind == "REQUIREMENT"
            },
            {
                node.workflow_node_id
                for node in published.nodes
                if node.kind == "REQUIREMENT"
            },
        )
        self.assertEqual(
            canonical_requirement_tree_sha256(published),
            canonical_requirement_tree_sha256(published),
        )
        self.assertIsNotNone(published.requirement_evidence_bundle)
        bundle = published.requirement_evidence_bundle
        assert bundle is not None
        self.assertEqual(
            {binding.requirement_entry_id for binding in bundle.requirement_bindings},
            {"published-entry-1", "published-entry-2"},
        )
        self.assertTrue(
            all(binding.source_refs for binding in bundle.requirement_bindings)
        )
        evidence_by_requirement_id = {
            binding.requirement_entry_id: binding.binding_id
            for binding in bundle.requirement_bindings
        }
        self.assertEqual(
            {
                node.requirement_evidence_binding_id
                for node in published.nodes
                if node.requirement_entry_id
            },
            set(evidence_by_requirement_id.values()),
        )

    def test_v1_snapshot_without_evidence_extension_remains_parseable(self) -> None:
        published = build_requirement_tree_snapshot(
            workbench=_workbench(),
            mode=RequirementTreeMode.REQUIREMENT_SET,
            requirement_set_id="set-legacy-evidence",
            requirement_entries=_entries(published=True),
            forced_state=RequirementTreeState.READY,
        )
        raw = published.model_dump(mode="json")
        raw.pop("requirement_evidence_bundle", None)
        for node in raw["nodes"]:
            node.pop("requirement_evidence_binding_id", None)

        restored = RequirementTreeSnapshotV1.model_validate(raw)

        self.assertIsNone(restored.requirement_evidence_bundle)
        self.assertTrue(
            all(node.requirement_evidence_binding_id is None for node in restored.nodes)
        )
        with self.assertRaisesRegex(
            ValueError,
            "immutable requirement evidence",
        ):
            persist_requirement_tree_snapshot(
                requirement_set_id="set-legacy-evidence",
                snapshot=restored,
            )

    def test_published_tree_rejects_unbound_flat_requirement(self) -> None:
        with self.assertRaisesRegex(ValueError, "Effective Structure owner"):
            build_requirement_tree_snapshot(
                workbench=_workbench(),
                mode=RequirementTreeMode.REQUIREMENT_SET,
                requirement_set_id="set-1",
                requirement_entries=[
                    {
                        "id": "unbound",
                        "requirement_entry_id": "entry-unbound",
                        "display_text": "不得从平铺列表猜测章节。",
                    }
                ],
                forced_state=RequirementTreeState.READY,
            )

    def test_empty_completed_workbench_reports_not_started(self) -> None:
        workbench = _workbench()
        workbench["structure"]["nodes"] = []
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            requirement_entries=[],
        )
        self.assertEqual(snapshot.state, RequirementTreeState.NOT_STARTED)
        self.assertEqual(snapshot.nodes, ())

    def test_context_rows_and_empty_rows_never_become_requirement_leaves(
        self,
    ) -> None:
        entries = [
            {
                "id": "requirement-1",
                "owner_structure_node_id": CLAUSE_ID,
                "source_order": 21,
                "text": "接口强度应不低于 40 kN。",
                "decision_mode": "DIRECT",
                "role": "PRIMARY",
                "semantic_statuses": ["CANDIDATE", "UNRESOLVED"],
                "source_refs": [
                    {
                        "kind": "TEXT_RANGE",
                        "source_id": "TXT-101",
                        "owner_structure_node_id": CLAUSE_ID,
                    }
                ],
            },
            {
                "id": "context-1",
                "owner_structure_node_id": CLAUSE_ID,
                "source_order": 22,
                "text": "仅用于解释的上下文。",
                "decision_mode": "NONE",
                "role": "CONTENT",
            },
            {
                "id": "empty-1",
                "owner_structure_node_id": CLAUSE_ID,
                "source_order": 23,
                "text": "   ",
                "decision_mode": "DIRECT",
                "role": "CHILD",
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=_workbench(),
            requirement_entries=entries,
        )

        leaves = [node for node in snapshot.nodes if node.kind == "REQUIREMENT"]
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0].status, "NEEDS_ATTENTION")
        self.assertEqual(
            next(node for node in snapshot.nodes if node.id == ROOT_ID).attention_count,
            2,
        )

    def test_completed_root_only_tree_is_partial_not_ready(self) -> None:
        workbench = _workbench()
        workbench["structure"]["nodes"] = [workbench["structure"]["nodes"][0]]
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            requirement_entries=[],
        )

        self.assertEqual(snapshot.state, RequirementTreeState.RUNNING_PARTIAL)
        self.assertEqual(len(snapshot.nodes), 1)
        self.assertEqual(snapshot.nodes[0].kind, "DOCUMENT")
        self.assertEqual(snapshot.nodes[0].requirement_count, 0)

    def test_completed_partial_source_without_requirements_requires_reextraction(
        self,
    ) -> None:
        workbench = _workbench()
        workbench["structure"]["nodes"] = [workbench["structure"]["nodes"][0]]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "pipeline": {
                    "source_ir": {
                        "status": "PARTIAL",
                    }
                }
            },
            requirement_entries=[],
        )

        self.assertEqual(
            snapshot.state,
            RequirementTreeState.REQUIRES_REEXTRACTION,
        )

    def test_completed_partial_source_with_reconciled_capture_can_be_ready(
        self,
    ) -> None:
        workbench = _workbench()
        workbench["structure_inspection"] = {
            "state": "READY",
            "inspection_runtime_version": STRUCTURE_INSPECTION_RUNTIME_VERSION,
            "counts": {"capture_gap_count": 0},
        }

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "pipeline": {
                    "source_ir": {
                        "status": "PARTIAL",
                    }
                }
            },
            requirement_entries=[],
        )

        self.assertEqual(snapshot.state, RequirementTreeState.READY)

    def test_completed_partial_source_with_capture_gap_requires_reextraction(
        self,
    ) -> None:
        workbench = _workbench()
        workbench["structure_inspection"] = {
            "state": "READY",
            "inspection_runtime_version": STRUCTURE_INSPECTION_RUNTIME_VERSION,
            "counts": {"capture_gap_count": 1},
        }

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "pipeline": {
                    "source_ir": {
                        "status": "PARTIAL",
                    }
                }
            },
            requirement_entries=[],
        )

        self.assertEqual(
            snapshot.state,
            RequirementTreeState.REQUIRES_REEXTRACTION,
        )

    def test_capture_gap_blocks_existing_requirement_leaves(self) -> None:
        workbench = _workbench()
        workbench["structure_inspection"] = {
            "state": "READY",
            "inspection_runtime_version": STRUCTURE_INSPECTION_RUNTIME_VERSION,
            "counts": {"capture_gap_count": 1},
        }

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "pipeline": {
                    "source_ir": {
                        "status": "PARTIAL",
                    }
                }
            },
            requirement_entries=_entries(),
        )

        self.assertGreater(
            sum(node.requirement_count for node in snapshot.nodes),
            0,
        )
        self.assertEqual(
            snapshot.state,
            RequirementTreeState.REQUIRES_REEXTRACTION,
        )

    def test_completed_partial_source_with_legacy_capture_receipt_fails_closed(
        self,
    ) -> None:
        workbench = _workbench()
        workbench["structure_inspection"] = {
            "state": "READY",
            "inspection_runtime_version": "1.4.0",
            "counts": {"capture_gap_count": 0},
        }

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "pipeline": {
                    "source_ir": {
                        "status": "PARTIAL",
                    }
                }
            },
            requirement_entries=[],
        )

        self.assertEqual(
            snapshot.state,
            RequirementTreeState.REQUIRES_REEXTRACTION,
        )

    def test_table_rows_never_become_tree_layers_even_when_flagged(self) -> None:
        workbench = _workbench()
        workbench["structure"]["nodes"].append(
            {
                "id": TABLE_ROW_ID,
                "parent_id": CLAUSE_ID,
                "kind": "TABLE_ROW",
                "order": 22,
                "label": "原表格中的一行",
                "health_codes": ["TABLE_TEXT_CHANNEL_CONFLICT"],
                "source_refs": [{"kind": "SOURCE_NODE", "source_id": "TBL-001"}],
            }
        )

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            requirement_entries=_entries(),
        )

        self.assertNotIn(TABLE_ROW_ID, {node.id for node in snapshot.nodes})
        self.assertEqual(
            {node.kind for node in snapshot.nodes},
            {"DOCUMENT", "SECTION", "CLAUSE", "REQUIREMENT"},
        )

    def test_table_and_text_channels_share_one_exact_requirement_leaf(self) -> None:
        shared = {
            "display_text": "供应商应提交完整的验证报告。",
            "page_start": 4,
            "page_end": 4,
        }
        snapshot = build_requirement_tree_snapshot(
            workbench=_workbench(),
            requirement_entries=[
                {
                    **shared,
                    "id": "table-channel",
                    "requirement_entry_id": "table-entry",
                    "source_order": 40,
                    "target_structure": {
                        "id": CLAUSE_ID,
                        "kind": "table_row",
                    },
                },
                {
                    **shared,
                    "id": "text-channel",
                    "requirement_entry_id": "text-entry",
                    "source_order": 41,
                    "target_structure": {
                        "id": CLAUSE_ID,
                        "kind": "content",
                    },
                },
            ],
        )

        leaves = [node for node in snapshot.nodes if node.kind == "REQUIREMENT"]
        self.assertEqual(len(leaves), 1)
        self.assertEqual(leaves[0].requirement_entry_id, "text-entry")

    def test_numeric_table_title_uses_the_meaningful_item_label(self) -> None:
        snapshot = build_requirement_tree_snapshot(
            workbench=_workbench(),
            requirement_entries=[
                {
                    "id": "dimension-row",
                    "requirement_entry_id": "dimension-entry",
                    "owner_structure_node_id": CLAUSE_ID,
                    "source_order": 40,
                    "title": "1",
                    "text": ("1\n基本尺寸\n活塞桶直径应为 Φ32–48 mm。"),
                }
            ],
        )

        leaf = next(node for node in snapshot.nodes if node.kind == "REQUIREMENT")
        self.assertEqual(leaf.label, "基本尺寸")

    def test_structure_content_uses_one_authoritative_presentation_layer(
        self,
    ) -> None:
        snapshot = build_requirement_tree_snapshot(
            workbench=_workbench(),
            payload={
                "review_groups": [
                    {
                        "boundary_structure_node_id": CLAUSE_ID,
                        "context_blocks": [
                            {"kind": "text", "text": "完整条款正文"},
                            {
                                "kind": "table",
                                "table_id": "table-context-1",
                                "source_id": "node-table-context-1",
                            },
                        ],
                    }
                ],
                "tables": [
                    {
                        "id": "table-context-1",
                        "source_node_id": "node-table-context-1",
                        "cells": [
                            {"row": 0, "column": 0, "text": "参数"},
                            {"row": 0, "column": 1, "text": "要求"},
                        ],
                    }
                ],
                "requirement_groups": [
                    {
                        "boundary_structure_node_id": CLAUSE_ID,
                        "context_blocks": [
                            {"kind": "text", "text": "不应重复的需求组正文"}
                        ],
                    }
                ],
                "items": [
                    {
                        "target_structure": {"id": CLAUSE_ID},
                        "context_text": "不应追加的条目回退正文",
                    }
                ],
            },
            requirement_entries=_entries(),
        )

        clause = next(node for node in snapshot.nodes if node.id == CLAUSE_ID)
        self.assertEqual(clause.content_blocks[0]["kind"], "text")
        self.assertEqual(clause.content_blocks[0]["text"], "完整条款正文")
        self.assertEqual(clause.content_blocks[0]["source_order"], 20)
        self.assertEqual(clause.content_blocks[0]["source_offset"], 0)
        self.assertTrue(
            str(clause.content_blocks[0]["reading_event_id"]).startswith("reading-")
        )
        self.assertEqual(
            clause.content_blocks[1]["cells"],
            [
                {"row": 0, "column": 0, "text": "参数"},
                {"row": 0, "column": 1, "text": "要求"},
            ],
        )
        self.assertEqual(clause.table_refs[0]["table_id"], "table-context-1")
        self.assertTrue(clause.summary.startswith("完整条款正文"))

    def test_content_label_prefers_exact_source_ir_over_presentation_title(
        self,
    ) -> None:
        source_text = "前缀文字\nØ\n诊断通讯测试，供应商应提交报告。\n后缀文字"
        exact_text = "Ø\n诊断通讯测试，供应商应提交报告。"
        start = source_text.index(exact_text)
        end = start + len(exact_text)
        source_index = {
            "source-content": {
                "id": "source-content",
                "text": source_text,
            }
        }
        node = {
            "structure_node_id": "content-exact-source",
            "kind": "CONTENT",
            "source_refs": [{"source_id": "source-content"}],
            "semantic_source_spans": [
                {
                    "source_ref": {"source_id": "source-content"},
                    "start_offset": start,
                    "end_offset": end,
                }
            ],
        }

        self.assertEqual(
            _node_label(
                node,
                document={},
                source_index=source_index,
                presentation={"title": "Ø"},
                durable_presentation={"label": "Ø"},
            ),
            exact_text,
        )
        self.assertEqual(
            _node_label(
                {
                    **node,
                    "semantic_source_spans": [],
                },
                document={},
                source_index=source_index,
                presentation={"title": "Ø"},
                durable_presentation={"label": "Ø"},
            ),
            source_text,
        )

    def test_child_sections_slice_their_own_subtree_from_parent_context(
        self,
    ) -> None:
        document_id = "document"
        chapter_id = "chapter-9"
        section_id = "section-9-1"
        first_child_id = "section-9-1-1"
        second_child_id = "section-9-1-2"
        next_section_id = "section-9-2"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            {
                "id": document_id,
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": chapter_id,
                "parent_id": document_id,
                "kind": "SECTION",
                "order": 10,
                "label": "9 技术要求",
                "source_refs": [{"source_id": "source-9"}],
            },
            {
                "id": section_id,
                "parent_id": chapter_id,
                "kind": "SECTION",
                "order": 20,
                "label": "9.1 信息安全要求",
                "source_refs": [{"source_id": "source-9-1"}],
            },
            {
                "id": first_child_id,
                "parent_id": section_id,
                "kind": "SECTION",
                "order": 30,
                "label": "9.1.1 第一子节",
                "source_refs": [{"source_id": "source-9-1-1"}],
            },
            {
                "id": second_child_id,
                "parent_id": section_id,
                "kind": "SECTION",
                "order": 40,
                "label": "9.1.2 第二子节",
                "source_refs": [{"source_id": "source-9-1-2"}],
            },
            {
                "id": next_section_id,
                "parent_id": chapter_id,
                "kind": "SECTION",
                "order": 50,
                "label": "9.2 下一节",
                "source_refs": [{"source_id": "source-9-2"}],
            },
        ]
        chapter_blocks = [
            {
                "kind": "text",
                "source_id": "source-9",
                "text": "9 技术要求",
                "page_start": 21,
            },
            {
                "kind": "text",
                "source_id": "source-intro",
                "text": "第九章引言",
                "page_start": 21,
            },
            {
                "kind": "text",
                "source_id": "source-9-1",
                "text": "9.1 信息安全要求",
                "page_start": 22,
            },
            {
                "kind": "text",
                "source_id": "source-9-1-intro",
                "text": "9.1 自身正文",
                "page_start": 22,
            },
            {
                "kind": "text",
                "source_id": "source-9-1-1",
                "text": "9.1.1 第一子节",
                "page_start": 22,
            },
            {
                "kind": "text",
                "source_id": "source-first-body",
                "text": "第一子节正文",
                "page_start": 22,
            },
            {
                "kind": "text",
                "source_id": "source-9-1-2",
                "text": "9.1.2 第二子节",
                "page_start": 23,
            },
            {
                "kind": "text",
                "source_id": "source-second-body",
                "text": "第二子节正文",
                "page_start": 23,
            },
            {
                "kind": "text",
                "source_id": "source-9-2",
                "text": "9.2 下一节",
                "page_start": 24,
            },
            {
                "kind": "text",
                "source_id": "source-next-body",
                "text": "下一节正文",
                "page_start": 24,
            },
        ]
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "review_groups": [
                    {
                        "boundary_structure_node_id": chapter_id,
                        "context_blocks": chapter_blocks,
                    }
                ],
                "items": [
                    {
                        "target_structure": {"id": node_id},
                        "text": label,
                        "context_text": "错误的整章回退正文",
                    }
                    for node_id, label in (
                        (section_id, "9.1 信息安全要求"),
                        (first_child_id, "9.1.1 第一子节"),
                        (second_child_id, "9.1.2 第二子节"),
                        (next_section_id, "9.2 下一节"),
                    )
                ],
            },
            requirement_entries=[],
        )
        by_id = {node.id: node for node in snapshot.nodes}
        chapter_text = _test_block_text(by_id[chapter_id].content_blocks)
        section_text = _test_block_text(by_id[section_id].content_blocks)
        first_child_text = _test_block_text(by_id[first_child_id].content_blocks)
        next_section_text = _test_block_text(by_id[next_section_id].content_blocks)

        self.assertIn("第九章引言", chapter_text)
        self.assertIn("9.1 自身正文", section_text)
        self.assertIn("第一子节正文", section_text)
        self.assertIn("第二子节正文", section_text)
        self.assertNotIn("第九章引言", section_text)
        self.assertNotIn("下一节正文", section_text)
        self.assertEqual(
            first_child_text,
            "9.1.1 第一子节\n第一子节正文",
        )
        self.assertNotIn("错误的整章回退正文", first_child_text)
        self.assertEqual(
            next_section_text,
            "9.2 下一节\n下一节正文",
        )
        self.assertEqual(by_id[section_id].page_start, 22)
        self.assertEqual(by_id[section_id].page_end, 23)

    def test_context_slice_stops_at_earliest_physical_peer_boundary(
        self,
    ) -> None:
        document_id = "document-physical-boundary"
        section_id = "section-physical-boundary"
        first_group_id = "group-first"
        first_item_id = "group-first-item-one"
        delayed_item_id = "group-first-item-two"
        intervening_peer_id = "group-peer-six"
        later_peer_id = "group-peer-seven"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            {
                "id": document_id,
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": section_id,
                "parent_id": document_id,
                "kind": "SECTION",
                "order": 10,
                "label": "Technical requirements",
                "source_refs": [{"source_id": "source-section"}],
            },
            {
                "id": first_group_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 20,
                "label": "First group",
                "source_refs": [{"source_id": "source-group"}],
            },
            {
                "id": first_item_id,
                "parent_id": first_group_id,
                "kind": "CLAUSE",
                "order": 30,
                "label": "First nested item",
                "source_refs": [{"source_id": "source-item-one"}],
            },
            {
                # Parent-first order puts this child before the section peers,
                # even though its immutable source fact occurs after them.
                "id": delayed_item_id,
                "parent_id": first_group_id,
                "kind": "CLAUSE",
                "order": 40,
                "label": "Delayed nested item",
                "source_refs": [{"source_id": "source-item-two"}],
            },
            {
                "id": intervening_peer_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 50,
                "label": "Intervening peer",
                "source_refs": [{"source_id": "source-peer-six"}],
            },
            {
                "id": later_peer_id,
                "parent_id": section_id,
                "kind": "CLAUSE",
                "order": 60,
                "label": "Later peer",
                "source_refs": [{"source_id": "source-peer-seven"}],
            },
        ]
        context_blocks = [
            {
                "kind": "text",
                "source_id": "source-section",
                "text": "Technical requirements",
            },
            {
                "kind": "text",
                "source_id": "source-group",
                "text": "First group",
            },
            {
                "kind": "text",
                "source_id": "source-item-one",
                "text": "First nested item",
            },
            {
                "kind": "text",
                "source_id": "source-item-one-body",
                "text": "First nested body",
            },
            {
                "kind": "text",
                "source_id": "source-peer-six",
                "text": "Intervening peer",
            },
            {
                "kind": "text",
                "source_id": "source-peer-six-body",
                "text": "Intervening peer body",
            },
            {
                "kind": "text",
                "source_id": "source-peer-seven",
                "text": "Later peer",
            },
            {
                "kind": "text",
                "source_id": "source-item-two",
                "text": "Delayed nested item",
            },
        ]
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "review_groups": [
                    {
                        "boundary_structure_node_id": section_id,
                        "context_blocks": context_blocks,
                    }
                ]
            },
            requirement_entries=[],
        )
        by_id = {node.id: node for node in snapshot.nodes}
        first_item_text = _test_block_text(by_id[first_item_id].content_blocks)

        self.assertEqual(
            first_item_text,
            "First nested item\nFirst nested body",
        )
        self.assertNotIn("Intervening peer", first_item_text)
        self.assertNotIn("Later peer", first_item_text)

    def test_source_bound_structure_descendants_fill_their_own_boundary(
        self,
    ) -> None:
        document_id = "document-source-fallback"
        first_section_id = "section-source-fallback-a"
        first_content_id = "content-source-fallback-a"
        first_table_id = "table-source-fallback-a"
        first_figure_id = "figure-source-fallback-a"
        second_section_id = "section-source-fallback-b"
        second_content_id = "content-source-fallback-b"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            {
                "id": document_id,
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": first_section_id,
                "parent_id": document_id,
                "kind": "SECTION",
                "order": 10,
                "label": "1 第一节",
                "source_refs": [{"source_id": "source-section-a"}],
            },
            {
                "id": first_content_id,
                "parent_id": first_section_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "第一节完整正文",
                "page_start": 2,
                "page_end": 2,
                "source_refs": [
                    {
                        "source_id": "source-content-a",
                        "page_number": 2,
                    }
                ],
            },
            {
                "id": first_table_id,
                "parent_id": first_section_id,
                "kind": "TABLE",
                "order": 12,
                "label": "第一节参数表",
                "page_start": 2,
                "page_end": 2,
                "source_refs": [
                    {
                        "source_id": "source-table-a",
                        "page_number": 2,
                    }
                ],
            },
            {
                "id": first_figure_id,
                "parent_id": first_section_id,
                "kind": "FIGURE",
                "order": 13,
                "label": "第一节示意图",
                "page_start": 3,
                "page_end": 3,
                "source_refs": [
                    {
                        "source_id": "source-figure-a",
                        "page_number": 3,
                    }
                ],
            },
            {
                "id": second_section_id,
                "parent_id": document_id,
                "kind": "SECTION",
                "order": 20,
                "label": "2 第二节",
                "source_refs": [{"source_id": "source-section-b"}],
            },
            {
                "id": second_content_id,
                "parent_id": second_section_id,
                "kind": "CONTENT",
                "order": 21,
                "label": "第二节正文不得越界进入第一节",
                "page_start": 4,
                "page_end": 4,
                "source_refs": [
                    {
                        "source_id": "source-content-b",
                        "page_number": 4,
                    }
                ],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "tables": [
                    {
                        "id": "table-a",
                        "source_node_id": "source-table-a",
                        "cells": [
                            {"row": 0, "column": 0, "text": "参数"},
                            {"row": 0, "column": 1, "text": "要求"},
                        ],
                    }
                ]
            },
            requirement_entries=[],
        )

        by_id = {node.id: node for node in snapshot.nodes}
        first = by_id[first_section_id]
        second = by_id[second_section_id]
        first_text = _test_block_text(first.content_blocks)
        second_text = _test_block_text(second.content_blocks)

        self.assertIn("第一节完整正文", first_text)
        self.assertIn("第一节示意图", first_text)
        self.assertNotIn("第二节正文不得越界进入第一节", first_text)
        self.assertEqual(second_text, "第二节正文不得越界进入第一节")
        self.assertEqual(first.table_refs[0]["cells"][0]["text"], "参数")
        self.assertEqual(
            first.figure_refs[0]["source_id"],
            "source-figure-a",
        )
        self.assertFalse(
            {
                first_content_id,
                first_table_id,
                first_figure_id,
                second_content_id,
            }
            & set(by_id)
        )
        self.assertEqual(first.page_start, 2)
        self.assertEqual(first.page_end, 3)

    def test_source_bound_table_cells_are_not_repeated_as_plain_text(
        self,
    ) -> None:
        section_id = "section-table-presentation-dedupe"
        table_node_id = "table-presentation-dedupe"
        cell_content_id = "table-cell-presentation-dedupe"
        table_source_id = "source-table-presentation-dedupe"
        workbench = _workbench()
        workbench["structure"]["nodes"] = [
            workbench["structure"]["nodes"][0],
            {
                "id": section_id,
                "parent_id": ROOT_ID,
                "kind": "SECTION",
                "order": 10,
                "label": "1.3 职责与分工",
                "source_refs": [{"source_id": "source-section-presentation"}],
            },
            {
                "id": "content-before-presentation-table",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 11,
                "label": "职责正文。",
                "source_refs": [{"source_id": "source-content-before-table"}],
            },
            {
                "id": table_node_id,
                "parent_id": section_id,
                "kind": "TABLE",
                "order": 12,
                "label": "职责分工表",
                "source_refs": [{"source_id": table_source_id}],
            },
            {
                "id": cell_content_id,
                "parent_id": table_node_id,
                "kind": "CONTENT",
                "order": 13,
                "label": "供应商负责开发",
                "source_refs": [{"source_id": "source-table-cell-text"}],
            },
            {
                "id": "content-after-presentation-table",
                "parent_id": section_id,
                "kind": "CONTENT",
                "order": 14,
                "label": "表后说明。",
                "source_refs": [{"source_id": "source-content-after-table"}],
            },
        ]

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "tables": [
                    {
                        "id": "table-native-presentation-dedupe",
                        "source_node_id": table_source_id,
                        "cells": [
                            {
                                "id": "cell-native-presentation-dedupe",
                                "row": 0,
                                "column": 0,
                                "text": "供应商负责开发",
                            }
                        ],
                    }
                ],
            },
        )
        section = next(node for node in snapshot.nodes if node.id == section_id)

        self.assertEqual(
            [
                str(block.get("structure_node_id") or "")
                for block in section.content_blocks
            ],
            [
                "content-before-presentation-table",
                table_node_id,
                "content-after-presentation-table",
            ],
        )
        self.assertEqual(
            sum(
                str(block.get("kind") or "").lower() == "table"
                for block in section.content_blocks
            ),
            1,
        )
        self.assertFalse(
            any(
                str(block.get("structure_node_id") or "") == cell_content_id
                for block in section.content_blocks
            )
        )
        self.assertEqual(section.table_refs[0]["cells"][0]["text"], "供应商负责开发")

    def test_table_is_content_of_nearest_visible_structure(self) -> None:
        workbench = _workbench()
        workbench["structure"]["nodes"].append(
            {
                "id": TABLE_ID,
                "parent_id": SECTION_ID,
                "kind": "TABLE",
                "order": 19,
                "label": "参数表",
                "source_refs": [{"kind": "SOURCE_NODE", "source_id": "node-table-1"}],
            }
        )
        clause = next(
            node for node in workbench["structure"]["nodes"] if node["id"] == CLAUSE_ID
        )
        clause["parent_id"] = TABLE_ID
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "tables": [
                    {
                        "id": "table-1",
                        "source_node_id": "node-table-1",
                    }
                ]
            },
            requirement_entries=[
                {
                    "id": "table-requirement",
                    "requirement_entry_id": "table-requirement-entry",
                    "owner_structure_node_id": CLAUSE_ID,
                    "source_order": 21,
                    "title": "强度要求",
                    "text": "接口强度应不低于 40 kN。",
                }
            ],
        )

        by_id = {node.id: node for node in snapshot.nodes}
        self.assertNotIn(TABLE_ID, by_id)
        self.assertEqual(by_id[CLAUSE_ID].parent_id, SECTION_ID)
        self.assertEqual(by_id[SECTION_ID].table_refs[0]["id"], "table-1")
        self.assertEqual(
            by_id[SECTION_ID].table_refs[0]["source_id"],
            "node-table-1",
        )
        leaf = next(node for node in snapshot.nodes if node.kind == "REQUIREMENT")
        self.assertEqual(leaf.parent_id, CLAUSE_ID)
        self.assertEqual(leaf.structure_node_id, CLAUSE_ID)
        self.assertEqual(leaf.table_refs[0]["table_id"], "table-1")
        self.assertEqual(
            [block["kind"] for block in leaf.content_blocks],
            ["paragraph", "table"],
        )


class RequirementTreeSnapshotPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_verified_requirement_tree_snapshot_cache()

    def tearDown(self) -> None:
        clear_verified_requirement_tree_snapshot_cache()

    def test_verified_v1_snapshot_drops_deprecated_embedded_focus(self) -> None:
        snapshot = build_requirement_tree_snapshot(
            workbench=_workbench(),
            mode=RequirementTreeMode.REQUIREMENT_SET,
            requirement_set_id="set-legacy-focus",
            requirement_entries=_entries(published=True),
            forced_state=RequirementTreeState.READY,
        )
        raw = snapshot.model_dump(mode="json")
        raw["nodes"][0]["focus_regions"] = [
            {
                "page_number": 1,
                "bbox": [1, 1, 2, 2],
            }
        ]
        encoded = json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            object_store = LocalObjectStore(Path(temporary))
            stored = object_store.put(
                key="requirement-trees/legacy-focus.json",
                source=io.BytesIO(encoded),
                expected_sha256=hashlib.sha256(encoded).hexdigest(),
                media_type="application/json",
            )
            with patch.object(
                RUNTIME_STATE,
                "container",
                SimpleNamespace(object_store=object_store),
            ):
                loaded = load_requirement_set_tree_snapshot(
                    {
                        "id": "set-legacy-focus",
                        "project_id": "project-1",
                        "sor_document_id": "document-1",
                        "source_hash": SOURCE_HASH,
                        "preview_generation_id": "preview-1",
                        "requirement_tree_object_key": stored.key,
                        "requirement_tree_sha256": stored.sha256,
                        "requirement_tree_schema_version": (
                            REQUIREMENT_TREE_SCHEMA_VERSION
                        ),
                        "structure_snapshot_id": EFFECTIVE_ID,
                        "structure_tree_sha256": TREE_HASH,
                        "structure_revision": 2,
                    }
                )

        self.assertEqual(loaded.nodes[0].id, snapshot.nodes[0].id)
        self.assertFalse(hasattr(loaded.nodes[0], "focus_regions"))

    def test_local_object_snapshot_is_hash_verified_and_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database_path = root / "app.db"
            object_store = LocalObjectStore(root / "objects")
            with (
                patch.object(db, "DB_PATH", database_path),
                patch.object(
                    RUNTIME_STATE,
                    "container",
                    SimpleNamespace(object_store=object_store),
                ),
            ):
                db.init_db()
                project = db.create_project("Tree snapshots", "Customer")
                source = root / "source.pdf"
                source.write_bytes(b"source")
                document = db.add_sor_document(
                    project_id=project["id"],
                    file_name="source.pdf",
                    file_type="pdf",
                    file_path=str(source),
                    extracted_text="",
                    source_sha256=SOURCE_HASH,
                )
                requirement_set = db.create_requirement_set(
                    project["id"],
                    document["id"],
                    source_hash=SOURCE_HASH,
                    preview_generation_id="preview-1",
                )
                workbench = _workbench()
                workbench["source"].update(
                    {
                        "project_id": project["id"],
                        "document_id": document["id"],
                    }
                )
                workbench["source_pages"] = {
                    "geometry_index": {
                        "geometry_index_id": "d" * 64,
                        "geometry_index_sha256": "e" * 64,
                    }
                }
                snapshot = build_requirement_tree_snapshot(
                    workbench=workbench,
                    mode=RequirementTreeMode.REQUIREMENT_SET,
                    requirement_set_id=requirement_set["id"],
                    requirement_entries=_entries(published=True),
                    forced_state=RequirementTreeState.READY,
                )

                attached = persist_requirement_tree_snapshot(
                    requirement_set_id=requirement_set["id"],
                    snapshot=snapshot,
                )
                stored = object_store.stat(attached["requirement_tree_object_key"])
                self.assertIsNotNone(stored)
                self.assertEqual(
                    stored.sha256,
                    canonical_requirement_tree_sha256(snapshot),
                )
                loaded = load_requirement_set_tree_snapshot(
                    db.get_requirement_set(requirement_set["id"]) or {}
                )
                self.assertEqual(loaded, snapshot)
                self.assertEqual(
                    loaded.source_identity.geometry_index_id,
                    "d" * 64,
                )
                self.assertEqual(
                    loaded.source_identity.geometry_index_sha256,
                    "e" * 64,
                )
                activated = activate_document_requirement_set(
                    project["id"],
                    document["id"],
                    requirement_set["id"],
                )
                self.assertEqual(
                    activated["active_requirement_set"]["id"],
                    requirement_set["id"],
                )

                derived_set = db.create_requirement_set(
                    project["id"],
                    document["id"],
                    source_hash=SOURCE_HASH,
                    preview_generation_id="preview-2",
                    supersedes_id=requirement_set["id"],
                )
                cloned = clone_requirement_set_tree_snapshot(
                    source_requirement_set=(
                        db.get_requirement_set(requirement_set["id"]) or {}
                    ),
                    target_requirement_set=derived_set,
                    requirement_id_mapping={
                        "published-entry-1": "derived-entry-1",
                        "published-entry-2": "derived-entry-2",
                    },
                )
                self.assertEqual(
                    [node.id for node in cloned.nodes],
                    [node.id for node in snapshot.nodes],
                )
                self.assertEqual(
                    {
                        node.requirement_entry_id
                        for node in cloned.nodes
                        if node.requirement_entry_id
                    },
                    {"derived-entry-1", "derived-entry-2"},
                )
                self.assertEqual(
                    cloned.source_identity.requirement_set_id,
                    derived_set["id"],
                )
                self.assertEqual(
                    cloned.source_identity.geometry_index_sha256,
                    "e" * 64,
                )
                cloned_bundle = cloned.requirement_evidence_bundle
                self.assertIsNotNone(cloned_bundle)
                assert cloned_bundle is not None
                self.assertEqual(
                    {
                        binding.requirement_entry_id
                        for binding in cloned_bundle.requirement_bindings
                    },
                    {"derived-entry-1", "derived-entry-2"},
                )
                cloned_evidence_ids = {
                    binding.binding_id for binding in cloned_bundle.requirement_bindings
                }
                self.assertEqual(
                    {
                        node.requirement_evidence_binding_id
                        for node in cloned.nodes
                        if node.requirement_entry_id
                    },
                    cloned_evidence_ids,
                )

                historical_set = db.create_requirement_set(
                    project["id"],
                    document["id"],
                    source_hash=SOURCE_HASH,
                )
                with self.assertRaises(ApplicationError) as activation_error:
                    activate_document_requirement_set(
                        project["id"],
                        document["id"],
                        historical_set["id"],
                    )
                self.assertEqual(activation_error.exception.status_code, 409)

                with self.assertRaises(db.RequirementSetMutationBlocked):
                    db.attach_requirement_tree_snapshot(
                        requirement_set["id"],
                        object_key=attached["requirement_tree_object_key"],
                        tree_sha256=hashlib.sha256(b"different").hexdigest(),
                        schema_version="requirement-tree-snapshot/v1",
                        structure_snapshot_id=EFFECTIVE_ID,
                        structure_tree_sha256=TREE_HASH,
                        structure_revision=2,
                    )

    def test_historical_set_without_provable_tree_requires_reextraction(self) -> None:
        snapshot = missing_requirement_set_tree_snapshot(
            {
                "id": "historical-set",
                "project_id": "project-1",
                "sor_document_id": "document-1",
                "source_hash": SOURCE_HASH,
            }
        )
        self.assertEqual(snapshot.state, RequirementTreeState.REQUIRES_REEXTRACTION)
        self.assertEqual(snapshot.nodes, ())

    def test_verified_snapshot_is_reused_for_immutable_requirement_set(
        self,
    ) -> None:
        snapshot = build_requirement_tree_snapshot(
            workbench=_workbench(),
            mode=RequirementTreeMode.REQUIREMENT_SET,
            requirement_set_id="set-cached",
            requirement_entries=_entries(published=True),
            forced_state=RequirementTreeState.READY,
        )
        encoded = json.dumps(
            snapshot.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            object_store = LocalObjectStore(Path(temporary))
            stored = object_store.put(
                key="requirement-trees/cached.json",
                source=io.BytesIO(encoded),
                expected_sha256=hashlib.sha256(encoded).hexdigest(),
                media_type="application/json",
            )
            requirement_set = {
                "id": "set-cached",
                "project_id": "project-1",
                "sor_document_id": "document-1",
                "source_hash": SOURCE_HASH,
                "preview_generation_id": "preview-1",
                "requirement_tree_object_key": stored.key,
                "requirement_tree_sha256": stored.sha256,
                "requirement_tree_schema_version": (REQUIREMENT_TREE_SCHEMA_VERSION),
                "structure_snapshot_id": EFFECTIVE_ID,
                "structure_tree_sha256": TREE_HASH,
                "structure_revision": 2,
            }
            with (
                patch.object(
                    RUNTIME_STATE,
                    "container",
                    SimpleNamespace(object_store=object_store),
                ),
                patch.object(
                    object_store,
                    "stat",
                    wraps=object_store.stat,
                ) as stat,
                patch.object(
                    object_store,
                    "open",
                    wraps=object_store.open,
                ) as open_object,
            ):
                first = load_requirement_set_tree_snapshot(requirement_set)
                second = load_requirement_set_tree_snapshot(requirement_set)

        self.assertIs(first, second)
        self.assertEqual(stat.call_count, 1)
        self.assertEqual(open_object.call_count, 1)

    def test_snapshot_loader_rejects_tampered_version_lineage(self) -> None:
        snapshot = build_requirement_tree_snapshot(
            workbench=_workbench(),
            mode=RequirementTreeMode.REQUIREMENT_SET,
            requirement_set_id="set-lineage",
            requirement_entries=_entries(published=True),
            forced_state=RequirementTreeState.READY,
        )
        encoded = canonical_requirement_tree_bytes(snapshot)
        with tempfile.TemporaryDirectory() as temporary:
            object_store = LocalObjectStore(Path(temporary))
            stored = object_store.put(
                key="requirement-trees/lineage.json",
                source=io.BytesIO(encoded),
                expected_sha256=hashlib.sha256(encoded).hexdigest(),
                media_type="application/json",
            )
            base = {
                "id": "set-lineage",
                "project_id": "project-1",
                "sor_document_id": "document-1",
                "source_hash": SOURCE_HASH,
                "preview_generation_id": "preview-1",
                "requirement_tree_object_key": stored.key,
                "requirement_tree_sha256": stored.sha256,
                "requirement_tree_schema_version": (REQUIREMENT_TREE_SCHEMA_VERSION),
                "structure_snapshot_id": EFFECTIVE_ID,
                "structure_tree_sha256": TREE_HASH,
                "structure_revision": 2,
            }
            tampered_cases = (
                {"preview_generation_id": "preview-other"},
                {"requirement_tree_schema_version": "requirement-tree/v0"},
                {
                    "publication_key": "a" * 64,
                    "publication_state": "READY",
                },
                {
                    "publication_key": _working_tree_publication_key(snapshot),
                    "publication_state": "BUILDING",
                },
            )
            with patch.object(
                RUNTIME_STATE,
                "container",
                SimpleNamespace(object_store=object_store),
            ):
                for changes in tampered_cases:
                    with self.subTest(changes=changes):
                        clear_verified_requirement_tree_snapshot_cache()
                        with self.assertRaises(RequirementTreeSnapshotError):
                            load_requirement_set_tree_snapshot({**base, **changes})


if __name__ == "__main__":
    unittest.main()
