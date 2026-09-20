from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from types import SimpleNamespace

from app.application.extraction.requirement_tree import (
    _table_record_primary_pages,
    attach_toc_navigation_entries,
    build_requirement_tree_snapshot,
)
from app.application.extraction.serialize_structure_workbench import (
    _persisted_structure_presentation,
    _serialize_node,
)
from app.application.extraction.structure_presentation import (
    STRUCTURE_PRESENTATION_INDEX_SCHEMA_VERSION,
    build_structure_bilingual_projection_sidecar,
)
from app.domain.extraction.table_records import TableRecordKind


class StructureSourceBoundaryProjectionTests(unittest.TestCase):
    def test_table_record_page_uses_primary_cells_not_header_context(self) -> None:
        record = SimpleNamespace(
            kind=TableRecordKind.ROW,
            row_bindings=(
                SimpleNamespace(
                    table_id="table-body",
                    cell_ids=("cell-body",),
                ),
            ),
        )
        table_blocks = (
            {
                "id": "table-header",
                "page_start": 33,
                "cells": [
                    {
                        "id": "cell-header",
                        "location": {"page_number": 33},
                    }
                ],
            },
            {
                "id": "table-body",
                "page_start": 34,
                "cells": [
                    {
                        "id": "cell-body",
                        "location": {"page_number": 34},
                    }
                ],
            },
        )

        self.assertEqual(
            _table_record_primary_pages(record, table_blocks=table_blocks),
            (34,),
        )

    def test_matrix_record_page_uses_row_column_intersection(self) -> None:
        record = SimpleNamespace(
            kind=TableRecordKind.MATRIX_INTERSECTION,
            row_bindings=(
                SimpleNamespace(
                    table_id="table-matrix",
                    cell_ids=("row-label", "matrix-value"),
                ),
            ),
            column_bindings=(
                SimpleNamespace(
                    table_id="table-matrix",
                    cell_ids=("column-label", "matrix-value"),
                ),
            ),
        )
        table_blocks = (
            {
                "id": "table-matrix",
                "cells": [
                    {
                        "id": "row-label",
                        "location": {"page_number": 4},
                    },
                    {
                        "id": "column-label",
                        "location": {"page_number": 5},
                    },
                    {
                        "id": "matrix-value",
                        "location": {"page_number": 6},
                    },
                ],
            },
        )

        self.assertEqual(
            _table_record_primary_pages(record, table_blocks=table_blocks),
            (6,),
        )

    def test_exact_span_offset_wins_over_same_source_wide_ref(self) -> None:
        serialized = _serialize_node(
            {
                "structure_node_id": "structure-inline-second",
                "parent_structure_node_id": "structure-section",
                "kind": "clause",
                "source_order": 5,
                "source_refs": [{"kind": "source_node", "source_id": "source-shared"}],
            },
            depth=2,
            label="1.2 第二项",
            path=["文档", "章节", "1.2 第二项"],
            source_index={
                "source-shared": {
                    "kind": "paragraph",
                    "page_number": 8,
                    "source_order": 42,
                    "text": "1.1 第一项。\n1.2 第二项。",
                }
            },
            presentation=None,
            durable_presentation=None,
            requirement_ids=(),
            health_codes=(),
            source_spans=(
                {
                    "source_ref": {
                        "kind": "source_node",
                        "source_id": "source-shared",
                    },
                    "start_offset": 9,
                    "end_offset": 16,
                },
                {
                    "source_ref": {
                        "kind": "source_node",
                        "source_id": "source-shared",
                    },
                    "start_offset": "unknown",
                    "end_offset": 16,
                },
            ),
        )

        self.assertEqual(
            (
                serialized["physical_source_order"],
                serialized["physical_source_offset"],
            ),
            (42, 9),
        )

    def test_durable_toc_source_facts_restore_split_navigation(self) -> None:
        source_sha256 = "a" * 64
        snapshot_id = "effective-1"
        toc_id = "toc-navigation"
        target_id = "target-section"
        payload = {
            "source_sha256": source_sha256,
            "structure_presentation_index": {
                "schema_version": STRUCTURE_PRESENTATION_INDEX_SCHEMA_VERSION,
                "source_ir_id": "source-ir-1",
                "source_sha256": source_sha256,
                "structure_snapshot_id": snapshot_id,
                "page_count": 12,
                "bilingual_projection": (
                    build_structure_bilingual_projection_sidecar(None)
                ),
                "nodes": {
                    toc_id: {
                        "label": "目 录",
                        "owned_text": "目 录",
                        "source_refs": [
                            {
                                "kind": "source_node",
                                "source_id": "toc-number",
                                "source_kind": "paragraph",
                                "page_number": 2,
                            },
                            {
                                "kind": "source_node",
                                "source_id": "toc-title",
                                "source_kind": "paragraph",
                                "page_number": 2,
                            },
                        ],
                        "toc_source_text_by_id": {
                            "toc-number": "1.2",
                            "toc-title": "报价要求........................7",
                        },
                        "physical_source_order": 1,
                        "physical_source_offset": 0,
                        "page_start": 2,
                        "page_end": 2,
                    },
                    target_id: {
                        "label": "1.2 报价要求",
                        "owned_text": "1.2 报价要求",
                        "source_refs": [
                            {
                                "kind": "source_node",
                                "source_id": "target-source",
                                "source_kind": "paragraph",
                                "page_number": 12,
                            }
                        ],
                        "toc_source_text_by_id": {},
                        "physical_source_order": 20,
                        "physical_source_offset": 0,
                        "page_start": 12,
                        "page_end": 12,
                    },
                    "shared-content": {
                        "label": "正文复用目录来源",
                        "owned_text": "正文复用目录来源",
                        "source_refs": [
                            {
                                "kind": "source_node",
                                "source_id": "toc-title",
                                "source_kind": "paragraph",
                                "page_number": 2,
                            }
                        ],
                        "toc_source_text_by_id": {},
                        "physical_source_order": 21,
                        "physical_source_offset": 0,
                        "page_start": 2,
                        "page_end": 2,
                    },
                },
            },
        }
        presentations, source_index, page_count = _persisted_structure_presentation(
            payload,
            structure_agent={
                "effective_structure_identity": {"snapshot_id": snapshot_id}
            },
        )
        nodes = [
            {
                "id": "document-root",
                "parent_id": "",
                "kind": "DOCUMENT",
                "order": 0,
                "label": "Document",
                "source_refs": [],
            },
            {
                "id": toc_id,
                "parent_id": "document-root",
                "kind": "GROUP",
                "boundary_basis": "TOC_BLOCK",
                "order": 1,
                "label": presentations[toc_id]["label"],
                "source_refs": presentations[toc_id]["source_refs"],
            },
            {
                "id": target_id,
                "parent_id": "document-root",
                "kind": "SECTION",
                "order": 20,
                "explicit_label": "1.2",
                "label": presentations[target_id]["label"],
                "page_start": 12,
                "page_end": 12,
                "source_refs": presentations[target_id]["source_refs"],
            },
        ]

        enriched = attach_toc_navigation_entries(nodes, source_index=source_index)
        toc = next(node for node in enriched if node["id"] == toc_id)

        self.assertEqual(page_count, 12)
        self.assertEqual(len(toc["toc_entries"]), 1)
        self.assertEqual(toc["toc_entries"][0]["number"], "1.2")
        self.assertEqual(toc["toc_entries"][0]["printed_page_label"], "7")
        self.assertEqual(toc["toc_entries"][0]["target_page"], 12)

    def test_aggregate_presentation_pages_do_not_widen_structure_node(self) -> None:
        serialized = _serialize_node(
            {
                "structure_node_id": "structure-content",
                "parent_structure_node_id": "structure-section",
                "kind": "content",
                "source_order": 10,
                "source_refs": [{"kind": "source_node", "source_id": "source-page-13"}],
            },
            depth=2,
            label="本节点只来自第十三页。",
            path=["文档", "章节", "本节点只来自第十三页。"],
            source_index={
                "source-page-13": {
                    "kind": "paragraph",
                    "page_number": 13,
                }
            },
            presentation={
                "id": "aggregate-review-item",
                "page_start": 13,
                "page_end": 14,
            },
            durable_presentation={
                "source_refs": [{"kind": "source_node", "source_id": "source-page-13"}],
                "page_start": 13,
                "page_end": 14,
            },
            requirement_ids=("aggregate-review-item",),
            health_codes=(),
            source_spans=(
                {
                    "source_ref": {
                        "kind": "source_node",
                        "source_id": "source-page-13",
                    },
                    "start_offset": 0,
                    "end_offset": 12,
                },
            ),
        )

        self.assertEqual((serialized["page_start"], serialized["page_end"]), (13, 13))
        self.assertEqual(serialized["clause_id"], "aggregate-review-item")

    def test_leaf_fallback_excludes_parent_owned_following_content(self) -> None:
        root_id = "structure-root"
        section_id = "structure-section"
        terminal_id = "structure-terminal-e"
        following_content_id = "structure-parent-content"
        terminal_source_id = "source-terminal-e"
        following_source_id = "source-parent-content"
        terminal_text = "e) 供应商对产品设计和质量负责。"
        following_text = "供应商与主机厂在项目活动中的职责如下表所示："
        workbench = {
            "project_id": "project-1",
            "document_id": "document-1",
            "source": {
                "project_id": "project-1",
                "document_id": "document-1",
                "source_ir_id": "source-ir-1",
                "source_sha256": "a" * 64,
                "preview_generation_id": "preview-1",
            },
            "agent": {"status": "COMPLETED", "result_state": "STABLE"},
            "structure": {
                "base_snapshot_id": "base-1",
                "effective_snapshot_id": "effective-1",
                "effective_tree_sha256": "b" * 64,
                "revision": 0,
                "nodes": [
                    {
                        "id": root_id,
                        "parent_id": "",
                        "kind": "DOCUMENT",
                        "order": 0,
                        "label": "Document",
                        "source_refs": [],
                    },
                    {
                        "id": section_id,
                        "parent_id": root_id,
                        "kind": "SECTION",
                        "order": 10,
                        "label": "1.3 职责与分工",
                        "source_refs": [
                            {"source_id": "source-section", "page_number": 11}
                        ],
                    },
                    {
                        "id": terminal_id,
                        "parent_id": section_id,
                        "kind": "CLAUSE",
                        "order": 20,
                        "label": terminal_text,
                        "boundary_basis": "EXPLICIT_NUMBER",
                        "list_ordinal": 5,
                        "page_start": 11,
                        "page_end": 11,
                        "source_refs": [
                            {"source_id": terminal_source_id, "page_number": 11}
                        ],
                    },
                    {
                        "id": following_content_id,
                        "parent_id": section_id,
                        "kind": "CONTENT",
                        "order": 21,
                        "label": following_text,
                        "page_start": 11,
                        "page_end": 11,
                        "source_refs": [
                            {"source_id": following_source_id, "page_number": 11}
                        ],
                    },
                ],
            },
        }
        payload = {
            "mode": "LOCAL_STRUCTURE_PROJECTION",
            "review_groups": [
                {
                    "boundary_structure_node_id": section_id,
                    "context_blocks": [
                        {
                            "kind": "text",
                            "source_id": "source-section",
                            "text": "1.3 职责与分工",
                            "page_start": 11,
                            "page_end": 11,
                        },
                        {
                            "kind": "text",
                            "source_id": terminal_source_id,
                            "text": terminal_text,
                            "page_start": 11,
                            "page_end": 11,
                        },
                        {
                            "kind": "text",
                            "source_id": following_source_id,
                            "text": following_text,
                            "page_start": 11,
                            "page_end": 11,
                        },
                    ],
                }
            ],
        }

        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload=payload,
        )
        terminal = next(node for node in snapshot.nodes if node.id == terminal_id)
        terminal_source_ids = {
            str(ref.get("source_id") or "") for ref in terminal.source_refs
        }
        following = next(
            node
            for node in snapshot.nodes
            if node.projection_role == "DIRECT_CONTENT" and following_text in node.label
        )

        self.assertEqual(terminal.projection_role, "STRUCTURE_PROXY")
        self.assertEqual(terminal_source_ids, {terminal_source_id})
        self.assertEqual(
            {str(block.get("source_id") or "") for block in terminal.content_blocks},
            {terminal_source_id},
        )
        self.assertEqual(following.parent_id, section_id)
        self.assertEqual(
            {str(ref.get("source_id") or "") for ref in following.source_refs},
            {following_source_id},
        )

    def test_structure_container_keeps_its_exact_span_when_it_has_children(
        self,
    ) -> None:
        root_id = "structure-root"
        clause_id = "structure-clause-with-table"
        child_id = "structure-child"
        shared_source_id = "source-shared-paragraph"
        workbench = {
            "project_id": "project-1",
            "document_id": "document-1",
            "source": {
                "project_id": "project-1",
                "document_id": "document-1",
                "source_ir_id": "source-ir-1",
                "source_sha256": "a" * 64,
                "preview_generation_id": "preview-1",
            },
            "agent": {"status": "COMPLETED", "result_state": "STABLE"},
            "structure": {
                "base_snapshot_id": "base-1",
                "effective_snapshot_id": "effective-1",
                "effective_tree_sha256": "b" * 64,
                "revision": 0,
                "nodes": [
                    {
                        "id": root_id,
                        "parent_id": "",
                        "kind": "DOCUMENT",
                        "order": 0,
                        "label": "Document",
                        "source_refs": [],
                    },
                    {
                        "id": clause_id,
                        "parent_id": root_id,
                        "kind": "SECTION",
                        "order": 10,
                        "label": "4.1.4 当前条款",
                        "page_start": 10,
                        "page_end": 10,
                        "source_refs": [
                            {
                                "kind": "source_node",
                                "source_id": shared_source_id,
                                "page_number": 10,
                                "source_kind": "paragraph",
                            }
                        ],
                        "source_spans": [
                            {
                                "source_ref": {
                                    "kind": "source_node",
                                    "source_id": shared_source_id,
                                },
                                "start_offset": 20,
                                "end_offset": 30,
                            }
                        ],
                    },
                    {
                        "id": child_id,
                        "parent_id": clause_id,
                        "kind": "SECTION",
                        "order": 11,
                        "label": "4.1.4.1 子条款",
                        "page_start": 10,
                        "page_end": 10,
                        "source_refs": [
                            {
                                "kind": "source_node",
                                "source_id": "source-child",
                                "page_number": 10,
                            }
                        ],
                    },
                ],
            },
        }
        snapshot = build_requirement_tree_snapshot(
            workbench=workbench,
            payload={
                "mode": "LOCAL_STRUCTURE_PROJECTION",
                "review_groups": [],
                "tables": [],
            },
        )
        clause = next(node for node in snapshot.nodes if node.id == clause_id)

        self.assertNotEqual(clause.projection_role, "STRUCTURE_PROXY")
        self.assertTrue(clause.child_ids)
        self.assertEqual(
            clause.source_refs,
            (
                {
                    "kind": "source_node",
                    "source_id": shared_source_id,
                    "page_number": 10,
                    "source_kind": "paragraph",
                    "start_offset": 20,
                    "end_offset": 30,
                },
            ),
        )


if __name__ == "__main__":
    unittest.main()
