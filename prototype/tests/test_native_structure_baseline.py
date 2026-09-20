from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
from types import SimpleNamespace
import unittest

from app.application.agent_workflow.native_structure_baseline import (
    NativeStructureBaselineError,
    NativeStructureBaselineArtifactV1,
    build_native_structure_baseline_artifact,
    compile_native_structure_accepted_baseline,
    native_structure_baseline_artifact_bytes,
    native_structure_baseline_object_key,
    _table_record_row_windows,
)
from app.application.agent_workflow.extraction_runtime import AgentKitIdentityV1
from app.domain.extraction.source_ir import SourceIRSnapshot, stable_node_id

if __package__:
    from .test_d2_logical_structure_v2 import _source_fixture
else:
    from test_d2_logical_structure_v2 import _source_fixture


class NativeStructureAcceptedBaselineTests(unittest.TestCase):
    def test_table_record_windows_never_cut_a_row_spanning_cell(self) -> None:
        cells = tuple(
            SimpleNamespace(row=row, row_span=(3 if row == 7 else 1))
            for row in range(20)
        )
        windows = _table_record_row_windows(
            cells=cells,
            start_row=0,
            end_row=20,
            max_rows=4,
            max_cells=4,
        )

        boundaries = {value for window in windows for value in window}
        self.assertFalse({8, 9}.intersection(boundaries))
        self.assertEqual(
            tuple(range(20)),
            tuple(row for start, end in windows for row in range(start, end)),
        )

    def setUp(self) -> None:
        self.source = _source_fixture()
        self.kit = AgentKitIdentityV1(
            kit_id="reqpilotx-document-extraction",
            kit_version="0.8.9",
            manifest_sha256="1" * 64,
            bundle_sha256="2" * 64,
            policy_id="reqpilotx-extraction-policy",
            policy_version="1.0.0",
            tool_registry_id="reqpilotx-document-evidence-tools",
            tool_registry_version="1.3.0",
        )
        self.reference, self.previews = _native_reference(self.source)

    def compile(self, **updates):  # type: ignore[no-untyped-def]
        values = {
            "project_id": "project-native-baseline",
            "document_id": "document-native-baseline",
            "source_ir": self.source,
            "public_reference": self.reference,
            "table_previews": self.previews,
            "kit_identity": self.kit,
        }
        values.update(updates)
        return compile_native_structure_accepted_baseline(**values)

    def test_compiles_stable_region_aware_d3_input(self) -> None:
        first = self.compile()
        second = self.compile()

        self.assertEqual(first.identity, second.identity)
        self.assertEqual(first.identity.status, "ACCEPTED")
        self.assertEqual(first.identity.physical_table_count, 2)
        self.assertEqual(first.identity.table_region_count, 3)
        self.assertEqual(first.identity.logical_object_count, 4)
        self.assertEqual(first.identity.exact_source_segment_count, 13)
        self.assertEqual(first.v2_compilation.proposal.status.value, "COMPLETE")
        self.assertEqual(first.d3_stage_input.segment_set.status.value, "COMPLETE")
        self.assertEqual(
            tuple(
                item.candidate_scope_key
                for item in first.table_region_artifact.logical_scopes
            ),
            (
                "native.scope.lt-first-a",
                "native.scope.lt-first-b",
                "native.scope.lt-second",
            ),
        )
        table_segments = tuple(
            item
            for item in first.d3_stage_input.segment_set.segments
            if item.evidence_kind.value == "TABLE_CELL"
        )
        self.assertEqual(len(table_segments), 8)
        self.assertEqual(
            {item.source_text for item in table_segments},
            {
                "table-1-cell-0",
                "table-1-cell-1",
                "table-1-cell-2",
                "table-1-cell-3",
                "table-2-cell-0",
                "table-2-cell-1",
                "table-2-cell-2",
                "table-2-cell-3",
            },
        )
        artifact = build_native_structure_baseline_artifact(first)
        encoded = native_structure_baseline_artifact_bytes(artifact)
        self.assertEqual(
            NativeStructureBaselineArtifactV1.model_validate_json(encoded),
            artifact,
        )
        self.assertTrue(
            native_structure_baseline_object_key(first.identity).endswith(
                f"/{first.identity.baseline_sha256}.json"
            )
        )

        with self.assertRaisesRegex(ValueError, "artifact SHA drifted"):
            NativeStructureBaselineArtifactV1.model_validate(
                artifact.model_dump(mode="json") | {"artifact_sha256": "f" * 64}
            )

    def test_source_and_advancement_drift_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            NativeStructureBaselineError,
            "NATIVE_BASELINE_NOT_ADVANCEMENT_READY",
        ):
            self.compile(
                public_reference={
                    **self.reference,
                    "source_sha256": "f" * 64,
                }
            )

        continuation = dict(self.reference["continuation"])
        continuation["advancement_allowed"] = False
        with self.assertRaisesRegex(
            NativeStructureBaselineError,
            "NATIVE_BASELINE_NOT_ADVANCEMENT_READY",
        ):
            self.compile(
                public_reference={
                    **self.reference,
                    "continuation": continuation,
                }
            )

    def test_table_cell_identity_drift_is_rejected(self) -> None:
        first = dict(self.previews[0])
        cells = [dict(item) for item in first["cells"]]
        cells[0]["cell_id"] = "cell-" + "f" * 32
        first["cells"] = cells
        with self.assertRaisesRegex(
            NativeStructureBaselineError,
            "NATIVE_BASELINE_TABLE_CELL_DRIFT",
        ):
            self.compile(table_previews=(first, *self.previews[1:]))

    def test_visible_non_table_text_cannot_disappear_from_native_tree(self) -> None:
        original = self.source.nodes[0]
        locator = original.native_locator.model_copy(update={"block_index": 99})
        extra = original.model_copy(
            update={
                "source_node_id": stable_node_id(
                    source_sha256=self.source.source_sha256,
                    adapter_id=self.source.adapter_id,
                    adapter_version=self.source.adapter_version,
                    kind=original.kind,
                    native_locator=locator,
                ),
                "source_order": 99,
                "raw_text": "This text is visible but not owned.",
                "native_locator": locator,
            }
        )
        source = self.source.model_copy(update={"nodes": (*self.source.nodes, extra)})
        with self.assertRaisesRegex(
            NativeStructureBaselineError,
            "NATIVE_BASELINE_VISIBLE_TEXT_NOT_OWNED",
        ):
            self.compile(source_ir=source)


def _native_reference(
    source: SourceIRSnapshot,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    text = source.nodes[0]
    tables = tuple(item for item in source.nodes if item.table_payload is not None)
    first_table = tables[0].table_payload
    second_table = tables[1].table_payload
    assert first_table is not None and second_table is not None
    root_id = "native-root"
    first_node_id = "native-first-table"
    second_node_id = "native-second-table"
    nodes = [
        {
            "node_id": root_id,
            "parent_id": None,
            "order": 0,
            "kind": "DOCUMENT",
            "label": "",
            "title": "Synthetic source",
            "title_authority": "VISIBLE_SOURCE",
            "pages": [1, 2, 3],
            "content_blocks": [],
            "finding_codes": [],
            "source_anchors": [],
            "source_content": [
                {
                    "content_id": "native-content-root",
                    "page": 1,
                    "bbox": [10.0, 10.0, 200.0, 20.0],
                    "source_bounds": [0.0, 0.0, 300.0, 300.0],
                    "source_handles": [text.source_node_id],
                    "text": text.raw_text,
                    "text_sha256": hashlib.sha256(
                        text.raw_text.encode("utf-8")
                    ).hexdigest(),
                    "precision": "EXACT_SOURCE_RANGE",
                    "is_title": True,
                }
            ],
        },
        {
            "node_id": first_node_id,
            "parent_id": root_id,
            "order": 1,
            "kind": "SECTION",
            "label": "1",
            "title": "First table",
            "title_authority": "VISIBLE_SOURCE",
            "pages": [2],
            "content_blocks": [
                {
                    "kind": "LOGICAL_TABLE",
                    "logical_table_scope_id": "lt-first-a",
                },
                {
                    "kind": "LOGICAL_TABLE",
                    "logical_table_scope_id": "lt-first-b",
                },
            ],
            "finding_codes": [],
            "source_anchors": [],
            "source_content": [],
        },
        {
            "node_id": second_node_id,
            "parent_id": root_id,
            "order": 2,
            "kind": "SECTION",
            "label": "2",
            "title": "Second table",
            "title_authority": "VISIBLE_SOURCE",
            "pages": [3],
            "content_blocks": [
                {
                    "kind": "LOGICAL_TABLE",
                    "logical_table_scope_id": "lt-second",
                }
            ],
            "finding_codes": [],
            "source_anchors": [],
            "source_content": [],
        },
    ]
    physical = (
        {
            "table_token": "T0001",
            "page": 2,
            "regions": [
                {
                    "row_range": [0, 1],
                    "kind": "LOGICAL_TABLE",
                    "scope_id": "lt-first-a",
                },
                {
                    "row_range": [1, 2],
                    "kind": "LOGICAL_TABLE",
                    "scope_id": "lt-first-b",
                },
            ],
        },
        {
            "table_token": "T0002",
            "page": 3,
            "regions": [
                {
                    "row_range": [0, 2],
                    "kind": "LOGICAL_TABLE",
                    "scope_id": "lt-second",
                }
            ],
        },
    )
    logical_tables = (
        {
            "scope_id": "lt-first-a",
            "title": "First A",
            "owner_unit_id": "unit-main",
            "role": "REQUIREMENT_TABLE",
            "pages": [2],
            "fragments": [{"table_token": "T0001", "page": 2, "row_range": [0, 1]}],
        },
        {
            "scope_id": "lt-first-b",
            "title": "First B",
            "owner_unit_id": "unit-main",
            "role": "REQUIREMENT_TABLE",
            "pages": [2],
            "fragments": [{"table_token": "T0001", "page": 2, "row_range": [1, 2]}],
        },
        {
            "scope_id": "lt-second",
            "title": "Second",
            "owner_unit_id": "unit-main",
            "role": "REQUIREMENT_TABLE",
            "pages": [3],
            "fragments": [{"table_token": "T0002", "page": 3, "row_range": [0, 2]}],
        },
    )
    previews = tuple(
        _preview(
            scope_id=scope_id,
            token=token,
            page=page,
            table=table,
            start=start,
            end=end,
        )
        for scope_id, token, page, table, start, end in (
            ("lt-first-a", "T0001", 2, first_table, 0, 1),
            ("lt-first-b", "T0001", 2, first_table, 1, 2),
            ("lt-second", "T0002", 3, second_table, 0, 2),
        )
    )
    reference = {
        "schema_version": "native-structure-reference-public/v1",
        "availability": "READY",
        "artifact_id": "synthetic-native-reference-v1",
        "source_sha256": source.source_sha256,
        "artifact_status": "COMPLETE",
        "coverage_status": "PASS",
        "boundary_status": "PASS",
        "workflow_step": "DOCUMENT_STRUCTURE_RECOVERY",
        "document_tree": {
            "schema_version": "native-document-structure-tree/v1",
            "root_ids": [root_id],
            "nodes": nodes,
        },
        "logical_tables": list(logical_tables),
        "physical_table_dispositions": list(physical),
        "nested_physical_tables": [],
        "nested_visual_tables": [],
        "structure_proposals": [],
        "validation": {"status": "PASS", "checks": {}},
        "visual_summary": {"page_count": 3, "opened_page_count": 3},
        "continuation": {
            "status": "IDLE",
            "can_continue": False,
            "advancement_allowed": True,
            "evidence_state": "SUFFICIENT",
            "default_resolution_policy": "NONE",
            "latest_handoff": None,
        },
    }
    return reference, previews


def _preview(
    *,
    scope_id: str,
    token: str,
    page: int,
    table,
    start: int,
    end: int,
) -> dict[str, object]:  # type: ignore[no-untyped-def]
    cells = tuple(
        item
        for item in sorted(
            table.cells,
            key=lambda cell: (cell.row, cell.column, cell.cell_id),
        )
        if start <= item.row < end
    )
    return {
        "fragment_id": f"preview-{scope_id}",
        "logical_table_scope_id": scope_id,
        "table_token": token,
        "page": page,
        "row_range": [start, end],
        "row_count": end - start,
        "column_count": table.column_count,
        "bbox": [10.0, 10.0, 200.0, 100.0],
        "source_bounds": [0.0, 0.0, 300.0, 300.0],
        "cells": [
            {
                "cell_id": item.cell_id,
                "row": item.row - start,
                "column": item.column,
                "row_span": item.row_span,
                "column_span": item.column_span,
                "text": item.raw_text,
                "bbox": [10.0, 10.0, 20.0, 20.0],
            }
            for item in cells
        ],
    }


if __name__ == "__main__":
    unittest.main()
