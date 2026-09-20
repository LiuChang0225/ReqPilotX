from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.adapters.documents.pdf_source_crop_renderer import PdfVisiblePageGeometry
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.extraction.geometry_evidence import (
    InMemoryGeometryEvidence,
    PersistedGeometryEvidence,
    _focus_regions_for_nodes,
    _geometry_focus_scope_nodes,
    _table_blocks_for_record,
    build_pdf_page_geometry_preview,
    focus_regions_for_table_record,
    get_document_geometry_focus,
    load_pdf_page_geometry_descriptor_by_identity,
    persist_pdf_page_geometry_preview,
    validated_pdf_page_geometry_descriptor,
)
from app.application.errors import ApplicationError
from app.application.runtime import RUNTIME_STATE
from app.domain.extraction.page_geometry import (
    PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION,
    PDF_VISIBLE_COORDINATE_SPACE,
    PdfPageGeometryIndexV1,
    PdfPageGeometryLineV1,
    PdfPageGeometryObjectV1,
    PdfPageGeometrySourceIdentityV1,
    create_pdf_page_geometry_index,
)
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    PdfTextLineGeometry,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
)
from app.domain.extraction.requirement_tree import (
    RequirementTreeMode,
    RequirementTreeNodeV1,
    RequirementTreeSnapshotV1,
    RequirementTreeSourceIdentity,
    RequirementTreeState,
    RequirementTreeStructureIdentity,
)
from app.domain.extraction.table_records import TableRecordKind


SOURCE_HASH = "a" * 64
ADAPTER_ID = "pdf-test"
ADAPTER_VERSION = "1.0"


def _source_identity() -> PdfPageGeometrySourceIdentityV1:
    return PdfPageGeometrySourceIdentityV1(
        source_sha256=SOURCE_HASH,
        source_ir_id="sir-" + "b" * 32,
        source_ir_schema_version="source-ir/v1",
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
    )


def _page() -> PdfPageGeometryIndexV1:
    source_id = "source-node-" + "1" * 32
    return create_pdf_page_geometry_index(
        source_identity=_source_identity(),
        page_number=10,
        page_width=595,
        page_height=842,
        rotation_cw_degrees=0,
        text_lines=(
            PdfPageGeometryLineV1(
                source_node_id=source_id,
                source_text_length=42,
                start_offset=0,
                end_offset=20,
                bbox=(70, 80, 300, 94),
                source_order=10_000_000,
            ),
            PdfPageGeometryLineV1(
                source_node_id=source_id,
                source_text_length=42,
                start_offset=21,
                end_offset=42,
                bbox=(70, 98, 320, 112),
                source_order=10_000_001,
            ),
        ),
        tables=(
            PdfPageGeometryObjectV1(
                source_node_id="table-1",
                kind="TABLE",
                bbox=(63, 300, 546, 391),
                source_order=12_000_000,
                table_id="table-1",
            ),
        ),
    )


class PdfPageGeometryContractTests(unittest.TestCase):
    def test_bounded_ref_outranks_same_source_whole_block_fallback(self) -> None:
        source_id = "source-node-" + "6" * 32
        page = create_pdf_page_geometry_index(
            source_identity=_source_identity(),
            page_number=1,
            page_width=595,
            page_height=842,
            rotation_cw_degrees=0,
            text_lines=tuple(
                PdfPageGeometryLineV1(
                    source_node_id=source_id,
                    source_text_length=15,
                    start_offset=index * 5,
                    end_offset=(index + 1) * 5,
                    bbox=(10, 10 + index * 20, 210, 20 + index * 20),
                    source_order=index,
                )
                for index in range(3)
            ),
        )

        regions = InMemoryGeometryEvidence((page,)).regions_for_content(
            content_blocks=(
                {
                    "kind": "text",
                    "source_id": source_id,
                    "page_start": 1,
                },
            ),
            source_refs=(
                {"source_id": source_id, "page_number": 1},
                {
                    "source_id": source_id,
                    "start_offset": 10,
                    "end_offset": 15,
                },
            ),
        )

        self.assertEqual(len(regions), 1)
        self.assertEqual((regions[0].start_offset, regions[0].end_offset), (10, 15))

    def test_table_record_resolves_full_table_facts_outside_selected_leaf(
        self,
    ) -> None:
        table = {
            "kind": "table",
            "id": "table-1",
            "source_node_id": "source-table-1",
            "cells": [{"id": "cell-1"}],
        }
        enriched_table = {
            **table,
            "table_id": "table-1",
            "source_id": "source-table-1",
            "source_refs": [{"source_id": "source-table-1"}],
            "structure_node_id": "structure-table-1",
        }
        tree = SimpleNamespace(
            nodes=(
                SimpleNamespace(
                    table_refs=(
                        {
                            "kind": "table_reference",
                            "table_ids": ["table-1"],
                        },
                    ),
                    content_blocks=(),
                ),
                SimpleNamespace(
                    table_refs=(table,),
                    content_blocks=(enriched_table,),
                ),
            )
        )
        record = SimpleNamespace(
            kind=TableRecordKind.ROW,
            table_ids=("table-1",),
            row_bindings=(
                SimpleNamespace(table_id="table-1", cell_ids=("cell-1",)),
            ),
            column_bindings=(),
        )

        self.assertEqual(
            _table_blocks_for_record(tree=tree, record=record),
            (table,),
        )

    def test_table_record_focus_uses_bound_row_or_matrix_cells(self) -> None:
        source_id = "source-node-" + "9" * 32
        page = create_pdf_page_geometry_index(
            source_identity=_source_identity(),
            page_number=1,
            page_width=595,
            page_height=842,
            rotation_cw_degrees=0,
            tables=(
                PdfPageGeometryObjectV1(
                    source_node_id=source_id,
                    kind="TABLE",
                    bbox=(10, 10, 210, 130),
                    source_order=10,
                    table_id="table-1",
                ),
            ),
        )
        evidence = InMemoryGeometryEvidence((page,))
        table = {
            "kind": "table",
            "id": "table-1",
            "source_node_id": source_id,
            "location": {
                "page_number": 1,
                "bbox": [10, 10, 210, 130],
            },
            "cells": [
                {
                    "id": "cell-header",
                    "location": {
                        "page_number": 1,
                        "bbox": [10, 10, 210, 30],
                    },
                },
                {
                    "id": "cell-row-1-a",
                    "location": {
                        "page_number": 1,
                        "bbox": [10, 30, 80, 70],
                    },
                },
                {
                    "id": "cell-row-1-b",
                    "location": {
                        "page_number": 1,
                        "bbox": [80, 30, 210, 70],
                    },
                },
                {
                    "id": "cell-row-2",
                    "location": {
                        "page_number": 1,
                        "bbox": [10, 70, 210, 110],
                    },
                },
            ],
        }
        row_record = SimpleNamespace(
            record_id="table-record-row",
            kind=TableRecordKind.ROW,
            table_ids=("table-1",),
            row_bindings=(
                SimpleNamespace(
                    table_id="table-1",
                    cell_ids=("cell-row-1-a", "cell-row-1-b"),
                ),
            ),
            column_bindings=(),
        )
        matrix_record = SimpleNamespace(
            record_id="table-record-matrix",
            kind=TableRecordKind.MATRIX_INTERSECTION,
            table_ids=("table-1",),
            row_bindings=(
                SimpleNamespace(
                    table_id="table-1",
                    cell_ids=("cell-row-1-a", "cell-row-1-b"),
                ),
            ),
            column_bindings=(
                SimpleNamespace(
                    table_id="table-1",
                    cell_ids=("cell-row-1-b",),
                ),
            ),
        )

        row_regions = focus_regions_for_table_record(
            evidence=evidence,
            record=row_record,
            table_blocks=(table,),
        )
        matrix_regions = focus_regions_for_table_record(
            evidence=evidence,
            record=matrix_record,
            table_blocks=(table,),
        )

        self.assertEqual(len(row_regions), 1)
        self.assertEqual(row_regions[0].bbox, (10.0, 30.0, 210.0, 70.0))
        self.assertEqual(row_regions[0].role, "TABLE_RECORD")
        self.assertEqual(row_regions[0].precision, "EXACT_TABLE_ROW")
        self.assertEqual(matrix_regions[0].bbox, (80.0, 30.0, 210.0, 70.0))
        self.assertEqual(matrix_regions[0].precision, "EXACT_TABLE_CELL")

    def test_table_record_focus_splits_continuation_pages_and_fails_closed(
        self,
    ) -> None:
        first_source_id = "source-node-" + "8" * 32
        second_source_id = "source-node-" + "7" * 32
        pages = (
            create_pdf_page_geometry_index(
                source_identity=_source_identity(),
                page_number=1,
                page_width=595,
                page_height=842,
                rotation_cw_degrees=0,
                tables=(
                    PdfPageGeometryObjectV1(
                        source_node_id=first_source_id,
                        kind="TABLE",
                        bbox=(10, 10, 210, 90),
                        source_order=10,
                        table_id="table-1",
                    ),
                ),
            ),
            create_pdf_page_geometry_index(
                source_identity=_source_identity(),
                page_number=2,
                page_width=595,
                page_height=842,
                rotation_cw_degrees=0,
                tables=(
                    PdfPageGeometryObjectV1(
                        source_node_id=second_source_id,
                        kind="TABLE",
                        bbox=(10, 20, 210, 100),
                        source_order=20,
                        table_id="table-2",
                    ),
                ),
            ),
        )
        evidence = InMemoryGeometryEvidence(pages)
        table_blocks = (
            {
                "kind": "table",
                "id": "table-1",
                "source_node_id": first_source_id,
                "location": {
                    "page_number": 1,
                    "bbox": [10, 10, 210, 90],
                },
                "cells": [
                    {
                        "id": "cell-1",
                        "location": {
                            "page_number": 1,
                            "bbox": [10, 30, 210, 60],
                        },
                    },
                ],
            },
            {
                "kind": "table",
                "id": "table-2",
                "source_node_id": second_source_id,
                "location": {
                    "page_number": 2,
                    "bbox": [10, 20, 210, 100],
                },
                "cells": [
                    {
                        "id": "cell-2",
                        "location": {
                            "page_number": 2,
                            "bbox": [10, 40, 210, 80],
                        },
                    },
                ],
            },
        )
        record = SimpleNamespace(
            record_id="table-record-continuation",
            kind=TableRecordKind.ROW,
            table_ids=("table-1", "table-2"),
            row_bindings=(
                SimpleNamespace(table_id="table-1", cell_ids=("cell-1",)),
                SimpleNamespace(table_id="table-2", cell_ids=("cell-2",)),
            ),
            column_bindings=(),
        )

        regions = focus_regions_for_table_record(
            evidence=evidence,
            record=record,
            table_blocks=table_blocks,
        )
        invalid_second = {
            **table_blocks[1],
            "cells": [
                {
                    "id": "cell-2",
                    "location": {
                        "page_number": 2,
                        "bbox": [10, 40, 260, 80],
                    },
                },
            ],
        }
        invalid = focus_regions_for_table_record(
            evidence=evidence,
            record=record,
            table_blocks=(table_blocks[0], invalid_second),
        )
        context_record = SimpleNamespace(
            record_id="table-record-with-header-context",
            kind=TableRecordKind.ROW,
            table_ids=("table-1", "table-2"),
            row_bindings=(
                SimpleNamespace(table_id="table-2", cell_ids=("cell-2",)),
            ),
            column_bindings=(),
        )
        context_regions = focus_regions_for_table_record(
            evidence=evidence,
            record=context_record,
            table_blocks=table_blocks,
        )

        self.assertEqual(
            [(region.page_number, region.bbox) for region in regions],
            [
                (1, (10.0, 30.0, 210.0, 60.0)),
                (2, (10.0, 40.0, 210.0, 80.0)),
            ],
        )
        self.assertEqual(invalid, ())
        self.assertEqual(
            [(region.page_number, region.bbox) for region in context_regions],
            [(2, (10.0, 40.0, 210.0, 80.0))],
        )

    def test_page_index_hash_rejects_any_geometry_mutation(self) -> None:
        page = _page()
        self.assertEqual(page.coordinate_space, PDF_VISIBLE_COORDINATE_SPACE)
        self.assertEqual(len(page.index_sha256), 64)
        tampered = page.model_dump(mode="json")
        tampered["page_width"] = 600
        with self.assertRaisesRegex(ValueError, "index_sha256"):
            PdfPageGeometryIndexV1.model_validate(tampered)

    def test_geometry_port_projects_exact_line_caption_and_table_regions(
        self,
    ) -> None:
        page = _page()
        source_id = page.text_lines[0].source_node_id
        source_refs = [
            {
                "kind": "source_node",
                "source_id": source_id,
                "page_number": 10,
                "bbox": [70, 80, 540, 250],
            }
        ]
        presentation = {
            "context_blocks": [
                {
                    "kind": "text",
                    "source_id": source_id,
                    "page_start": 10,
                    "start_offset": 21,
                    "end_offset": 42,
                    "location": {"bbox": [70, 80, 540, 250]},
                },
                {
                    "kind": "text",
                    "source_id": "caption-1",
                    "page_start": 10,
                    "location": {"bbox": [250, 260, 360, 288]},
                },
                {
                    "kind": "table",
                    "source_id": "table-1",
                    "page_start": 10,
                    "location": {"bbox": [63, 300, 546, 391]},
                },
            ]
        }
        current = [
            region.as_dict()
            for region in InMemoryGeometryEvidence((page,)).regions_for_content(
                content_blocks=presentation["context_blocks"],
                source_refs=source_refs,
            )
        ]
        self.assertEqual(
            [region["role"] for region in current],
            ["CLAUSE_TEXT", "TABLE_CAPTION", "TABLE"],
        )
        self.assertEqual(
            [region["precision"] for region in current],
            ["EXACT_LINE", "EXACT_BLOCK", "EXACT_BLOCK"],
        )
        self.assertEqual(current[0]["bbox"], [70.0, 98.0, 320.0, 112.0])

    def test_no_offset_prose_and_table_are_resolved_independently(self) -> None:
        prose_id = "source-node-" + "4" * 32
        table_id = "source-node-" + "5" * 32
        page = create_pdf_page_geometry_index(
            source_identity=_source_identity(),
            page_number=1,
            page_width=595,
            page_height=842,
            rotation_cw_degrees=0,
            text_lines=(
                PdfPageGeometryLineV1(
                    source_node_id=prose_id,
                    source_text_length=24,
                    start_offset=0,
                    end_offset=12,
                    bbox=(70, 80, 300, 94),
                    source_order=10,
                ),
                PdfPageGeometryLineV1(
                    source_node_id=prose_id,
                    source_text_length=24,
                    start_offset=12,
                    end_offset=24,
                    bbox=(70, 98, 320, 112),
                    source_order=11,
                ),
            ),
            tables=(
                PdfPageGeometryObjectV1(
                    source_node_id=table_id,
                    kind="TABLE",
                    bbox=(63, 180, 546, 320),
                    source_order=20,
                    table_id="table-1",
                ),
            ),
        )
        evidence = InMemoryGeometryEvidence((page,))

        regions = evidence.regions_for_content(
            content_blocks=(
                {
                    "kind": "text",
                    "source_id": prose_id,
                    "page_start": 1,
                },
                {
                    "kind": "text",
                    "source_id": "caption-1",
                    "page_start": 1,
                    "location": {"bbox": [80, 150, 260, 166]},
                },
                {
                    "kind": "table",
                    "source_id": table_id,
                    "page_start": 1,
                },
            ),
            source_refs=(
                {"source_id": prose_id, "page_number": 1},
                {"source_id": table_id, "page_number": 1},
            ),
        )

        self.assertEqual(
            [
                (region.source_id, region.role, region.precision)
                for region in regions
            ],
            [
                (prose_id, "CLAUSE_TEXT", "EXACT_LINE"),
                (prose_id, "CLAUSE_TEXT", "EXACT_LINE"),
                ("caption-1", "TABLE_CAPTION", "EXACT_BLOCK"),
                (table_id, "TABLE", "EXACT_BLOCK"),
            ],
        )

    def test_structure_container_focus_unions_source_subtree_in_order(self) -> None:
        heading_id = "source-node-" + "1" * 32
        own_text_id = "source-node-" + "2" * 32
        child_text_id = "source-node-" + "3" * 32
        table_id = "source-node-" + "4" * 32
        figure_id = "source-node-" + "5" * 32
        table_cell_id = "source-node-" + "6" * 32
        page = create_pdf_page_geometry_index(
            source_identity=_source_identity(),
            page_number=1,
            page_width=595,
            page_height=842,
            rotation_cw_degrees=0,
            text_lines=(
                *tuple(
                    PdfPageGeometryLineV1(
                        source_node_id=source_id,
                        source_text_length=12,
                        start_offset=0,
                        end_offset=12,
                        bbox=(70, y, 320, y + 14),
                        source_order=order,
                    )
                    for source_id, y, order in (
                        (heading_id, 80, 10),
                        (own_text_id, 110, 20),
                        (child_text_id, 140, 30),
                    )
                ),
                PdfPageGeometryLineV1(
                    source_node_id=table_cell_id,
                    source_text_length=12,
                    start_offset=0,
                    end_offset=12,
                    bbox=(80, 200, 300, 214),
                    source_order=41,
                ),
            ),
            tables=(
                PdfPageGeometryObjectV1(
                    source_node_id=table_id,
                    kind="TABLE",
                    bbox=(63, 180, 546, 320),
                    source_order=40,
                    table_id="table-1",
                ),
            ),
            figures=(
                PdfPageGeometryObjectV1(
                    source_node_id=figure_id,
                    kind="FIGURE",
                    bbox=(90, 350, 300, 500),
                    source_order=50,
                    asset_ref="figure-1",
                ),
            ),
        )
        nodes = (
            RequirementTreeNodeV1(
                id="section-1",
                parent_id=None,
                kind="SECTION",
                depth=0,
                source_order=10,
                label="1.3 职责与分工",
                child_ids=("direct-1", "clause-1", "table-1", "figure-1"),
                source_refs=(
                    {"source_id": heading_id, "page_start": 1},
                ),
                content_blocks=(
                    {
                        "kind": "table",
                        "source_id": table_id,
                        "page_start": 1,
                    },
                    {
                        "kind": "text",
                        "source_id": table_cell_id,
                        "page_start": 1,
                        "source_refs": (
                            {
                                "kind": "table_cell",
                                "source_id": table_cell_id,
                                "page_number": 1,
                            },
                        ),
                    },
                ),
            ),
            RequirementTreeNodeV1(
                id="direct-1",
                parent_id="section-1",
                kind="DIRECT_CONTENT",
                depth=1,
                source_order=20,
                label="直属正文",
                structure_node_id="section-1",
                projection_role="DIRECT_CONTENT",
                source_refs=(
                    {"source_id": own_text_id, "page_number": 1},
                ),
                content_blocks=(
                    {
                        "kind": "text",
                        "source_id": own_text_id,
                        "page_start": 1,
                    },
                ),
            ),
            RequirementTreeNodeV1(
                id="clause-1",
                parent_id="section-1",
                kind="CLAUSE",
                depth=1,
                source_order=30,
                label="a) 子条款",
                structure_node_id="clause-1",
                projection_role="STRUCTURE_PROXY",
                source_refs=(
                    {"source_id": child_text_id, "page_number": 1},
                ),
                content_blocks=(
                    {
                        "kind": "text",
                        "source_id": child_text_id,
                        "page_start": 1,
                    },
                ),
            ),
            RequirementTreeNodeV1(
                id="table-1",
                parent_id="section-1",
                kind="TABLE",
                depth=1,
                source_order=40,
                label="职责表",
                structure_node_id="table-source-1",
                projection_role="TABLE_CONTAINER",
                source_refs=(
                    {"source_id": table_id, "page_number": 1},
                ),
                content_blocks=(
                    {
                        "kind": "table",
                        "source_id": table_id,
                        "page_start": 1,
                    },
                ),
            ),
            RequirementTreeNodeV1(
                id="figure-1",
                parent_id="section-1",
                kind="FIGURE",
                depth=1,
                source_order=50,
                label="职责图",
                structure_node_id="figure-source-1",
                projection_role="STRUCTURE",
                source_refs=(
                    {"source_id": figure_id, "page_number": 1},
                ),
                content_blocks=(
                    {
                        "kind": "figure",
                        "source_id": figure_id,
                        "page_start": 1,
                    },
                ),
            ),
        )
        node_by_id = {node.id: node for node in nodes}

        focus_nodes, scope = _geometry_focus_scope_nodes(
            selected=nodes[0],
            node_by_id=node_by_id,
        )
        regions = _focus_regions_for_nodes(
            evidence=InMemoryGeometryEvidence((page,)),
            nodes=focus_nodes,
        )
        leaf_nodes, leaf_scope = _geometry_focus_scope_nodes(
            selected=nodes[2],
            node_by_id=node_by_id,
        )

        self.assertEqual(scope, "SUBTREE")
        self.assertEqual(
            [node.id for node in focus_nodes],
            ["section-1", "direct-1", "clause-1", "table-1", "figure-1"],
        )
        self.assertEqual(
            [(region.source_id, region.role) for region in regions],
            [
                (heading_id, "CLAUSE_TEXT"),
                (own_text_id, "CLAUSE_TEXT"),
                (child_text_id, "CLAUSE_TEXT"),
                (table_id, "TABLE"),
                (figure_id, "FIGURE"),
            ],
        )
        self.assertEqual(leaf_scope, "OWN")
        self.assertEqual(leaf_nodes, (nodes[2],))

    def test_direct_content_highlights_only_its_explicit_figure_refs(self) -> None:
        first_text_id = "source-node-" + "a" * 32
        second_text_id = "source-node-" + "b" * 32
        figure_id = "source-node-" + "c" * 32
        page = create_pdf_page_geometry_index(
            source_identity=_source_identity(),
            page_number=20,
            page_width=595,
            page_height=842,
            rotation_cw_degrees=0,
            text_lines=(
                PdfPageGeometryLineV1(
                    source_node_id=first_text_id,
                    source_text_length=12,
                    start_offset=0,
                    end_offset=12,
                    bbox=(70, 120, 420, 136),
                    source_order=10,
                ),
                PdfPageGeometryLineV1(
                    source_node_id=second_text_id,
                    source_text_length=12,
                    start_offset=0,
                    end_offset=12,
                    bbox=(70, 420, 420, 436),
                    source_order=30,
                ),
            ),
            figures=(
                PdfPageGeometryObjectV1(
                    source_node_id=figure_id,
                    kind="FIGURE",
                    bbox=(110, 170, 485, 380),
                    source_order=20,
                    asset_ref="asset-shared-figure",
                ),
            ),
        )
        first = RequirementTreeNodeV1(
            id="direct-content-first",
            parent_id="section-shared-figure",
            kind="DIRECT_CONTENT",
            depth=1,
            source_order=10,
            label="正文 A",
            structure_node_id="section-shared-figure",
            projection_role="DIRECT_CONTENT",
            source_refs=({"source_id": first_text_id, "page_number": 20},),
            content_blocks=(
                {
                    "kind": "text",
                    "source_id": first_text_id,
                    "page_start": 20,
                    "start_offset": 0,
                    "end_offset": 12,
                },
            ),
            figure_refs=(
                {
                    "kind": "image",
                    "source_id": figure_id,
                    "page_start": 20,
                },
            ),
        )
        second = RequirementTreeNodeV1(
            id="direct-content-second",
            parent_id="section-shared-figure",
            kind="DIRECT_CONTENT",
            depth=1,
            source_order=30,
            label="正文 B",
            structure_node_id="section-shared-figure",
            projection_role="DIRECT_CONTENT",
            source_refs=({"source_id": second_text_id, "page_number": 20},),
            content_blocks=(
                {
                    "kind": "text",
                    "source_id": second_text_id,
                    "page_start": 20,
                    "start_offset": 0,
                    "end_offset": 12,
                },
            ),
        )
        evidence = InMemoryGeometryEvidence((page,))

        first_regions = _focus_regions_for_nodes(evidence=evidence, nodes=(first,))
        second_regions = _focus_regions_for_nodes(evidence=evidence, nodes=(second,))

        self.assertEqual(
            [
                (region.source_id, region.role, region.precision)
                for region in first_regions
            ],
            [
                (first_text_id, "CLAUSE_TEXT", "EXACT_LINE"),
                (figure_id, "FIGURE", "EXACT_BLOCK"),
            ],
        )
        self.assertEqual(
            [
                (region.source_id, region.role, region.precision)
                for region in second_regions
            ],
            [(second_text_id, "CLAUSE_TEXT", "EXACT_LINE")],
        )
        self.assertEqual(
            tuple(ref["source_id"] for ref in first.source_refs),
            (first_text_id,),
        )

    def test_structure_proxy_focus_keeps_independent_title_and_body_exact_set(
        self,
    ) -> None:
        title_id = "source-node-" + "7" * 32
        body_id = "source-node-" + "8" * 32
        page = create_pdf_page_geometry_index(
            source_identity=_source_identity(),
            page_number=9,
            page_width=595,
            page_height=842,
            rotation_cw_degrees=0,
            text_lines=(
                PdfPageGeometryLineV1(
                    source_node_id=title_id,
                    source_text_length=8,
                    start_offset=0,
                    end_offset=8,
                    bbox=(70, 307, 149, 319),
                    source_order=10,
                ),
                PdfPageGeometryLineV1(
                    source_node_id=body_id,
                    source_text_length=18,
                    start_offset=0,
                    end_offset=18,
                    bbox=(92, 327, 539, 338),
                    source_order=20,
                ),
            ),
        )
        proxy = RequirementTreeNodeV1(
            id="section-1-2-1",
            parent_id=None,
            kind="SECTION",
            depth=0,
            source_order=10,
            label="1.2.1 基本原则",
            child_ids=(),
            structure_node_id="section-1-2-1",
            projection_role="STRUCTURE_PROXY",
            source_refs=(
                {
                    "kind": "SOURCE_NODE",
                    "source_id": title_id,
                    "page_number": 9,
                },
                {
                    "kind": "SOURCE_NODE",
                    "source_id": title_id,
                    "page_number": 9,
                    "start_offset": 0,
                    "end_offset": 8,
                },
                {
                    "kind": "SOURCE_NODE",
                    "source_id": body_id,
                    "page_number": 9,
                },
            ),
            content_blocks=(
                {
                    "kind": "text",
                    "source_id": body_id,
                    "page_start": 9,
                    "text": "供应商应包含全部必要活动。",
                },
            ),
        )
        focus_nodes, scope = _geometry_focus_scope_nodes(
            selected=proxy,
            node_by_id={proxy.id: proxy},
        )
        regions = _focus_regions_for_nodes(
            evidence=InMemoryGeometryEvidence((page,)),
            nodes=focus_nodes,
        )

        self.assertEqual(scope, "OWN")
        self.assertEqual(
            [(region.source_id, region.role, region.precision) for region in regions],
            [
                (title_id, "CLAUSE_TEXT", "EXACT_LINE"),
                (body_id, "CLAUSE_TEXT", "EXACT_LINE"),
            ],
        )

    def test_source_ir_lines_are_projected_with_offsets_and_reading_order(
        self,
    ) -> None:
        document_id = stable_document_id(SOURCE_HASH)
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=0,
            bbox=(70, 80, 320, 112),
        )
        source_id = stable_node_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=SourceNodeKind.PARAGRAPH,
            native_locator=locator,
        )
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
            nodes=(
                SourceNode(
                    source_node_id=source_id,
                    kind=SourceNodeKind.PARAGRAPH,
                    source_order=7,
                    raw_text="first line\nsecond line",
                    native_locator=locator,
                    pdf_text_lines=(
                        PdfTextLineGeometry(
                            start_offset=0,
                            end_offset=10,
                            bbox=(70, 80, 200, 94),
                        ),
                        PdfTextLineGeometry(
                            start_offset=11,
                            end_offset=22,
                            bbox=(70, 98, 220, 112),
                        ),
                    ),
                ),
            ),
            created_at=datetime.now(timezone.utc),
        )
        preview = build_pdf_page_geometry_preview(
            source_ir,
            page_geometries=(
                PdfVisiblePageGeometry(
                    page_number=1,
                    bounds=(0, 0, 595, 842),
                    rotation_cw_degrees=0,
                ),
            ),
        )
        page = PdfPageGeometryIndexV1.model_validate(preview["pages"]["1"])
        self.assertEqual(
            [
                (
                    line.source_node_id,
                    line.start_offset,
                    line.end_offset,
                    line.source_order,
                )
                for line in page.text_lines
            ],
            [
                (source_id, 0, 10, 7_000_000),
                (source_id, 11, 22, 7_000_001),
            ],
        )

    def test_persisted_adapter_verifies_manifest_and_page_bytes(self) -> None:
        page = _page()
        rebuilt = create_pdf_page_geometry_index(
            source_identity=page.source_identity,
            page_number=1,
            page_width=page.page_width,
            page_height=page.page_height,
            rotation_cw_degrees=page.rotation_cw_degrees,
            text_lines=page.text_lines,
            tables=page.tables,
        )
        preview = {
            "schema_version": PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION,
            "source_identity": page.source_identity.model_dump(mode="json"),
            "coordinate_space": PDF_VISIBLE_COORDINATE_SPACE,
            "page_count": 1,
            "pages": {"1": rebuilt.model_dump(mode="json")},
        }

        with tempfile.TemporaryDirectory() as temporary:
            previous_container = RUNTIME_STATE.container
            RUNTIME_STATE.container = SimpleNamespace(
                object_store=LocalObjectStore(Path(temporary))
            )
            try:
                with patch(
                    "app.application.ingestion.db.register_stored_object"
                ):
                    descriptor = persist_pdf_page_geometry_preview(
                        preview,
                        project_id="project-1",
                        document_id="document-1",
                    )
                    second_descriptor = persist_pdf_page_geometry_preview(
                        preview,
                        project_id="project-2",
                        document_id="document-2",
                    )
                assert descriptor is not None
                assert second_descriptor is not None
                validated = validated_pdf_page_geometry_descriptor(
                    descriptor,
                    project_id="project-1",
                    document_id="document-1",
                    expected_source_sha256=SOURCE_HASH,
                )
                assert validated is not None
                loaded = PersistedGeometryEvidence(validated).load_page(1)
                first_page = descriptor["pages"]["1"]
                second_page = second_descriptor["pages"]["1"]
                self.assertEqual(
                    first_page["storage_key"],
                    second_page["storage_key"],
                )
                self.assertNotEqual(
                    descriptor["manifest"]["storage_key"],
                    second_descriptor["manifest"]["storage_key"],
                )
                self.assertIsNotNone(
                    RUNTIME_STATE.container.object_store.stat(
                        descriptor["manifest"]["storage_key"]
                    )
                )
                self.assertIsNotNone(
                    RUNTIME_STATE.container.object_store.stat(
                        second_descriptor["manifest"]["storage_key"]
                    )
                )
                RUNTIME_STATE.container = SimpleNamespace(
                    object_store=LocalObjectStore(Path(temporary))
                )
                restarted_descriptor = (
                    load_pdf_page_geometry_descriptor_by_identity(
                        project_id="project-1",
                        document_id="document-1",
                        source_sha256=SOURCE_HASH,
                        geometry_index_id=descriptor["geometry_index_id"],
                        geometry_index_sha256=descriptor[
                            "geometry_index_sha256"
                        ],
                    )
                )
                assert restarted_descriptor is not None
                restarted = PersistedGeometryEvidence(
                    restarted_descriptor
                ).load_page(1)
            finally:
                RUNTIME_STATE.container = previous_container

        self.assertEqual(loaded, rebuilt)
        self.assertEqual(restarted, rebuilt)

    def test_page_only_refs_resolve_text_figure_cross_page_and_reading_order(
        self,
    ) -> None:
        identity = _source_identity()
        first_id = "source-node-" + "1" * 32
        second_id = "source-node-" + "2" * 32
        figure_id = "source-node-" + "3" * 32
        first = create_pdf_page_geometry_index(
            source_identity=identity,
            page_number=1,
            page_width=842,
            page_height=595,
            rotation_cw_degrees=90,
            text_lines=(
                PdfPageGeometryLineV1(
                    source_node_id=first_id,
                    source_text_length=20,
                    start_offset=0,
                    end_offset=10,
                    bbox=(430, 80, 780, 96),
                    source_order=10,
                ),
                PdfPageGeometryLineV1(
                    source_node_id=first_id,
                    source_text_length=20,
                    start_offset=10,
                    end_offset=20,
                    bbox=(60, 110, 390, 126),
                    source_order=11,
                ),
            ),
            figures=(
                PdfPageGeometryObjectV1(
                    source_node_id=figure_id,
                    kind="FIGURE",
                    bbox=(60, 180, 360, 360),
                    source_order=12,
                    asset_ref="figure-1",
                ),
            ),
        )
        second = create_pdf_page_geometry_index(
            source_identity=identity,
            page_number=2,
            page_width=595,
            page_height=842,
            rotation_cw_degrees=0,
            text_lines=(
                PdfPageGeometryLineV1(
                    source_node_id=second_id,
                    source_text_length=12,
                    start_offset=0,
                    end_offset=12,
                    bbox=(70, 80, 320, 96),
                    source_order=20,
                ),
            ),
        )
        evidence = InMemoryGeometryEvidence((first, second))
        fallback = evidence.regions_for_content(
            content_blocks=(),
            source_refs=(
                {"source_id": first_id, "page_number": 1},
                {"source_id": figure_id, "page_number": 1},
            ),
        )
        cross_page = evidence.regions_for_content(
            content_blocks=(
                {
                    "kind": "text",
                    "source_id": first_id,
                    "page_start": 1,
                    "start_offset": 10,
                    "end_offset": 20,
                },
                {
                    "kind": "figure",
                    "source_id": figure_id,
                    "page_start": 1,
                },
                {
                    "kind": "text",
                    "source_id": second_id,
                    "page_start": 2,
                    "start_offset": 0,
                    "end_offset": 12,
                },
            ),
            source_refs=(
                {"source_id": first_id, "page_number": 1},
                {"source_id": figure_id, "page_number": 1},
                {"source_id": second_id, "page_number": 2},
            ),
        )

        self.assertEqual(first.rotation_cw_degrees, 90)
        self.assertEqual(
            [(region.role, region.precision) for region in fallback],
            [
                ("CLAUSE_TEXT", "EXACT_LINE"),
                ("CLAUSE_TEXT", "EXACT_LINE"),
                ("FIGURE", "EXACT_BLOCK"),
            ],
        )
        self.assertEqual(
            [
                (region.page_number, region.role, region.precision)
                for region in cross_page
            ],
            [
                (1, "CLAUSE_TEXT", "EXACT_LINE"),
                (1, "FIGURE", "EXACT_BLOCK"),
                (2, "CLAUSE_TEXT", "EXACT_LINE"),
            ],
        )
        self.assertEqual(
            [
                item["source_order"]
                for item in evidence.reading_evidence_between(
                    page_number=1,
                    start_source_order=10,
                    end_source_order=12,
                )
            ],
            [10, 11, 12],
        )

    def test_focus_endpoint_loads_only_the_selected_verified_node(self) -> None:
        page = create_pdf_page_geometry_index(
            source_identity=_source_identity(),
            page_number=1,
            page_width=595,
            page_height=842,
            rotation_cw_degrees=0,
            text_lines=(
                PdfPageGeometryLineV1(
                    source_node_id="source-node-" + "1" * 32,
                    source_text_length=20,
                    start_offset=0,
                    end_offset=20,
                    bbox=(70, 80, 300, 94),
                    source_order=1,
                ),
            ),
        )
        preview = {
            "schema_version": PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION,
            "source_identity": page.source_identity.model_dump(mode="json"),
            "coordinate_space": PDF_VISIBLE_COORDINATE_SPACE,
            "page_count": 1,
            "pages": {"1": page.model_dump(mode="json")},
        }
        with tempfile.TemporaryDirectory() as temporary:
            previous_container = RUNTIME_STATE.container
            RUNTIME_STATE.container = SimpleNamespace(
                object_store=LocalObjectStore(Path(temporary))
            )
            try:
                with patch(
                    "app.application.ingestion.db.register_stored_object"
                ):
                    descriptor = persist_pdf_page_geometry_preview(
                        preview,
                        project_id="project-1",
                        document_id="document-1",
                    )
                assert descriptor is not None
                tree = RequirementTreeSnapshotV1(
                    source_identity=RequirementTreeSourceIdentity(
                        project_id="project-1",
                        document_id="document-1",
                        source_ir_id=page.source_identity.source_ir_id,
                        source_sha256=SOURCE_HASH,
                        geometry_index_id=descriptor["geometry_index_id"],
                        geometry_index_sha256=descriptor[
                            "geometry_index_sha256"
                        ],
                    ),
                    structure_identity=RequirementTreeStructureIdentity(
                        effective_snapshot_id="effective-1",
                        effective_tree_sha256="b" * 64,
                    ),
                    mode=RequirementTreeMode.WORKING,
                    state=RequirementTreeState.READY,
                    root_node_ids=("clause-1",),
                    nodes=(
                        RequirementTreeNodeV1(
                            id="clause-1",
                            parent_id=None,
                            kind="CLAUSE",
                            depth=0,
                            source_order=1,
                            label="4.1.4 试验电压",
                            page_start=1,
                            page_end=1,
                            source_refs=(
                                {
                                    "source_id": page.text_lines[0].source_node_id,
                                    "page_number": 1,
                                    "bbox": [70, 80, 300, 94],
                                },
                            ),
                            content_blocks=(
                                {
                                    "kind": "text",
                                    "source_id": page.text_lines[0].source_node_id,
                                    "page_start": 1,
                                    "start_offset": 0,
                                    "end_offset": 20,
                                },
                            ),
                        ),
                    ),
                )
                payload = {
                    "source_sha256": SOURCE_HASH,
                    "source_page_snapshot": {
                        "geometry_index": descriptor,
                    },
                }
                response = SimpleNamespace(headers={})
                with (
                    patch(
                        "app.application.extraction.review_support."
                        "_require_project_document",
                        return_value={
                            "id": "document-1",
                            "project_id": "project-1",
                            "source_sha256": SOURCE_HASH,
                        },
                    ),
                    patch(
                        "app.application.extraction.structure_workspace."
                        "_structure_agent_job_payload",
                        return_value=({"status": "COMPLETED"}, payload, {}),
                    ),
                    patch(
                        "app.application.extraction.structure_workspace."
                        "_serialize_structure_workbench_for_api",
                        return_value={
                            "requirement_tree": tree.model_dump(mode="json")
                        },
                    ) as serialize_workbench,
                ):
                    result = get_document_geometry_focus(
                        "project-1",
                        "document-1",
                        "clause-1",
                        response,
                    )
                    mismatched_tree = tree.model_copy(
                        update={
                            "source_identity": tree.source_identity.model_copy(
                                update={"geometry_index_sha256": "c" * 64}
                            )
                        }
                    )
                    serialize_workbench.return_value = {
                        "requirement_tree": mismatched_tree.model_dump(mode="json")
                    }
                    with self.assertRaises(ApplicationError) as mismatch:
                        get_document_geometry_focus(
                            "project-1",
                            "document-1",
                            "clause-1",
                            response,
                        )
            finally:
                RUNTIME_STATE.container = previous_container

        self.assertEqual(result["resolved_node_id"], "clause-1")
        self.assertEqual(result["focus_scope"], "OWN")
        self.assertEqual(len(result["regions"]), 1)
        self.assertEqual(result["regions"][0]["precision"], "EXACT_LINE")
        self.assertEqual(result["page_numbers"], [1])
        self.assertEqual(mismatch.exception.status_code, 409)
        self.assertEqual(mismatch.exception.code, "geometry_lineage_mismatch")

    def test_requirement_set_focus_loads_its_historical_geometry_after_restart(
        self,
    ) -> None:
        page = create_pdf_page_geometry_index(
            source_identity=_source_identity(),
            page_number=1,
            page_width=595,
            page_height=842,
            rotation_cw_degrees=0,
            text_lines=(
                PdfPageGeometryLineV1(
                    source_node_id="source-node-" + "7" * 32,
                    source_text_length=16,
                    start_offset=0,
                    end_offset=16,
                    bbox=(70, 80, 300, 94),
                    source_order=1,
                ),
            ),
        )
        preview = {
            "schema_version": PDF_PAGE_GEOMETRY_MANIFEST_SCHEMA_VERSION,
            "source_identity": page.source_identity.model_dump(mode="json"),
            "coordinate_space": PDF_VISIBLE_COORDINATE_SPACE,
            "page_count": 1,
            "pages": {"1": page.model_dump(mode="json")},
        }
        with tempfile.TemporaryDirectory() as temporary:
            previous_container = RUNTIME_STATE.container
            RUNTIME_STATE.container = SimpleNamespace(
                object_store=LocalObjectStore(Path(temporary))
            )
            try:
                with patch(
                    "app.application.ingestion.db.register_stored_object"
                ):
                    descriptor = persist_pdf_page_geometry_preview(
                        preview,
                        project_id="project-1",
                        document_id="document-1",
                    )
                assert descriptor is not None
                tree = RequirementTreeSnapshotV1(
                    source_identity=RequirementTreeSourceIdentity(
                        project_id="project-1",
                        document_id="document-1",
                        source_ir_id=page.source_identity.source_ir_id,
                        source_sha256=SOURCE_HASH,
                        requirement_set_id="set-1",
                        geometry_index_id=descriptor["geometry_index_id"],
                        geometry_index_sha256=descriptor[
                            "geometry_index_sha256"
                        ],
                    ),
                    structure_identity=RequirementTreeStructureIdentity(
                        effective_snapshot_id="effective-1",
                        effective_tree_sha256="b" * 64,
                    ),
                    mode=RequirementTreeMode.REQUIREMENT_SET,
                    state=RequirementTreeState.READY,
                    root_node_ids=("clause-1",),
                    nodes=(
                        RequirementTreeNodeV1(
                            id="clause-1",
                            parent_id=None,
                            kind="CLAUSE",
                            depth=0,
                            source_order=1,
                            label="历史条款",
                            page_start=1,
                            page_end=1,
                            source_refs=(
                                {
                                    "source_id": page.text_lines[
                                        0
                                    ].source_node_id,
                                    "page_number": 1,
                                },
                            ),
                        ),
                    ),
                )
                RUNTIME_STATE.container = SimpleNamespace(
                    object_store=LocalObjectStore(Path(temporary))
                )
                response = SimpleNamespace(headers={})
                with (
                    patch(
                        "app.application.extraction.review_support."
                        "_require_project_document",
                        return_value={
                            "id": "document-1",
                            "project_id": "project-1",
                            "source_sha256": SOURCE_HASH,
                        },
                    ),
                    patch(
                        "app.application.extraction.geometry_evidence."
                        "db.get_requirement_set",
                        return_value={
                            "id": "set-1",
                            "project_id": "project-1",
                            "sor_document_id": "document-1",
                        },
                    ),
                    patch(
                        "app.application.extraction.requirement_tree_snapshots."
                        "load_requirement_set_tree_snapshot",
                        return_value=tree,
                    ),
                    patch(
                        "app.application.extraction.structure_workspace."
                        "_structure_agent_job_payload"
                    ) as current_extraction,
                ):
                    result = get_document_geometry_focus(
                        "project-1",
                        "document-1",
                        "clause-1",
                        response,
                        requirement_set_id="set-1",
                    )
            finally:
                RUNTIME_STATE.container = previous_container

        current_extraction.assert_not_called()
        self.assertEqual(result["focus_scope"], "OWN")
        self.assertEqual(result["page_numbers"], [1])
        self.assertEqual(result["regions"][0]["precision"], "EXACT_LINE")


if __name__ == "__main__":
    unittest.main()
