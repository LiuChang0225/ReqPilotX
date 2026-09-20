from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from app.application.agent_workflow.d2_table_region_parent_input import (
    D2TableRegionParentInputStatus,
    D2TableRegionParentInputV1,
    build_d2_table_region_parent_input,
)
from app.application.agent_workflow.d2_table_regions import (
    D2EmptyPhysicalTableDispositionCandidateV1,
    D2EmptyPhysicalTableDispositionKind,
    D2LogicalObjectPartTableRegionCandidateV1,
    D2LogicalScopePartTableRegionCandidateV1,
    D2NestedValueTableRegionCandidateV1,
    D2NonBusinessFalseTableRegionCandidateV1,
    D2PhysicalTableCellOrdinalSpanV1,
    D2TableLogicalScopeCandidateV1,
    D2TableRegionCandidateArtifactV1,
    D2TableRegionDispositionKind,
    D2UnresolvedTableRegionCandidateV1,
    compile_d2_table_regions,
    d2_table_region_source_ir_canonical_sha256,
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


SOURCE_SHA = "a" * 64
KIT_SHA = "b" * 64
D1_TABLE_MAP_ID = "agent-table-map-artifact-test"
D1_TABLE_MAP_SHA = "1" * 64


class D2TableRegionParentInputTests(unittest.TestCase):
    def test_two_sibling_scopes_in_one_table_are_exact_and_non_overlapping(
        self,
    ) -> None:
        source = _source_fixture((1,))
        table_id = _table_ids(source)[0]
        artifact = _compile(
            source,
            regions=(
                D2LogicalScopePartTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
                    region_key="scope-a-region",
                    physical_table_id=table_id,
                    cell_spans=(_span(0, 1), _span(2, 3)),
                    logical_scope_key="scope-a",
                    reason_codes=("logical_grid",),
                ),
                D2LogicalScopePartTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
                    region_key="scope-b-region",
                    physical_table_id=table_id,
                    cell_spans=(_span(1, 2), _span(3, 4)),
                    logical_scope_key="scope-b",
                    reason_codes=("logical_grid",),
                ),
            ),
            scopes=(
                _scope("scope-a", ("scope-a-region",)),
                _scope("scope-b", ("scope-b-region",)),
            ),
        )

        compiled = _adapt(source, artifact)
        stage_input = compiled.stage_input

        self.assertEqual(stage_input.status, D2TableRegionParentInputStatus.COMPLETE)
        self.assertEqual(len(stage_input.logical_scopes), 2)
        self.assertEqual(len(stage_input.source_segments), 4)
        self.assertEqual(
            tuple(item.source_position for item in stage_input.source_segments),
            ((0, 0), (0, 1), (0, 2), (0, 3)),
        )
        first_scope_ids = set(stage_input.logical_scopes[0].ordered_segment_ids)
        second_scope_ids = set(stage_input.logical_scopes[1].ordered_segment_ids)
        first_ids = {
            item.cell_ids[0]
            for item in stage_input.source_segments
            if item.segment_id in first_scope_ids
        }
        second_ids = {
            item.cell_ids[0]
            for item in stage_input.source_segments
            if item.segment_id in second_scope_ids
        }
        self.assertFalse(first_ids.intersection(second_ids))
        self.assertEqual(
            stage_input.source_segments[0].source_text,
            "table-1-cell-0",
        )
        self.assertEqual(
            stage_input.source_segments[0].evidence_refs,
            tuple(
                cell.cell_evidence_sha256
                for cell in stage_input.source_segments[0].verbatim_cells
            ),
        )

    def test_one_scope_keeps_region_order_across_physical_pages(self) -> None:
        source = _source_fixture((1, 2))
        first_table, second_table = _table_ids(source)
        artifact = _compile(
            source,
            regions=(
                _scope_region("scope-page-one", first_table, 0, 4, "scope-a"),
                _scope_region("scope-page-two", second_table, 0, 4, "scope-a"),
            ),
            scopes=(_scope("scope-a", ("scope-page-one", "scope-page-two")),),
        )

        stage_input = _adapt(source, artifact).stage_input
        scope = stage_input.logical_scopes[0]
        segments = tuple(
            next(
                item
                for item in stage_input.source_segments
                if item.segment_id == segment_id
            )
            for segment_id in scope.ordered_segment_ids
        )

        self.assertEqual(
            tuple(item.physical_table_id for item in segments),
            (first_table,) * 4 + (second_table,) * 4,
        )
        self.assertEqual(
            tuple(item.page_numbers for item in segments),
            ((1,),) * 4 + ((2,),) * 4,
        )
        self.assertEqual(
            tuple(item.physical_table_source_order for item in segments),
            (0,) * 4 + (1,) * 4,
        )

    def test_adjacent_cell_tokens_remain_separate_row_major_segments(self) -> None:
        source = _source_fixture(
            (1,),
            rows=1,
            columns=2,
            cell_texts=("24", "V"),
        )
        table_id = _table_ids(source)[0]
        artifact = _compile(
            source,
            regions=(_scope_region("electrical", table_id, 0, 2, "scope-a"),),
            scopes=(_scope("scope-a", ("electrical",)),),
        )

        segments = _adapt(source, artifact).stage_input.source_segments

        self.assertEqual(tuple(item.source_text for item in segments), ("24", "V"))
        self.assertEqual(
            tuple(item.source_position for item in segments),
            ((0, 0), (0, 1)),
        )
        self.assertNotIn("24V", tuple(item.source_text for item in segments))

    def test_logical_object_part_becomes_an_ordinary_content_segment(self) -> None:
        source = _source_fixture((1,))
        table_id = _table_ids(source)[0]
        artifact = _compile(
            source,
            regions=(
                D2LogicalObjectPartTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
                    region_key="ordinary-prose",
                    physical_table_id=table_id,
                    cell_spans=(_span(0, 3),),
                    logical_object_key="paragraph-a",
                    reason_codes=("ordinary_content_inside_table_frame",),
                ),
                _false_region("layout-tail", table_id, 3, 4),
            ),
        )

        stage_input = _adapt(source, artifact).stage_input
        segments = stage_input.source_segments[:3]
        logical_object = stage_input.logical_objects[0]

        self.assertTrue(
            all(
                item.disposition is D2TableRegionDispositionKind.LOGICAL_OBJECT_PART
                for item in segments
            )
        )
        self.assertTrue(
            all(item.candidate_logical_object_key == "paragraph-a" for item in segments)
        )
        self.assertTrue(
            all(
                item.logical_object_id == logical_object.logical_object_id
                for item in segments
            )
        )
        self.assertEqual(
            logical_object.ordered_segment_ids,
            tuple(item.segment_id for item in segments),
        )
        self.assertEqual(
            stage_input.non_business_region_evidence[0].cell_ids,
            (artifact.regions[1].cell_refs[0].cell_id,),
        )

    def test_nested_false_and_unresolved_keep_evidence_without_semantic_segments(
        self,
    ) -> None:
        source = _source_fixture((1,), rows=2, columns=3)
        table_id = _table_ids(source)[0]
        artifact = _compile(
            source,
            regions=(
                _scope_region("owner", table_id, 0, 1, "scope-a"),
                D2NestedValueTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.NESTED_VALUE_REGION,
                    region_key="nested-value",
                    physical_table_id=table_id,
                    cell_spans=(_span(1, 2),),
                    nested_owner_region_key="owner",
                    reason_codes=("nested_value",),
                ),
                _false_region("layout-only", table_id, 2, 3),
                D2UnresolvedTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.UNRESOLVED,
                    region_key="needs-decision",
                    physical_table_id=table_id,
                    cell_spans=(_span(3, 5),),
                    reason_codes=("ambiguous_region",),
                ),
                D2LogicalObjectPartTableRegionCandidateV1(
                    disposition=D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
                    region_key="prose-tail",
                    physical_table_id=table_id,
                    cell_spans=(_span(5, 6),),
                    logical_object_key="paragraph-a",
                    reason_codes=("ordinary_content_inside_table_frame",),
                ),
            ),
            scopes=(_scope("scope-a", ("owner",)),),
        )

        compiled = _adapt(source, artifact)
        stage_input = compiled.stage_input
        nested = stage_input.nested_region_evidence[0]
        false = stage_input.non_business_region_evidence[0]
        unresolved = stage_input.unresolved_scopes[0]
        owner_segments = tuple(
            item
            for item in stage_input.source_segments
            if item.region_id == nested.nested_owner_region_id
        )

        self.assertEqual(stage_input.status, D2TableRegionParentInputStatus.PARTIAL)
        self.assertEqual(len(stage_input.source_segments), 2)
        self.assertEqual(
            nested.owner_source_segment_ids,
            tuple(item.segment_id for item in owner_segments),
        )
        self.assertEqual(
            stage_input.logical_scopes[0].nested_region_ids,
            (nested.region_id,),
        )
        self.assertEqual(nested.source_text, "table-1-cell-1")
        self.assertEqual(false.source_text, "table-1-cell-2")
        self.assertEqual(
            unresolved.source_text,
            "table-1-cell-3table-1-cell-4",
        )
        semantic_region_ids = {item.region_id for item in stage_input.source_segments}
        self.assertNotIn(nested.region_id, semantic_region_ids)
        self.assertNotIn(false.region_id, semantic_region_ids)
        self.assertNotIn(unresolved.region_id, semantic_region_ids)
        partitioned_cell_ids = tuple(
            cell.cell_id
            for region_id in stage_input.ordered_region_ids
            for cell in _record_by_region(stage_input, region_id).verbatim_cells
        )
        self.assertEqual(
            partitioned_cell_ids,
            stage_input.physical_partitions[0].canonical_cell_ids,
        )

        receipt_json = json.dumps(compiled.receipt.model_dump(mode="json"))
        self.assertTrue(compiled.receipt.content_free)
        self.assertNotIn("table-1-cell", receipt_json)
        self.assertNotIn("raw_text", receipt_json)

    def test_tamper_drift_replay_and_deterministic_rebuild(self) -> None:
        source = _source_fixture((1,))
        table_id = _table_ids(source)[0]
        artifact = _compile(
            source,
            regions=(_scope_region("whole", table_id, 0, 4, "scope-a"),),
            scopes=(_scope("scope-a", ("whole",)),),
        )

        first = _adapt(source, artifact)
        second = _adapt(source, artifact)
        self.assertEqual(
            first.model_dump(mode="json"),
            second.model_dump(mode="json"),
        )

        tampered_payload = first.stage_input.model_dump(mode="json")
        tampered_payload["source_segments"][0]["source_text"] = "tampered"
        with self.assertRaisesRegex(ValidationError, "concatenate exact cells"):
            D2TableRegionParentInputV1.model_validate(tampered_payload)

        tampered_artifact = artifact.model_copy(update={"artifact_sha256": "c" * 64})
        with self.assertRaisesRegex(ValidationError, "artifact SHA"):
            _adapt(source, tampered_artifact)

        source_node = source.nodes[0]
        assert source_node.table_payload is not None
        cells = list(source_node.table_payload.cells)
        cells[0] = cells[0].model_copy(update={"raw_text": "changed"})
        changed_table = source_node.table_payload.model_copy(
            update={"cells": tuple(cells)}
        )
        changed_node = source_node.model_copy(update={"table_payload": changed_table})
        changed_source = source.model_copy(update={"nodes": (changed_node,)})
        with self.assertRaisesRegex(ValueError, "SourceIR identity is stale"):
            _adapt(changed_source, artifact)

        with self.assertRaisesRegex(ValueError, "Agent Kit identity is stale"):
            build_d2_table_region_parent_input(
                source,
                artifact,
                expected_kit_bundle_sha256="d" * 64,
                expected_d1_table_map_artifact_id=D1_TABLE_MAP_ID,
                expected_d1_table_map_artifact_sha256=D1_TABLE_MAP_SHA,
                expected_region_artifact_id=artifact.artifact_id,
                expected_region_artifact_sha256=artifact.artifact_sha256,
            )

        replay_artifact = _compile(
            source,
            regions=(
                _scope_region("first", table_id, 0, 2, "scope-a"),
                _scope_region("second", table_id, 2, 4, "scope-b"),
            ),
            scopes=(
                _scope("scope-a", ("first",)),
                _scope("scope-b", ("second",)),
            ),
        )
        with self.assertRaisesRegex(ValueError, "replayed"):
            build_d2_table_region_parent_input(
                source,
                replay_artifact,
                expected_kit_bundle_sha256=KIT_SHA,
                expected_d1_table_map_artifact_id=D1_TABLE_MAP_ID,
                expected_d1_table_map_artifact_sha256=D1_TABLE_MAP_SHA,
                expected_region_artifact_id=artifact.artifact_id,
                expected_region_artifact_sha256=artifact.artifact_sha256,
            )

    def test_empty_table_and_supplemental_text_are_explicit_not_silently_lost(
        self,
    ) -> None:
        source = _source_fixture(
            (1,),
            rows=1,
            columns=1,
            empty_table=True,
            table_raw_text="rendered table fallback",
            table_notes=("source note",),
        )
        table_id = _table_ids(source)[0]
        artifact = _compile(
            source,
            regions=(),
            empty_table_dispositions=(
                D2EmptyPhysicalTableDispositionCandidateV1(
                    physical_table_id=table_id,
                    disposition=D2EmptyPhysicalTableDispositionKind.UNRESOLVED,
                    reason_codes=("empty_table_requires_resolution",),
                ),
            ),
        )

        compiled = _adapt(source, artifact)
        stage_input = compiled.stage_input

        self.assertEqual(stage_input.status, D2TableRegionParentInputStatus.PARTIAL)
        self.assertFalse(stage_input.source_segments)
        self.assertFalse(stage_input.ordered_region_ids)
        self.assertFalse(stage_input.physical_partitions[0].canonical_cell_ids)
        self.assertEqual(
            stage_input.physical_partitions[0].empty_table_disposition,
            D2EmptyPhysicalTableDispositionKind.UNRESOLVED,
        )
        residual = stage_input.supplemental_table_evidence[0]
        self.assertEqual(residual.table_node_raw_text, "rendered table fallback")
        self.assertEqual(residual.table_notes, ("source note",))
        receipt_json = json.dumps(compiled.receipt.model_dump(mode="json"))
        self.assertNotIn("rendered table fallback", receipt_json)
        self.assertNotIn("source note", receipt_json)

    def test_local_segment_id_survives_unrelated_table_revision(self) -> None:
        source = _source_fixture((1, 2))
        first_table, second_table = _table_ids(source)
        regions = (
            _scope_region("first", first_table, 0, 4, "scope-a"),
            _scope_region("second", second_table, 0, 4, "scope-b"),
        )
        scopes = (
            _scope("scope-a", ("first",)),
            _scope("scope-b", ("second",)),
        )
        first_artifact = _compile(source, regions=regions, scopes=scopes)
        first_input = _adapt(source, first_artifact).stage_input

        second_node = source.nodes[1]
        assert second_node.table_payload is not None
        changed_cells = list(second_node.table_payload.cells)
        changed_cells[0] = changed_cells[0].model_copy(
            update={"raw_text": "unrelated revision"}
        )
        changed_table = second_node.table_payload.model_copy(
            update={"cells": tuple(changed_cells)}
        )
        changed_node = second_node.model_copy(update={"table_payload": changed_table})
        revised_source = source.model_copy(
            update={"nodes": (source.nodes[0], changed_node)}
        )
        revised_artifact = _compile(revised_source, regions=regions, scopes=scopes)
        revised_input = _adapt(revised_source, revised_artifact).stage_input

        first_ids = tuple(
            item.segment_id
            for item in first_input.source_segments
            if item.physical_table_id == first_table
        )
        revised_ids = tuple(
            item.segment_id
            for item in revised_input.source_segments
            if item.physical_table_id == first_table
        )
        first_shas = tuple(
            item.segment_sha256
            for item in first_input.source_segments
            if item.physical_table_id == first_table
        )
        revised_shas = tuple(
            item.segment_sha256
            for item in revised_input.source_segments
            if item.physical_table_id == first_table
        )
        self.assertEqual(first_ids, revised_ids)
        self.assertNotEqual(first_shas, revised_shas)


def _adapt(source: SourceIRSnapshot, artifact):
    return build_d2_table_region_parent_input(
        source,
        artifact,
        expected_kit_bundle_sha256=KIT_SHA,
        expected_d1_table_map_artifact_id=D1_TABLE_MAP_ID,
        expected_d1_table_map_artifact_sha256=D1_TABLE_MAP_SHA,
        expected_region_artifact_id=artifact.artifact_id,
        expected_region_artifact_sha256=artifact.artifact_sha256,
    )


def _compile(
    source: SourceIRSnapshot,
    *,
    regions: tuple[object, ...],
    scopes: tuple[D2TableLogicalScopeCandidateV1, ...] = (),
    empty_table_dispositions: tuple[
        D2EmptyPhysicalTableDispositionCandidateV1, ...
    ] = (),
):
    candidate = D2TableRegionCandidateArtifactV1(
        source_ir_id=source.source_ir_id,
        source_sha256=source.source_sha256,
        source_ir_canonical_sha256=d2_table_region_source_ir_canonical_sha256(source),
        kit_bundle_sha256=KIT_SHA,
        d1_table_map_artifact_id=D1_TABLE_MAP_ID,
        d1_table_map_artifact_sha256=D1_TABLE_MAP_SHA,
        regions=regions,
        logical_scopes=scopes,
        empty_table_dispositions=empty_table_dispositions,
    )
    return compile_d2_table_regions(
        source,
        candidate,
        expected_kit_bundle_sha256=KIT_SHA,
        expected_d1_table_map_artifact_id=D1_TABLE_MAP_ID,
        expected_d1_table_map_artifact_sha256=D1_TABLE_MAP_SHA,
    )


def _scope_region(
    region_key: str,
    table_id: str,
    start: int,
    end: int,
    scope_key: str,
) -> D2LogicalScopePartTableRegionCandidateV1:
    return D2LogicalScopePartTableRegionCandidateV1(
        disposition=D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
        region_key=region_key,
        physical_table_id=table_id,
        cell_spans=(_span(start, end),),
        logical_scope_key=scope_key,
        reason_codes=("logical_grid",),
    )


def _false_region(
    region_key: str,
    table_id: str,
    start: int,
    end: int,
) -> D2NonBusinessFalseTableRegionCandidateV1:
    return D2NonBusinessFalseTableRegionCandidateV1(
        disposition=D2TableRegionDispositionKind.NON_BUSINESS_FALSE_REGION,
        region_key=region_key,
        physical_table_id=table_id,
        cell_spans=(_span(start, end),),
        reason_codes=("layout_only",),
    )


def _scope(
    scope_key: str,
    region_keys: tuple[str, ...],
) -> D2TableLogicalScopeCandidateV1:
    return D2TableLogicalScopeCandidateV1(
        logical_scope_key=scope_key,
        ordered_region_keys=region_keys,
        reason_codes=("same_logical_table",),
    )


def _span(start: int, end: int) -> D2PhysicalTableCellOrdinalSpanV1:
    return D2PhysicalTableCellOrdinalSpanV1(
        start_cell_ordinal=start,
        end_cell_ordinal_exclusive=end,
    )


def _table_ids(source: SourceIRSnapshot) -> tuple[str, ...]:
    return tuple(
        node.table_payload.table_id
        for node in source.nodes
        if node.table_payload is not None
    )


def _source_fixture(
    pages: tuple[int, ...],
    *,
    rows: int = 2,
    columns: int = 2,
    cell_texts: tuple[str, ...] | None = None,
    empty_table: bool = False,
    table_raw_text: str = "",
    table_notes: tuple[str, ...] = (),
) -> SourceIRSnapshot:
    adapter_id = "table-region-parent-input-test"
    adapter_version = "1.0.0"
    nodes: list[SourceNode] = []
    for table_index, page_number in enumerate(pages):
        table_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=page_number,
            table_index=table_index,
            bbox=(10.0, 10.0, 300.0, 200.0),
        )
        table_id = stable_table_id(
            source_sha256=SOURCE_SHA,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            native_locator=table_locator,
        )
        if cell_texts is not None and len(cell_texts) != rows * columns:
            raise ValueError("cell_texts must match one table's cell count")
        cells = (
            ()
            if empty_table
            else tuple(
                TableCellIR(
                    cell_id=stable_cell_id(table_id, row, column),
                    row=row,
                    column=column,
                    raw_text=(
                        cell_texts[row * columns + column]
                        if cell_texts is not None
                        else f"table-{table_index + 1}-cell-{row * columns + column}"
                    ),
                    native_locator=NativeLocator(
                        format=NativeDocumentFormat.PDF,
                        page_number=page_number,
                        table_index=table_index,
                        row_index=row,
                        column_index=column,
                        bbox=(
                            float(column * 100),
                            float(row * 40),
                            float((column + 1) * 100),
                            float((row + 1) * 40),
                        ),
                    ),
                )
                for row in range(rows)
                for column in range(columns)
            )
        )
        table = TableIR(
            table_id=table_id,
            row_count=rows,
            column_count=columns,
            cells=cells,
            notes=table_notes,
        )
        nodes.append(
            SourceNode(
                source_node_id=stable_node_id(
                    source_sha256=SOURCE_SHA,
                    adapter_id=adapter_id,
                    adapter_version=adapter_version,
                    kind=SourceNodeKind.TABLE,
                    native_locator=table_locator,
                ),
                kind=SourceNodeKind.TABLE,
                source_order=table_index,
                raw_text=table_raw_text,
                native_locator=table_locator,
                table_payload=table,
            )
        )
    document_id = stable_document_id(SOURCE_SHA)
    return SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_SHA,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
        ),
        document_id=document_id,
        source_sha256=SOURCE_SHA,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        status=SourceIRStatus.COMPLETE,
        nodes=tuple(nodes),
        created_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
    )


def _record_by_region(stage_input, region_id: str):
    return next(
        item
        for item in (
            *stage_input.source_segments,
            *stage_input.nested_region_evidence,
            *stage_input.non_business_region_evidence,
            *stage_input.unresolved_scopes,
        )
        if item.region_id == region_id
    )


if __name__ == "__main__":
    unittest.main()
